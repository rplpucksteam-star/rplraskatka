import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# SQLAlchemy
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    Text,
    BigInteger,
    select,
    delete,
    update,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")

# Если URL начинается с postgresql:// (без +asyncpg), заменяем на асинхронный драйвер
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Часовой пояс (Москва, UTC+3) ---
MSK_TZ = timezone(timedelta(hours=3))
UTC_TZ = timezone.utc

def msk_to_utc(msk_naive: datetime) -> datetime:
    """Преобразует наивное время, введённое как московское, в наивное UTC."""
    msk_aware = msk_naive.replace(tzinfo=MSK_TZ)
    utc_aware = msk_aware.astimezone(UTC_TZ)
    return utc_aware.replace(tzinfo=None)

def utc_to_msk(utc_naive: datetime) -> datetime:
    """Преобразует наивное UTC в осознанное московское время (для отображения)."""
    utc_aware = utc_naive.replace(tzinfo=UTC_TZ)
    msk_aware = utc_aware.astimezone(MSK_TZ)
    return msk_aware

# --- База данных ---
Base = declarative_base()

class Chat(Base):
    __tablename__ = "chats"
    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow)

class ServerSetting(Base):
    __tablename__ = "server_settings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(50), unique=True, nullable=False)
    value = Column(Text, nullable=False)

class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    team1 = Column(String(100), nullable=False)
    team2 = Column(String(100), nullable=False)
    match_time = Column(DateTime, nullable=False)  # хранится в UTC
    created_at = Column(DateTime, default=datetime.utcnow)
    notification_45_sent = Column(Boolean, default=False)
    notification_15_sent = Column(Boolean, default=False)

class ChannelSubscription(Base):
    __tablename__ = "channel_subscriptions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(BigInteger, unique=True, nullable=False)
    channel_username = Column(String(100), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

class RepostedMessage(Base):
    __tablename__ = "reposted_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(BigInteger, nullable=False)
    message_id = Column(Integer, nullable=False)
    reposted_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('channel_id', 'message_id'),)

# Асинхронный движок
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# --- Вспомогательные функции БД ---
async def get_chats() -> List[Chat]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Chat))
        return result.scalars().all()

async def get_chat_by_id(chat_id: int) -> Optional[Chat]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Chat).where(Chat.chat_id == chat_id))
        return result.scalar_one_or_none()

async def add_chat(chat_id: int, name: str) -> Chat:
    async with AsyncSessionLocal() as session:
        chat = Chat(chat_id=chat_id, name=name)
        session.add(chat)
        await session.commit()
        return chat

async def delete_chat(chat_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(Chat).where(Chat.chat_id == chat_id))
        await session.commit()
        return result.rowcount > 0

async def get_server_setting(key: str) -> Optional[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ServerSetting).where(ServerSetting.key == key))
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

async def set_server_setting(key: str, value: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ServerSetting).where(ServerSetting.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = value
        else:
            setting = ServerSetting(key=key, value=value)
            session.add(setting)
        await session.commit()

async def get_matches() -> List[Match]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Match))
        return result.scalars().all()

async def add_match(team1: str, team2: str, match_time: datetime) -> Match:
    async with AsyncSessionLocal() as session:
        match = Match(team1=team1, team2=team2, match_time=match_time)
        session.add(match)
        await session.commit()
        return match

async def update_match_notification_sent(match_id: int, field: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Match).where(Match.id == match_id).values(**{field: True})
        )
        await session.commit()

async def get_channel_subscriptions() -> List[ChannelSubscription]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChannelSubscription))
        return result.scalars().all()

async def add_channel_subscription(channel_id: int, username: Optional[str] = None) -> ChannelSubscription:
    async with AsyncSessionLocal() as session:
        sub = ChannelSubscription(channel_id=channel_id, channel_username=username)
        session.add(sub)
        await session.commit()
        return sub

async def delete_channel_subscription(channel_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(ChannelSubscription).where(ChannelSubscription.channel_id == channel_id))
        await session.commit()
        return result.rowcount > 0

async def is_reposted(channel_id: int, message_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RepostedMessage).where(
                RepostedMessage.channel_id == channel_id,
                RepostedMessage.message_id == message_id
            )
        )
        return result.scalar_one_or_none() is not None

async def mark_reposted(channel_id: int, message_id: int) -> None:
    async with AsyncSessionLocal() as session:
        repost = RepostedMessage(channel_id=channel_id, message_id=message_id)
        session.add(repost)
        await session.commit()

# --- Обработчики команд ---
# Состояния для ConversationHandler
(LOGIN, PASSWORD, ADMIN_MENU,
 ADD_CHAT_ID, ADD_CHAT_NAME,
 DELETE_CHAT_SELECT,
 SET_SERVER_IP, SET_SERVER_PORT, SET_SERVER_PASSWORD,
 ADD_MATCH_TEAM1, ADD_MATCH_TEAM2, ADD_MATCH_TIME,
 ADD_CHANNEL_ID, DELETE_CHANNEL_SELECT) = range(14)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "В данном боте нет ничего интересного, он предназначен для раскаток и всему подобному.\n"
        "Лучше перейди и играй в нашем боте в карточки игроков Puck - @rplpuck_bot."
    )

# --- Админка ---
async def adminkarpl_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Введите логин:")
    return LOGIN

async def adminkarpl_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    login = update.message.text
    if login == "adminrpl":
        context.user_data['login'] = login
        await update.message.reply_text("Введите пароль:")
        return PASSWORD
    else:
        await update.message.reply_text("Неверный логин. Попробуйте снова /adminkarpl")
        return ConversationHandler.END

async def adminkarpl_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    if password == "rpl1488":
        await update.message.reply_text("Авторизация успешна!", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU
    else:
        await update.message.reply_text("Неверный пароль. Попробуйте снова /adminkarpl")
        return ConversationHandler.END

def admin_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("Управление чатами", callback_data="menu_chats")],
        [InlineKeyboardButton("Настройки сервера", callback_data="menu_server")],
        [InlineKeyboardButton("Добавить матч", callback_data="menu_add_match")],
        [InlineKeyboardButton("Привязать канал", callback_data="menu_add_channel")],
        [InlineKeyboardButton("Управление каналами", callback_data="menu_channels")],
        [InlineKeyboardButton("Выйти", callback_data="menu_exit")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "menu_chats":
        await query.edit_message_text("Выберите действие:", reply_markup=chats_menu_keyboard())
    elif data == "menu_server":
        await show_server_settings(query)
    elif data == "menu_add_match":
        chats = await get_chats()
        if chats:
            buttons = [[InlineKeyboardButton(chat.name, callback_data=f"team1_{chat.name}")] for chat in chats]
            buttons.append([InlineKeyboardButton("Ввести вручную", callback_data="team1_manual")])
            await query.edit_message_text("Выберите первую команду:", reply_markup=InlineKeyboardMarkup(buttons))
            return ADD_MATCH_TEAM1
        else:
            await query.edit_message_text("Нет добавленных чатов. Сначала добавьте чаты.")
            return ADMIN_MENU
    elif data == "menu_add_channel":
        await query.edit_message_text("Введите ID канала (число) или @username:")
        return ADD_CHANNEL_ID
    elif data == "menu_channels":
        await show_channels(query)
    elif data == "menu_exit":
        await query.edit_message_text("Выход из админки.")
        return ConversationHandler.END

async def show_server_settings(query):
    ip = await get_server_setting("server_ip") or "не задан"
    port = await get_server_setting("server_port") or "не задан"
    password = await get_server_setting("server_password") or "не задан"
    text = f"Текущие настройки сервера:\nIP: {ip}\nPort: {port}\nPassword: {password}"
    keyboard = [
        [InlineKeyboardButton("Изменить IP", callback_data="edit_ip")],
        [InlineKeyboardButton("Изменить Port", callback_data="edit_port")],
        [InlineKeyboardButton("Изменить Password", callback_data="edit_password")],
        [InlineKeyboardButton("Назад", callback_data="back_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

def chats_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Добавить чат", callback_data="add_chat")],
        [InlineKeyboardButton("Удалить чат", callback_data="delete_chat")],
        [InlineKeyboardButton("Список чатов", callback_data="list_chats")],
        [InlineKeyboardButton("Назад", callback_data="back_menu")],
    ])

async def chats_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "add_chat":
        await query.edit_message_text("Введите ID чата (число):")
        return ADD_CHAT_ID
    elif data == "delete_chat":
        chats = await get_chats()
        if not chats:
            await query.edit_message_text("Нет чатов для удаления.")
            return ADMIN_MENU
        buttons = [[InlineKeyboardButton(f"{c.name} ({c.chat_id})", callback_data=f"del_{c.chat_id}")] for c in chats]
        buttons.append([InlineKeyboardButton("Назад", callback_data="back_menu")])
        await query.edit_message_text("Выберите чат для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
        return DELETE_CHAT_SELECT
    elif data == "list_chats":
        chats = await get_chats()
        text = "Нет добавленных чатов." if not chats else "Список чатов:\n" + "\n".join([f"{c.name} (ID: {c.chat_id})" for c in chats])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back_menu")]]))
        return ADMIN_MENU
    elif data == "back_menu":
        await query.edit_message_text("Главное меню:", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU

async def add_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        chat_id = int(update.message.text)
        context.user_data['temp_chat_id'] = chat_id
        await update.message.reply_text("Введите название чата (например, Динамо Москва):")
        return ADD_CHAT_NAME
    except ValueError:
        await update.message.reply_text("Некорректный ID. Введите число.")
        return ADD_CHAT_ID

async def add_chat_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text
    chat_id = context.user_data['temp_chat_id']
    existing = await get_chat_by_id(chat_id)
    if existing:
        await update.message.reply_text(f"Чат с ID {chat_id} уже существует (название: {existing.name}).")
    else:
        await add_chat(chat_id, name)
        await update.message.reply_text(f"Чат '{name}' (ID: {chat_id}) добавлен.")
    await update.message.reply_text("Меню управления чатами:", reply_markup=chats_menu_keyboard())
    return ADMIN_MENU

async def delete_chat_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_menu":
        await query.edit_message_text("Главное меню:", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU
    if data.startswith("del_"):
        chat_id = int(data.split("_")[1])
        success = await delete_chat(chat_id)
        if success:
            await query.edit_message_text(f"Чат с ID {chat_id} удален.")
        else:
            await query.edit_message_text("Не удалось удалить чат.")
        chats = await get_chats()
        if not chats:
            await query.edit_message_text("Нет чатов.", reply_markup=chats_menu_keyboard())
        else:
            buttons = [[InlineKeyboardButton(f"{c.name} ({c.chat_id})", callback_data=f"del_{c.chat_id}")] for c in chats]
            buttons.append([InlineKeyboardButton("Назад", callback_data="back_menu")])
            await query.edit_message_text("Выберите чат для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
        return DELETE_CHAT_SELECT
    else:
        await query.edit_message_text("Неизвестная команда.")
        return DELETE_CHAT_SELECT

async def server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "edit_ip":
        await query.edit_message_text("Введите новый IP сервера:")
        context.user_data['editing_key'] = 'server_ip'
        return SET_SERVER_IP
    elif data == "edit_port":
        await query.edit_message_text("Введите новый Port сервера:")
        context.user_data['editing_key'] = 'server_port'
        return SET_SERVER_PORT
    elif data == "edit_password":
        await query.edit_message_text("Введите новый Password сервера:")
        context.user_data['editing_key'] = 'server_password'
        return SET_SERVER_PASSWORD
    elif data == "back_menu":
        await query.edit_message_text("Главное меню:", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU

async def set_server_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> int:
    value = update.message.text
    await set_server_setting(key, value)
    await update.message.reply_text(f"Настройка {key} обновлена.")
    await show_server_settings_simple(update)
    return ADMIN_MENU

async def show_server_settings_simple(update):
    ip = await get_server_setting("server_ip") or "не задан"
    port = await get_server_setting("server_port") or "не задан"
    password = await get_server_setting("server_password") or "не задан"
    text = f"Текущие настройки сервера:\nIP: {ip}\nPort: {port}\nPassword: {password}"
    keyboard = [
        [InlineKeyboardButton("Изменить IP", callback_data="edit_ip")],
        [InlineKeyboardButton("Изменить Port", callback_data="edit_port")],
        [InlineKeyboardButton("Изменить Password", callback_data="edit_password")],
        [InlineKeyboardButton("Назад", callback_data="back_menu")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def add_match_team1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("team1_"):
        team = data.split("_", 1)[1]
        context.user_data['match_team1'] = team
        chats = await get_chats()
        buttons = [[InlineKeyboardButton(c.name, callback_data=f"team2_{c.name}")] for c in chats if c.name != team]
        buttons.append([InlineKeyboardButton("Ввести вручную", callback_data="team2_manual")])
        await query.edit_message_text("Выберите вторую команду:", reply_markup=InlineKeyboardMarkup(buttons))
        return ADD_MATCH_TEAM2
    elif data == "team1_manual":
        await query.edit_message_text("Введите название первой команды вручную:")
        return ADD_MATCH_TEAM1

async def add_match_team1_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    team = update.message.text
    context.user_data['match_team1'] = team
    chats = await get_chats()
    buttons = [[InlineKeyboardButton(c.name, callback_data=f"team2_{c.name}")] for c in chats if c.name != team]
    buttons.append([InlineKeyboardButton("Ввести вручную", callback_data="team2_manual")])
    await update.message.reply_text("Выберите вторую команду:", reply_markup=InlineKeyboardMarkup(buttons))
    return ADD_MATCH_TEAM2

async def add_match_team2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("team2_"):
        team = data.split("_", 1)[1]
        context.user_data['match_team2'] = team
        await query.edit_message_text("Введите дату и время матча в формате ГГГГ-ММ-ДД ЧЧ:ММ (по МСК):")
        return ADD_MATCH_TIME
    elif data == "team2_manual":
        await query.edit_message_text("Введите название второй команды вручную:")
        return ADD_MATCH_TEAM2

async def add_match_team2_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    team = update.message.text
    context.user_data['match_team2'] = team
    await update.message.reply_text("Введите дату и время матча в формате ГГГГ-ММ-ДД ЧЧ:ММ (по МСК):")
    return ADD_MATCH_TIME

async def add_match_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    try:
        msk_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text("Неверный формат. Используйте ГГГГ-ММ-ДД ЧЧ:ММ (по МСК)")
        return ADD_MATCH_TIME
    utc_naive = msk_to_utc(msk_naive)
    team1 = context.user_data['match_team1']
    team2 = context.user_data['match_team2']
    match = await add_match(team1, team2, utc_naive)
    msk_display = utc_to_msk(utc_naive)
    await update.message.reply_text(
        f"Матч {team1} - {team2} на {msk_display.strftime('%Y-%m-%d %H:%M')} (МСК) добавлен."
    )
    await schedule_match_notifications(context.bot, match)
    await update.message.reply_text("Главное меню:", reply_markup=admin_menu_keyboard())
    return ADMIN_MENU

# --- Планирование уведомлений ---
async def schedule_match_notifications(bot, match: Match):
    time_45 = match.match_time - timedelta(minutes=45)
    time_15 = match.match_time - timedelta(minutes=15)
    now = datetime.utcnow()
    if now < time_45 and not match.notification_45_sent:
        if bot.job_queue:
            bot.job_queue.run_at(
                send_45_min_notification,
                time_45,
                name=f"match_{match.id}_45",
                user_id=match.id,
            )
    if now < time_15 and not match.notification_15_sent:
        if bot.job_queue:
            bot.job_queue.run_at(
                send_15_min_notification,
                time_15,
                name=f"match_{match.id}_15",
                user_id=match.id,
            )

async def send_45_min_notification(context: ContextTypes.DEFAULT_TYPE):
    match_id = context.job.user_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()
        if not match or match.notification_45_sent:
            return
        chats = await get_chats()
        for chat in chats:
            try:
                await context.bot.send_message(
                    chat_id=chat.chat_id,
                    text=f"Внимание, до матча {match.team1} - {match.team2} осталось 45 минут. Не забудьте прийти!"
                )
            except Exception as e:
                logger.error(f"Не удалось отправить в чат {chat.chat_id}: {e}")
        await update_match_notification_sent(match_id, "notification_45_sent")

async def send_15_min_notification(context: ContextTypes.DEFAULT_TYPE):
    match_id = context.job.user_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()
        if not match or match.notification_15_sent:
            return
        ip = await get_server_setting("server_ip") or "не задан"
        port = await get_server_setting("server_port") or "не задан"
        password = await get_server_setting("server_password") or "не задан"
        text = (
            f"калл Раскатка!\n"
            f"(Название сервера)\n"
            f"IP сервера: {ip}\n"
            f"Port сервера: {port}\n"
            f"Password сервера: {password}\n\n"
            f"Вы блу.\n"
            f"Если что, хозяева - ред({match.team1})\n"
            f"гости - блу({match.team2})"
        )
        chats = await get_chats()
        for chat in chats:
            try:
                await context.bot.send_message(chat_id=chat.chat_id, text=text)
            except Exception as e:
                logger.error(f"Не удалось отправить в чат {chat.chat_id}: {e}")
        await update_match_notification_sent(match_id, "notification_15_sent")

# --- Привязка канала ---
async def add_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    channel_input = update.message.text.strip()
    if channel_input.startswith('@'):
        username = channel_input[1:]
        try:
            chat = await context.bot.get_chat(username)
            channel_id = chat.id
        except Exception as e:
            await update.message.reply_text(f"Не удалось найти канал @{username}. Ошибка: {e}")
            return ADD_CHANNEL_ID
    else:
        try:
            channel_id = int(channel_input)
        except ValueError:
            await update.message.reply_text("Неверный формат. Введите ID канала (число) или @username.")
            return ADD_CHANNEL_ID
    async with AsyncSessionLocal() as session:
        existing = await session.execute(select(ChannelSubscription).where(ChannelSubscription.channel_id == channel_id))
        if existing.scalar_one_or_none():
            await update.message.reply_text("Этот канал уже привязан.")
        else:
            await add_channel_subscription(channel_id, username if channel_input.startswith('@') else None)
            await update.message.reply_text(f"Канал {channel_input} привязан.")
    await update.message.reply_text("Главное меню:", reply_markup=admin_menu_keyboard())
    return ADMIN_MENU

async def show_channels(query):
    subs = await get_channel_subscriptions()
    if not subs:
        text = "Нет привязанных каналов."
    else:
        text = "Привязанные каналы:\n" + "\n".join([f"ID: {s.channel_id}" + (f" (@{s.channel_username})" if s.channel_username else "") for s in subs])
    keyboard = [
        [InlineKeyboardButton("Удалить канал", callback_data="delete_channel")],
        [InlineKeyboardButton("Назад", callback_data="back_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_channel_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_menu":
        await query.edit_message_text("Главное меню:", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU
    if data == "delete_channel":
        subs = await get_channel_subscriptions()
        if not subs:
            await query.edit_message_text("Нет привязанных каналов.")
            return ADMIN_MENU
        buttons = []
        for s in subs:
            label = f"{s.channel_id}" + (f" (@{s.channel_username})" if s.channel_username else "")
            buttons.append([InlineKeyboardButton(label, callback_data=f"delchan_{s.channel_id}")])
        buttons.append([InlineKeyboardButton("Назад", callback_data="back_menu")])
        await query.edit_message_text("Выберите канал для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
        return DELETE_CHANNEL_SELECT

async def delete_channel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("delchan_"):
        channel_id = int(data.split("_")[1])
        success = await delete_channel_subscription(channel_id)
        if success:
            await query.edit_message_text("Канал удален.")
        else:
            await query.edit_message_text("Не удалось удалить канал.")
        subs = await get_channel_subscriptions()
        if not subs:
            await query.edit_message_text("Нет привязанных каналов.")
        else:
            buttons = []
            for s in subs:
                label = f"{s.channel_id}" + (f" (@{s.channel_username})" if s.channel_username else "")
                buttons.append([InlineKeyboardButton(label, callback_data=f"delchan_{s.channel_id}")])
            buttons.append([InlineKeyboardButton("Назад", callback_data="back_menu")])
            await query.edit_message_text("Выберите канал для удаления:", reply_markup=InlineKeyboardMarkup(buttons))
        return DELETE_CHANNEL_SELECT
    elif data == "back_menu":
        await query.edit_message_text("Главное меню:", reply_markup=admin_menu_keyboard())
        return ADMIN_MENU

# --- Репост из канала ---
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    channel_id = update.channel_post.chat_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ChannelSubscription).where(ChannelSubscription.channel_id == channel_id))
        if not result.scalar_one_or_none():
            return
    text = update.channel_post.text or update.channel_post.caption or ""
    if not ("#rplpuck" in text and ("#MatchDay" in text or "#result" in text)):
        return
    if await is_reposted(channel_id, update.channel_post.message_id):
        return
    chats = await get_chats()
    for chat in chats:
        try:
            await update.channel_post.forward(chat_id=chat.chat_id)
        except Exception as e:
            logger.error(f"Не удалось переслать в чат {chat.chat_id}: {e}")
    await mark_reposted(channel_id, update.channel_post.message_id)

# --- Восстановление задач при запуске ---
async def restore_jobs(application: Application):
    matches = await get_matches()
    now = datetime.utcnow()
    for match in matches:
        time_45 = match.match_time - timedelta(minutes=45)
        time_15 = match.match_time - timedelta(minutes=15)
        if not match.notification_45_sent and now < time_45:
            application.job_queue.run_at(
                send_45_min_notification,
                time_45,
                name=f"match_{match.id}_45",
                user_id=match.id,
            )
        if not match.notification_15_sent and now < time_15:
            application.job_queue.run_at(
                send_15_min_notification,
                time_15,
                name=f"match_{match.id}_15",
                user_id=match.id,
            )

# --- Основная функция ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.run_until_complete(restore_jobs(application))
    
    application.add_handler(CommandHandler("start", start))
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl_start)],
        states={
            LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adminkarpl_login)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, adminkarpl_password)],
            ADMIN_MENU: [CallbackQueryHandler(admin_menu_callback)],
            ADD_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chat_id)],
            ADD_CHAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chat_name)],
            DELETE_CHAT_SELECT: [CallbackQueryHandler(delete_chat_select)],
            SET_SERVER_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: set_server_value(u,c,'server_ip'))],
            SET_SERVER_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: set_server_value(u,c,'server_port'))],
            SET_SERVER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: set_server_value(u,c,'server_password'))],
            ADD_MATCH_TEAM1: [
                CallbackQueryHandler(add_match_team1),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_match_team1_text)
            ],
            ADD_MATCH_TEAM2: [
                CallbackQueryHandler(add_match_team2),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_match_team2_text)
            ],
            ADD_MATCH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_match_time)],
            ADD_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_id)],
            DELETE_CHANNEL_SELECT: [CallbackQueryHandler(delete_channel_confirm)],
        },
        fallbacks=[],
    )
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
