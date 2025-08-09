# -*- coding: utf-8 -*-
import os
import re
import asyncio
import hashlib
import logging
import random
import sys
import tempfile
import uuid
import base64
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

import tweepy
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
from github import Github
from openai import OpenAI  # openai>=1.35.0

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(funcName)s %(message)s')

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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
if not OPENAI_API_KEY:
    logging.error("Не задан OPENAI_API_KEY!")
    sys.exit(1)

# -----------------------------------------------------------------------------
# ГЛОБАЛЫЕ ОБЪЕКТЫ
# -----------------------------------------------------------------------------
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)
approval_lock = asyncio.Lock()

DB_FILE = "post_history.db"
TZ = ZoneInfo("Europe/Kyiv")

client_oa = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------------------------------------------------
# ТАЙМЕРЫ
# -----------------------------------------------------------------------------
TIMER_PUBLISH_DEFAULT = 180            # ожидание решения на старте (заглушка)
TIMER_PUBLISH_EXTEND  = 600            # ← 10 минут после любого взаимодействия
AUTO_SHUTDOWN_AFTER_SECONDS = 600      # 10 минут после последнего взаимодействия

DISABLE_WEB_PREVIEW = True

# -----------------------------------------------------------------------------
# ПОДПИСИ
# -----------------------------------------------------------------------------
TELEGRAM_SIGNATURE_HTML = '\n\n<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/aicoin_eth">Twitter X</a>'

# -----------------------------------------------------------------------------
# ФОЛБЭК‑КАРТИНКИ
# -----------------------------------------------------------------------------
fallback_images = [
    "https://upload.wikimedia.org/wikipedia/commons/9/99/Sample_User_Icon.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

# -----------------------------------------------------------------------------
# СОСТОЯНИЕ
# -----------------------------------------------------------------------------
post_data = {
    "text_en": "AI Coin blends blockchain with AI for smarter, faster, community-driven decisions.",
    "ai_hashtags": ["#AiCoin", "#AI", "$Ai", "#crypto"],
    "image_url": random.choice(fallback_images),
    "timestamp": None,
    "post_id": 0,
    "is_manual": False
}
prev_data = post_data.copy()

user_self_post = {}
# mode: "placeholder" — стартовый предпросмотр; "normal" — обычный режим
pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT, "mode": "normal"}
do_not_disturb = {"active": False}
last_action_time = {}
last_button_pressed_at = None

# -----------------------------------------------------------------------------
# ДНЕВНОЙ ПЛАН (14:00, 15:00, 16:00 — Киев)
# -----------------------------------------------------------------------------
day_plan = []  # {"time": dt, "text": str, "tags": list[str], "img": str|None, "status": "scheduled|published|skipped", "note": str}

# -----------------------------------------------------------------------------
# СТАРТОВОЕ МЕНЮ (как в первом файле) + одна кнопка плана
# -----------------------------------------------------------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Пост", callback_data="post_menu")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🆕 Новый пост (ИИ)", callback_data="new_post_ai")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],  # единственная новая кнопка
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
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
# TWITTER / GITHUB
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
# ПОСТОСТРОИТЕЛИ (EN, TG=полный, TW<=200)
# -----------------------------------------------------------------------------
_TCO_LEN = 23
_URL_RE = re.compile(r'https?://\S+', flags=re.UNICODE)

LINKS_SIGNATURE = "Learn more | telegram: https://t.me/AiCoin_ETH | website: https://getaicoin.com/"
MY_HASHTAGS_STR = "#AiCoin #AI $Ai #crypto"
TW_MAX = 200

def twitter_len(s: str) -> int:
    if not s: return 0
    s = normalize("NFC", s)
    return len(_URL_RE.sub('X' * _TCO_LEN, s))

def trim_plain_to(s: str, max_len: int) -> str:
    if not s: return s
    s = normalize("NFC", s).strip()
    if len(s) <= max_len: return s
    ell = '…'
    s = s[: max_len - len(ell)]
    return (s + ell).rstrip()

def trim_to_twitter_len(s: str, max_len: int) -> str:
    if not s: return s
    s = normalize("NFC", s).strip()
    if twitter_len(s) <= max_len: return s
    ell = '…'
    while s and twitter_len(s + ell) > max_len:
        s = s[:-1]
    return (s + ell).rstrip()

def _dedup_hashtags(*tags_groups):
    seen, out = set(), []
    def norm_tag(t: str) -> str:
        t = t.strip()
        if not t: return ""
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        return t
    def is_topic_ok(t: str) -> bool:
        tl = t.lower()
        return ("ai" in tl) or ("crypto" in tl) or tl.startswith("$ai")
    def feed(group):
        if not group: return
        items = group.split() if isinstance(group, str) else list(group)
        for raw in items:
            tag = norm_tag(raw)
            if not tag or not is_topic_ok(tag): continue
            key = tag.lower()
            if key in seen: continue
            seen.add(key); out.append(tag)
    for g in tags_groups: feed(g)
    return " ".join(out)

def compose_full_text_without_links(ai_text_en: str, ai_hashtags=None) -> str:
    body = trim_plain_to((ai_text_en or "").strip(), 666)
    tags = _dedup_hashtags(MY_HASHTAGS_STR, ai_hashtags or [])
    if body and tags:
        return f"{body} {tags}"
    return body or tags

def compose_full_text_with_links(ai_text_en: str, ai_hashtags=None) -> str:
    body = trim_plain_to((ai_text_en or "").strip(), 666)
    tags = _dedup_hashtags(MY_HASHTAGS_STR, ai_hashtags or [])
    suffix_parts = [LINKS_SIGNATURE]
    if tags: suffix_parts.append(tags)
    suffix = " ".join(suffix_parts).strip()
    if body and suffix: return f"{body} {suffix}"
    return body or suffix

def build_twitter_post(ai_text_en: str, ai_hashtags=None) -> str:
    suffix_text = compose_full_text_with_links("", ai_hashtags)
    body = trim_plain_to((ai_text_en or "").strip(), 666)

    sep = " " if body and suffix_text else ""
    allowed_for_body = TW_MAX - (1 if sep else 0) - twitter_len(suffix_text)
    if allowed_for_body < 0:
        return trim_to_twitter_len(suffix_text, TW_MAX)

    body_trimmed = trim_to_twitter_len(body, allowed_for_body)
    composed = (f"{body_trimmed}{sep}{suffix_text}").strip()

    while twitter_len(composed) > TW_MAX and body_trimmed:
        body_trimmed = trim_to_twitter_len(body_trimmed[:-1], allowed_for_body)
        composed = (f"{body_trimmed}{sep}{suffix_text}").strip()

    if not body_trimmed and twitter_len(suffix_text) > TW_MAX:
        composed = trim_to_twitter_len(suffix_text, TW_MAX)
    return composed

def build_telegram_post(ai_text_en: str, ai_hashtags=None) -> str:
    return compose_full_text_without_links(ai_text_en, ai_hashtags)

def build_twitter_preview(ai_text_en: str, ai_hashtags=None) -> str:
    return build_twitter_post(ai_text_en, ai_hashtags)

def build_telegram_preview(ai_text_en: str, ai_hashtags=None) -> str:
    return build_telegram_post(ai_text_en, ai_hashtags)

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
# БЕЗОПАСНАЯ ОТПРАВКА
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
        await bot.send_message(chat_id=chat_id, text="Ошибка предпросмотра. Вот текст поста:\n\n" + text,
                               reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW, parse_mode="HTML")

# -----------------------------------------------------------------------------
# ПРЕДПРОСМОТР (ДВЕ КАРТОЧКИ — как в первом)
# -----------------------------------------------------------------------------
async def preview_split(bot, chat_id, ai_text_en, ai_hashtags=None, image_url=None, header: str | None = None):
    twitter_txt = build_twitter_preview(ai_text_en, ai_hashtags)
    telegram_txt = build_telegram_preview(ai_text_en, ai_hashtags)
    hdr_tw = f"<b>{header}</b>\n" if header else ""
    hdr_tg = f"<b>{header}</b>\n" if header else ""

    tw_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"{hdr_tw}<b>Twitter:</b>\n{twitter_txt}", reply_markup=tw_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"{hdr_tw}<b>Twitter:</b>\n{twitter_txt}", parse_mode="HTML",
                                   reply_markup=tw_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"{hdr_tw}<b>Twitter:</b>\n{twitter_txt}", parse_mode="HTML",
                               reply_markup=tw_markup, disable_web_page_preview=True)

    tg_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main"),
         InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")]
    ])
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"{hdr_tg}<b>Telegram:</b>\n{telegram_txt}", reply_markup=tg_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"{hdr_tg}<b>Telegram:</b>\n{telegram_txt}", parse_mode="HTML",
                                   reply_markup=tg_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"{hdr_tg}<b>Telegram:</b>\n{telegram_txt}", parse_mode="HTML",
                               reply_markup=tg_markup, disable_web_page_preview=True)

# -----------------------------------------------------------------------------
# ФОТО (с фолбэком)
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
                tmp_file.write(response.content); tmp_file.close()
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
# БАЗА ДАННЫХ
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
    if not text: return ""
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
            await db.execute("INSERT INTO posts (text, text_hash, timestamp, image_hash) VALUES (?, ?, ?, ?)",
                             (text, text_hash, datetime.now(TZ).isoformat(), image_hash))
            await db.commit()
        except Exception as e:
            logging.warning(f"save_post_to_history: возможно дубликат или ошибка вставки: {e}")

# -----------------------------------------------------------------------------
# ИИ (текст, картинка — пока фолбэк)
# -----------------------------------------------------------------------------
def _oa_chat_text(prompt: str) -> str:
    try:
        resp = client_oa.chat_completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"You write concise, inspiring social promos for a crypto+AI project called Ai Coin. Avoid the words 'google' or 'trends'. Keep it 1–3 short sentences, energetic, non-technical, in English."},
                {"role":"user","content":prompt}
            ],
            temperature=0.9,
            max_tokens=220,
        )
        txt = (resp.choices[0].message.content or "").strip()
        txt = txt.strip('"\n` ')
        return txt
    except Exception as e:
        logging.warning(f"_oa_chat_text error: {e}")
        return "Ai Coin fuses AI with blockchain to turn community ideas into real actions. Join builders shaping the next wave of crypto utility."

def _oa_image_b64(prompt: str, size: str = "1024x1024") -> bytes | None:
    try:
        img = client_oa.images.generate(model="gpt-image-1", prompt=prompt, size=size, n=1)
        b64 = img.data[0].b64_json
        return base64.b64decode(b64)
    except Exception as e:
        logging.warning(f"_oa_image_b64 error: {e}")
        return None

async def generate_image_to_github(prompt: str) -> str:
    # Пока используем фолбэк
    return random.choice(fallback_images)

async def ai_generate_content_en(topic_hint: str) -> tuple[str, list[str], str | None]:
    text_prompt = (
        "Create a short social promo (1–3 sentences) about Ai Coin: an AI-integrated crypto project where holders can propose ideas, "
        "AI analyzes them, and the community votes on-chain. Tone: inspiring, community-first, clear benefits, no jargon. "
        f"Emphasize: {topic_hint}."
    )
    text_en = _oa_chat_text(text_prompt)

    extra_tags_prompt = (
        "Give me 3 short, relevant crypto+AI hashtags for a social post about Ai Coin (no duplicates of #AiCoin, #AI, #crypto, $Ai), "
        "single line, space-separated, each begins with #, only AI/crypto topics."
    )
    tags_line = _oa_chat_text(extra_tags_prompt)
    ai_tags = [t for t in tags_line.split() if t.startswith("#") and len(t) > 1][:4]

    image_url = await generate_image_to_github("ai coin futuristic minimal, no text")
    return (text_en, ai_tags, image_url)

# -----------------------------------------------------------------------------
# ДНЕВНОЙ ПЛАН / ОТЧЁТ / ПУБЛИКАЦИЯ
# -----------------------------------------------------------------------------
def _today_time(hour: int, minute: int) -> datetime:
    now = datetime.now(TZ)
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

async def ensure_unique_bundle(text_en: str, tags: list[str], img: str | None, max_tries: int = 3, topic_hint: str = "variant") -> tuple[str, list[str], str | None, bool]:
    tw = build_twitter_post(text_en, tags)
    tg = build_telegram_post(text_en, tags)
    if not await is_duplicate_post(tw, img) and not await is_duplicate_post(tg, img):
        return text_en, tags, img, False
    for _ in range(max_tries):
        nt, ntags, nimg = await ai_generate_content_en(topic_hint)
        tw2 = build_twitter_post(nt, ntags)
        tg2 = build_telegram_post(nt, ntags)
        if not await is_duplicate_post(tw2, nimg) and not await is_duplicate_post(tg2, nimg):
            return nt, ntags, nimg, True
    return text_en, tags, img, False

async def report_day_plan_status():
    if not day_plan:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="План на день пуст.", disable_web_page_preview=True)
        return
    lines = ["<b>Day plan (Kyiv):</b>"]
    for slot in day_plan:
        tstr = slot["time"].strftime("%H:%M")
        status = slot["status"]
        emoji = "🟡" if status == "scheduled" else ("✅" if status == "published" else "⏭️")
        note = f" — {slot['note']}" if slot.get("note") else ""
        lines.append(f"{tstr} — {emoji} {status.capitalize()}{note}")
    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

async def preview_day_plan():
    for slot in day_plan:
        tstr = slot["time"].strftime("%H:%M")
        header = f"{tstr} • Scheduled preview"
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, slot["text"], slot["tags"], slot["img"], header=header)

async def publish_slot(slot_idx: int):
    slot = day_plan[slot_idx]
    text_en, tags, img = slot["text"], slot["tags"], slot["img"]
    tw_text = build_twitter_post(text_en, tags)
    tg_text = build_telegram_post(text_en, tags)

    attempts = 0
    replaced = False
    while attempts < 3 and (await is_duplicate_post(tw_text, img) or await is_duplicate_post(tg_text, img)):
        attempts += 1
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"{slot['time'].strftime('%H:%M')} — ⏳ Duplicate detected, generating replacement… ({attempts}/3)")
        nt, ntags, nimg = await ai_generate_content_en("fresh replacement")
        nt, ntags, nimg, rep2 = await ensure_unique_bundle(nt, ntags, nimg, max_tries=1, topic_hint="freshest")
        text_en, tags, img = nt, ntags, nimg
        tw_text = build_twitter_post(text_en, tags)
        tg_text = build_telegram_post(text_en, tags)
        replaced = replaced or rep2

    if replaced:
        slot["note"] = (slot.get("note") or "") + " (replaced before publish)"
        slot["text"], slot["tags"], slot["img"] = text_en, tags, img

    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"{slot['time'].strftime('%H:%M')} — ▶️ Publishing…")

    try:
        tg_ok = await publish_post_to_telegram(tg_text, img)
        tw_ok = publish_post_to_twitter(tw_text, img)
    except Exception as e:
        tg_ok = False; tw_ok = False
        slot["note"] = (slot.get("note") or "") + f" (exception: {e})"

    if tg_ok: await save_post_to_history(tg_text, img)
    if tw_ok: await save_post_to_history(tw_text, img)

    if tg_ok and tw_ok:
        slot["status"] = "published"
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"{slot['time'].strftime('%H:%M')} — ✅ Published (TG+TW)")
    else:
        slot["status"] = "skipped"
        slot["note"] = (slot.get("note") or "") + " (publish error)"
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"{slot['time'].strftime('%H:%M')} — ⏭️ Skipped due to error")

    # После автопоста или ошибки — выключаем сразу (требование)
    shutdown_bot_and_exit()

async def schedule_slot(slot_idx: int):
    slot = day_plan[slot_idx]
    when = slot["time"]
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"⏰ Scheduled {when.strftime('%H:%M')} (Kyiv)")
    await asyncio.sleep(max(0, (when - datetime.now(TZ)).total_seconds()))
    await publish_slot(slot_idx)

async def build_day_plan_for_today():
    """Генерация 3 слотов: 14:00, 15:00, 16:00 (Kyiv)."""
    global day_plan
    now = datetime.now(TZ)
    t14 = _today_time(14, 0)
    t15 = _today_time(15, 0)
    t16 = _today_time(16, 0)
    targets = [t if t > now else t + timedelta(days=1) for t in (t14, t15, t16)]

    topics = [
        "Utility, community growth and joining early.",
        "Governance: holders propose, AI analyzes, tokenholders vote on-chain (>51% wins).",
        "AI-powered proposals and speed of execution."
    ]

    bundles = await asyncio.gather(*(ai_generate_content_en(th) for th in topics))

    unique_bundles = []
    for (text, tags, img), hint in zip(bundles, topics):
        nt, ntags, nimg, replaced = await ensure_unique_bundle(text, tags, img, max_tries=3, topic_hint=hint+" variant")
        note = "replacement prepared" if replaced else ""
        unique_bundles.append((nt, ntags, nimg, note))

    day_plan = []
    for t, (text, tags, img, note) in zip(targets, unique_bundles):
        day_plan.append({"time": t, "text": text, "tags": tags, "img": img, "status": "scheduled", "note": note})

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
            tmp.write(r.content); tmp.close()
            file_path = tmp.name

        if file_path:
            media = twitter_api_v1.media_upload(file_path)
            media_ids = [media.media_id_string]
            os.remove(file_path)

        final_text = build_twitter_post(text, [])
        twitter_client_v2.create_tweet(text=final_text, media_ids=media_ids)
        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True
    except Exception as e:
        logging.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Twitter: {e}"))
        if github_filename: delete_image_from_github(github_filename)
        return False

# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ В TELEGRAM
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text, image_url=None):
    try:
        text_with_signature = (text or "") + TELEGRAM_SIGNATURE_HTML
        if image_url:
            await channel_bot.send_photo(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, photo=image_url, caption=text_with_signature, parse_mode="HTML")
        else:
            await channel_bot.send_message(chat_id=TELEGRAM_CHANNEL_USERNAME_ID, text=text_with_signature,
                                           parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as e:
        logging.error(f"Ошибка публикации в Telegram: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Telegram: {e}")
        return False

# -----------------------------------------------------------------------------
# СТАРТ: split‑предпросмотр + меню, при молчании — выключение без поста
# -----------------------------------------------------------------------------
async def send_start_placeholder():
    text_en = post_data["text_en"]
    ai_tags = post_data.get("ai_hashtags") or []
    img_url = post_data.get("image_url")
    try:
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, text_en, ai_tags, image_url=img_url, header="Стартовое сообщение")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        # режим "placeholder": если нет действий — отключаемся БЕЗ публикации
        pending_post.update({"active": True, "timer": datetime.now(TZ), "timeout": TIMER_PUBLISH_DEFAULT, "mode": "placeholder"})
    except Exception as e:
        logging.error(f"Ошибка отправки заглушки: {e}")

# -----------------------------------------------------------------------------
# ТАЙМЕР: только для стартовой заглушки (молчание => выключение без публикации)
# -----------------------------------------------------------------------------
async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        try:
            if pending_post["active"] and pending_post.get("timer"):
                passed = (datetime.now(TZ) - pending_post["timer"]).total_seconds()
                if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                    if pending_post.get("mode") == "placeholder":
                        try:
                            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⌛ Нет действий. Выключаюсь без публикации.")
                        except Exception:
                            pass
                        shutdown_bot_and_exit()
                    else:
                        pending_post["active"] = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_timer error: {e}")

# -----------------------------------------------------------------------------
# АВТОВЫКЛЮЧЕНИЕ ПО НЕАКТИВНОСТИ (10 минут, любое действие — продлевает)
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
                    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Нет активности 10 минут. Отключаюсь.")
                except Exception:
                    pass
                shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"check_inactivity_shutdown error: {e}")

# -----------------------------------------------------------------------------
# CALLBACK HANDLER
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, last_action_time
    query = update.callback_query
    data = query.data
    await query.answer()

    now = datetime.now(TZ)
    last_button_pressed_at = now

    # Любая кнопка — продлевает жизнь и выводит из placeholder‑режима
    pending_post["active"] = True
    pending_post["timer"] = now
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    user_id = update.effective_user.id
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 1:
        return
    last_action_time[user_id] = now

    if data == "shutdown_bot":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот выключен.")
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data == "cancel_to_main":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "post_menu":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Выберите тип публикации:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🐦💬 Twitter + Telegram", callback_data="post_both")],
                [InlineKeyboardButton("🐦 Только Twitter", callback_data="post_twitter")],
                [InlineKeyboardButton("💬 Только Telegram", callback_data="post_telegram")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
            ])
        )
        return

    if data == "self_post":
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Введите текст поста (EN) и (опционально) приложите фото одним сообщением:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]])
        )
        return

    if data == "new_post_ai":
        text_en, ai_tags, img = await ai_generate_content_en("General awareness and community invite.")
        post_data["text_en"] = text_en
        post_data["ai_hashtags"] = ai_tags
        post_data["image_url"] = img
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, text_en, ai_tags, image_url=img)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "approve":
        # Показать split‑предпросмотр текущего поста (как в первом файле)
        await preview_split(
            approval_bot,
            TELEGRAM_APPROVAL_CHAT_ID,
            post_data.get("text_en") or "",
            post_data.get("ai_hashtags") or [],
            image_url=post_data.get("image_url"),
            header="Предпросмотр"
        )
        return

    if data == "show_day_plan":
        await report_day_plan_status()
        await preview_day_plan()
        return

    if data in ("post_twitter", "post_telegram", "post_both"):
        publish_tg = data in ("post_telegram", "post_both")
        publish_tw = data in ("post_twitter", "post_both")
        await publish_flow(publish_tg=publish_tg, publish_tw=publish_tw)
        return

    if data == "new_post":
        post_data["text_en"] = f"Test EN post #{post_data['post_id'] + 1}"
        post_data["ai_hashtags"] = ["#AiCoin", "#AI", "$Ai", "#crypto"]
        post_data["image_url"] = random.choice(fallback_images)
        post_data["post_id"] += 1
        post_data["is_manual"] = True
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_en"], post_data["ai_hashtags"], image_url=post_data["image_url"])
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=post_end_keyboard())
        return

    if data == "end_day":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}",
            parse_mode="HTML", reply_markup=get_start_menu())
        return

    if data == "edit_post":
        user_self_post[":edit:"] = {'state': 'wait_edit'}
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✏️ Пришлите новый текст (EN) и/или фото одним сообщением (или ответом на предпросмотр).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]]))
        return

    if data == "think" or data == "chat":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🧐 Думаем дальше…" if data == "think" else ("💬 Начинаем чат:\n" + (post_data.get("text_en") or "")),
            reply_markup=get_start_menu() if data == "think" else post_end_keyboard())
        return

# -----------------------------------------------------------------------------
# РУЧНОЙ ВВОД ПОСЛЕ «Сделай сам»
# -----------------------------------------------------------------------------
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Любое сообщение продлевает жизнь на 10 минут
    pending_post["active"] = True
    pending_post["timer"] = datetime.now(TZ)
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    text = update.message.text or update.message.caption or ""
    image_url = None

    if update.message.photo:
        try:
            image_url = await process_telegram_photo(update.message.photo[-1].file_id, approval_bot)
        except Exception as e:
            logging.warning(f"handle_manual_input: cannot process photo: {e}")
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось обработать фото. Пришлите ещё раз или только текст.")
            return

    post_data["text_en"] = text.strip() or post_data.get("text_en") or ""
    post_data["ai_hashtags"] = post_data.get("ai_hashtags") or []
    post_data["image_url"] = image_url if image_url else post_data.get("image_url")
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    try:
        await preview_split(approval_bot, TELEGRAM_APPROVAL_CHAT_ID, post_data["text_en"], post_data["ai_hashtags"], image_url=post_data["image_url"])
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
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось показать предпросмотр. Попробуйте снова.")

# -----------------------------------------------------------------------------
# ПУБЛИКАЦИЯ: общая логика/дедупликация/БД (по кнопкам)
# -----------------------------------------------------------------------------
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()
    ai_tags = post_data.get("ai_hashtags") or []
    img = post_data.get("image_url") or None

    twitter_text = build_twitter_preview(base_text_en, ai_tags)
    telegram_text = build_telegram_preview(base_text_en, ai_tags)

    tg_status = None
    tw_status = None

    if publish_tg:
        if await is_duplicate_post(telegram_text, img):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=telegram_text, image_url=img)
            if tg_status: await save_post_to_history(telegram_text, img)

    if publish_tw:
        if await is_duplicate_post(twitter_text, img):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Twitter. Публикация пропущена.")
            tw_status = False
        else:
            twitter_text = build_twitter_post(base_text_en, ai_tags)
            tw_status = publish_post_to_twitter(twitter_text, img)
            if tw_status: await save_post_to_history(twitter_text, img)

    if publish_tg:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram.")
    if publish_tw:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter.")

    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# MESSAGE HANDLER (любое сообщение продлевает работу на 10 минут)
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at
    last_button_pressed_at = datetime.now(TZ)

    pending_post["active"] = True
    pending_post["timer"] = last_button_pressed_at
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if pending_post.get("active"):
        return await handle_manual_input(update, context)

    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Открой меню и выбери действие:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# STARTUP
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()

    # фоновые задачи
    asyncio.create_task(check_timer())                 # только для стартовой заглушки
    asyncio.create_task(check_inactivity_shutdown())   # общее авто‑выключение по неактивности

    # Старт: split‑предпросмотр + меню, без автопубликации если молчим
    text_en, ai_tags, img = await ai_generate_content_en("General invite and value.")
    post_data["text_en"] = text_en
    post_data["ai_hashtags"] = ai_tags
    post_data["image_url"] = img
    await send_start_placeholder()

    # План дня 14:00/15:00/16:00: отчёт + предпросмотры + расписание
    await build_day_plan_for_today()
    await report_day_plan_status()
    await preview_day_plan()
    for idx in range(len(day_plan)):
        asyncio.create_task(schedule_slot(idx))

    logging.info("Бот запущен. Стартовый предпросмотр показан, меню выведено. План дня (14/15/16 Kyiv) запущен, предпросмотры отправлены.")

# -----------------------------------------------------------------------------
# Выключение
# -----------------------------------------------------------------------------
def shutdown_bot_and_exit():
    try:
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!"))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).post_init(on_start).build()
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))
    app.run_polling(poll_interval=0.12, timeout=1)

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()