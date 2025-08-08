# -*- coding: utf-8 -*-
import os
import io
import re
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Europe/Kyiv")

# расписание/таймеры
scheduled_posts_per_day = 6
manual_posts_today = 0
TIMER_PUBLISH_DEFAULT = 180   # 3 минуты — автопост (если ничего не нажали)
TIMER_PUBLISH_EXTEND  = 180   # не используется как продление авто— мы авто отменяем при любом клике
AUTO_SHUTDOWN_AFTER_SECONDS = 600  # 10 минут после последней кнопки (ручной режим)

# предпросмотр ссылок в Telegram — отключаем
DISABLE_WEB_PREVIEW = True

# -----------------------------------------------------------------------------
# ЛИМИТЫ ДЛИНЫ ТЕКСТА / ПОМОЩНИКИ ДЛЯ X(Twitter)
# -----------------------------------------------------------------------------
_TCO_LEN = 23
_URL_RE = re.compile(r'https?://\S+', flags=re.UNICODE)

def twitter_len(s: str) -> int:
    """Длина для X: любая URL учитывается как 23 символа; остальное — фактическая длина."""
    if not s:
        return 0
    s = normalize("NFC", s)
    return len(_URL_RE.sub('X' * _TCO_LEN, s))

def trim_to_twitter_len(s: str, max_len: int) -> str:
    """
    Обрезает строку s так, чтобы её twitter_len <= max_len.
    Если пришлось резать, добавляет '…' и гарантирует, что вместе с '…' тоже <= max_len.
    """
    if not s:
        return s
    s = normalize("NFC", s).strip()
    if twitter_len(s) <= max_len:
        return s
    ell = '…'
    # Режем посимвольно с конца, пока вместе с многоточием не влезет.
    while s and twitter_len(s + ell) > max_len:
        s = s[:-1]
    return (s + ell).rstrip()

def enforce_telegram_666(text: str) -> str:
    if not text:
        return text
    t = normalize("NFC", text).strip()
    if len(t) <= 666:
        return t
    ell = '…'
    return (t[: max(0, 666 - len(ell))] + ell).rstrip()

# -----------------------------------------------------------------------------
# ЗАГЛУШКА НА СТАРТЕ (~650 символов) + картинка
# -----------------------------------------------------------------------------
PLACEHOLDER_TEXT = (
    "AI Coin — симбиоз блокчейна и искусственного интеллекта. Мы создаём экосистему, "
    "в которой алгоритмы ищут тренды, подсказывают инсайты и помогают принимать решения. "
    "Прозрачность, скорость и сообщество — три кита нашего развития. Присоединяйтесь к "
    "новой эре цифровой экономики: релизы, коллаборации и гайды уже на подходе. "
    "Следите за обновлениями, делитесь идеями и становитесь частью движения. "
    "Будущее строится вместе. Подробнее на сайте и в X."
)
PLACEHOLDER_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg"

WELCOME_HASHTAGS = "#AiCoin #AI #crypto #тренды #бот #новости"
test_images = [
    "https://upload.wikimedia.org/wikipedia/commons/4/47/PNG_transparency_demonstration_1.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/1/17/Google-flutter-logo.png",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]
WELCOME_POST_RU = PLACEHOLDER_TEXT  # одно стартовое сообщение

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
# ПОСТОСТРОИТЕЛИ (жёсткие лимиты)
# -----------------------------------------------------------------------------
TWITTER_SIGNATURE = " Learn more: https://getaicoin.com/ | X: https://x.com/aicoin_eth #AiCoin #AI $Ai #crypto"
TELEGRAM_SIGNATURE_HTML = '\n\n<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/aicoin_eth">X (Twitter)</a>'

def build_twitter_post(user_text_ru: str) -> str:
    """
    Режем ТОЛЬКО пользовательский текст так, чтобы:
    twitter_len(user_text) + (1, если есть текст и подпись) + twitter_len(signature) <= 280
    URL считаются как 23 символа (t.co). Хэштеги — обычные символы.
    """
    base = (user_text_ru or "").strip()
    sig = TWITTER_SIGNATURE.strip()

    if not base:
        # нет пользовательского текста — публикуем только подпись (подрежем на всякий случай)
        return trim_to_twitter_len(sig, 280)

    # считаем доступный лимит под base с учётом пробела между base и подписью
    sep = " "
    sig_len = twitter_len(sig)
    # минимум 1 символ на разделитель
    allowed_for_base = 280 - sig_len - len(sep)
    if allowed_for_base < 0:
        # подпись сама не влазит — урежем её и вернём без base
        return trim_to_twitter_len(sig, 280)

    base_trimmed = trim_to_twitter_len(base, allowed_for_base)
    composed = f"{base_trimmed}{sep}{sig}".strip()
    # safety: если внезапно не влезло (из-за многоточия и т.д.) — доурежем базу
    while twitter_len(composed) > 280 and base_trimmed:
        base_trimmed = trim_to_twitter_len(base_trimmed[:-1], allowed_for_base)
        composed = f"{base_trimmed}{sep}{sig}".strip()
    # если базу обнулили — публикуем только подпись
    if not base_trimmed:
        return trim_to_twitter_len(sig, 280)
    return composed

def build_twitter_preview(user_text_ru: str) -> str:
    return build_twitter_post(user_text_ru)

def build_telegram_post(user_text_ru: str) -> str:
    composed = ((user_text_ru or "").strip() + TELEGRAM_SIGNATURE_HTML)
    return enforce_telegram_666(composed)

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
        logging.error(f"Ошибка удаления файла на GitHub: {e}")

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
                (text, text_hash, datetime.now(TZ).isoformat(), image_hash)
            )
            await db.commit()
        except Exception as e:
            logging.warning(f"save_post_to_history: возможно дубликат или ошибка вставки: {e}")

# -----------------------------------------------------------------------------
# ПРЕДПРОСМОТР: РАЗДЕЛЁННЫЙ (Twitter/Telegram — два сообщения)
# -----------------------------------------------------------------------------
async def preview_split(bot, chat_id, text, image_url=None):
    twitter_txt = build_twitter_preview(text)
    telegram_txt = build_telegram_preview(text)

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
# ЗАГЛУШКА ПРИ СТАРТЕ (одно сообщение + меню, запуск 3-мин. таймера)
# -----------------------------------------------------------------------------
async def send_start_placeholder():
    text = PLACEHOLDER_TEXT
    img_url = PLACEHOLDER_IMAGE
    post_data["text_ru"] = text
    post_data["image_url"] = img_url
    try:
        await safe_preview_post(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            text=f"<b>Стартовое сообщение</b>\n\n{build_telegram_preview(text)}",
            image_url=img_url,
            reply_markup=get_start_menu()
        )
        # включаем 3-минутный авто-таймер
        pending_post.update({"active": True, "timer": datetime.now(TZ), "timeout": TIMER_PUBLISH_DEFAULT})
    except Exception as e:
        logging.error(f"Ошибка отправки заглушки: {e}")

# -----------------------------------------------------------------------------
# ТАЙМЕР АВТОПУБЛИКАЦИИ (для авто-режима)
# -----------------------------------------------------------------------------
async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        try:
            if pending_post["active"] and pending_post.get("timer"):
                passed = (datetime.now(TZ) - pending_post["timer"]).total_seconds()
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
                    shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_timer error: {e}")

# -----------------------------------------------------------------------------
# АВТОВЫКЛЮЧЕНИЕ ПО НЕАКТИВНОСТИ (ручной режим)
# -----------------------------------------------------------------------------
async def check_inactivity_shutdown():
    global last_button_pressed_at
    while True:
        try:
            await asyncio.sleep(5)
            if last_button_pressed_at is None:
                continue
            idle = (datetime.now(TZ) - last_button_pressed_at).total_seconds()
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
# ВСПОМОГАТЕЛЬНОЕ: планирование 3 тестовых автопостов (23:30, 23:45, 00:00)
# -----------------------------------------------------------------------------
def _next_dt_at(hour: int, minute: int) -> datetime:
    now = datetime.now(TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

async def schedule_post_at(when: datetime, text: str, image_url: str | None, tag: str):
    await approval_bot.send_message(
        TELEGRAM_APPROVAL_CHAT_ID,
        f"⏰ Запланировано: {tag} на {when.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    await asyncio.sleep((when - datetime.now(TZ)).total_seconds())
    post_data["text_ru"] = text
    post_data["image_url"] = image_url
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"▶️ Автозапуск: {tag}")
    tg_ok = await publish_post_to_telegram(build_telegram_preview(text), image_url)
    tw_ok = publish_post_to_twitter(build_twitter_preview(text), image_url)
    await approval_bot.send_message(
        TELEGRAM_APPROVAL_CHAT_ID,
        f"Готово: {tag} — Telegram: {'✅' if tg_ok else '❌'}, Twitter: {'✅' if tw_ok else '❌'}"
    )

async def schedule_test_runs():
    texts = [
        "AI Coin запускает серию тестов. Проверяем автопост, лимиты и медиа. Следите за апдейтами и присоединяйтесь. #AiCoin #AI #crypto",
        "Эксперимент №2: публикации по расписанию каждые 15 минут. Текст+хештеги и проверка длины. #AiCoin #automation",
        "Финальный шаг теста: публикация на полуночной отметке. Спасибо за участие! #AiCoin #test"
    ]
    imgs = [random.choice(test_images), None, random.choice(test_images)]

    t1 = _next_dt_at(23, 30)
    t2 = _next_dt_at(23, 45)
    t3 = _next_dt_at(0, 0)  # 00:00 следующего дня

    asyncio.create_task(schedule_post_at(t1, texts[0], imgs[0], "Тест-1 (23:30)"))
    asyncio.create_task(schedule_post_at(t2, texts[1], imgs[1], "Тест-2 (23:45)"))
    asyncio.create_task(schedule_post_at(t3, texts[2], imgs[2], "Тест-3 (00:00)"))

# -----------------------------------------------------------------------------
# CALLBACK HANDLER (единый)
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, prev_data, manual_posts_today, last_action_time
    query = update.callback_query
    data = query.data
    await query.answer()

    last_button_pressed_at = datetime.now(TZ)

    if pending_post["active"]:
        pending_post["active"] = False

    user_id = update.effective_user.id
    now = datetime.now(TZ)
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 2:
        return
    last_action_time[user_id] = now

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
        pending_post["active"] = True
        pending_post["timer"] = datetime.now(TZ)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                                        text="✍️ Введите текст поста и (опционально) приложите фото одним сообщением:",
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
                                        ]))
        return

    if data == "new_post_ai":
        text, img = post_data["text_ru"], post_data["image_url"]
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, text, image_url=img)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="Главное меню:",
            reply_markup=get_start_menu()
        )
        return

    if data == "approve":
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
        return

    if data in ("post_twitter", "post_telegram", "post_both"):
        publish_tg = data in ("post_telegram", "post_both")
        publish_tw = data in ("post_twitter", "post_both")
        pending_post["active"] = False
        await publish_flow(publish_tg=publish_tg, publish_tw=publish_tw)
        return

    if data == "new_post":
        post_data["text_ru"] = f"Тестовый новый пост #{post_data['post_id'] + 1}"
        post_data["image_url"] = random.choice(test_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
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
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}",
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
    text = update.message.text or update.message.caption or ""
    image_url = None

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

    # публикуем ровно то, что прислал пользователь (текст/фото/оба)
    post_data["text_ru"] = text if text else ""
    post_data["image_url"] = image_url if image_url else (None)
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    try:
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_ru"], image_url=post_data["image_url"])
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
    img = post_data.get("image_url") or None

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

    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Выберите действие:", reply_markup=get_start_menu())

    if not post_data.get("is_manual"):
        shutdown_bot_and_exit()

# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at
    last_button_pressed_at = datetime.now(TZ)

    if pending_post.get("active"):
        return await handle_manual_input(update, context)

    await approval_bot.send_message(
        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        text="Открой меню и выбери действие:",
        reply_markup=get_start_menu()
    )

# -----------------------------------------------------------------------------
# STARTUP: одно сообщение (текст+картинка) и стартовое меню + расписание
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    asyncio.create_task(check_timer())
    asyncio.create_task(check_inactivity_shutdown())

    await send_start_placeholder()  # стартовое сообщение + запуск 3-мин таймера
    await schedule_test_runs()      # план: 23:30, 23:45, 00:00 по Киеву

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

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))

    app.run_polling(poll_interval=0.12, timeout=1)

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()