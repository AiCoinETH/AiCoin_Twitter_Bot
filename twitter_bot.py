# -*- coding: utf-8 -*-
"""
twitter_bot.py — основной бот согласования/генерации/публикации.
Стартует ОДНИМ сообщением: «Предпросмотр» (запланированный авто‑превью поста)
c меню действий, где есть кнопка «🗓 ИИ план на день» (в planner.py).
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
    from planner import set_ai_generator
except ImportError:
    set_ai_generator = None
from planner import USER_STATE as PLANNER_STATE

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s",
)
log = logging.getLogger("twitter_bot")

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID_STR = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")  # @username или id

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = "images_for_posts"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Жёсткие проверки окружения
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

# OpenAI
client_oa = OpenAI(api_key=OPENAI_API_KEY, max_retries=0, timeout=10)
OPENAI_QUOTA_WARNED = False

# Таймеры
TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND  = 600
AUTO_SHUTDOWN_AFTER_SECONDS = 600

DISABLE_WEB_PREVIEW = True
TELEGRAM_SIGNATURE_HTML = ""

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
def start_preview_keyboard():
    # Компактное меню под единый предпросмотр
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter"),
         InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post"),
         InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb"),
         InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def get_start_menu():
    # Запасное «Главное меню» (используется в некоторых ответах)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def post_choice_keyboard():
    return start_preview_keyboard()

def twitter_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
    ])

def telegram_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_to_main")]
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
# ТЕКСТ/ХЭШТЕГИ/ДЛИНЫ
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
# Изображения
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
        tmp_file.write(r.content); tmp_file.close()
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
# ЕДИНЫЙ ПРЕДПРОСМОТР (1 сообщение)
# -----------------------------------------------------------------------------
async def send_single_preview(text_en: str, ai_hashtags=None, image_url=None, header: str | None = "Предпросмотр"):
    caption = build_telegram_preview(text_en, ai_hashtags or [])
    hdr = f"<b>{header}</b>\n" if header else ""
    text = f"{hdr}{caption}".strip()

    try:
        if image_url:
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                image_url,
                caption=text,
                reply_markup=start_preview_keyboard()
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=start_preview_keyboard()
            )
    except Exception as e:
        log.warning(f"send_single_preview failed, fallback to text: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=start_preview_keyboard()
        )

# -----------------------------------------------------------------------------
# Отправка фото c локальным скачиванием
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
# ИИ-генерация
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

# регистрируем генератор для planner.py
try:
    if set_ai_generator:
        set_ai_generator(ai_generate_content_en)
        log.info("Planner AI generator registered.")
    else:
        log.info("Planner AI generator not registered (set_ai_generator not found).")
except Exception as e:
    log.warning(f"Cannot register planner AI generator: {e}")

# -----------------------------------------------------------------------------
# Публикация
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
# СОВМЕСТИМОСТЬ СО СТАРЫМ ПАЙПЛАЙНОМ (если где-то дергают)
# -----------------------------------------------------------------------------
def generate_post(topic_hint: str = "General invite and value."):
    """
    Синхронная обёртка: возвращает (text, image_url).
    На старте не вызываем. Только для внешних вызовов.
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        text_en = post_data.get("text_en") or ""
        tags = post_data.get("ai_hashtags") or []
        img = post_data.get("image_url")
        return build_telegram_post(text_en, tags), img
    else:
        text_en, tags, img = loop.run_until_complete(ai_generate_content_en(topic_hint))
        return build_telegram_post(text_en, tags), img

# -----------------------------------------------------------------------------
# CALLBACKS / INPUT / FLOW
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, last_action_time, manual_expected_until
    query = update.callback_query
    data = query.data
    await query.answer()

    # Всё «планировочное» — отдаём planner.py (group=0)
    planner_exact = {
        "PLAN_OPEN", "OPEN_PLAN_MODE", "OPEN_GEN_MODE",
        "PLAN_DONE", "GEN_DONE", "PLAN_ADD_MORE", "GEN_ADD_MORE",
        "STEP_BACK", "PLAN_LIST_TODAY", "PLAN_AI_BUILD_NOW",
        "BACK_MAIN_MENU"
    }
    planner_prefixes = (
        "PLAN_", "ITEM_MENU:", "DEL_ITEM:", "EDIT_TIME:", "EDIT_ITEM:",
        "EDIT_FIELD:", "AI_FILL_TEXT:", "CLONE_ITEM:", "AI_NEW_FROM:"
    )
    if (data in planner_exact) or any(data.startswith(p) for p in planner_prefixes):
        return

    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post["active"] = True
    pending_post["timer"] = now
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    user_id = update.effective_user.id
    if user_id in last_action_time and (now - last_action_time[user_id]).seconds < 1:
        return
    last_action_time[user_id] = now

    if data == "show_day_plan":
        manual_expected_until = None
        return await open_planner(update, context)

    if data == "shutdown_bot":
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🔴 Бот выключен.")
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data in ("cancel_to_main", "BACK_MAIN_MENU"):
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "self_post":
        # Сброс состояния планировщика (чтобы не перехватывал ручной ввод)
        try:
            uid = update.effective_user.id
            st = PLANNER_STATE.get(uid)
            if st:
                cur = st.get("current")
                if cur:
                    cur.mode = "none"
                    cur.step = "idle"
                    cur.text = None
                    cur.topic = None
                    cur.time_str = None
                    cur.image_url = None
                st["mode"] = "none"
        except Exception:
            pass

        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Введите текст поста (EN) и (опционально) приложите фото одним сообщением:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]])
        )
        manual_expected_until = now + timedelta(minutes=5)
        return

    if data == "approve":
        await send_single_preview(
            post_data.get("text_en") or "",
            post_data.get("ai_hashtags") or [],
            image_url=post_data.get("image_url"),
            header="Предпросмотр"
        )
        return

    if data in ("post_twitter", "post_telegram", "post_both"):
        publish_tg = data in ("post_telegram", "post_both")
        publish_tw = data in ("post_twitter", "post_both")
        await publish_flow(publish_tg=publish_tg, publish_tw=publish_tw)
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=get_start_menu())
        return

    if data == "end_day":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}",
            parse_mode="HTML", reply_markup=get_start_menu())
        return

# --- Ручной ввод ---
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
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
            log.warning(f"handle_manual_input: cannot process photo: {e}")
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось обработать фото. Пришлите ещё раз или только текст.")
            manual_expected_until = None
            return
    elif getattr(update.message, "document", None) and getattr(update.message.document, "mime_type", ""):
        if update.message.document.mime_type.startswith("image/"):
            try:
                image_url = await process_telegram_photo(update.message.document.file_id, approval_bot)
            except Exception as e:
                log.warning(f"handle_manual_input: cannot process image document: {e}")
                await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось обработать изображение-документ. Пришлите ещё раз или только текст.")
                manual_expected_until = None
                return

    post_data["text_en"] = text.strip() or post_data.get("text_en") or ""
    post_data["image_url"] = image_url if image_url else post_data.get("image_url")
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    try:
        await send_single_preview(
            post_data["text_en"],
            post_data.get("ai_hashtags") or [],
            image_url=post_data["image_url"],
            header="Предпросмотр"
        )
    except Exception as e:
        log.error(f"handle_manual_input preview failed: {e}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="❌ Не удалось показать предпросмотр. Попробуйте снова.")
    finally:
        manual_expected_until = None

# --- Публикация ---
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()
    ai_tags = post_data.get("ai_hashtags") or []
    img = post_data.get("image_url") or None

    twitter_text = build_twitter_preview(base_text_en, ai_tags)
    telegram_text = build_telegram_preview(base_text_en, ai_tags)

    tg_status = None
    tw_status = None

    if do_not_disturb["active"]:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

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
            tw_status = publish_post_to_twitter(twitter_text, img)
            if tw_status: await save_post_to_history(twitter_text, img)

    if publish_tg:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram.")
    if publish_tw:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter.")

    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Главное меню:", reply_markup=get_start_menu())

# --- Маршрутизация сообщений ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, manual_expected_until
    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post["active"] = True
    pending_post["timer"] = now
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    # 1) «Сделай сам» — ручной режим
    if manual_expected_until and now <= manual_expected_until:
        return await handle_manual_input(update, context)

    # 2) если планировщик активен — он перехватит (group=0)
    try:
        uid = update.effective_user.id
        st = PLANNER_STATE.get(uid) or {}
        cur = st.get("current")
        cur_mode = getattr(cur, "mode", "none") if cur else "none"
        cur_step = getattr(cur, "step", "idle") if cur else "idle"
        if (cur_mode in ("plan", "gen", "edit")) or (cur_step in (
            "waiting_topic", "waiting_text", "waiting_time",
            "editing_time", "editing_text", "editing_topic", "editing_image"
        )):
            return
    except Exception:
        pass

    # 3) иначе — ручной ввод
    return await handle_manual_input(update, context)

# -----------------------------------------------------------------------------
# STARTUP / SHUTDOWN / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()

    # Генерим авто‑контент для стартового предпросмотра (с фоллбэком при 429)
    try:
        text_en, ai_tags, img = await ai_generate_content_en("General invite and value.")
    except Exception:
        text_en, ai_tags, img = post_data["text_en"], post_data.get("ai_hashtags") or [], post_data.get("image_url")

    post_data["text_en"] = text_en
    post_data["ai_hashtags"] = ai_tags
    post_data["image_url"] = img

    # ЕДИНЫЙ запланированный предпросмотр (ровно одно сообщение)
    await send_single_preview(post_data["text_en"], post_data["ai_hashtags"], image_url=post_data["image_url"], header="Предпросмотр")

    log.info("Бот запущен. Отправлен ЕДИНЫЙ запланированный предпросмотр. Планирование — в planner.py.")

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

def shutdown_bot_and_exit():
    try:
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="🔴 Бот полностью выключен. GitHub Actions больше не тратит минуты!")
        )
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

def main():
    app = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    # Планировщик
    register_planner_handlers(app)

    # Наши обработчики
    app.add_handler(CallbackQueryHandler(callback_handler), group=5)
    app.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, message_handler),
        group=10
    )

    # Фоновый авто‑выключатель
    asyncio.get_event_loop().create_task(check_inactivity_shutdown())

    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()