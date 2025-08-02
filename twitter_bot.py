import os
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
from datetime import datetime, timedelta, time as dt_time

import tweepy
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
from PIL import Image  # Нужно для конвертации изображения перед публикацией в Twitter

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# --- Переменные окружения ---
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

TELEGRAM_PHOTO_LIMIT = 10 * 1024 * 1024  # 10 MB
TELEGRAM_CAPTION_LIMIT = 1024

TELEGRAM_LINKS = "Веб сайт: https://getaicoin.com/ | Twitter: https://x.com/AiCoin_ETH"

if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    logging.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    logging.error("Не заданы обязательные переменные окружения для Twitter!")
    sys.exit(1)

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

approval_lock = asyncio.Lock()
DB_FILE = "post_history.db"
MAX_HISTORY_POSTS = 15
manual_posts_today = 0
TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND = 900

test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

WELCOME_POST_RU = (
    "🚀 Привет! Это бот публикаций.\n\n"
    "ИИ-генерация, новости, идеи, генерация картинок и многое другое."
)
WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"

post_data = {
    "text_ru": WELCOME_POST_RU,
    "text_en": WELCOME_POST_RU,
    "image_url": test_images[0],
    "timestamp": None,
    "post_id": 0,
    "is_manual": False
}
prev_data = post_data.copy()
user_self_post = {}

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT}
do_not_disturb = {"active": False}
last_action_time = {}

# --- Главное меню ---
def main_keyboard(timer: int = None):
    think_label = "🕒 Подумать" if timer is None else f"🕒 Думаем... {timer} сек"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton(think_label, callback_data="think")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat"), InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"), InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
    ])

def post_choice_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
    ])

def post_end_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post_manual")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")]
    ])

# --- Twitter ---
def get_twitter_clients():
    api_v1 = tweepy.API(
        tweepy.OAuth1UserHandler(
            TWITTER_API_KEY,
            TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN,
            TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return api_v1

twitter_api_v1 = get_twitter_clients()

def build_twitter_post(text_ru: str) -> str:
    signature = (
        "\nПодробнее в Telegram: t.me/AiCoin_ETH или на сайте: https://getaicoin.com/ "
        "#AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    )
    max_length = 280
    reserve = max_length - len(signature)
    if len(text_ru) > reserve:
        main_part = text_ru[:reserve - 3].rstrip() + "..."
    else:
        main_part = text_ru
    return main_part + signature

def build_telegram_post(text: str) -> str:
    links = "\n\n" + TELEGRAM_LINKS
    reserve = TELEGRAM_CAPTION_LIMIT - len(links)
    if len(text) > reserve:
        text = text[:reserve - 3].rstrip() + "..."
    return text + links

def hash_text(text: str):
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()

def hash_image(img_path: str):
    with open(img_path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

async def is_duplicate_post(text, image_url, db_file=DB_FILE):
    text_hash = hash_text(text)
    img_hash = None
    try:
        if image_url and str(image_url).startswith("http"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            tmp.write(r.content)
            tmp.close()
            img_hash = hash_image(tmp.name)
            os.remove(tmp.name)
        elif image_url:
            img_hash = image_url
    except Exception:
        img_hash = None

    async with aiosqlite.connect(db_file) as db:
        async with db.execute("SELECT text_hash, image_hash FROM posts ORDER BY id DESC LIMIT ?", (MAX_HISTORY_POSTS,)) as cursor:
            async for row in cursor:
                if text_hash == row[0]:
                    return True
                if img_hash and img_hash == row[1]:
                    return True
    return False

async def save_post_to_db(text, image_url, db_file=DB_FILE):
    text_hash = hash_text(text)
    img_hash = None
    try:
        if image_url and str(image_url).startswith("http"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            tmp.write(r.content)
            tmp.close()
            img_hash = hash_image(tmp.name)
            os.remove(tmp.name)
        elif image_url:
            img_hash = image_url
    except Exception:
        img_hash = None

    async with aiosqlite.connect(db_file) as db:
        await db.execute("INSERT INTO posts (text, timestamp, text_hash, image_hash) VALUES (?, ?, ?, ?)", (
            text, datetime.now().isoformat(), text_hash, img_hash
        ))
        await db.commit()
        await db.execute(f"DELETE FROM posts WHERE id NOT IN (SELECT id FROM posts ORDER BY id DESC LIMIT {MAX_HISTORY_POSTS})")
        await db.commit()

# --- Скачивание картинки ---
def download_image(url_or_file_id, is_telegram_file=False, bot=None):
    if is_telegram_file:
        loop = asyncio.get_event_loop()
        file = loop.run_until_complete(bot.get_file(url_or_file_id))
        file_url = file.file_path if file.file_path.startswith("http") else f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
        r = requests.get(file_url)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")  # changed to .png for RGBA support
        tmp.write(r.content)
        tmp.close()
        if os.path.getsize(tmp.name) > TELEGRAM_PHOTO_LIMIT:
            raise ValueError("❗️Файл слишком большой для Telegram (>10MB)!")
        return tmp.name
    else:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")  # changed to .png
        tmp.write(r.content)
        tmp.close()
        if os.path.getsize(tmp.name) > TELEGRAM_PHOTO_LIMIT:
            raise ValueError("❗️Файл слишком большой для Telegram (>10MB)!")
        return tmp.name

async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None):
    file_path = None
    try:
        is_telegram = not (str(url_or_file_id).startswith("http"))
        file_path = download_image(url_or_file_id, is_telegram, bot if is_telegram else None)

        # Конвертируем PNG с альфа-каналом в JPEG для Telegram
        with Image.open(file_path) as img:
            if img.mode == "RGBA":
                img = img.convert("RGB")
                converted_path = file_path + ".jpg"
                img.save(converted_path, "JPEG")
                os.remove(file_path)
                file_path = converted_path

        msg = await bot.send_photo(chat_id=chat_id, photo=open(file_path, "rb"), caption=caption)
        return msg
    except ValueError as ve:
        await bot.send_message(chat_id=chat_id, text=str(ve), disable_web_page_preview=True)
        logging.error(str(ve))
        if caption:
            await bot.send_message(chat_id=chat_id, text=caption, disable_web_page_preview=True)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❗️Ошибка при отправке фото: {e}", disable_web_page_preview=True)
        logging.error(str(e))
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# --- Публикация в Twitter с медиа ---
def twitter_post(text: str, image_path=None):
    try:
        if image_path:
            # Убедимся, что картинка в JPEG и корректного формата
            with Image.open(image_path) as img:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(image_path, format="JPEG")

            media = twitter_api_v1.media_upload(image_path)
            post_result = twitter_api_v1.update_status(status=text, media_ids=[media.media_id])
        else:
            post_result = twitter_api_v1.update_status(status=text)

        logging.info(f"Tweet успешно опубликован: {post_result.id}")
        return True
    except tweepy.TweepyException as e:
        logging.error(f"Ошибка публикации в Twitter: {e}")
        return False

# --- Обработчики Telegram ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Обработка callback_data из кнопок
    data = query.data
    logging.info(f"Получен CallbackQuery: {data}")

    if data == "approve":
        # Публикация поста
        text = post_data["text_ru"]
        image_url = post_data.get("image_url")
        is_duplicate = await is_duplicate_post(text, image_url)
        if is_duplicate:
            await query.edit_message_text("⚠️ Такой пост уже был опубликован недавно.")
            return

        # Скачивание картинки (если есть)
        img_path = None
        if image_url:
            try:
                img_path = download_image(image_url)
            except Exception as e:
                await query.edit_message_text(f"Ошибка загрузки изображения: {e}")
                return

        tweet_text = build_twitter_post(text)

        # Публикация в Twitter
        if twitter_post(tweet_text, img_path):
            await query.edit_message_text("✅ Пост успешно опубликован в Twitter!")
            # Сохраняем в базу
            await save_post_to_db(text, image_url)
        else:
            await query.edit_message_text("❌ Ошибка публикации в Twitter!")

        # Публикация в Telegram канал (без картинки)
        telegram_text = build_telegram_post(text)
        await channel_bot.send_message(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, text=telegram_text)

        if img_path and os.path.exists(img_path):
            os.remove(img_path)

    elif data == "self_post":
        # Логика для ручного создания поста
        await query.edit_message_text("✍️ Введите ваш текст для поста...")

    elif data == "shutdown_bot":
        await query.edit_message_text("🔴 Завершение работы бота.")
        sys.exit(0)

    else:
        await query.edit_message_text(f"Нажата кнопка: {data}")

async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("⚠️ Текст не может быть пустым.")
        return
    post_data["text_ru"] = text
    post_data["image_url"] = None
    post_data["is_manual"] = True
    await update.message.reply_text("Ваш текст принят. Выберите действие.", reply_markup=main_keyboard())

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, timestamp TEXT, text_hash TEXT, image_hash TEXT)"
        )
        await db.commit()

async def main():
    await init_db()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self_post_message_handler))

    logging.info("Старт Telegram бота модерации и публикации…")
    await application.run_polling()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
    finally:
        if not loop.is_closed():
            loop.close()