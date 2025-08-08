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
TIMER_PUBLISH_DEFAULT = 900   # 15 минут — авто режим (ожидание решения)
TIMER_PUBLISH_EXTEND  = 900   # продление при действиях
AUTO_SHUTDOWN_AFTER_SECONDS = 600  # 10 минут после последней кнопки (ручной режим)

# предпросмотр ссылок в Telegram — отключаем
DISABLE_WEB_PREVIEW = True

# -----------------------------------------------------------------------------
# ЗАГЛУШКА НА СТАРТЕ (≈200 символов) + картинка
# -----------------------------------------------------------------------------
PLACEHOLDER_TEXT = (
    "AiCoin — мост между AI и криптой. Мы превращаем сигналы рынка в понятные решения: "
    "алерты, генерации, аналитика. Подключайся к комьюнити, следи за апдейтами и "
    "будь на шаг впереди. Learn more: https://getaicoin.com/ Join Telegram: https://t.me/AiCoin_ETH"
)
PLACEHOLDER_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg"

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
    "image_url": random.choice(test_images),
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
    # универсальная — если захочешь одним нажатием отправлять в оба
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
# ПОСТОСТРОИТЕЛИ (твои требования)
# -----------------------------------------------------------------------------
TWITTER_SIGNATURE = " Learn more: https://getaicoin.com/ | Join Telegram: https://t.me/AiCoin_ETH #AiCoin #AI $Ai #crypto #blockchain #DeFi"
TELEGRAM_SIGNATURE_HTML = '\n\n<a href="https://getaicoin.com/">Website</a> | <a href="https://t.me/AiCoin_ETH">Join Telegram</a>'

def build_twitter_post(user_text_ru: str) -> str:
    """
    Обрезаем так, чтобы ВСЁ вместе с подписью умещалось в 280.
    (Обрезка происходит на этапе предпросмотра/перед публикацией.)
    """
    base = (user_text_ru or "").strip()
    max_len = 280
    spare = max_len - len(TWITTER_SIGNATURE)
    if spare < 0:
        # если внезапно подпись длиннее 280 — жестко тронкаем подпись
        return TWITTER_SIGNATURE[:max_len]
    if len(base) > spare:
        base = base[:max(0, spare - 1)].rstrip() + "…"
    return base + TWITTER_SIGNATURE

def build_twitter_preview(user_text_ru: str) -> str:
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
    return build_telegram_post(user_text_ru)

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
# БЕЗОПАСНАЯ ОТПРАВКА С ОТКЛЮЧЁННЫМ WEB-PREVIEW
# -----------------------------------------------------------------------------
async def safe_preview_post(bot, chat_id, text, image_url=None, reply_markup=None):
    try:
        if image_url:
            try:
                await send_photo_with_download(bot, chat_id, image_url, caption=text, reply_markup=reply_markup)
            except Exception as e:
                logging.warning(f"safe_preview_post: image send failed, fallback to text: {e}")
                await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                       disable_web_page_preview=DISABLE_WEB_PREVIEW, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup,
                                   disable_web_page_preview=DISABLE_WEB_PREVIEW, parse_mode="HTML")
    except Exception as e:
        await bot.send_message(
            chat_id=chat_id,
            text="Ошибка предпросмотра. Вот текст поста:\n\n" + text,
            reply_markup=reply_markup,
            disable_web_page_preview=DISABLE_WEB_PREVIEW,
            parse_mode="HTML"
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
        # Храним нормализованный хеш текста + хеш изображения,
        # и накрываем их уникальным индексом.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                text_hash TEXT,
                timestamp TEXT NOT NULL,
                image_hash TEXT
            )
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_unique
            ON posts (COALESCE(text_hash, ''), COALESCE(image_hash, ''));
        """)
        await db.commit()

def normalize_text_for_hashing(text: str) -> str:
    if not text:
        return ""
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
    Проверяем дубль по (text_hash, image_hash). Текст нормализуем.
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
    Сохраняем (если ещё не сохранён) по уникальности (text_hash, image_hash).
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
            # Скорее всего дубликат — гасим в лог
            logging.warning(f"save_post_to_history: возможно дубликат или ошибка вставки: {e}")

# -----------------------------------------------------------------------------
# ПРЕДПРОСМОТР: РАЗДЕЛЁННЫЙ (Twitter/Telegram — два сообщения)
# -----------------------------------------------------------------------------
async def preview_split(bot, chat_id, text, image_url=None):
    """
    1) Отдельное сообщение под Twitter (≤280 с подписью)
    2) Отдельное сообщение под Telegram (≤750 + HTML подпись)
    У ссылок web-preview выключен (через send_message fallback'ом).
    """
    twitter_txt = build_twitter_preview(text)
    telegram_txt = build_telegram_preview(text)

    # Twitter карточка
    tw_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"<b>Twitter:</b>\n{twitter_txt}", reply_markup=tw_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"<b>Twitter:</b>\n{twitter_txt}", parse_mode="HTML",
                                   reply_markup=tw_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"<b>Twitter:</b>\n{twitter_txt}", parse_mode="HTML",
                               reply_markup=tw_markup, disable_web_page_preview=True)

    # Telegram карточка
    tg_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"<b>Telegram:</b>\n{telegram_txt}", reply_markup=tg_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"<b>Telegram:</b>\n{telegram_txt}", parse_mode="HTML",
                                   reply_markup=tg_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"<b>Telegram:</b>\n{telegram_txt}", parse_mode="HTML",
                               reply_markup=tg_markup, disable_web_page_preview=True)

# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ В TWITTER
# -----------------------------------------------------------------------------
def publish_post_to_twitter(text, image_url=None):
    github_filename = None
    try:
        media_ids = None
        file_path = None
        if image_url:
            if not str(image_url).startswith("http"):
                logging.error("Telegram file_id не поддерживается напрямую для Twitter публикации.")
                return False
            r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'})
            r.raise_for_status()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.write(r.content)
            tmp.close()
            file_path = tmp.name

        if file_path:
            media = twitter_api_v1.media_upload(file_path)
            media_ids = [media.media_id_string]
            os.remove(file_path)

        twitter_client_v2.create_tweet(text=text, media_ids=media_ids)
        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Twitter: {e}"
        ))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ В TELEGRAM КАНАЛ
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text, image_url=None):
    try:
        if image_url:
            await send_photo_with_download(channel_bot, TELEGRAM_CHANNEL_USERNAME_ID, image_url, caption=text)
        else:
            await channel_bot.send_message(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, text=text,
                                           parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as e:
        pending_post["active"] = False
        logging.error(f"Ошибка публикации в Telegram: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Telegram: {e}"
        )
        return False

# -----------------------------------------------------------------------------
# СТАРТОВОЕ МЕНЮ
# -----------------------------------------------------------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Пост", callback_data="post_menu")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🆕 Новый пост (ИИ)", callback_data="new_post_ai")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

# -----------------------------------------------------------------------------
# ЗАГЛУШКА ПРИ СТАРТЕ
# -----------------------------------------------------------------------------
async def send_start_placeholder():
    placeholders = [
        ("🚀 AiCoin — это революция в мире криптовалют и AI! Подключайтесь к нам сегодня и будьте в тренде будущего финансов.", "images_for_posts/placeholder1.jpg"),
        ("💡 AiCoin объединяет блокчейн и искусственный интеллект, чтобы сделать криптомир умнее и быстрее.", "images_for_posts/placeholder2.jpg"),
        ("🌐 С AiCoin вы получаете доступ к технологиям, которые меняют правила игры.", "images_for_posts/placeholder3.jpg"),
        ("🔥 Присоединяйтесь к AiCoin — станьте частью новой эры криптовалют!", "images_for_posts/placeholder4.jpg")
    ]
    text, img_path = random.choice(placeholders)
    try:
        if os.path.exists(img_path):
            with open(img_path, "rb") as img:
                await approval_bot.send_photo(chat_id=TELEGRAM_APPROVAL_CHAT_ID, photo=img, caption=text)
        else:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=text)
    except Exception as e:
        logging.error(f"Ошибка отправки заглушки: {e}")

# -----------------------------------------------------------------------------
# ТАЙМЕР АВТОПУБЛИКАЦИИ (для авто-режима)
# -----------------------------------------------------------------------------
async def check_timer():
    """
    Следит за pending_post["active"] + timeout.
    Если истекло — автопубликация в оба канала и выключение (авторежим).
    """
    while True:
        await asyncio.sleep(0.5)
        try:
            if pending_post["active"] and pending_post.get("timer"):
                passed = (datetime.now() - pending_post["timer"]).total_seconds()
                if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                    base_text = (post_data.get("text_ru") or "").strip()
                    twitter_text = build_twitter_preview(base_text)
                    telegram_text = build_telegram_preview(base_text)

                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⌛ Время ожидания истекло. Публикую автоматически."
                    )
                    tg_ok = await publish_post_to_telegram(telegram_text, post_data.get("image_url"))
                    tw_ok = publish_post_to_twitter(twitter_text, post_data.get("image_url"))

                    await approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text=f"Статус автопубликации — Telegram: {'✅' if tg_ok else '❌'}, Twitter: {'✅' if tw_ok else '❌'}"
                    )
                    # авто-режим — выключаемся сразу
                    shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_timer error: {e}")

# -----------------------------------------------------------------------------
# АВТОВЫКЛЮЧЕНИЕ ПО НЕАКТИВНОСТИ (ручной режим)
# -----------------------------------------------------------------------------
async def check_inactivity_shutdown():
    """
    Если 10 минут нет нажатий/сообщений — выключаемся.
    Работает для ручного режима, когда pending_post уже не активен.
    """
    global last_button_pressed_at
    while True:
        try:
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_inactivity_shutdown error: {e}")

# -----------------------------------------------------------------------------
# CALLBACK HANDLER (единый)
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, prev_data, manual_posts_today, last_action_time
    query = update.callback_query
    data = query.data
    await query.answer()

    # фикс активности
    last_button_pressed_at = datetime.now()

    # продлеваем таймер ожидания решения, если мы в pending_post
    if pending_post["active"]:
        pending_post["timer"] = datetime.now()
        pending_post["timeout"] = TIMER_PUBLISH_EXTEND

    # анти-спам по кнопкам
    user_id = update.effective_user.id
    now = datetime.now()
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 2:
        return
    last_action_time[user_id] = now

    # маршрутизация
    if data == "shutdown_bot":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот выключен.")
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data == "cancel_to_main":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=get_start_menu()
        )
        return

    if data == "post_menu":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                                        text="Выберите тип публикации:",
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("🐦💬 Twitter + Telegram", callback_data="post_both")],
                                            [InlineKeyboardButton("🐦 Только Twitter", callback_data="post_twitter")],
                                            [InlineKeyboardButton("💬 Только Telegram", callback_data="post_telegram")],
                                            [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
                                        ]))
        return

    if data == "self_post":
        pending_post["active"] = True  # ждём ввода для предпросмотра
        pending_post["timer"] = datetime.now()
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                                        text="✍️ Введите текст поста и (опционально) приложите фото одним сообщением:",
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
                                        ]))
        return

    if data == "new_post_ai":
        # генерируем заглушку ИИ
        text, img = await new_post_ai()
        # показываем split-предпросмотр и сразу меню
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, text, image_url=img)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=get_start_menu()
        )
        pending_post.update({"active": True, "timer": datetime.now(), "timeout": TIMER_PUBLISH_DEFAULT})
        return

    if data == "approve":
        # показать split-предпросмотр по текущему post_data
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
        pending_post.update({"active": True, "timer": datetime.now(), "timeout": TIMER_PUBLISH_DEFAULT})
        return

    if data in ("post_twitter", "post_telegram", "post_both"):
        publish_tg = data in ("post_telegram", "post_both")
        publish_tw = data in ("post_twitter", "post_both")
        pending_post["active"] = False
        await publish_flow(publish_tg=publish_tg, publish_tw=publish_tw)
        return

    if data == "new_post":
        # сгенерить черновой новый пост и показать предпросмотр
        post_data["text_ru"] = f"Тестовый новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = False
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
        pending_post.update({"active": True, "timer": datetime.now(), "timeout": TIMER_PUBLISH_DEFAULT})
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=post_end_keyboard()
        )
        return

    if data == "end_day":
        pending_post["active"] = False
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), dt_time(hour=9))
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M')}",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    if data == "edit_post":
        user_self_post[":edit:"] = {'state': 'wait_edit'}
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✏️ Пришлите новый текст и/или фото одним сообщением (или ответом на предпросмотр).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]])
        )
        return

    if data == "think" or data == "chat":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🧐 Думаем дальше…" if data == "think" else ("💬 Начинаем чат:\n" + post_data["text_ru"]),
            reply_markup=main_keyboard() if data == "think" else post_end_keyboard()
        )
        return

# -----------------------------------------------------------------------------
# РУЧНОЙ ВВОД ПОСЛЕ «Сделай сам»
# -----------------------------------------------------------------------------
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пользователь прислал текст/фото после нажатия 'Сделай сам'.
    Готовим post_data и показываем раздельный предпросмотр (Twitter/Telegram).
    """
    text = update.message.text or update.message.caption or ""
    image_url = None

    # если прислали фото — грузим в GitHub и получаем URL
    if update.message.photo:
        try:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        except Exception as e:
            logging.warning(f"handle_manual_input: cannot process photo: {e}")
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="❌ Не удалось обработать фото. Пришлите ещё раз или только текст."
            )
            return

    # заполняем текущий контекст поста
    post_data["text_ru"] = text if text else post_data["text_ru"]
    post_data["image_url"] = image_url if image_url else post_data.get("image_url", None)
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    # показываем split-предпросмотр (Twitter/Telegram двумя сообщениями)
    try:
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
        # панель действий под предпросмотрами
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Выберите действие:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🐦 Пост в Twitter", callback_data="post_twitter")],
                [InlineKeyboardButton("💬 Пост в Telegram", callback_data="post_telegram")],
                [InlineKeyboardButton("🐦💬 ПОСТ в оба", callback_data="post_both")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")],
                [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
            ])
        )
        pending_post.update({"active": True, "timer": datetime.now(), "timeout": TIMER_PUBLISH_EXTEND})
    except Exception as e:
        logging.error(f"handle_manual_input preview failed: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="❌ Не удалось показать предпросмотр. Попробуйте снова.",
        )

# -----------------------------------------------------------------------------
# ПУБЛИКАЦИОННЫЙ ПОТОК (со статусами, БД и выключениями)
# -----------------------------------------------------------------------------
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text = (post_data.get("text_ru") or "").strip()
    img = post_data.get("image_url")

    twitter_text = build_twitter_preview(base_text)
    telegram_text = build_telegram_preview(base_text)

    tg_status = None
    tw_status = None

    if publish_tg:
        if await is_duplicate_post(telegram_text, img):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=telegram_text, image_url=img)
            if tg_status:
                await save_post_to_history(telegram_text, img)

    if publish_tw:
        if await is_duplicate_post(twitter_text, img):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Twitter. Публикация пропущена.")
            tw_status = False
        else:
            tw_status = publish_post_to_twitter(twitter_text, img)
            if tw_status:
                await save_post_to_history(twitter_text, img)

    # статусы
    if publish_tg:
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram."
        )
    if publish_tw:
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter."
        )

    # меню после публикации
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Выберите действие:", reply_markup=get_start_menu())

    # авто-режим — выключаемся сразу; ручной — останется и вырубится по неактивности
    if not post_data.get("is_manual"):
        shutdown_bot_and_exit()

# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at
    last_button_pressed_at = datetime.now()

    # если пользователь пришёл после кнопки self_post — обрабатываем как ручной ввод
    if pending_post.get("active"):
        return await handle_manual_input(update, context)

    # иначе просто подсказываем меню
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Открой меню и выбери действие:",
        reply_markup=get_start_menu()
    )

# -----------------------------------------------------------------------------
# STARTUP: одна заглушка (~200 символов + картинка) и стартовое меню
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    # фоновые задачи: таймер автопостинга (для авторежима) и автоотключение по неактивности
    asyncio.create_task(check_timer())
    asyncio.create_task(check_inactivity_shutdown())

    # одна заглушка
    await send_start_placeholder()

    # показать стартовое меню сразу
    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Главное меню:",
        reply_markup=get_start_menu()
    )
    logging.info("Бот запущен. Заглушка отправлена. Главное меню показано.")

# -----------------------------------------------------------------------------
# Выключение
# -----------------------------------------------------------------------------
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
# MAIN (регистрация хендлеров)
# -----------------------------------------------------------------------------
def main():
    app = Application.builder()\
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)\
        .post_init(on_start)\
        .build()

    # кнопки и сообщения
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))

    app.run_polling(poll_interval=0.12, timeout=1)

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()
    