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

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# Чтение переменных окружения
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0"))
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # Для публикаций в канал

if not TELEGRAM_BOT_TOKEN_APPROVAL or not TELEGRAM_APPROVAL_CHAT_ID or not TELEGRAM_CHANNEL_ID:
    logging.error("Переменные окружения для бота или каналов не заданы. Завершение работы.")
    exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)

# Тестовые картинки
test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

post_data = {
    "text_ru": "Майнинговые токены снова в фокусе...",
    "image_url": test_images[0],
    "timestamp": None,
    "post_id": 0
}
prev_data = post_data.copy()

pending_post = {"active": False, "timer": None}
text_in_progress = False
image_in_progress = False
full_in_progress = False
chat_in_progress = False

do_not_disturb = {"active": False}
countdown_task = None
last_action_time = {}
approval_message_ids = {"photo": None, "timer": None}

# Клавиатура для управления постами
keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("✅ Пост", callback_data="approve")],
    [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
    [InlineKeyboardButton("📝 Новый текст", callback_data="regenerate")],
    [InlineKeyboardButton("🖼️ Новая картинка", callback_data="new_image")],
    [InlineKeyboardButton("🆕 Пост целиком", callback_data="new_post")],
    [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
    [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")]
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

async def save_post_to_history(text, image_url=None):
    def get_hash(url):
        try:
            import requests
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            return hashlib.sha256(r.content).hexdigest()
        except Exception as e:
            logging.warning(f"Не удалось получить хеш изображения: {e}")
            return None

    image_hash = get_hash(image_url) if image_url else None
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
            (text, datetime.now().isoformat(), image_hash)
        )
        await db.commit()
    logging.info("Пост сохранён в историю.")

async def send_post_for_approval():
    if do_not_disturb["active"]:
        logging.info("Режим 'Не беспокоить' активен, пропуск отправки на одобрение.")
        return
    if pending_post["active"]:
        logging.info("Есть активный пост, пропуск отправки нового.")
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
        logging.info("Пост отправлен на одобрение.")
    except telegram.error.RetryAfter as e:
        logging.warning(f"Rate limit, ждем {e.retry_after} сек.")
        await asyncio.sleep(e.retry_after)
        await send_post_for_approval()
    except Exception as e:
        logging.error(f"Ошибка отправки поста на одобрение: {e}")

async def send_timer_message():
    countdown_msg = await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="⏳ Таймер: 60 секунд",
        reply_markup=keyboard
    )
    approval_message_ids["timer"] = countdown_msg.message_id

    async def update_countdown(message_id):
        for i in range(59, -1, -1):
            await asyncio.sleep(1)
            try:
                await approval_bot.edit_message_text(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    message_id=message_id,
                    text=f"⏳ Таймер: {i} секунд",
                    reply_markup=keyboard
                )
            except Exception:
                pass
        pending_post["active"] = False

    global countdown_task
    if countdown_task is not None and not countdown_task.done():
        countdown_task.cancel()
    countdown_task = asyncio.create_task(update_countdown(approval_message_ids["timer"]))

async def publish_post():
    if TELEGRAM_CHANNEL_ID:
        try:
            await approval_bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=post_data["image_url"],
                caption=post_data["text_ru"]
            )
            logging.info("Пост опубликован в канал.")
        except telegram.error.RetryAfter as e:
            logging.warning(f"Rate limit при публикации, ждем {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after)
            await approval_bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=post_data["image_url"],
                caption=post_data["text_ru"]
            )
        except Exception as e:
            logging.error(f"Ошибка публикации поста в канал: {e}")
    else:
        logging.error("TELEGRAM_CHANNEL_ID не задан.")
    await save_post_to_history(post_data["text_ru"], post_data["image_url"])
    pending_post["active"] = False

async def check_timer():
    while True:
        await asyncio.sleep(5)
        if pending_post["active"] and pending_post["timer"]:
            if datetime.now() - pending_post["timer"] > timedelta(seconds=60):
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="⌛ Время ожидания истекло. Публикую автоматически."
                )
                await publish_post()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global text_in_progress, image_in_progress, full_in_progress, chat_in_progress, countdown_task, last_action_time
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    now = datetime.now()

    # Антиспам: 15 секунд между действиями одного пользователя
    if user_id in last_action_time:
        if (now - last_action_time[user_id]).total_seconds() < 15:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="⏳ Идёт обработка предыдущего запроса, попробуйте чуть позже."
            )
            return
    last_action_time[user_id] = now

    try:
        action = query.data

        if action != 'approve':
            pending_post["timer"] = datetime.now()

        if text_in_progress or image_in_progress or full_in_progress or chat_in_progress:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="⏳ Бот выполняет задачу, подождите..."
            )
            return

        prev_data.update(post_data)

        if action == 'approve':
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Обработка публикации...")
            await publish_post()

        elif action == 'regenerate':
            text_in_progress = True
            try:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация нового текста (тест)...")
                post_data['text_ru'] = f"Тестовый новый текст {post_data['post_id'] + 1}"
                post_data['post_id'] += 1
                await send_post_for_approval()
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка генерации текста: {e}")
            finally:
                text_in_progress = False

        elif action == 'new_image':
            image_in_progress = True
            try:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация новой картинки (тест)...")
                new_image = random.choice([img for img in test_images if img != post_data['image_url']])
                post_data['image_url'] = new_image
                post_data['post_id'] += 1
                await send_post_for_approval()
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка генерации картинки: {e}")
            finally:
                image_in_progress = False

        elif action == 'new_post':
            full_in_progress = True
            try:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Генерация полного поста и картинки (тест)...")
                post_data['text_ru'] = f"Тестовый новый пост {post_data['post_id'] + 1}"
                new_image = random.choice([img for img in test_images if img != post_data['image_url']])
                post_data['image_url'] = new_image
                post_data['post_id'] += 1
                await send_post_for_approval()
            except Exception as e:
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка генерации поста: {e}")
            finally:
                full_in_progress = False

        elif action == 'think':
            if countdown_task is not None and not countdown_task.done():
                countdown_task.cancel()
            pending_post['timer'] = datetime.now()
            await send_timer_message()

        elif action == 'chat':
            chat_in_progress = True
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text='💬 [Заглушка] Начало чата\n' + post_data['text_ru']
                )
            finally:
                chat_in_progress = False

        elif action == 'do_not_disturb':
            do_not_disturb['active'] = True
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='🌙 Режим "Не беспокоить" включен.')

        elif action == 'end_day':
            pending_post['active'] = False
            do_not_disturb['active'] = True
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='🔚 Сегодняшняя публикация завершена.')

        elif action == 'restore_previous':
            post_data.update(prev_data)
            await send_post_for_approval()
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text='↩️ Восстановлен предыдущий вариант поста.')

    except Exception as e:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка: {e}")
        logging.error(f"Ошибка в button_handler: {e}")

async def delayed_start(app: Application):
    await init_db()
    await send_post_for_approval()
    asyncio.create_task(check_timer())
    logging.info("Бот запущен и готов к работе.")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(delayed_start).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
