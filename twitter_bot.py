import os
import asyncio
import hashlib
import logging
import random
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
import aiosqlite
import telegram.error

# https://gptonline.ai/ — поддержка и инструкции

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ====== ТЕСТОВЫЕ ДАННЫЕ ГРУППЫ ======
TELEGRAM_BOT_TOKEN_APPROVAL = "8326777624:AAG_Owp9T4zsFryttparUnqjqtrVhpHR_LQ"
TELEGRAM_APPROVAL_CHAT_ID  = -1002892475684      # ID группы для модерации
TELEGRAM_APPROVAL_USER_ID  = 6105016521          # не используется, просто для примера
TELEGRAM_CHANNEL_ID        = "@test_channel_name"  # замените на свой канал или оставьте как есть для теста
# ====================================

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg"
]

post_data = {
    "text_ru":   "Майнинговые токены снова в фокусе...",
    "image_url": test_images[0],
    "timestamp": None,
    "post_id":   0
}
prev_data = post_data.copy()

do_not_disturb       = {"active": False}
pending_post         = {"active": False, "timer": None}
last_action_time     = {}
approval_message_ids = {"photo": None, "timer": None}

keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")]
])

DB_FILE = "post_history.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
        """)
        await db.commit()
    logging.info("База данных инициализирована.")

def get_image_hash(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения: {e}")
        return None

async def save_post_to_history(text: str, image_url: str | None = None):
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")

async def is_duplicate(text: str, image_url: str) -> bool:
    img_hash = get_image_hash(image_url)
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE text = ? OR image_hash = ?",
            (text, img_hash)
        )
        row = await cursor.fetchone()
    return row[0] > 0

async def send_post_for_approval():
    if do_not_disturb["active"]:
        logging.info("Режим 'Не беспокоить' активен — пропуск отправки.")
        return

    if pending_post["active"]:
        logging.info("Уже есть активный пост — ожидаем решения.")
        return

    if await is_duplicate(post_data["text_ru"], post_data["image_url"]):
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⚠️ Этот пост уже публиковался ранее — отменено."
        )
        return

    post_data["timestamp"] = datetime.now()
    pending_post.update({"active": True, "timer": datetime.now()})
    try:
        photo_msg = await approval_bot.send_photo(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"],
            reply_markup=keyboard
        )
        approval_message_ids["photo"] = photo_msg.message_id

        # 60 секунд на решение
        for sec in range(59, -1, -1):
            await asyncio.sleep(1)
            try:
                await approval_bot.edit_message_text(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=approval_message_ids["photo"],
                    text=f"⏳ Таймер: {sec} сек.",
                    reply_markup=keyboard
                )
            except Exception:
                pass

    except Exception as e:
        logging.error(f"Ошибка при отправке на согласование: {e}")
    finally:
        pending_post["active"] = False

async def publish_post():
    if not TELEGRAM_CHANNEL_ID:
        logging.error("TELEGRAM_CHANNEL_ID не задан.")
        return

    try:
        await approval_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"]
        )
        logging.info("Пост опубликован в канал.")
    except telegram.error.RetryAfter as e:
        logging.warning(f"Rate limit при публикации, ждём {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
        await approval_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"]
        )
    except Exception as e:
        logging.error(f"Ошибка публикации: {e}")
        return

    await save_post_to_history(post_data["text_ru"], post_data["image_url"])
    pending_post["active"] = False

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"]:
            if datetime.now() - pending_post["timer"] > timedelta(seconds=60):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время модерации истекло, публикую автоматически."
                )
                await publish_post()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global prev_data

    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).total_seconds() < 15:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Подождите немного перед следующим действием."
        )
        return
    last_action_time[user_id] = now

    action = query.data
    prev_data = post_data.copy()

    try:
        if action == "approve":
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Публикация поста…")
            await publish_post()

        elif action == "think":
            pending_post["timer"] = datetime.now()
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🧐 Думаем дальше…")

        elif action == "regenerate":
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔄 Генерация нового текста…")
            post_data["text_ru"] = f"Новый тестовый текст #{post_data['post_id'] + 1}"
            post_data["post_id"] += 1
            await send_post_for_approval()

    except Exception as e:
        logging.error(f"Ошибка в button_handler: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка: {e}")

async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval()
    asyncio.create_task(check_timer())
    logging.info("Бот запущен и готов к работе.")

def main():
    app = Application.builder() \
        .token(TELEGRAM_BOT_TOKEN_APPROVAL) \
        .post_init(delayed_start) \
        .build()

    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
