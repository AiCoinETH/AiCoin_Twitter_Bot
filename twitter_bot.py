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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")    # токен для группы модерации
TELEGRAM_APPROVAL_CHAT_ID   = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL  = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")     # токен для публикации
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")  # '@AiCoin_ETH'

if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_BOT_TOKEN_CHANNEL or not TELEGRAM_CHANNEL_USERNAME_ID:
    logging.error("Не заданы обязательные переменные окружения (BOT_TOKEN_APPROVAL, APPROVAL_CHAT_ID, BOT_TOKEN_CHANNEL или CHANNEL_USERNAME_ID)")
    exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

# ========== ДАННЫЕ ДЛЯ ТЕСТА ==========
test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

post_data = {
    "text_ru":   "Майнинговые токены снова в фокусе...",
    "image_url": test_images[0],
    "timestamp": None,
    "post_id":   0
}
prev_data = post_data.copy()

pending_post         = {"active": False, "timer": None}
do_not_disturb       = {"active": False}
last_action_time     = {}
approval_message_ids = {"photo": None}
DB_FILE = "post_history.db"

# ========== КЛАВИАТУРА ДЛЯ МОДЕРАЦИИ ==========
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")]
])

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
            """
        )
        await db.commit()
    logging.info("База данных инициализирована.")

def get_image_hash(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, timeout=3)  # Было 5, теперь 3
        r.raise_for_status()
        return hashlib.sha256(r.content).hexdigest()
    except Exception as e:
        logging.warning(f"Не удалось получить хеш изображения: {e}")
        return None

async def save_post_to_history(text, image_url=None):
    image_hash = get_image_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")

# ========== ОТПРАВКА НА МОДЕРАЦИЮ ==========
async def send_post_for_approval():
    if do_not_disturb["active"] or pending_post["active"]:
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
        logging.info("Пост отправлен на согласование.")
    except Exception as e:
        logging.error(f"Ошибка при отправке на согласование: {e}")

# ========== ПУБЛИКАЦИЯ В КАНАЛ ==========
async def publish_post_to_channel():
    try:
        msg = await channel_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"]
        )
        logging.info(f"Пост опубликован в канал {TELEGRAM_CHANNEL_USERNAME_ID}, message_id={msg.message_id}")
    except telegram.error.Forbidden as e:
        logging.error(f"Forbidden: Бот не админ или не может писать в канал {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")
    except telegram.error.BadRequest as e:
        logging.error(f"BadRequest: Проверьте username канала {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")
    except Exception as e:
        logging.error(f"Ошибка публикации в канал {TELEGRAM_CHANNEL_USERNAME_ID}: {e}")

    # Логирование только по факту публикации (быстрее основной поток)
    asyncio.create_task(save_post_to_history(post_data["text_ru"], post_data["image_url"]))
    pending_post["active"] = False

# ========== ТАЙМЕР МОДЕРАЦИИ ==========
async def check_timer():
    while True:
        await asyncio.sleep(0.3)  # Было 1 секунда, теперь быстрее
        if pending_post["active"] and pending_post.get("timer") and (datetime.now() - pending_post["timer"]) > timedelta(seconds=60):
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло. Публикую автоматически."
                )
            except Exception:
                pass
            await publish_post_to_channel()
            pending_post["active"] = False

# ========== ОБРАБОТЧИК КНОПОК ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data
    await update.callback_query.answer()
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:  # Было 5, теперь 3
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...")
        return
    last_action_time[user_id] = now
    action = update.callback_query.data
    prev_data.update(post_data)
    if action == 'approve':
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Публикую в канал…")
        await publish_post_to_channel()
    elif action == 'think':
        pending_post["timer"] = datetime.now()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🧐 Думаем дальше…")
    elif action == 'regenerate':
        post_data["text_ru"] = f"Новый тестовый текст #{post_data['post_id'] + 1}"
        post_data["post_id"] += 1
        await send_post_for_approval()
    elif action == "new_image":
        post_data["image_url"] = random.choice([img for img in test_images if img != post_data["image_url"]])
        await send_post_for_approval()
    elif action == "new_post":
        post_data["text_ru"] = f"Новый тестовый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        await send_post_for_approval()
    elif action == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Начинаем чат:\n" + post_data["text_ru"]
        )
    elif action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}."
        )
    elif action == "restore_previous":
        post_data.update(prev_data)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="↩️ Восстановлен предыдущий вариант.")
        await send_post_for_approval()
    elif action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔚 Завершили публикации на сегодня.")

# ========== ЗАПУСК ==========
async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval()
    asyncio.create_task(check_timer())
    logging.info("Бот запущен и готов к работе.")

def main():
    logging.info("Старт Telegram бота модерации и публикации…")
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    # Ускоренный polling!
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()
