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

# AI-инструменты и документация: https://gptonline.ai/

# ——————————————————————————————————————————————————————————————————————————————
# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# ——————————————————————————————————————————————————————————————————————————————
# Чтение переменных окружения
TELEGRAM_BOT_TOKEN_APPROVAL    = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID      = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID      = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_CHANNEL_USERNAME_ID   = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")  # '@AiCoin_ETH'

# Проверяем, что всё задано
if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_CHANNEL_USERNAME_ID:
    logging.error(
        "Нужно задать в окружении:\n"
        "  TELEGRAM_BOT_TOKEN_APPROVAL\n"
        "  TELEGRAM_APPROVAL_CHAT_ID\n"
        "  TELEGRAM_APPROVAL_USER_ID\n"
        "  TELEGRAM_CHANNEL_USERNAME_ID"
    )
    exit(1)

# Логируем, что подхвачено
logging.info(f"BOT_TOKEN          = {TELEGRAM_BOT_TOKEN_APPROVAL[:8]}…")
logging.info(f"APPROVAL_CHAT_ID   = {TELEGRAM_APPROVAL_CHAT_ID}")
logging.info(f"APPROVAL_USER_ID   = {TELEGRAM_APPROVAL_USER_ID}")
logging.info(f"CHANNEL_USERNAME   = {TELEGRAM_CHANNEL_USERNAME_ID}")

# Создаём экземпляр бота
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

# ——————————————————————————————————————————————————————————————————————————————
# Тестовые картинки
test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

# Текущее состояние поста
post_data = {
    "text_ru":    "Майнинговые токены снова в фокусе...",
    "image_url":  test_images[0],
    "timestamp":  None,
    "post_id":    0
}
prev_data = post_data.copy()

pending_post    = {"active": False, "timer": None}
text_in_progress  = False
image_in_progress = False
full_in_progress  = False
chat_in_progress  = False

do_not_disturb   = {"active": False}
countdown_task   = None
last_action_time = {}
approval_message_ids = {"photo": None, "timer": None}

# ——————————————————————————————————————————————————————————————————————————————
# Клавиатура для управления
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост",           callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать",      callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст",   callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком",  callback_data="new_post")],
    [
      InlineKeyboardButton("💬 Поговорить",   callback_data="chat"),
      InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")
    ],
    [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"),
     InlineKeyboardButton("🔚 Завершить",            callback_data="end_day")]
])

DB_FILE = "post_history.db"

# ——————————————————————————————————————————————————————————————————————————————
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
    logging.info("✅ База данных инициализирована.")

async def save_post_to_history(text, image_url=None):
    def get_hash(url):
        try:
            import requests
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            return hashlib.sha256(r.content).hexdigest()
        except Exception as e:
            logging.warning(f"⚠️ Не удалось получить хеш изображения: {e}")
            return None

    image_hash = get_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("✅ Пост сохранён в историю.")

# ——————————————————————————————————————————————————————————————————————————————
async def send_post_for_approval():
    if do_not_disturb["active"]:
        logging.info("🔕 Режим «Не беспокоить» активен, пропуск отправки.")
        return
    if pending_post["active"]:
        logging.info("⏳ Есть активный пост, новый не отправляется.")
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
        logging.info("📨 Пост отправлен на одобрение.")
    except telegram.error.RetryAfter as e:
        logging.warning(f"🐢 Rate limit, ждём {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
        await send_post_for_approval()
    except Exception as e:
        logging.error(f"❌ Ошибка отправки на одобрение: {e}")

# ——————————————————————————————————————————————————————————————————————————————
async def send_timer_message():
    countdown_msg = await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="⏳ Таймер: 60 секунд",
        reply_markup=keyboard
    )
    approval_message_ids["timer"] = countdown_msg.message_id

    async def update_countdown(msg_id):
        for i in range(59, -1, -1):
            await asyncio.sleep(1)
            try:
                await approval_bot.edit_message_text(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=msg_id,
                    text=f"⏳ Таймер: {i} секунд",
                    reply_markup=keyboard
                )
            except:
                pass
        pending_post["active"] = False

    global countdown_task
    if countdown_task and not countdown_task.done():
        countdown_task.cancel()
    countdown_task = asyncio.create_task(update_countdown(approval_message_ids["timer"]))

# ——————————————————————————————————————————————————————————————————————————————
async def publish_post():
    global pending_post, text_in_progress, image_in_progress, full_in_progress, chat_in_progress

    logging.info(f"🚀 Публикация в канал {TELEGRAM_CHANNEL_USERNAME_ID}...")
    try:
        msg = await approval_bot.send_photo(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            photo=post_data["image_url"],
            caption=post_data["text_ru"]
        )
        logging.info(f"✅ Опубликовано, message_id={msg.message_id}")
    except telegram.error.RetryAfter as e:
        logging.warning(f"🐢 Rate limit при публикации, ждём {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
        return await publish_post()
    except Exception as e:
        logging.error(f"❌ Ошибка публикации: {e}")

    await save_post_to_history(post_data["text_ru"], post_data["image_url"])

    # Сбрасываем все флаги, чтобы можно было отправлять следующий пост
    pending_post["active"] = False
    text_in_progress = image_in_progress = full_in_progress = chat_in_progress = False
    logging.info("🔄 Все флаги сброшены, готов к следующему посту.")

# ——————————————————————————————————————————————————————————————————————————————
async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"]:
            if datetime.now() - pending_post["timer"] > timedelta(seconds=60):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло, публикую автоматически."
                )
                await publish_post()

# ——————————————————————————————————————————————————————————————————————————————
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global text_in_progress, image_in_progress, full_in_progress, chat_in_progress, last_action_time

    await update.callback_query.answer()
    user_id = update.effective_user.id
    now = datetime.now()

    # Антиспам: 15 секунд между нажатиями одного пользователя
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 15:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Идёт обработка предыдущего запроса. Попробуйте через несколько секунд."
        )
        return
    last_action_time[user_id] = now

    action = update.callback_query.data
    prev_data.update(post_data)

    if action == 'approve':
        logging.info(f"👌 Пользователь {user_id} подтвердил публикацию.")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⏳ Публикую пост..."
        )
        await publish_post()

    elif action == 'think':
        if countdown_task and not countdown_task.done():
            countdown_task.cancel()
        pending_post["timer"] = datetime.now()
        await send_timer_message()

    elif action == 'regenerate':
        text_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерирую новый текст...")
            post_data['text_ru'] = f"Тестовый новый текст {post_data['post_id'] + 1}"
            post_data['post_id'] += 1
            await send_post_for_approval()
        finally:
            text_in_progress = False

    elif action == 'new_image':
        image_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерирую новую картинку...")
            post_data['image_url'] = random.choice([img for img in test_images if img != post_data['image_url']])
            post_data['post_id'] += 1
            await send_post_for_approval()
        finally:
            image_in_progress = False

    elif action == 'new_post':
        full_in_progress = True
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерирую полный пост...")
            post_data['text_ru'] = f"Тестовый новый пост {post_data['post_id'] + 1}"
            post_data['image_url'] = random.choice([img for img in test_images if img != post_data['image_url']])
            post_data['post_id'] += 1
            await send_post_for_approval()
        finally:
            full_in_progress = False

    elif action == 'chat':
        chat_in_progress = True
        try:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"💬 Чат: {post_data['text_ru']}"
            )
        finally:
            chat_in_progress = False

    elif action == 'do_not_disturb':
        do_not_disturb['active'] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='🌙 Не беспокоить включен.')

    elif action == 'end_day':
        pending_post['active'] = False
        do_not_disturb['active'] = True
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='🔚 Рабочий день завершён.')

    elif action == 'restore_previous':
        post_data.update(prev_data)
        await send_post_for_approval()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='↩️ Предыдущий пост восстановлен.')

# ——————————————————————————————————————————————————————————————————————————————
async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval()
    asyncio.create_task(check_timer())
    logging.info("✅ Бот запущен и готов к работе.")

def main():
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
