import asyncio
import logging
import os
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

import asyncpg
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, ErrorEvent
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================================================
#                       КОНФИГ
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "adminrpl")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "rpl1488")

REMIND_BEFORE_1 = 45
REMIND_BEFORE_2 = 15

ALLOWED_HASHTAGS = {"#rplpuck", "#matchday", "#result"}
PUCK_BOT_USERNAME = "@rplpuck_bot"

SCHEDULER_INTERVAL = 20

MSK = ZoneInfo("Europe/Moscow")


def esc(value) -> str:
    return escape(str(value))


# =========================================================
#                        БАЗА ДАННЫХ
# =========================================================

_pool: asyncpg.Pool | None = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=15)
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT UNIQUE NOT NULL,
                name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT UNIQUE NOT NULL,
                title TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_chats (
                id SERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
                chat_id BIGINT NOT NULL,
                UNIQUE(channel_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS servers (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                port TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS matches (
                id SERIAL PRIMARY KEY,
                team_home_chat_id BIGINT NOT NULL,
                team_home_name TEXT NOT NULL,
                team_away_chat_id BIGINT NOT NULL,
                team_away_name TEXT NOT NULL,
                match_time TIMESTAMPTZ NOT NULL,
                server_name TEXT NOT NULL DEFAULT '',
                server_ip TEXT NOT NULL DEFAULT '',
                server_port TEXT NOT NULL DEFAULT '',
                server_password TEXT NOT NULL DEFAULT '',
                notified_45 BOOLEAN NOT NULL DEFAULT FALSE,
                notified_15 BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


async def add_chat(chat_id: int, name: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, name) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET name = EXCLUDED.name
            """,
            chat_id, name,
        )


async def get_chats():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM chats ORDER BY name")


async def get_chat(chat_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM chats WHERE chat_id = $1", chat_id)


async def add_channel(channel_id: int, title: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channels (channel_id, title) VALUES ($1, $2)
            ON CONFLICT (channel_id) DO UPDATE SET title = EXCLUDED.title
            """,
            channel_id, title,
        )


async def get_channels():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM channels ORDER BY title")


async def get_channel(channel_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM channels WHERE channel_id = $1", channel_id)


async def link_channel_chat(channel_id: int, chat_id: int):
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO channel_chats (channel_id, chat_id) VALUES ($1, $2)
            ON CONFLICT (channel_id, chat_id) DO NOTHING
            """,
            channel_id, chat_id,
        )


async def get_linked_chats(channel_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT c.* FROM chats c
            JOIN channel_chats cc ON cc.chat_id = c.chat_id
            WHERE cc.channel_id = $1
            ORDER BY c.name
            """,
            channel_id,
        )


async def get_linked_chat_ids(channel_id: int):
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM channel_chats WHERE channel_id = $1", channel_id)
        return {r["chat_id"] for r in rows}


async def add_server(name: str, ip: str, port: str, password: str):
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            "INSERT INTO servers (name, ip, port, password) VALUES ($1,$2,$3,$4) RETURNING *",
            name, ip, port, password,
        )


async def get_servers():
    async with _pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM servers ORDER BY id DESC")


async def get_server(server_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM servers WHERE id = $1", server_id)


async def delete_server(server_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM servers WHERE id = $1", server_id)


async def add_match(team_home_chat_id, team_home_name, team_away_chat_id, team_away_name,
                     match_time, server_name, server_ip, server_port, server_password):
    # Приводим время к UTC
    if match_time.tzinfo is not None:
        match_time = match_time.astimezone(timezone.utc)
    async with _pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO matches (
                team_home_chat_id, team_home_name,
                team_away_chat_id, team_away_name,
                match_time, server_name, server_ip, server_port, server_password
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING *
            """,
            team_home_chat_id, team_home_name, team_away_chat_id, team_away_name,
            match_time, server_name, server_ip, server_port, server_password,
        )


async def get_upcoming_matches():
    async with _pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM matches WHERE match_time > NOW() - INTERVAL '2 hours' ORDER BY match_time"
        )


async def get_match(match_id: int):
    async with _pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM matches WHERE id = $1", match_id)


async def delete_match(match_id: int):
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM matches WHERE id = $1", match_id)


async def get_matches_due_for(minutes_before: int, column: str):
    query = f"""
        SELECT * FROM matches
        WHERE {column} = FALSE
          AND match_time - (INTERVAL '1 minute' * $1) <= NOW()
          AND match_time > NOW() - INTERVAL '30 minutes'
    """
    async with _pool.acquire() as conn:
        return await conn.fetch(query, minutes_before)


async def mark_notified(match_id: int, column: str):
    query = f"UPDATE matches SET {column} = TRUE WHERE id = $1"
    async with _pool.acquire() as conn:
        await conn.execute(query, match_id)


# =========================================================
#                    ТЕКСТЫ СООБЩЕНИЙ
# =========================================================

def reminder_45_text(team_home: str, team_away: str) -> str:
    return (
        "⏰ <b>Внимание, до матча осталось 45 минут!</b>\n"
        "Не забудьте прийти! 🙌\n\n"
        f"🆚 {esc(team_home)} — {esc(team_away)}"
    )


def raskatka_text(server_name, server_ip, server_port, server_password, team_home, team_away, color) -> str:
    return (
        "🎮 <b>Калл! Раскатка!</b>\n\n"
        f"🖥 <b>{esc(server_name)}</b>\n"
        f"🌐 IP сервера: <code>{esc(server_ip)}</code>\n"
        f"🔌 Port сервера: <code>{esc(server_port)}</code>\n"
        f"🔑 Password сервера: <code>{esc(server_password)}</code>\n\n"
        f"👉 Вы {color}.\n\n"
        "ℹ️ Если что:\n"
        f"🔴 Хозяева — ред ({esc(team_home)})\n"
        f"🔵 Гости — блу ({esc(team_away)})"
    )


# =========================================================
#                  FSM-СОСТОЯНИЯ АДМИНКИ
# =========================================================

class AdminAuth(StatesGroup):
    waiting_login = State()
    waiting_password = State()


class AddChat(StatesGroup):
    waiting_id = State()
    waiting_name = State()


class AddServer(StatesGroup):
    waiting_name = State()
    waiting_ip = State()
    waiting_port = State()
    waiting_password = State()


class AddMatch(StatesGroup):
    waiting_team1 = State()
    waiting_team2 = State()
    waiting_datetime = State()
    waiting_server = State()


class AddChannel(StatesGroup):
    waiting_id = State()
    waiting_title = State()
    waiting_chats = State()


# =========================================================
#                       КЛАВИАТУРЫ
# =========================================================

def admin_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить чат", callback_data="adm:add_chat")
    kb.button(text="📋 Список чатов", callback_data="adm:list_chats")
    kb.button(text="🆚 Добавить матч", callback_data="adm:add_match")
    kb.button(text="📅 Список матчей", callback_data="adm:list_matches")
    kb.button(text="🖥 Добавить сервер", callback_data="adm:add_server")
    kb.button(text="🗄 Список серверов", callback_data="adm:list_servers")
    kb.button(text="📡 Привязать канал", callback_data="adm:add_channel")
    kb.button(text="🔗 Список каналов", callback_data="adm:list_channels")
    kb.adjust(2)
    return kb.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    return kb.as_markup()


def chats_choice_kb(chats, prefix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in chats:
        kb.button(text=f"🏒 {c['name']}", callback_data=f"{prefix}:{c['chat_id']}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


def chats_multiselect_kb(chats, selected: set) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in chats:
        mark = "✅" if c["chat_id"] in selected else "⬜️"
        kb.button(text=f"{mark} {c['name']}", callback_data=f"cf_toggle:{c['chat_id']}")
    kb.button(text="✔️ Готово", callback_data="cf_done")
    kb.adjust(1)
    return kb.as_markup()


def matches_list_kb(matches) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for m in matches:
        local_time = m["match_time"].astimezone(MSK)
        label = f"{m['team_home_name']} 🆚 {m['team_away_name']} — {local_time.strftime('%d.%m %H:%M')} МСК"
        kb.button(text=label, callback_data=f"adm:match:{m['id']}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


def match_card_kb(match_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить матч", callback_data=f"adm:del_match:{match_id}")
    kb.button(text="⬅️ К списку матчей", callback_data="adm:list_matches")
    kb.adjust(1)
    return kb.as_markup()


def servers_choice_kb(servers) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in servers:
        kb.button(text=f"🖥 {s['name']} ({s['ip']}:{s['port']})", callback_data=f"srv:{s['id']}")
    kb.button(text="➕ Новый сервер", callback_data="adm:add_server_inline")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


def servers_list_kb(servers) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in servers:
        kb.button(text=f"🗑 {s['name']} ({s['ip']}:{s['port']})", callback_data=f"adm:del_server:{s['id']}")
    kb.button(text="⬅️ В меню", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


# =========================================================
#                    ХЕНДЛЕРЫ
# =========================================================

start_router = Router()
channel_router = Router()
auth_router = Router()
panel_router = Router()

AUTHED_ADMINS: set[int] = set()


def is_authed(user_id: int) -> bool:
    return user_id in AUTHED_ADMINS


@start_router.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "👋 <b>Привет!</b>\n\n"
        "В данном боте нет ничего интересного, он предназначен для раскаток "
        "и всему подобному.\n\n"
        f"🃏 Лучше перейди и играй в нашем боте в карточки игроков Puck — "
        f"{PUCK_BOT_USERNAME}"
    )
    await message.answer(text)


def _has_allowed_hashtag(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(tag in lowered for tag in ALLOWED_HASHTAGS)


@channel_router.channel_post()
async def on_channel_post(message: Message, bot: Bot):
    channel = await get_channel(message.chat.id)
    if not channel:
        return
    text = message.text or message.caption or ""
    if not _has_allowed_hashtag(text):
        return
    chat_ids = await get_linked_chat_ids(message.chat.id)
    for chat_id in chat_ids:
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=message.chat.id, message_id=message.message_id)
        except Exception as e:
            logging.warning(f"⚠️ Не удалось переслать пост в чат {chat_id}: {e}")


@auth_router.message(Command("adminkarpl"))
async def cmd_admin(message: Message, state: FSMContext):
    if is_authed(message.from_user.id):
        await message.answer("🔐 <b>Админ-панель</b>", reply_markup=admin_main_menu())
        return
    await state.set_state(AdminAuth.waiting_login)
    await message.answer("🔐 Введите <b>логин</b>:")


@auth_router.message(AdminAuth.waiting_login)
async def process_login(message: Message, state: FSMContext):
    if message.text != ADMIN_LOGIN:
        await message.answer("❌ Неверный логин. Попробуйте ещё раз /adminkarpl")
        await state.clear()
        return
    await state.set_state(AdminAuth.waiting_password)
    await message.answer("🔑 Введите <b>пароль</b>:")


@auth_router.message(AdminAuth.waiting_password)
async def process_password(message: Message, state: FSMContext):
    if message.text != ADMIN_PASSWORD:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз /adminkarpl")
        await state.clear()
        return
    AUTHED_ADMINS.add(message.from_user.id)
    await state.clear()
    await message.answer("✅ Доступ разрешён!\n\n🔐 <b>Админ-панель</b>", reply_markup=admin_main_menu())


@panel_router.callback_query.middleware()
async def check_authed_cb(handler, event: CallbackQuery, data):
    if not is_authed(event.from_user.id):
        await event.answer("⛔️ Сначала войдите: /adminkarpl", show_alert=True)
        return
    return await handler(event, data)


@panel_router.message.middleware()
async def check_authed_msg(handler, event: Message, data):
    if not is_authed(event.from_user.id):
        return
    return await handler(event, data)


@panel_router.callback_query(F.data == "adm:menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("🔐 <b>Админ-панель</b>", reply_markup=admin_main_menu())
    await call.answer()


# ---------- Добавление чата ----------
@panel_router.callback_query(F.data == "adm:add_chat")
async def cb_add_chat(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddChat.waiting_id)
    await call.message.edit_text(
        "➕ <b>Добавление чата</b>\n\nПришлите <b>ID чата</b> (например, -1001234567890):",
        reply_markup=back_to_menu_kb(),
    )
    await call.answer()


@panel_router.message(AddChat.waiting_id)
async def process_chat_id(message: Message, state: FSMContext):
    try:
        chat_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(chat_id=chat_id)
    await state.set_state(AddChat.waiting_name)
    await message.answer("✏️ Теперь пришлите <b>название</b> команды/чата (например, «Динамо Москва»):")


@panel_router.message(AddChat.waiting_name)
async def process_chat_name(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data["chat_id"]
    name = message.text.strip()
    await add_chat(chat_id, name)
    await state.clear()
    await message.answer(f"✅ Чат <b>{esc(name)}</b> (<code>{chat_id}</code>) добавлен!", reply_markup=admin_main_menu())


@panel_router.callback_query(F.data == "adm:list_chats")
async def cb_list_chats(call: CallbackQuery):
    chats = await get_chats()
    if not chats:
        await call.message.edit_text("📋 Пока нет ни одного чата.", reply_markup=back_to_menu_kb())
        await call.answer()
        return
    lines = [f"🏒 <b>{esc(c['name'])}</b> — <code>{c['chat_id']}</code>" for c in chats]
    await call.message.edit_text("📋 <b>Список чатов:</b>\n\n" + "\n".join(lines), reply_markup=back_to_menu_kb())
    await call.answer()


# ---------- Добавление сервера ----------
@panel_router.callback_query(F.data == "adm:add_server")
async def cb_add_server(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddServer.waiting_name)
    await call.message.edit_text(
        "🖥 <b>Новый сервер</b>\n\n1️⃣ Пришлите <b>название</b> сервера (например, «Сервер #1»):",
        reply_markup=back_to_menu_kb(),
    )
    await call.answer()


@panel_router.callback_query(AddMatch.waiting_server, F.data == "adm:add_server_inline")
async def cb_add_server_inline(call: CallbackQuery, state: FSMContext):
    await state.update_data(resume_match=True)
    await state.set_state(AddServer.waiting_name)
    await call.message.edit_text("🖥 <b>Новый сервер</b>\n\n1️⃣ Пришлите <b>название</b> сервера:")
    await call.answer()


@panel_router.message(AddServer.waiting_name)
async def process_server_name(message: Message, state: FSMContext):
    await state.update_data(srv_name=message.text.strip())
    await state.set_state(AddServer.waiting_ip)
    await message.answer("2️⃣ Пришлите <b>IP</b> сервера:")


@panel_router.message(AddServer.waiting_ip)
async def process_server_ip(message: Message, state: FSMContext):
    await state.update_data(srv_ip=message.text.strip())
    await state.set_state(AddServer.waiting_port)
    await message.answer("3️⃣ Пришлите <b>Port</b> сервера:")


@panel_router.message(AddServer.waiting_port)
async def process_server_port(message: Message, state: FSMContext):
    await state.update_data(srv_port=message.text.strip())
    await state.set_state(AddServer.waiting_password)
    await message.answer("4️⃣ Пришлите <b>Password</b> сервера:")


@panel_router.message(AddServer.waiting_password)
async def process_server_password(message: Message, state: FSMContext):
    data = await state.get_data()
    password = message.text.strip()
    server = await add_server(data["srv_name"], data["srv_ip"], data["srv_port"], password)

    if data.get("resume_match"):
        required = ("team1_id", "team1_name", "team2_id", "team2_name", "match_time")
        if not all(key in data for key in required):
            await state.clear()
            await message.answer(
                "❌ Данные о матче утеряны. Пожалуйста, начните заново: /adminkarpl",
                reply_markup=admin_main_menu(),
            )
            return
        try:
            match_time = datetime.fromisoformat(data["match_time"])
        except ValueError:
            await state.clear()
            await message.answer(
                "❌ Неверный формат даты. Начните создание матча заново: /adminkarpl",
                reply_markup=admin_main_menu(),
            )
            return
        try:
            match = await add_match(
                team_home_chat_id=data["team1_id"],
                team_home_name=data["team1_name"],
                team_away_chat_id=data["team2_id"],
                team_away_name=data["team2_name"],
                match_time=match_time,
                server_name=server["name"],
                server_ip=server["ip"],
                server_port=server["port"],
                server_password=server["password"],
            )
        except Exception as e:
            logging.exception("Ошибка при сохранении матча после создания сервера")
            await state.clear()
            # ВРЕМЕННО: показываем текст ошибки для отладки
            await message.answer(
                f"❌ Не удалось сохранить матч. Ошибка:\n<code>{escape(str(e))}</code>",
                reply_markup=admin_main_menu(),
            )
            return
        await state.clear()
        local_time = match["match_time"].astimezone(MSK)
        text = (
            "✅ <b>Сервер сохранён и матч создан!</b>\n\n"
            f"🆚 {esc(match['team_home_name'])} — {esc(match['team_away_name'])}\n"
            f"🕒 {local_time.strftime('%d.%m.%Y %H:%M')} МСК\n"
            f"🖥 {esc(match['server_name'])}\n\n"
            "Бот сам пришлёт напоминание за 45 мин. и раскатку за 15 мин. до матча 🔔"
        )
        await message.answer(text, reply_markup=admin_main_menu())
        return

    await state.clear()
    await message.answer(
        f"✅ Сервер <b>{esc(server['name'])}</b> сохранён!\n"
        "Теперь он будет доступен для выбора при создании матчей. 🖥",
        reply_markup=admin_main_menu(),
    )


@panel_router.callback_query(F.data == "adm:list_servers")
async def cb_list_servers(call: CallbackQuery):
    servers = await get_servers()
    if not servers:
        await call.message.edit_text("🗄 Пока нет сохранённых серверов.", reply_markup=back_to_menu_kb())
        await call.answer()
        return
    await call.message.edit_text(
        "🗄 <b>Серверы</b> (нажмите на сервер, чтобы удалить):",
        reply_markup=servers_list_kb(servers),
    )
    await call.answer()


@panel_router.callback_query(F.data.startswith("adm:del_server:"))
async def cb_delete_server(call: CallbackQuery):
    server_id = int(call.data.split(":")[2])
    await delete_server(server_id)
    await call.answer("🗑 Сервер удалён", show_alert=True)
    servers = await get_servers()
    if not servers:
        await call.message.edit_text("🗄 Пока нет сохранённых серверов.", reply_markup=back_to_menu_kb())
        return
    await call.message.edit_text("🗄 <b>Серверы:</b>", reply_markup=servers_list_kb(servers))


# ---------- Добавление матча ----------
@panel_router.callback_query(F.data == "adm:add_match")
async def cb_add_match(call: CallbackQuery, state: FSMContext):
    chats = await get_chats()
    if len(chats) < 2:
        await call.message.edit_text(
            "❌ Нужно как минимум 2 добавленных чата, чтобы создать матч.",
            reply_markup=back_to_menu_kb(),
        )
        await call.answer()
        return
    await state.set_state(AddMatch.waiting_team1)
    await call.message.edit_text(
        "🆚 <b>Новый матч</b>\n\n1️⃣ Выберите <b>первую команду (хозяева, ред)</b>:",
        reply_markup=chats_choice_kb(chats, "team1"),
    )
    await call.answer()


@panel_router.callback_query(AddMatch.waiting_team1, F.data.startswith("team1:"))
async def cb_pick_team1(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    chat = await get_chat(chat_id)
    await state.update_data(team1_id=chat_id, team1_name=chat["name"])
    all_chats = await get_chats()
    remaining = [c for c in all_chats if c["chat_id"] != chat_id]
    await state.set_state(AddMatch.waiting_team2)
    await call.message.edit_text(
        f"1️⃣ Хозяева: <b>{esc(chat['name'])}</b> ✅\n\n2️⃣ Выберите <b>вторую команду (гости, блу)</b>:",
        reply_markup=chats_choice_kb(remaining, "team2"),
    )
    await call.answer()


@panel_router.callback_query(AddMatch.waiting_team2, F.data.startswith("team2:"))
async def cb_pick_team2(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    chat = await get_chat(chat_id)
    await state.update_data(team2_id=chat_id, team2_name=chat["name"])
    await state.set_state(AddMatch.waiting_datetime)
    data = await state.get_data()
    await call.message.edit_text(
        f"1️⃣ Хозяева: <b>{esc(data['team1_name'])}</b> ✅\n"
        f"2️⃣ Гости: <b>{esc(chat['name'])}</b> ✅\n\n"
        "3️⃣ Пришлите <b>дату и время матча по МСК</b> в формате:\n"
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        "Например: <code>25.08.2026 20:30</code>",
    )
    await call.answer()


@panel_router.message(AddMatch.waiting_datetime)
async def process_match_datetime(message: Message, state: FSMContext):
    try:
        naive_dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Пришлите так: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\nНапример: <code>25.08.2026 20:30</code>"
        )
        return
    match_time_msk = naive_dt.replace(tzinfo=MSK)
    await state.update_data(match_time=match_time_msk.isoformat())
    servers = await get_servers()
    if not servers:
        await state.update_data(resume_match=True)
        await state.set_state(AddServer.waiting_name)
        await message.answer(
            "🖥 Сохранённых серверов пока нет — самое время добавить первый!\n\n"
            "1️⃣ Пришлите <b>название</b> сервера:"
        )
        return
    await state.set_state(AddMatch.waiting_server)
    await message.answer("4️⃣ Выберите <b>сервер</b> для этого матча:", reply_markup=servers_choice_kb(servers))


@panel_router.callback_query(AddMatch.waiting_server, F.data.startswith("srv:"))
async def cb_pick_server(call: CallbackQuery, state: FSMContext):
    server_id = int(call.data.split(":")[1])
    server = await get_server(server_id)
    if not server:
        await call.answer("Сервер не найден", show_alert=True)
        return
    data = await state.get_data()
    required = ("team1_id", "team1_name", "team2_id", "team2_name", "match_time")
    if not all(key in data for key in required):
        await state.clear()
        await call.message.edit_text(
            "❌ Данные о матче утеряны. Пожалуйста, начните заново: /adminkarpl",
            reply_markup=back_to_menu_kb(),
        )
        await call.answer()
        return
    try:
        match_time = datetime.fromisoformat(data["match_time"])
    except ValueError:
        await state.clear()
        await call.message.edit_text(
            "❌ Неверный формат даты. Начните создание матча заново: /adminkarpl",
            reply_markup=back_to_menu_kb(),
        )
        await call.answer()
        return
    try:
        match = await add_match(
            team_home_chat_id=data["team1_id"],
            team_home_name=data["team1_name"],
            team_away_chat_id=data["team2_id"],
            team_away_name=data["team2_name"],
            match_time=match_time,
            server_name=server["name"],
            server_ip=server["ip"],
            server_port=server["port"],
            server_password=server["password"],
        )
    except Exception as e:
        logging.exception("Ошибка при сохранении матча")
        await state.clear()
        # ВРЕМЕННО: показываем текст ошибки для отладки
        await call.message.edit_text(
            f"❌ Не удалось сохранить матч в базе данных. Ошибка:\n<code>{escape(str(e))}</code>",
            reply_markup=back_to_menu_kb(),
        )
        await call.answer()
        return
    await state.clear()
    local_time = match["match_time"].astimezone(MSK)
    text = (
        "✅ <b>Матч создан!</b>\n\n"
        f"🆚 {esc(match['team_home_name'])} — {esc(match['team_away_name'])}\n"
        f"🕒 {local_time.strftime('%d.%m.%Y %H:%M')} МСК\n"
        f"🖥 {esc(match['server_name'])}\n\n"
        "Бот сам пришлёт напоминание за 45 мин. и раскатку за 15 мин. до матча 🔔"
    )
    await call.message.edit_text(text, reply_markup=admin_main_menu())
    await call.answer()


# ---------- Список / удаление матчей ----------
@panel_router.callback_query(F.data == "adm:list_matches")
async def cb_list_matches(call: CallbackQuery):
    matches = await get_upcoming_matches()
    if not matches:
        await call.message.edit_text("📅 Ближайших матчей нет.", reply_markup=back_to_menu_kb())
        await call.answer()
        return
    await call.message.edit_text(
        "📅 <b>Ближайшие матчи</b> (время МСК, нажмите, чтобы посмотреть/удалить):",
        reply_markup=matches_list_kb(matches),
    )
    await call.answer()


@panel_router.callback_query(F.data.startswith("adm:match:"))
async def cb_match_card(call: CallbackQuery):
    match_id = int(call.data.split(":")[2])
    m = await get_match(match_id)
    if not m:
        await call.answer("Матч не найден", show_alert=True)
        return
    local_time = m["match_time"].astimezone(MSK)
    text = (
        f"🆚 <b>{esc(m['team_home_name'])} — {esc(m['team_away_name'])}</b>\n"
        f"🕒 {local_time.strftime('%d.%m.%Y %H:%M')} МСК\n\n"
        f"🖥 {esc(m['server_name'])}\n"
        f"🌐 IP: <code>{esc(m['server_ip'])}</code>\n"
        f"🔌 Port: <code>{esc(m['server_port'])}</code>\n"
        f"🔑 Password: <code>{esc(m['server_password'])}</code>\n\n"
        f"🔔 45 мин: {'✅' if m['notified_45'] else '⏳'}   "
        f"🔔 15 мин: {'✅' if m['notified_15'] else '⏳'}"
    )
    await call.message.edit_text(text, reply_markup=match_card_kb(match_id))
    await call.answer()


@panel_router.callback_query(F.data.startswith("adm:del_match:"))
async def cb_delete_match(call: CallbackQuery):
    match_id = int(call.data.split(":")[2])
    await delete_match(match_id)
    await call.answer("🗑 Матч удалён", show_alert=True)
    matches = await get_upcoming_matches()
    if not matches:
        await call.message.edit_text("📅 Ближайших матчей нет.", reply_markup=back_to_menu_kb())
        return
    await call.message.edit_text("📅 <b>Ближайшие матчи:</b>", reply_markup=matches_list_kb(matches))


# ---------- Привязка канала ----------
@panel_router.callback_query(F.data == "adm:add_channel")
async def cb_add_channel(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting_id)
    await call.message.edit_text(
        "📡 <b>Привязка канала</b>\n\n"
        "1️⃣ Пришлите <b>ID канала</b> (например, -1001234567890).\n"
        "⚠️ Бот должен быть добавлен в канал администратором!",
        reply_markup=back_to_menu_kb(),
    )
    await call.answer()


@panel_router.message(AddChannel.waiting_id)
async def process_channel_id(message: Message, state: FSMContext):
    try:
        channel_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(channel_id=channel_id)
    await state.set_state(AddChannel.waiting_title)
    await message.answer("✏️ Пришлите <b>название</b> канала (для себя, отображаться нигде не будет):")


@panel_router.message(AddChannel.waiting_title)
async def process_channel_title(message: Message, state: FSMContext):
    data = await state.get_data()
    title = message.text.strip()
    await add_channel(data["channel_id"], title)
    await state.update_data(selected=set())
    chats = await get_chats()
    if not chats:
        await state.clear()
        await message.answer(
            "✅ Канал добавлен, но у вас пока нет ни одного чата для привязки.\n"
            "Сначала добавьте чаты через меню.",
            reply_markup=admin_main_menu(),
        )
        return
    await state.set_state(AddChannel.waiting_chats)
    await message.answer(
        "2️⃣ Выберите чаты, в которые пересылать посты из канала "
        "(#rplpuck #MatchDay #result), затем нажмите «Готово»:",
        reply_markup=chats_multiselect_kb(chats, set()),
    )


@panel_router.callback_query(AddChannel.waiting_chats, F.data.startswith("cf_toggle:"))
async def cb_toggle_chat(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    data = await state.get_data()
    selected: set = data.get("selected", set())
    if chat_id in selected:
        selected.remove(chat_id)
    else:
        selected.add(chat_id)
    await state.update_data(selected=selected)
    chats = await get_chats()
    await call.message.edit_reply_markup(reply_markup=chats_multiselect_kb(chats, selected))
    await call.answer()


@panel_router.callback_query(AddChannel.waiting_chats, F.data == "cf_done")
async def cb_channel_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data["channel_id"]
    selected: set = data.get("selected", set())
    for chat_id in selected:
        await link_channel_chat(channel_id, chat_id)
    await state.clear()
    await call.message.edit_text(f"✅ Канал привязан к {len(selected)} чат(ам)!", reply_markup=admin_main_menu())
    await call.answer()


@panel_router.callback_query(F.data == "adm:list_channels")
async def cb_list_channels(call: CallbackQuery):
    channels = await get_channels()
    if not channels:
        await call.message.edit_text("🔗 Пока нет привязанных каналов.", reply_markup=back_to_menu_kb())
        await call.answer()
        return
    lines = []
    for ch in channels:
        linked = await get_linked_chats(ch["channel_id"])
        names = ", ".join(esc(c["name"]) for c in linked) or "—"
        lines.append(f"📡 <b>{esc(ch['title'])}</b> (<code>{ch['channel_id']}</code>)\n   ↳ чаты: {names}")
    await call.message.edit_text("🔗 <b>Каналы:</b>\n\n" + "\n\n".join(lines), reply_markup=back_to_menu_kb())
    await call.answer()


# =========================================================
#              ПЛАНИРОВЩИК НАПОМИНАНИЙ
# =========================================================

async def _send_safe(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logging.warning(f"⚠️ Не удалось отправить сообщение в чат {chat_id}: {e}")


async def check_reminders(bot: Bot):
    due_45 = await get_matches_due_for(REMIND_BEFORE_1, "notified_45")
    for m in due_45:
        text = reminder_45_text(m["team_home_name"], m["team_away_name"])
        await _send_safe(bot, m["team_home_chat_id"], text)
        await _send_safe(bot, m["team_away_chat_id"], text)
        await mark_notified(m["id"], "notified_45")
    due_15 = await get_matches_due_for(REMIND_BEFORE_2, "notified_15")
    for m in due_15:
        home_text = raskatka_text(
            m["server_name"], m["server_ip"], m["server_port"], m["server_password"],
            m["team_home_name"], m["team_away_name"], color="ред",
        )
        away_text = raskatka_text(
            m["server_name"], m["server_ip"], m["server_port"], m["server_password"],
            m["team_home_name"], m["team_away_name"], color="блу",
        )
        await _send_safe(bot, m["team_home_chat_id"], home_text)
        await _send_safe(bot, m["team_away_chat_id"], away_text)
        await mark_notified(m["id"], "notified_15")


async def scheduler_loop(bot: Bot):
    while True:
        try:
            await check_reminders(bot)
        except Exception as e:
            logging.warning(f"⚠️ Ошибка в планировщике: {e}")
        await asyncio.sleep(SCHEDULER_INTERVAL)


# =========================================================
#                          MAIN
# =========================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise RuntimeError("❌ Не задан BOT_TOKEN в переменных окружения!")
    if not DATABASE_URL:
        raise RuntimeError("❌ Не задан DATABASE_URL в переменных окружения!")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(start_router)
    dp.include_router(auth_router)
    dp.include_router(panel_router)
    dp.include_router(channel_router)

    @dp.errors()
    async def global_error_handler(event: ErrorEvent):
        logging.exception("Необработанная ошибка при обработке апдейта", exc_info=event.exception)
        try:
            update = event.update
            if update.message:
                await update.message.answer("⚠️ Произошла ошибка. Попробуйте ещё раз или начните заново: /adminkarpl")
            elif update.callback_query:
                await update.callback_query.message.answer(
                    "⚠️ Произошла ошибка. Попробуйте ещё раз или начните заново: /adminkarpl"
                )
        except Exception:
            pass
        return True

    await init_pool()
    logging.info("✅ База данных подключена и готова")
    asyncio.create_task(scheduler_loop(bot))
    logging.info("✅ Планировщик напоминаний запущен (часовой пояс МСК)")
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("🚀 Бот запущен, начинаю polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
