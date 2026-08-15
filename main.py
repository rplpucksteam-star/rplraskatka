import os
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Часовой пояс (Москва, UTC+3)
MSK_TZ = pytz.timezone("Europe/Moscow")

# Токен и URL базы данных
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_LOGIN = "adminrpl"
ADMIN_PASSWORD = "rpl1488"
ADMIN_SESSION_HOURS = 12

# Состояния разговорных хэндлеров
(
    WAITING_LOGIN,
    WAITING_PASSWORD,
    # Команды и сервер
    ADD_TEAM_CHAT_ID,
    ADD_TEAM_NAME,
    SET_SERVER_NAME,
    SET_SERVER_IP,
    SET_SERVER_PORT,
    SET_SERVER_PASS,
    # Матчи
    MATCH_HOME_TEAM,
    MATCH_AWAY_TEAM,
    MATCH_TIME,
    # Канал
    SET_SOURCE_CHANNEL,
) = range(11)

# ---------- БАЗА ДАННЫХ (PostgreSQL) ----------
def get_db():
    url = DATABASE_URL
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    c = conn.cursor()

    # Админы
    c.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            login_time TIMESTAMP
        )
    """)

    # Команды / Чаты
    c.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT UNIQUE NOT NULL,
            team_name TEXT NOT NULL
        )
    """)

    # Настройки сервера и канала
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Матчи
    c.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            home_team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
            away_team_id INTEGER REFERENCES teams(id) ON DELETE CASCADE,
            match_time TIMESTAMP NOT NULL,
            warn_45_sent BOOLEAN DEFAULT FALSE,
            warn_15_sent BOOLEAN DEFAULT FALSE
        )
    """)

    conn.commit()
    conn.close()

def get_config(key, default=""):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE key = %s", (key,))
    row = c.fetchone()
    conn.close()
    return row["value"] if row else default

def set_config(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO bot_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT login_time FROM admins WHERE user_id = %s", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return True
    return False

def add_admin(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO admins (user_id, login_time) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET login_time = EXCLUDED.login_time",
        (user_id, datetime.now(MSK_TZ)),
    )
    conn.commit()
    conn.close()

def remove_admin(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

# ---------- КЛАВИАТУРЫ ----------
def admin_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["⚽ Добавить матч", "📋 Список матчей"],
            ["🏒 Добавить команду/чат", "🛡 Список команд"],
            ["⚙️ Настройки сервера", "📢 Настроить канал"],
            ["🚪 Выйти из админки"],
        ],
        resize_keyboard=True,
    )

# ---------- СТАРТ И АВТОРИЗАЦИЯ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "В данном боте нет ничего интересного, он предназначен для раскаток и всему подобному.\n"
        "Лучше перейди и играй в нашем боте в карточки игроков Puck - @rplpuck_bot."
    )
    await update.message.reply_text(text)

async def adminkarpl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Админ-панель доступна только в личных сообщениях с ботом.")
        return ConversationHandler.END

    if is_admin(update.effective_user.id):
        await update.message.reply_text("🔑 Вы уже авторизованы в админке!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END

    await update.message.reply_text("🔑 Введите логин:")
    return WAITING_LOGIN

async def wait_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login"] = update.message.text.strip()
    await update.message.reply_text("🔒 Введите пароль:")
    return WAITING_PASSWORD

async def wait_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login = context.user_data.get("login")
    password = update.message.text.strip()

    if login == ADMIN_LOGIN and password == ADMIN_PASSWORD:
        add_admin(update.effective_user.id)
        context.user_data.clear()
        await update.message.reply_text("✅ Вы успешно вошли в админ-панель!", reply_markup=admin_menu_keyboard())
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Неверный логин или пароль!")
        return ConversationHandler.END

# ---------- МЕНЮ АДМИНА ----------
async def admin_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END

    text = update.message.text

    if text == "🚪 Выйти из админки":
        remove_admin(user_id)
        await update.message.reply_text("🚪 Вы вышли из админ-панели.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    elif text == "⚙️ Настройки сервера":
        srv_name = get_config("server_name", "Не задано")
        srv_ip = get_config("server_ip", "Не задано")
        srv_port = get_config("server_port", "Не задано")
        srv_pass = get_config("server_pass", "Не задано")

        info = (
            f"⚙️ **Текущие настройки сервера:**\n\n"
            f"📌 Название: `{srv_name}`\n"
            f"🌐 IP: `{srv_ip}`\n"
            f"🔌 Порт: `{srv_port}`\n"
            f"🔑 Пароль: `{srv_pass}`\n\n"
            f"Введите новое **Название сервера** (или /cancel для отмены):"
        )
        await update.message.reply_text(info, parse_mode="Markdown")
        return SET_SERVER_NAME

    elif text == "🏒 Добавить команду/чат":
        await update.message.reply_text(
            "Введите **ID чата** команды (например: `-100123456789`):\n"
            "*(Убедитесь, что бот добавлен в этот чат)*"
        )
        return ADD_TEAM_CHAT_ID

    elif text == "🛡 Список команд":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM teams ORDER BY team_name")
        teams = c.fetchall()
        conn.close()

        if not teams:
            await update.message.reply_text("📭 Команды ещё не добавлены.")
            return

        msg = "🛡 **Список привязанных команд:**\n\n"
        for t in teams:
            msg += f"• **{t['team_name']}** (ID чата: `{t['chat_id']}`)\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

    elif text == "📢 Настроить канал":
        src_chan = get_config("source_channel", "Не привязан")
        await update.message.reply_text(
            f"📢 Текущий канал-источник: `{src_chan}`\n\n"
            "Введите `@username` или `ID` канала (бот должен быть админом канала):",
            parse_mode="Markdown",
        )
        return SET_SOURCE_CHANNEL

    elif text == "⚽ Добавить матч":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM teams ORDER BY team_name")
        teams = c.fetchall()
        conn.close()

        if len(teams) < 2:
            await update.message.reply_text("❌ Для создания матча нужно добавить хотя бы 2 команды!")
            return ConversationHandler.END

        buttons = [[InlineKeyboardButton(t["team_name"], callback_data=f"home_{t['id']}")] for t in teams]
        await update.message.reply_text("🔴 **Выберите хозяев (Хозяева - ред / Команда 1):**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
        return MATCH_HOME_TEAM

    elif text == "📋 Список матчей":
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            SELECT m.id, m.match_time, m.warn_45_sent, m.warn_15_sent,
                   t1.team_name as home_team, t2.team_name as away_team
            FROM matches m
            JOIN teams t1 ON m.home_team_id = t1.id
            JOIN teams t2 ON m.away_team_id = t2.id
            WHERE m.match_time > NOW() - INTERVAL '2 hours'
            ORDER BY m.match_time ASC
        """)
        matches = c.fetchall()
        conn.close()

        if not matches:
            await update.message.reply_text("📭 Запланированных матчей нет.")
            return

        msg = "📋 **Запланированные матчи:**\n\n"
        for m in matches:
            t_str = m["match_time"].strftime("%d.%m.%Y %H:%M MSK")
            msg += f"⚽ **{m['home_team']}** vs **{m['away_team']}**\n🕒 Время: `{t_str}`\n\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

# ---------- НАСТРОЙКА СЕРВЕРА ----------
async def set_server_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config("server_name", update.message.text.strip())
    await update.message.reply_text("🌐 Введите **IP сервера**:")
    return SET_SERVER_IP

async def set_server_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config("server_ip", update.message.text.strip())
    await update.message.reply_text("🔌 Введите **Port сервера**:")
    return SET_SERVER_PORT

async def set_server_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config("server_port", update.message.text.strip())
    await update.message.reply_text("🔑 Введите **Password сервера**:")
    return SET_SERVER_PASS

async def set_server_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_config("server_pass", update.message.text.strip())
    await update.message.reply_text("✅ Данные сервера успешно сохранены!", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END

# ---------- ДОБАВЛЕНИЕ КОМАНДЫ ----------
async def add_team_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = int(update.message.text.strip())
        context.user_data["new_chat_id"] = chat_id
        await update.message.reply_text("📝 Введите название клуба (например: `Динамо Москва`):", parse_mode="Markdown")
        return ADD_TEAM_NAME
    except ValueError:
        await update.message.reply_text("❌ ID чата должно быть числом! Попробуйте снова:")
        return ADD_TEAM_CHAT_ID

async def add_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_name = update.message.text.strip()
    chat_id = context.user_data.get("new_chat_id")

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO teams (chat_id, team_name) VALUES (%s, %s)", (chat_id, team_name))
        conn.commit()
        await update.message.reply_text(f"✅ Команда **{team_name}** успешно привязана к чату `{chat_id}`!", reply_markup=admin_menu_keyboard(), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка добавления: возможно данный чат уже привязан.", reply_markup=admin_menu_keyboard())
    conn.close()
    return ConversationHandler.END

# ---------- НАСТРОЙКА КАНАЛА ----------
async def set_source_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ch = update.message.text.strip()
    set_config("source_channel", ch)
    await update.message.reply_text(f"✅ Канал `{ch}` успешно привязан для пересылки постов!", reply_markup=admin_menu_keyboard(), parse_mode="Markdown")
    return ConversationHandler.END

# ---------- ДОБАВЛЕНИЕ МАТЧА ----------
async def match_home_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    home_id = int(query.data.split("_")[1])
    context.user_data["home_team_id"] = home_id

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM teams WHERE id != %s ORDER BY team_name", (home_id,))
    teams = c.fetchall()
    conn.close()

    buttons = [[InlineKeyboardButton(t["team_name"], callback_data=f"away_{t['id']}")] for t in teams]
    await query.message.edit_text("🔵 **Выберите гостей (Гости - блу / Команда 2):**", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return MATCH_AWAY_TEAM

async def match_away_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    away_id = int(query.data.split("_")[1])
    context.user_data["away_team_id"] = away_id

    await query.message.edit_text(
        "🕒 Введите дату и время матча по МСК в формате:\n`ДД.ММ.ГГГГ ЧЧ:ММ` или `ЧЧ:ММ` (если матч сегодня)\n\n"
        "Пример: `25.10.2026 19:30` или `21:00`",
        parse_mode="Markdown"
    )
    return MATCH_TIME

async def match_time_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    now_msk = datetime.now(MSK_TZ)

    try:
        if len(text) <= 5 and ":" in text:
            # ЧЧ:ММ (сегодня)
            t = datetime.strptime(text, "%H:%M").time()
            match_dt = datetime.combine(now_msk.date(), t)
            match_dt = MSK_TZ.localize(match_dt)
            if match_dt < now_msk:
                match_dt += timedelta(days=1)
        else:
            # ДД.ММ.ГГГГ ЧЧ:ММ
            match_dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
            match_dt = MSK_TZ.localize(match_dt)

    except ValueError:
        await update.message.reply_text("❌ Неверный формат времени! Попробуйте еще раз (например `20:00` или `15.11.2026 18:30`):")
        return MATCH_TIME

    home_id = context.user_data.get("home_team_id")
    away_id = context.user_data.get("away_team_id")

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO matches (home_team_id, away_team_id, match_time) VALUES (%s, %s, %s)",
        (home_id, away_id, match_dt.replace(tzinfo=None))
    )
    c.execute("SELECT team_name FROM teams WHERE id = %s", (home_id,))
    h_name = c.fetchone()["team_name"]
    c.execute("SELECT team_name FROM teams WHERE id = %s", (away_id,))
    a_name = c.fetchone()["team_name"]
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **Матч успешно создан!**\n\n"
        f"🔴 Хозяева (ред): **{h_name}**\n"
        f"🔵 Гости (блу): **{a_name}**\n"
        f"🕒 Время: `{match_dt.strftime('%d.%m.%Y %H:%M MSK')}`",
        reply_markup=admin_menu_keyboard(),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ---------- ПЕРЕСЫЛКА ПОСТОВ ИЗ КАНАЛА ----------
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    source_chan = get_config("source_channel")
    if not source_chan:
        return

    # Проверка, из того ли канала пост
    chat = msg.chat
    if str(chat.id) != source_chan and f"@{chat.username}" != source_chan:
        return

    text = msg.text or msg.caption or ""
    allowed_hashtags = ["#rplpuck", "#MatchDay", "#result"]

    # Проверка наличия одного из хэштегов
    if not any(tag.lower() in text.lower() for tag in allowed_hashtags):
        return

    # Получаем все чаты команд
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM teams")
    teams = c.fetchall()
    conn.close()

    for t in teams:
        try:
            await context.bot.forward_message(
                chat_id=t["chat_id"],
                from_chat_id=chat.id,
                message_id=msg.message_id
            )
        except Exception as e:
            logger.error(f"Не удалось переслать сообщение в чат {t['chat_id']}: {e}")

# ---------- ФОНОВЫЙ ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ (45 и 15 МИНУТ) ----------
async def scheduler_worker(app: Application):
    while True:
        try:
            now_utc = datetime.utcnow()
            conn = get_db()
            c = conn.cursor()

            # Получаем предстоящие матчи
            c.execute("""
                SELECT m.id, m.match_time, m.warn_45_sent, m.warn_15_sent,
                       t1.chat_id as home_chat, t1.team_name as home_name,
                       t2.chat_id as away_chat, t2.team_name as away_name
                FROM matches m
                JOIN teams t1 ON m.home_team_id = t1.id
                JOIN teams t2 ON m.away_team_id = t2.id
                WHERE m.match_time > NOW() - INTERVAL '1 hour'
                  AND (m.warn_45_sent = FALSE OR m.warn_15_sent = FALSE)
            """)
            matches = c.fetchall()

            for m in matches:
                m_time = m["match_time"]
                diff_minutes = (m_time - now_utc).total_seconds() / 60.0

                # Настройки сервера
                srv_name = get_config("server_name", "Не указано")
                srv_ip = get_config("server_ip", "Не указано")
                srv_port = get_config("server_port", "Не указано")
                srv_pass = get_config("server_pass", "Не указано")

                # --- 45 МИНУТ ДО МАТЧА ---
                if 40.0 <= diff_minutes <= 45.0 and not m["warn_45_sent"]:
                    msg_45 = "Внимание, до матча осталось 45 минут. Не забудьте прийти!"
                    for chat_id in [m["home_chat"], m["away_chat"]]:
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=msg_45)
                        except Exception as e:
                            logger.error(f"Ошибка отправки 45 мин в {chat_id}: {e}")

                    c.execute("UPDATE matches SET warn_45_sent = TRUE WHERE id = %s", (m["id"],))
                    conn.commit()

                # --- 15 МИНУТ ДО МАТЧА ---
                if 10.0 <= diff_minutes <= 15.0 and not m["warn_15_sent"]:
                    # Сообщение Хозяевам (Красные)
                    msg_home = (
                        f"калл Раскатка!\n"
                        f"{srv_name}\n"
                        f"IP сервера: {srv_ip}\n"
                        f"Port сервера: {srv_port}\n"
                        f"Password сервера: {srv_pass}\n\n"
                        f"Вы ред."
                    )
                    # Сообщение Гостям (Синие)
                    msg_away = (
                        f"калл Раскатка!\n"
                        f"{srv_name}\n"
                        f"IP сервера: {srv_ip}\n"
                        f"Port сервера: {srv_port}\n"
                        f"Password сервера: {srv_pass}\n\n"
                        f"Вы блу."
                    )

                    try:
                        await app.bot.send_message(chat_id=m["home_chat"], text=msg_home)
                    except Exception as e:
                        logger.error(f"Ошибка отправки 15 мин хозяевам {m['home_chat']}: {e}")

                    try:
                        await app.bot.send_message(chat_id=m["away_chat"], text=msg_away)
                    except Exception as e:
                        logger.error(f"Ошибка отправки 15 мин гостям {m['away_chat']}: {e}")

                    c.execute("UPDATE matches SET warn_15_sent = TRUE WHERE id = %s", (m["id"],))
                    conn.commit()

            conn.close()
        except Exception as e:
            logger.error(f"Ошибка в работы планировщика: {e}")

        await asyncio.sleep(25)

async def post_init(app: Application):
    init_db()
    asyncio.create_task(scheduler_worker(app))

# ---------- MAIN ----------
def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения!")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задан в переменных окружения!")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # Админ диалог
    conv_admin = ConversationHandler(
        entry_points=[CommandHandler("adminkarpl", adminkarpl)],
        states={
            WAITING_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_login)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_password)],
            # Выбор команд и чатов
            ADD_TEAM_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_team_chat_id)],
            ADD_TEAM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_team_name)],
            # Настройка сервера
            SET_SERVER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_server_name)],
            SET_SERVER_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_server_ip)],
            SET_SERVER_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_server_port)],
            SET_SERVER_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_server_pass)],
            # Настройка канала
            SET_SOURCE_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_source_channel)],
            # Создание матча
            MATCH_HOME_TEAM: [CallbackQueryHandler(match_home_selected, pattern="^home_")],
            MATCH_AWAY_TEAM: [CallbackQueryHandler(match_away_selected, pattern="^away_")],
            MATCH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, match_time_received)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("Отменено.", reply_markup=admin_menu_keyboard()))],
        per_message=False
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_admin)

    # Обработчик кнопок админ-панели
    app.add_handler(MessageHandler(filters.Regex("^(⚽ Добавить матч|📋 Список матчей|🏒 Добавить команду/чат|🛡 Список команд|⚙️ Настройки сервера|📢 Настроить канал|🚪 Выйти из админки)$") & filters.ChatType.PRIVATE, admin_button_handler))

    # Обработчик постов из каналов
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, channel_post_handler))

    logger.info("Бот раскаток запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
