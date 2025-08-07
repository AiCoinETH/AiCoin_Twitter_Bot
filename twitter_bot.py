# -*- coding: utf-8 -*-
import os
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, time as dt_time

import tweepy
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
from github import Github

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(funcName)s %(message)s'
)

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID_STR = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = "images_for_posts"

if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID_STR, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    logging.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    logging.error("Не заданы обязательные переменные окружения для Twitter!")
    sys.exit(1)
if not all([GITHUB_TOKEN, GITHUB_REPO]):
    logging.error("Не заданы обязательные переменные окружения GitHub!")
    sys.exit(1)

# -----------------------------------------------------------------------------
# ГЛОБАЛЫЕ ОБЪЕКТЫ
# -----------------------------------------------------------------------------
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)
approval_lock = asyncio.Lock()

DB_FILE = "post_history.db"

# расписание/таймеры
scheduled_posts_per_day = 6
manual_posts_today = 0
TIMER_PUBLISH_DEFAULT = 900   # 15 минут — авто режим
TIMER_PUBLISH_EXTEND  = 900   # продление при действиях
AUTO_SHUTDOWN_AFTER_SECONDS = 600  # 10 минут после последней кнопки

# предпросмотр ссылки в Telegram — отключаем
DISABLE_WEB_PREVIEW = True

WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"
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
pending_post = {
    "active": False,
    "timer": None,
    "timeout": TIMER_PUBLISH_DEFAULT
}
do_not_disturb = {"active": False}
last_action_time = {}
last_button_pressed_at = None  # для авто-выключения через 10 минут

# -----------------------------------------------------------------------------
# КЛАВИАТУРЫ
# -----------------------------------------------------------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🕒 Подумать", callback_data="think")],
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="edit_post")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat"),
         InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("↩️ Вернуть предыдущий пост", callback_data="restore_previous"),
         InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
    ])

def twitter_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def telegram_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def post_choice_keyboard():
    # оставляем универсальную — если захочешь одним нажатием отправлять в оба
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def post_end_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новый пост", callback_data="new_post_manual")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🌙 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔚 Завершить", callback_data="end_day")],
        [InlineKeyboardButton("💬 Поговорить", callback_data="chat")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

# -----------------------------------------------------------------------------
# TWITTER/ GITHUB КЛИЕНТЫ
# -----------------------------------------------------------------------------
def get_twitter_clients():
    client_v2 = tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET
    )
    api_v1 = tweepy.API(
        tweepy.OAuth1UserHandler(
            TWITTER_API_KEY,
            TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN,
            TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return client_v2, api_v1

twitter_client_v2, twitter_api_v1 = get_twitter_clients()
github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

# -----------------------------------------------------------------------------
# ПОСТОСТРОИТЕЛИ (с учётом твоих требований)
# -----------------------------------------------------------------------------
def build_twitter_post(text_ru: str) -> str:
    """
    Обрезаем на этапе предпросмотра:
    - Общий лимит: 280
    - Подпись: сайт + Join Telegram + хэштеги/тикер
    """
    signature = "Learn more: https://getaicoin.com/ | Join Telegram: https://t.me/AiCoin_ETH #AiCoin #Ai $Ai #crypto #blockchain #AI #DeFi"
    max_len = 280
    # +1 перенос строки между текстом и подписью
    reserved = len(signature) + 1
    if reserved >= max_len:
        # крайний кейс: если подпись вдруг длиннее лимита — режем подпись
        short_sig = signature[:max_len - 1]
        return short_sig

    room = max_len - reserved
    txt = (text_ru or "").strip()
    if len(txt) > room:
        txt = txt[:room - 3].rstrip() + "..."
    return f"{txt}\n{signature}"

def build_telegram_post(text_ru: str) -> str:
    """
    Телеграм: HTML ссылки, обрезка 750, предпросмотр ссылок выключаем в отправке.
    """
    max_len = 750
    txt = (text_ru or "").strip()
    if len(txt) > max_len:
        txt = txt[:max_len - 3].rstrip() + "..."
    signature = '\n\n<a href="https://getaicoin.com/">Website</a> | ' \
                '<a href="https://x.com/AiCoin_ETH">Twitter</a> | ' \
                '<a href="https://t.me/AiCoin_ETH">Telegram</a>'
    return txt + signature

# -----------------------------------------------------------------------------
# GITHUB HELPERS
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path, filename):
    logging.info(f"upload_image_to_github: image_path={image_path}, filename={filename}")
    with open(image_path, "rb") as img_file:
        content = img_file.read()
    try:
        github_repo.create_file(f"{GITHUB_IMAGE_PATH}/{filename}", "upload image for post", content, branch="main")
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        logging.info(f"upload_image_to_github: Загружено на GitHub: {url}")
        return url
    except Exception as e:
        logging.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None

def delete_image_from_github(filename):
    try:
        file_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        contents = github_repo.get_contents(file_path, ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
        logging.info(f"delete_image_from_github: Удалён файл с GitHub: {filename}")
    except Exception as e:
        logging.error(f"Ошибка удаления файла с GitHub: {e}")

# -----------------------------------------------------------------------------
# СКАЧИВАНИЕ ИЗОБРАЖЕНИЙ
# -----------------------------------------------------------------------------
async def download_image_async(url_or_file_id, is_telegram_file=False, bot=None, retries=3):
    if is_telegram_file:
        for attempt in range(retries):
            try:
                file = await bot.get_file(url_or_file_id)
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp_file.name)
                return tmp_file.name
            except Exception as e:
                logging.warning(f"download_image_async TG attempt {attempt+1} failed: {e}")
                await asyncio.sleep(1)
        raise Exception("Не удалось скачать файл из Telegram после нескольких попыток")
    else:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url_or_file_id, headers=headers, timeout=15)
        r.raise_for_status()
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp_file.write(r.content)
        tmp_file.close()
        return tmp_file.name

async def save_image_and_get_github_url(image_path):
    filename = f"{uuid.uuid4().hex}.jpg"
    url = upload_image_to_github(image_path, filename)
    return url, filename

async def process_telegram_photo(file_id: str, bot: Bot) -> str:
    file_path = await download_image_async(file_id, is_telegram_file=True, bot=bot)
    url, filename = await save_image_and_get_github_url(file_path)
    try:
        os.remove(file_path)
    except Exception:
        pass
    if not url:
        raise Exception("Не удалось загрузить фото на GitHub")
    return url

# -----------------------------------------------------------------------------
# БЕЗОПАСНАЯ ОТПРАВКА ПРЕДПРОСМОТРА (без web preview)
# -----------------------------------------------------------------------------
async def safe_preview_post(bot, chat_id, text, image_url=None, reply_markup=None):
    try:
        if image_url:
            try:
                await send_photo_with_download(bot, chat_id, image_url, caption=text, reply_markup=reply_markup)
            except Exception as e:
                logging.warning(f"safe_preview_post: image send failed, fallback to text: {e}")
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW, parse_mode="HTML")
    except Exception as e:
        await bot.send_message(
            chat_id=chat_id,
            text="Ошибка предпросмотра. Вот текст поста:\n\n" + text,
            reply_markup=reply_markup,
            disable_web_page_preview=DISABLE_WEB_PREVIEW,
            parse_mode="HTML"
        )

async def preview_dual_combined(bot, chat_id, text, image_url=None, reply_markup=None):
    """
    СТАРЫЙ комбинированный предпросмотр — оставлен на всякий случай.
    """
    preview = (
        f"<b>Telegram:</b>\n{build_telegram_post(text)}\n\n"
        f"<b>Twitter:</b>\n{build_twitter_post(text)}"
    )
    await safe_preview_post(bot, chat_id, preview, image_url=image_url, reply_markup=reply_markup)

async def preview_split(bot, chat_id, text, image_url=None):
    """
    НОВЫЙ разделённый предпросмотр: СНАЧАЛА Twitter, ПОТОМ Telegram.
    У каждого сообщения — своя клавиатура.
    """
    twitter_txt = build_twitter_post(text)
    telegram_txt = build_telegram_post(text)

    # Twitter карточка
    await safe_preview_post(
        bot, chat_id,
        f"<b>Предпросмотр для Twitter (280 символов, с подписью):</b>\n\n{twitter_txt}",
        image_url=image_url,
        reply_markup=twitter_preview_keyboard()
    )
    # Telegram карточка
    await safe_preview_post(
        bot, chat_id,
        f"<b>Предпросмотр для Telegram (750 символов, HTML ссылки):</b>\n\n{telegram_txt}",
        image_url=image_url,
        reply_markup=telegram_preview_keyboard()
    )

# -----------------------------------------------------------------------------
# ОТПРАВКА ФОТО С ПОДКАЧКОЙ (и fallback)
# -----------------------------------------------------------------------------
async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    github_filename = None

    def is_valid_image_url(url):
        try:
            resp = requests.head(url, timeout=5)
            return resp.headers.get('Content-Type', '').startswith('image/')
        except Exception:
            return False

    try:
        if isinstance(url_or_file_id, str) and url_or_file_id.startswith("images_for_posts/") and os.path.exists(url_or_file_id):
            with open(url_or_file_id, "rb") as img:
                msg = await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
            return msg, None
        elif not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            github_filename = url.split('/')[-1]
            msg = await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
            return msg, github_filename
        else:
            if not is_valid_image_url(url_or_file_id):
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
                return None, None
            try:
                response = requests.get(url_or_file_id, timeout=10)
                response.raise_for_status()
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp_file.write(response.content)
                tmp_file.close()
                with open(tmp_file.name, "rb") as img:
                    msg = await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
                os.remove(tmp_file.name)
                return msg, None
            except Exception:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
                return None, None
    except Exception as e:
        logging.error(f"Ошибка в send_photo_with_download: {e}")
        await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
        return None, None

# -----------------------------------------------------------------------------
# БАЗА ДАННЫХ: init + защита от дубликатов
# -----------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # posts: уникальность по text_hash + image_hash (оба могут быть NULL)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                text_hash TEXT,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
        """)
        # покрываем уникальным индексом комбинацию
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_unique
            ON posts (COALESCE(text_hash, ''), COALESCE(image_hash, ''));
        """)
        await db.commit()

def normalize_text_for_hashing(text: str) -> str:
    if not text:
        return ""
    # обрезаем пробелы, приводим к нижнему регистру
    return " ".join(text.strip().lower().split())

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

async def compute_image_hash_from_url(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return sha256_hex(r.content)
    except Exception as e:
        logging.warning(f"compute_image_hash_from_url failed: {e}")
        return None

async def is_duplicate_post(text: str, image_url: str | None) -> bool:
    """
    Проверяем, есть ли такой же пост (по хешу текста + хешу картинки)
    """
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None
    image_hash = None
    if image_url:
        try:
            r = requests.get(image_url, timeout=10)
            r.raise_for_status()
            image_hash = sha256_hex(r.content)
        except Exception:
            image_hash = None

    async with aiosqlite.connect(DB_FILE) as db:
        q = "SELECT 1 FROM posts WHERE COALESCE(text_hash,'') = COALESCE(?, '') AND COALESCE(image_hash,'') = COALESCE(?, '') LIMIT 1"
        async with db.execute(q, (text_hash, image_hash)) as cur:
            row = await cur.fetchone()
            return row is not None

async def save_post_to_history(text, image_url=None):
    """
    Сохраняем пост только если его ещё не было (уник по text_hash+image_hash).
    """
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None

    image_hash = None
    if image_url:
        try:
            r = requests.get(image_url, timeout=10)
            r.raise_for_status()
            image_hash = sha256_hex(r.content)
        except Exception:
            image_hash = None

    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT INTO posts (text, text_hash, timestamp, image_hash) VALUES (?, ?, ?, ?)",
                (text, text_hash, datetime.now().isoformat(), image_hash)
            )
            await db.commit()
        except Exception as e:
            logging.warning(f"save_post_to_history: возможно дубликат или ошибка вставки: {e}")
# -----------------------------------------------------------------------------
# ТАЙМЕРЫ / РАСПИСАНИЕ / АВТО-ВЫКЛЮЧЕНИЕ
# -----------------------------------------------------------------------------
def reset_timer(timeout=None):
    """
    Сбрасывает таймер автопубликации (когда пост отправлен на согласование).
    """
    pending_post["timer"] = datetime.now()
    if timeout:
        pending_post["timeout"] = timeout

async def check_timer():
    """
    Проверяем таймер автопубликации. Если время истекло — делаем автопост
    и выключаемся (в авторежиме).
    """
    while True:
        await asyncio.sleep(0.5)
        if pending_post["active"] and pending_post.get("timer"):
            passed = (datetime.now() - pending_post["timer"]).total_seconds()
            if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                try:
                    base_text = post_data["text_ru"].strip()

                    telegram_text = build_telegram_post(base_text)
                    twitter_text  = build_twitter_post(base_text)

                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⌛ Время ожидания истекло. Публикую автоматически."
                    )

                    # Telegram
                    tg_ok = await publish_post_to_telegram(
                        channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"]
                    )
                    # Twitter
                    tw_ok = publish_post_to_twitter(twitter_text, post_data["image_url"])

                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text=f"Статус автопубликации — Telegram: {'✅' if tg_ok else '❌'}, Twitter: {'✅' if tw_ok else '❌'}"
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard()
                    )

                    # Авто-режим: сразу вырубаемся после публикации
                    shutdown_bot_and_exit()
                except Exception as e:
                    pending_post["active"] = False
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text=f"❌ Ошибка при автопубликации: {e}"
                    )
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="Выберите действие:",
                        reply_markup=post_end_keyboard()
                    )
                finally:
                    pending_post["active"] = False

async def check_inactivity_shutdown():
    """
    Фоновая задача: если 10 минут нет нажатий кнопок — выключаемся.
    """
    global last_button_pressed_at
    while True:
        await asyncio.sleep(5)
        if last_button_pressed_at is None:
            continue
        idle = (datetime.now() - last_button_pressed_at).total_seconds()
        if idle >= AUTO_SHUTDOWN_AFTER_SECONDS:
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="🔴 Нет активности 10 минут. Отключаюсь."
                )
            except Exception:
                pass
            shutdown_bot_and_exit()
            return

def generate_random_schedule(posts_per_day=6, day_start_hour=6, day_end_hour=23, min_offset=-20, max_offset=20):
    if day_end_hour > 23:
        day_end_hour = 23
    now = datetime.now()
    today = now.date()
    start = datetime.combine(today, dt_time(hour=day_start_hour, minute=0, second=0))
    if now > start:
        start = now + timedelta(seconds=1)
    end = datetime.combine(today, dt_time(hour=day_end_hour, minute=0, second=0))
    total_seconds = int((end - start).total_seconds())
    if posts_per_day < 1:
        return []
    base_step = total_seconds // posts_per_day
    schedule = []
    for i in range(posts_per_day):
        base_sec = i * base_step
        offset_sec = random.randint(min_offset * 60, max_offset * 60) + random.randint(-59, 59)
        post_time = start + timedelta(seconds=base_sec + offset_sec)
        if post_time < start:
            post_time = start
        if post_time > end:
            post_time = end
        schedule.append(post_time)
    schedule.sort()
    return schedule

async def schedule_daily_posts():
    """
    Автогенерация расписания на день: отправляем на согласование (предпросмотр),
    ждём решения, повторяем пока не заполним дневную норму.
    """
    global manual_posts_today
    while True:
        manual_posts_today = 0
        now = datetime.now()
        if now.hour < 6:
            to_sleep = (datetime.combine(now.date(), dt_time(hour=6)) - now).total_seconds()
            await asyncio.sleep(to_sleep)

        posts_left = lambda: scheduled_posts_per_day - manual_posts_today

        while posts_left() > 0:
            schedule = generate_random_schedule(posts_per_day=posts_left())
            for post_time in schedule:
                if posts_left() <= 0:
                    break
                now = datetime.now()
                delay = (post_time - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)

                # готовим шаблон поста
                post_data["text_ru"] = f"Новый пост ({post_time.strftime('%H:%M:%S')})"
                post_data["image_url"] = random.choice(test_images)
                post_data["post_id"] += 1
                post_data["is_manual"] = False

                await send_post_for_approval()   # покажем split-предпросмотр
                # пока активен pending_post — ждём
                while pending_post["active"]:
                    await asyncio.sleep(1)

        # до завтра
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=0))
        to_next_day = (tomorrow - datetime.now()).total_seconds()
        await asyncio.sleep(to_next_day)
        manual_posts_today = 0

# -----------------------------------------------------------------------------
# ПРЕДПРОСМОТР НА СОГЛАСОВАНИЕ (раздельный)
# -----------------------------------------------------------------------------
async def send_post_for_approval():
    """
    Выводим раздельный предпросмотр (Twitter / Telegram).
    Сбрасываем таймер автопостинга.
    """
    async with approval_lock:
        if do_not_disturb["active"] or pending_post["active"]:
            return
        post_data["timestamp"] = datetime.now()
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        try:
            # если картинка ещё не URL — грузим в GitHub
            if post_data["image_url"] and not str(post_data["image_url"]).startswith("http"):
                url = await process_telegram_photo(post_data["image_url"], approval_bot)
                post_data["image_url"] = url

            # Раздельный предпросмотр
            await preview_split(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"]
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text="❌ Ошибка предпросмотра. Попробуем ещё раз позже.",
                    reply_markup=main_keyboard()
                )
            except Exception:
                pass

# -----------------------------------------------------------------------------
# SELF-POST / EDIT / ROUTER
# -----------------------------------------------------------------------------
SESSION_KEY = "self_approval"

async def self_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь присылает свой текст/фото. Делаем split-предпросмотр с урезанием
    (Twitter: 280 вместе с подписью, Telegram: 750 + HTML подпись), у каждого —
    свои кнопки.
    """
    global last_button_pressed_at
    last_button_pressed_at = datetime.now()

    key = SESSION_KEY
    state = user_self_post.get(key, {}).get('state')
    if state not in ['wait_post', 'wait_confirm']:
        await approval_bot.send_message(
            chat_id=update.effective_chat.id,
            text="✍️ Чтобы отправить свой пост, сначала нажми кнопку 'Сделай сам'!"
        )
        return

    text = update.message.text or update.message.caption or ""
    image_url = None
    if update.message.photo:
        try:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        except Exception:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось обработать фото. Попробуйте ещё раз.")
            return

    if not text and not image_url:
        await approval_bot.send_message(chat_id=update.effective_chat.id, text="❗️Пришлите хотя бы текст или фотографию для поста.")
        return

    user_self_post[key] = user_self_post.get(key, {})
    user_self_post[key]['text'] = text
    user_self_post[key]['image'] = image_url
    user_self_post[key]['state'] = 'wait_confirm'

    try:
        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            text,
            image_url=image_url
        )
        # Под основным split-просмотром даём узкую панель
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Выберите действие:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Завершить генерацию поста", callback_data="finish_self_post")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
                 InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
                [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
            ])
        )
    except Exception as e:
        logging.warning(f"self_post_message_handler preview split failed: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось показать предпросмотр поста. Попробуйте снова.")

async def edit_post_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at
    last_button_pressed_at = datetime.now()

    key = SESSION_KEY
    # REPLY на бота = редактирование текущего предпросмотра
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        text = update.message.text or update.message.caption or None
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        if text:
            post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        try:
            await preview_split(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"]
            )
        except Exception:
            pass
        return

    if key in user_self_post and user_self_post[key]['state'] == 'wait_edit':
        text = update.message.text or update.message.caption or None
        image_url = None
        if update.message.photo:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        if text:
            post_data["text_ru"] = text
        if image_url:
            post_data["image_url"] = image_url
        user_self_post.pop(key, None)
        try:
            await preview_split(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"]
            )
        except Exception:
            pass

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный роутер текст/фото. Если мы в режиме ввода — идём в self_post_message_handler.
    Если это ответ на предпросмотр — в edit_post_message_handler.
    """
    global last_button_pressed_at
    last_button_pressed_at = datetime.now()

    key = SESSION_KEY
    if update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        await edit_post_message_handler(update, context)
        return

    if not user_self_post.get(key):
        user_self_post[key] = {'text': '', 'image': None, 'state': 'wait_post'}

    state = user_self_post[key]['state']
    if state == 'wait_edit':
        await edit_post_message_handler(update, context)
        return
    if state in ['wait_post', 'wait_confirm']:
        await self_post_message_handler(update, context)
        return

    await approval_bot.send_message(
        chat_id=update.effective_chat.id,
        text="✍️ Чтобы отправить свой пост, сначала нажми кнопку 'Сделай сам'!"
    )

# -----------------------------------------------------------------------------
# ОБРАБОТЧИК КНОПОК
# -----------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_action_time, prev_data, manual_posts_today, last_button_pressed_at
    last_button_pressed_at = datetime.now()

    key = SESSION_KEY
    try:
        await update.callback_query.answer()
    except Exception as e:
        logging.warning(f"Не удалось ответить на callback_query: {e}")

    # Любая кнопка — продлеваем таймер автопубликации, если он был запущен
    if pending_post["active"]:
        reset_timer(TIMER_PUBLISH_EXTEND)
    else:
        pending_post["timeout"] = TIMER_PUBLISH_EXTEND

    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 3:
        logging.info(f"User {user_id} слишком часто нажимает кнопки")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⏳ Подождите немного...", reply_markup=main_keyboard())
        return
    last_action_time[user_id] = now

    action = update.callback_query.data
    prev_data.update(post_data)
    logging.info(f"[button_handler] action={action} user_id={user_id}")

    if action == "edit_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[key] = {'state': 'wait_edit'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✏️ Пришли новый текст и/или фото для редактирования поста (в одном сообщении), либо просто отправь новое сообщение в reply на текущий предпросмотр.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")],
                [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
            ])
        )
        return

    if action == "finish_self_post":
        info = user_self_post.get(key)
        if not (info and info["state"] == "wait_confirm"):
            logging.warning(f"[button_handler] Некорректный вызов finish_self_post")
            return

        text = info.get("text", "")
        image_url = info.get("image", None)

        post_data["text_ru"] = text
        post_data["image_url"] = image_url or random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        user_self_post.pop(key, None)

        try:
            await update.callback_query.message.delete()
        except Exception:
            pass

        logging.info(f"[button_handler] finish_self_post: предпросмотр: text='{post_data['text_ru'][:60]}...', image_url={post_data['image_url']}")

        try:
            await preview_split(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"]
            )
        except Exception as e:
            logging.error(f"[button_handler] Ошибка предпросмотра после finish_self_post: {e}")

        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action == "shutdown_bot":
        logging.info("Останавливаю бота по кнопке!")
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!")
        except Exception:
            pass
        await asyncio.sleep(2)
        shutdown_bot_and_exit()
        return

    if action == "approve":
        # Сразу раздельный предпросмотр текущего поста
        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"],
            image_url=post_data["image_url"]
        )
        logging.info("approve: split-предпросмотр успешно отправлен")
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action in ["post_twitter", "post_telegram", "post_both"]:
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })

        base_text = post_data["text_ru"].strip()

        telegram_text = build_telegram_post(base_text)
        twitter_text  = build_twitter_post(base_text)

        telegram_success = False
        twitter_success  = False

        if action in ["post_telegram", "post_both"]:
            try:
                telegram_success = await publish_post_to_telegram(
                    channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, telegram_text, post_data["image_url"]
                )
            except Exception as e:
                logging.error(f"Ошибка при публикации в Telegram: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Telegram: {e}")

        if action in ["post_twitter", "post_both"]:
            try:
                twitter_success = publish_post_to_twitter(twitter_text, post_data["image_url"])
            except Exception as e:
                logging.error(f"Ошибка при публикации в Twitter: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Не удалось отправить в Twitter: {e}")

        pending_post["active"] = False

        # Статусы по системам
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✅ Успешно отправлено в Telegram!" if telegram_success else "❌ Не удалось отправить в Telegram."
        )
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✅ Успешно отправлено в Twitter!" if twitter_success else "❌ Не удалось отправить в Twitter."
        )

        # Стартовое меню
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Выберите действие:",
            reply_markup=post_end_keyboard()
        )

        # Если это автопост — выключаемся сразу
        if not post_data.get("is_manual"):
            shutdown_bot_and_exit()
        return

    if action == "self_post":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post[key] = {'text': '', 'image': None, 'state': 'wait_post'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Напиши свой текст поста и (опционально) приложи фото — всё одним сообщением. После этого появится раздельный предпросмотр.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
            ])
        )
        return

    if action == "cancel_to_main":
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        user_self_post.pop(key, None)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=main_keyboard()
        )
        return

    if action == "restore_previous":
        post_data.update(prev_data)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="↩️ Восстановлен предыдущий вариант.",
            reply_markup=main_keyboard()
        )
        if pending_post["active"]:
            await send_post_for_approval()
        return

    if action == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        kb = main_keyboard()
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M')}",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    if action == "think":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🧐 Думаем дальше…",
            reply_markup=main_keyboard()
        )
        return

    if action == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="💬 Начинаем чат:\n" + post_data["text_ru"],
            reply_markup=post_end_keyboard()
        )
        return

    if action == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=post_end_keyboard()
        )
        return

    if action == "new_post":
        pending_post["active"] = False
        post_data["text_ru"] = f"Тестовый новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False

        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            image_url=post_data["image_url"],
        )
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return

    if action == "new_post_manual":
        pending_post["active"] = False
        post_data["text_ru"] = f"Ручной новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True

        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            image_url=post_data["image_url"],
        )
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        return
# -----------------------------------------------------------------------------
# НАСТРОЙКИ АВТО-ВЫКЛЮЧЕНИЯ (10 минут без нажатий = off)
# -----------------------------------------------------------------------------
AUTO_SHUTDOWN_AFTER_SECONDS = 10 * 60
last_button_pressed_at: datetime | None = None


# -----------------------------------------------------------------------------
# ANTI-DUPES / DB
# -----------------------------------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Храним сам текст, timestamp, image_hash, и уникальность по (text,image_hash)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                image_hash TEXT,
                UNIQUE(text, image_hash)
            )
        """)
        await db.commit()

def _hash_image_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

async def compute_image_hash(image_url_or_fileid: str | None) -> str | None:
    """
    Универсально считаем хэш картинки:
    - если file_id (telegram), скачиваем и считаем
    - если http/https, тянем контент и считаем
    """
    if not image_url_or_fileid:
        return None

    is_telegram = not str(image_url_or_fileid).startswith("http")
    try:
        if is_telegram:
            file_path = await download_image_async(image_url_or_fileid, True, approval_bot)
            with open(file_path, "rb") as f:
                h = _hash_image_bytes(f.read())
            try:
                os.remove(file_path)
            except Exception:
                pass
            return h
        else:
            r = requests.get(image_url_or_fileid, timeout=10)
            r.raise_for_status()
            return _hash_image_bytes(r.content)
    except Exception:
        return None

async def is_duplicate_post(text: str, image_url_or_fileid: str | None) -> bool:
    """
    Проверяем, есть ли в БД точный дубль по (text, image_hash).
    """
    img_hash = await compute_image_hash(image_url_or_fileid)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT 1 FROM posts WHERE text=? AND COALESCE(image_hash,'')=COALESCE(?, '') LIMIT 1",
            (text, img_hash)
        ) as cur:
            row = await cur.fetchone()
            return row is not None

async def save_post_to_history(text: str, image_url: str | None = None) -> bool:
    """
    Пишем пост в историю. Возвращаем True, если записали; False — если такой уже есть.
    """
    image_hash = None
    if image_url:
        try:
            if not str(image_url).startswith("http"):
                file_path = await download_image_async(image_url, True, approval_bot)
                with open(file_path, "rb") as f:
                    image_hash = hashlib.sha256(f.read()).hexdigest()
                try:
                    os.remove(file_path)
                except Exception:
                    pass
            else:
                r = requests.get(image_url, timeout=10)
                r.raise_for_status()
                image_hash = hashlib.sha256(r.content).hexdigest()
        except Exception:
            image_hash = None

    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT OR IGNORE INTO posts (text, timestamp, image_hash) VALUES (?, ?, ?)",
                (text, datetime.now().isoformat(), image_hash)
            )
            await db.commit()
        except Exception as e:
            logging.error(f"DB insert error: {e}")

        # проверим, вставилось ли
        async with db.execute(
            "SELECT 1 FROM posts WHERE text=? AND COALESCE(image_hash,'')=COALESCE(?, '') LIMIT 1",
            (text, image_hash)
        ) as cur:
            row = await cur.fetchone()
            return row is not None


# -----------------------------------------------------------------------------
# ПОСТРОЕНИЕ ТЕКСТОВ (обрезка, подписи, отключение превью)
# -----------------------------------------------------------------------------
TWITTER_SIGNATURE = " Learn more: https://getaicoin.com/ | Join Telegram: https://t.me/AiCoin_ETH #AiCoin #AI $Ai #crypto #blockchain #DeFi"
TELEGRAM_SIGNATURE_HTML = '\n\n<a href="https://getaicoin.com/">Website</a> | <a href="https://t.me/AiCoin_ETH">Join Telegram</a>'

def build_twitter_post(user_text_ru: str) -> str:
    """
    Обрезаем так, чтобы ВСЁ вместе с подписью умещалось в 280.
    На предпросмотре уже приходит подрезанный вариант.
    """
    base = (user_text_ru or "").strip()
    max_len = 280
    spare = max_len - len(TWITTER_SIGNATURE)
    if spare < 0:
        # если внезапно подпись длиннее 280 — жёстко тронкаем подпись
        sign = TWITTER_SIGNATURE[:max_len]
        return sign
    if len(base) > spare:
        base = base[:max(0, spare - 1)].rstrip() + "…"
    return base + TWITTER_SIGNATURE

def build_twitter_preview(user_text_ru: str) -> str:
    """
    Именно предпросмотрный вид для Twitter (тоже 280 макс).
    """
    return build_twitter_post(user_text_ru)

def build_telegram_post(user_text_ru: str) -> str:
    """
    Телеграм: ограничиваем тело 750 символами (без подписи),
    добавляем HTML-подпись. Превью сайтов отключаем при отправке.
    """
    base = (user_text_ru or "").strip()
    if len(base) > 750:
        base = base[:749].rstrip() + "…"
    return base + TELEGRAM_SIGNATURE_HTML

def build_telegram_preview(user_text_ru: str) -> str:
    """
    Предпросмотр для Телеграма (тот же формат, уже урезан).
    """
    return build_telegram_post(user_text_ru)


# -----------------------------------------------------------------------------
# SPLIT-ПРЕДПРОСМОТР (два отдельных сообщения)
# -----------------------------------------------------------------------------
async def preview_split(bot: Bot, chat_id: int, user_text_ru: str, image_url: str | None = None):
    """
    Два отдельных сообщения:
      1) Twitter: текст <=280 (с подписью), фото (если есть), кнопки для твиттера
      2) Telegram: текст <=750 (+ HTML подпись), фото (если есть), кнопки для телеги
    Под каждым — свой набор кнопок: пост, отмена, выключить, сделай сам.
    """
    # Twitter preview
    tw_text = build_twitter_preview(user_text_ru)
    tw_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    if image_url:
        try:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"<b>Twitter:</b>\n{tw_text}", reply_markup=tw_markup)
        except Exception:
            # fallback в текст (без превью ссылок)
            await bot.send_message(chat_id=chat_id, text=f"<b>Twitter:</b>\n{tw_text}", parse_mode="HTML",
                                   reply_markup=tw_markup, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id=chat_id, text=f"<b>Twitter:</b>\n{tw_text}", parse_mode="HTML",
                               reply_markup=tw_markup, disable_web_page_preview=True)

    # Telegram preview
    tg_text = build_telegram_preview(user_text_ru)
    tg_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    if image_url:
        try:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"<b>Telegram:</b>\n{tg_text}",
                                           reply_markup=tg_markup)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=f"<b>Telegram:</b>\n{tg_text}", parse_mode="HTML",
                                   reply_markup=tg_markup, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id=chat_id, text=f"<b>Telegram:</b>\n{tg_text}", parse_mode="HTML",
                               reply_markup=tg_markup, disable_web_page_preview=True)


# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ В TWITTER (логика из твоей «рабочей» версии)
# -----------------------------------------------------------------------------
def publish_post_to_twitter(text: str, image_url: str | None = None) -> bool:
    """
    1) если есть image_url — скачиваем, грузим в v1 media_upload, собираем media_ids
    2) публикуем твит через client v2 (create_tweet)
    3) если картинка из GitHub raw — удаляем её после отправки
    """
    github_filename = None
    file_path = None

    try:
        media_ids = None

        if image_url:
            if not str(image_url).startswith("http"):
                logging.error("Telegram file_id не поддерживается напрямую для Twitter публикации.")
                return False

            try:
                response = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
                response.raise_for_status()
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp.write(response.content)
                tmp.close()
                file_path = tmp.name
            except Exception as e:
                logging.error(f"Ошибка при скачивании картинки: {e}")
                return False

            # upload via v1
            try:
                media = twitter_api_v1.media_upload(file_path)
                media_ids = [media.media_id_string]
            except Exception as e:
                logging.error(f"Ошибка загрузки медиа в Twitter: {e}")
                return False
            finally:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        # tweet via v2
        try:
            twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        except Exception as e:
            logging.error(f"Ошибка публикации твита: {e}")
            return False

        # cleanup github raw
        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            try:
                delete_image_from_github(github_filename)
            except Exception as e:
                logging.warning(f"Ошибка удаления картинки с GitHub: {e}")

        return True

    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter (общая): {e}")
        try:
            asyncio.create_task(approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=f"❌ Ошибка при публикации в Twitter: {e}"
            ))
        except Exception:
            pass
        # safety
        if file_path:
            try:
                os.remove(file_path)
            except Exception:
                pass
        if github_filename:
            try:
                delete_image_from_github(github_filename)
            except Exception:
                pass
        return False


# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ В TELEGRAM
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(bot: Bot, chat_id: str | int, text: str, image_url: str | None):
    """
    Публикуем в канал. Если у нас URL-картинка с GitHub — после публикации удаляем.
    """
    github_filename = None
    try:
        msg, github_filename = await send_photo_with_download(bot, chat_id, image_url, caption=text)
        if github_filename:
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        logging.error(f"Ошибка при публикации в Telegram: {e}")
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Telegram: {e}")
        except Exception:
            pass
        if github_filename:
            delete_image_from_github(github_filename)
        return False


# -----------------------------------------------------------------------------
# ЗАПУСК: фоновые задачи и первичный предпросмотр
# -----------------------------------------------------------------------------
async def delayed_start(app: Application):
    await init_db()

    # фоны
    asyncio.create_task(schedule_daily_posts())
    asyncio.create_task(check_timer())
    asyncio.create_task(check_inactivity_shutdown())

    # стартовый split-предпросмотр
    try:
        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data["text_ru"],
            image_url=post_data["image_url"]
        )
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        logging.warning(f"initial preview failed: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
            reply_markup=main_keyboard(),
            disable_web_page_preview=True
        )
    logging.info("Бот успешно запущен и готов принимать сообщения")


def shutdown_bot_and_exit():
    try:
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"
        ))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)
# -----------------------------------------------------------------------------
# Функции активности и авто-выключение
# -----------------------------------------------------------------------------
def touch_activity():
    global last_button_pressed_at
    last_button_pressed_at = datetime.now()

async def check_inactivity_shutdown():
    """
    Фоновая задача: если 10 минут не было нажатий — выключаем бота.
    """
    while True:
        try:
            await asyncio.sleep(5)
            if last_button_pressed_at is None:
                continue
            if (datetime.now() - last_button_pressed_at).total_seconds() >= AUTO_SHUTDOWN_AFTER_SECONDS:
                try:
                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⏳ Не было активности 10 минут. Выключаюсь."
                    )
                except Exception:
                    pass
                shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_inactivity_shutdown error: {e}")


# -----------------------------------------------------------------------------
# Переопределяем отправку на согласование на split-предпросмотр ВСЕГДА
# -----------------------------------------------------------------------------
async def send_post_for_approval():
    """
    Отправляет два отдельных предпросмотра (Twitter и Telegram) + главное меню.
    (Переопределяет старую версию.)
    """
    async with approval_lock:
        if do_not_disturb["active"] or pending_post["active"]:
            return
        post_data["timestamp"] = datetime.now()
        pending_post.update({
            "active": True,
            "timer": datetime.now(),
            "timeout": TIMER_PUBLISH_DEFAULT
        })
        try:
            if post_data.get("image_url") and not str(post_data["image_url"]).startswith("http"):
                url = await process_telegram_photo(post_data["image_url"], approval_bot)
                post_data["image_url"] = url

            # split preview
            await preview_split(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data["text_ru"],
                image_url=post_data["image_url"]
            )
            # меню после двух предпросмотров
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Выберите действие:",
                reply_markup=main_keyboard()
            )
        except Exception as e:
            logging.error(f"Ошибка при отправке на согласование: {e}")
            # хотя бы текстом:
            try:
                await approval_bot.send_message(
                    chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                    text=post_data["text_ru"] + "\n\n" + WELCOME_HASHTAGS,
                    reply_markup=main_keyboard(),
                    disable_web_page_preview=True
                )
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Врапперы для фиксации активности (чтобы не лезть в уже определённые хендлеры)
# -----------------------------------------------------------------------------
async def button_handler_with_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_activity()
    return await button_handler(update, context)

async def message_router_with_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_activity()
    return await message_router(update, context)


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(delayed_start)\
        .build()

    # callback-кнопки и сообщения — через врапперы (чтобы фиксировать активность)
    app.add_handler(CallbackQueryHandler(button_handler_with_activity))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_router_with_activity))

    # запуск
    app.run_polling(poll_interval=0.12, timeout=1)


# -----------------------------------------------------------------------------
# Точка входа
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()