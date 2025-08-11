# -*- coding: utf-8 -*-
"""
twitter_bot.py — основной бот согласования/генерации/публикации.
Часть 1/2.
"""

import os
import re
import asyncio
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
from openai import OpenAI  # openai>=1.35.0

# === ПЛАНИРОВЩИК ===
from planner import register_planner_handlers, open_planner
try:
    from planner import set_ai_generator  # может отсутствовать — ок
except ImportError:
    set_ai_generator = None
from planner import USER_STATE as PLANNER_STATE

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ (структурное)
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s",
)
log = logging.getLogger("twitter_bot")

# -----------------------------------------------------------------------------
# НАСТРОЙКИ/ENV
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

# Проверки окружения (жесткие — как было)
if not all([TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID_STR, TELEGRAM_BOT_TOKEN_CHANNEL, TELEGRAM_CHANNEL_USERNAME_ID]):
    log.error("Не заданы обязательные переменные окружения Telegram!")
    sys.exit(1)
TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)
if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
    log.error("Не заданы обязательные переменные окружения для Twitter!")
    sys.exit(1)
if not all([GITHUB_TOKEN, GITHUB_REPO]):
    log.error("Не заданы обязательные переменные окружения GitHub!")
    sys.exit(1)
if not OPENAI_API_KEY:
    log.error("Не задан OPENAI_API_KEY!")
    sys.exit(1)

# -----------------------------------------------------------------------------
# ГЛОБАЛЫ
# -----------------------------------------------------------------------------
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

DB_FILE = "post_history.db"
TZ = ZoneInfo("Europe/Kyiv")

# OpenAI клиент
client_oa = OpenAI(api_key=OPENAI_API_KEY, max_retries=0, timeout=10)
OPENAI_QUOTA_WARNED = False

# Таймеры
TIMER_PUBLISH_DEFAULT = 180       # авто-пост плейсхолдера через 3 минуты
TIMER_PUBLISH_EXTEND  = 600       # при активности таймер продлеваем
AUTO_SHUTDOWN_AFTER_SECONDS = 600 # автовыключение при бездействии 10 минут

DISABLE_WEB_PREVIEW = True
TELEGRAM_SIGNATURE_HTML = ""  # пусто, чтобы не добавлять ссылки

# -----------------------------------------------------------------------------
# ДЕФОЛТНЫЕ ДАННЫЕ ПОСТА
# -----------------------------------------------------------------------------
fallback_images = [
    "https://upload.wikimedia.org/wikipedia/commons/9/99/Sample_User_Icon.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

post_data = {
    "text_en": "AI Coin blends blockchain with AI for smarter, faster, community-driven decisions.",
    "ai_hashtags": ["#AiCoin", "#AI", "$Ai", "#crypto"],
    "image_url": random.choice(fallback_images),
    "timestamp": None,
    "post_id": 0,
    "is_manual": False
}
prev_data = post_data.copy()

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT, "mode": "normal"}
do_not_disturb = {"active": False}
last_action_time = {}
last_button_pressed_at = None
manual_expected_until = None  # datetime | None

# -----------------------------------------------------------------------------
# МЕНЮ/КНОПКИ
# -----------------------------------------------------------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Пост", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
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

# -----------------------------------------------------------------------------
# TWITTER / GITHUB КЛИЕНТЫ
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
# ПОМОЩНИКИ ПО ТЕКСТУ/ХЭШТЕГАМ/ОГРАНИЧЕНИЮ ДЛИНЫ
# -----------------------------------------------------------------------------
_TCO_LEN = 23
_URL_RE = re.compile(r'https?://\S+', flags=re.UNICODE)
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

def build_twitter_post(ai_text_en: str, ai_hashtags=None) -> str:
    suffix_text = compose_full_text_without_links("", ai_hashtags)
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
# GitHub helpers (хостинг изображений)
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path, filename):
    with open(image_path, "rb") as img_file:
        content = img_file.read()
    try:
        github_repo.create_file(f"{GITHUB_IMAGE_PATH}/{filename}", "upload image for post", content, branch="main")
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        return url
    except Exception as e:
        log.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None

def delete_image_from_github(filename):
    try:
        file_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        contents = github_repo.get_contents(file_path, ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
    except Exception as e:
        log.error(f"Ошибка удаления файла на GitHub: {e}")

# -----------------------------------------------------------------------------
# Изображения: загрузка/сохранение
# -----------------------------------------------------------------------------
async def download_image_async(url_or_file_id, is_telegram_file=False, bot=None, retries=3):
    if is_telegram_file:
        for _ in range(retries):
            try:
                file = await bot.get_file(url_or_file_id)
                tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp_file.name)
                return tmp_file.name
            except Exception as e:
                log.warning(f"download_image_async TG failed: {e}")
                await asyncio.sleep(1)
        raise Exception("Не удалось скачать файл из Telegram")
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
    url, _ = await save_image_and_get_github_url(file_path)
    try:
        os.remove(file_path)
    except Exception:
        pass
    if not url:
        raise Exception("Не удалось загрузить фото на GitHub")
    return url

# -----------------------------------------------------------------------------
# Предпросмотр (две карточки подряд)
# -----------------------------------------------------------------------------
async def preview_split(bot, chat_id, ai_text_en, ai_hashtags=None, image_url=None, header: str | None = None):
    twitter_txt = build_twitter_preview(ai_text_en, ai_hashtags)
    telegram_txt = build_telegram_preview(ai_text_en, ai_hashtags)
    hdr = f"<b>{header}</b>\n" if header else ""

    tw_markup = twitter_preview_keyboard()
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"{hdr}<b>Twitter:</b>\n{twitter_txt}", reply_markup=tw_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"{hdr}<b>Twitter:</b>\n{twitter_txt}",
                                   parse_mode="HTML", reply_markup=tw_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"{hdr}<b>Twitter:</b>\n{twitter_txt}",
                               parse_mode="HTML", reply_markup=tw_markup, disable_web_page_preview=True)

    tg_markup = telegram_preview_keyboard()
    try:
        if image_url:
            await send_photo_with_download(bot, chat_id, image_url, caption=f"{hdr}<b>Telegram:</b>\n{telegram_txt}", reply_markup=tg_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=f"{hdr}<b>Telegram:</b>\n{telegram_txt}",
                                   parse_mode="HTML", reply_markup=tg_markup, disable_web_page_preview=True)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=f"{hdr}<b>Telegram:</b>\n{telegram_txt}",
                               parse_mode="HTML", reply_markup=tg_markup, disable_web_page_preview=True)

# -----------------------------------------------------------------------------
# Отправка фото с локальным скачиванием
# -----------------------------------------------------------------------------
async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    def is_valid_image_url(url):
        try:
            resp = requests.head(url, timeout=5)
            return resp.headers.get('Content-Type', '').startswith('image/')
        except Exception:
            return False
    try:
        if not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            msg = await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
            return msg, url.split('/')[-1]
        else:
            if not is_valid_image_url(url_or_file_id):
                await bot.send_message(chat_id=chat_id, text=caption or "", parse_mode="HTML",
                                       reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
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
                await bot.send_message(chat_id=chat_id, text=caption or "", parse_mode="HTML",
                                       reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
                return None, None
    except Exception as e:
        log.error(f"Ошибка в send_photo_with_download: {e}")
        await bot.send_message(chat_id=chat_id, text=caption or " ",
                               parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=DISABLE_WEB_PREVIEW)
        return None, None

# -----------------------------------------------------------------------------
# БД истории (дедупликация)
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
    import hashlib as _h
    return _h.sha256(data).hexdigest()

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
            log.warning(f"save_post_to_history: возможно дубликат или ошибка вставки: {e}")

# -----------------------------------------------------------------------------
# ИИ-генерация короткого текста и хэштегов
# -----------------------------------------------------------------------------
def _oa_chat_text(prompt: str) -> str:
    try:
        resp = client_oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"You write concise, inspiring social promos for a crypto+AI project called Ai Coin. Avoid the words 'google' or 'trends'. Keep it 1–3 short sentences, energetic, non-technical, in English."},
                {"role":"user","content":prompt}
            ],
            temperature=0.9,
            max_tokens=220,
        )
        txt = (resp.choices[0].message.content or "").strip()
        return txt.strip('"\n` ')
    except Exception as e:
        log.warning(f"_oa_chat_text error: {e}")
        try:
            global OPENAI_QUOTA_WARNED
            if (("429" in str(e)) or ("insufficient_quota" in str(e))) and not OPENAI_QUOTA_WARNED:
                OPENAI_QUOTA_WARNED = True
                asyncio.create_task(
                    approval_bot.send_message(
                        chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                        text="⚠️ OpenAI: insufficient quota (429). Пополните баланс OpenAI, иначе генерация не работает."
                    )
                )
        except Exception:
            pass
        return "Ai Coin fuses AI with blockchain to turn community ideas into real actions. Join builders shaping the next wave of crypto utility."

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

    image_url = random.choice(fallback_images)
    return (text_en, ai_tags, image_url)

# Связь с планировщиком (регистрация генератора)
try:
    if set_ai_generator:
        set_ai_generator(ai_generate_content_en)
        log.info("Planner AI generator registered.")
    else:
        log.info("Planner AI generator not registered (set_ai_generator not found).")
except Exception as e:
    log.warning(f"Cannot register planner AI generator: {e}")

# -----------------------------------------------------------------------------
# Публикация в Twitter (с компрессией изображений)
# -----------------------------------------------------------------------------
def _try_compress_image_inplace(path: str, target_bytes: int = 4_900_000, max_side: int = 2048) -> bool:
    try:
        from PIL import Image
        import os
        initial_size = os.path.getsize(path)
        if initial_size <= target_bytes:
            return True

        img = Image.open(path)
        img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, float(max_side) / float(max(w, h)))
        if scale < 1.0:
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.LANCZOS)

        for q in (85, 80, 75, 70, 65, 60, 55, 50, 45, 40):
            tmp = path + ".tmp.jpg"
            img.save(tmp, format="JPEG", quality=q, optimize=True)
            sz = os.path.getsize(tmp)
            if sz <= target_bytes:
                os.replace(tmp, path)
                return True
        os.replace(tmp, path)
        return os.path.getsize(path) <= target_bytes
    except Exception as e:
        log.warning(f"Pillow недоступен или ошибка сжатия: {e}")
        return False

def _download_to_temp_file(image_url: str) -> str | None:
    try:
        r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(r.content); tmp.close()
        return tmp.name
    except Exception as e:
        log.warning(f"Не удалось скачать картинку для Twitter: {e}")
        return None

def publish_post_to_twitter(text, image_url=None):
    github_filename = None
    try:
        media_ids = None
        final_text = build_twitter_post(text, [])

        if image_url and str(image_url).startswith("http"):
            file_path = _download_to_temp_file(image_url)
            if file_path:
                ok = _try_compress_image_inplace(file_path)
                if not ok:
                    log.warning("Картинку не удалось сжать до лимита — публикуем твит без изображений.")
                    os.remove(file_path)
                    file_path = None

            if file_path:
                try:
                    media = twitter_api_v1.media_upload(filename=file_path)
                    media_ids = [media.media_id_string]
                except Exception as e:
                    if "413" in str(e) or "Payload Too Large" in str(e):
                        log.warning("413 при загрузке в Twitter, пробую сильнее сжать и повторить…")
                        if _try_compress_image_inplace(file_path, target_bytes=3_800_000, max_side=1600):
                            media = twitter_api_v1.media_upload(filename=file_path)
                            media_ids = [media.media_id_string]
                        else:
                            log.warning("Не удалось сжать до безопасного размера — отправляю без изображения.")
                            media_ids = None
                    else:
                        raise
                finally:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        twitter_client_v2.create_tweet(text=final_text, media_ids=media_ids)

        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)
        return True

    except Exception as e:
        log.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"❌ Ошибка при публикации в Twitter: {e}"))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

# -----------------------------------------------------------------------------
# Публикация в Telegram-канал
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text, image_url=None):
    try:
        text_with_signature = (text or "")
        if image_url:
            await send_photo_with_download(
                channel_bot,
                TELEGRAM_CHANNEL_USERNAME_ID,
                image_url,
                caption=text_with_signature,
                reply_markup=None
            )
        else:
            await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=text_with_signature,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        return True
    except Exception as e:
        log.error(f"Ошибка публикации в Telegram: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Telegram: {e}"
        )
        return False

# -----------------------------------------------------------------------------
# Стартовое сообщение (плейсхолдер) + активация таймера
# -----------------------------------------------------------------------------
async def send_start_placeholder():
    text_en = post_data["text_en"]
    ai_tags = post_data.get("ai_hashtags") or []
    img_url = post_data.get("image_url")

    tg_preview = build_telegram_preview(text_en, ai_tags)

    try:
        if img_url:
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                img_url,
                caption=tg_preview,
                reply_markup=get_start_menu()
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=tg_preview,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=get_start_menu()
            )
    except Exception as e:
        log.warning(f"send_start_placeholder image failed, fallback to text: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=tg_preview,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=get_start_menu()
        )

    pending_post.update({"active": True, "timer": datetime.now(TZ), "timeout": TIMER_PUBLISH_DEFAULT, "mode": "placeholder"})

# -----------------------------------------------------------------------------
# Таймеры фоновые (автопост и автошатдаун)
# -----------------------------------------------------------------------------
async def check_timer():
    while True:
        await asyncio.sleep(0.5)
        try:
            if pending_post["active"] and pending_post.get("timer"):
                passed = (datetime.now(TZ) - pending_post["timer"]).total_seconds()
                if passed > pending_post.get("timeout", TIMER_PUBLISH_DEFAULT):
                    if pending_post.get("mode") == "placeholder":
                        base_text_en = (post_data.get("text_en") or "").strip()
                        hashtags = post_data.get("ai_hashtags") or []
                        twitter_text = build_twitter_preview(base_text_en, hashtags)
                        telegram_text = build_telegram_preview(base_text_en, hashtags)

                        tg_ok = await publish_post_to_telegram(telegram_text, post_data.get("image_url"))
                        tw_ok = publish_post_to_twitter(twitter_text, post_data.get("image_url"))

                        await approval_bot.send_message(
                            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                            text=f"Автопост: Telegram — {'✅' if tg_ok else '❌'}, Twitter — {'✅' if tw_ok else '❌'}. Выключаюсь."
                        )
                        shutdown_bot_and_exit()
                    else:
                        pending_post["active"] = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"check_timer error: {e}")

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
            log.warning(f"check_inactivity_shutdown error: {e}")

# === ДАЛЕЕ: callback_handler / handle_manual_input / publish_flow / message_handler
# === on_start / shutdown_bot_and_exit / main — будут в Части 2/2

# =======================  КОНЕЦ ЧАСТИ 1/2  =======================
# --- ОТПРАВКА В TWITTER ---

def post_to_twitter(text, image_path=None):
    try:
        auth = tweepy.OAuth1UserHandler(
            TWITTER_API_KEY, TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET
        )
        api = tweepy.API(auth)
        if image_path:
            media = api.media_upload(image_path)
            api.update_status(status=text, media_ids=[media.media_id])
        else:
            api.update_status(status=text)
        logging.info("Пост отправлен в Twitter.")
    except Exception as e:
        logging.error(f"Ошибка отправки в Twitter: {e}")


# --- ОТПРАВКА В TELEGRAM-КАНАЛ ---

async def post_to_telegram_channel(text, image_path=None):
    try:
        if image_path:
            await channel_bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=open(image_path, "rb"), caption=text)
        else:
            await channel_bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text)
        logging.info("Пост отправлен в Telegram-канал.")
    except Exception as e:
        logging.error(f"Ошибка отправки в Telegram-канал: {e}")


# --- ОБРАБОТКА КНОПОК ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "approve_post":
        post_text = context.user_data.get("post_text")
        image_path = context.user_data.get("post_image")
        post_to_twitter(post_text, image_path)
        await post_to_telegram_channel(post_text, image_path)
        await query.edit_message_caption(caption="✅ Пост опубликован!")
    elif data == "regenerate_post":
        await send_post_for_approval()
    elif data == "cancel_post":
        await query.edit_message_caption(caption="❌ Пост отменён.")


# --- ОТПРАВКА ПОСТА НА СОГЛАСОВАНИЕ ---

async def send_post_for_approval():
    text, image_path = generate_post()
    approval_bot_data["post_text"] = text
    approval_bot_data["post_image"] = image_path

    buttons = [
        [InlineKeyboardButton("✅ Опубликовать", callback_data="approve_post")],
        [InlineKeyboardButton("♻️ Заново", callback_data="regenerate_post")],
        [InlineKeyboardButton("🛑 Отменить", callback_data="cancel_post")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    if image_path:
        await approval_bot.send_photo(chat_id=TELEGRAM_APPROVAL_CHAT_ID, photo=open(image_path, "rb"), caption=text, reply_markup=reply_markup)
    else:
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=text, reply_markup=reply_markup)


# --- MAIN ---

async def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    await send_post_for_approval()
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
    