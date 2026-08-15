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

# SQLAlchemy – исправленный импорт
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
from sqlalchemy.orm import declarative_base  # <--- новый импорт
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
    msk_aware = msk_naive.replace(tzinfo=MSK_TZ)
    utc_aware = msk_aware.astimezone(UTC_TZ)
    return utc_aware.replace(tzinfo=None)

def utc_to_msk(utc_naive: datetime) -> datetime:
    utc_aware = utc_naive.replace(tzinfo=UTC_TZ)
    msk_aware = utc_aware.astimezone(MSK_TZ)
    return msk_aware

# --- База данных ---
Base = declarative_base()  # <--- теперь без предупреждения

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
    match_time = Column(DateTime, nullable=False)
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

# --- Вспомогательные функции БД (без изменений) ---
# ... (остальной код полностью идентичен предыдущей версии, 
#      так как все остальные функции не меняются)

# В конце main() – запуск бота
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
