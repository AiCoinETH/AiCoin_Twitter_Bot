# -*- coding: utf-8 -*-
"""
twitter_bot.py — основной бот согласования/генерации/публикации.
Стартует ОДНИМ сообщением: «Предпросмотр» (запланированный авто-превью поста)
c меню действий, где есть кнопка «🗓 ИИ план на день» (в planner.py).

Изменения:
- Twitter: жёсткий лимит 275 с учётом хвоста (site + TG + базовые хэштеги, дедуп).
- Telegram: НИ ОДНОГО хэштега; внизу только кликабельные ссылки:
  "website: https://getaicoin.com | Twitter: https://x.com/AiCoin_ETH".
- Исправлена публикация в X/Twitter: корректные параметры Tweepy v2 (media / media_ids)
  с автоматическим фолбэком и безопасной обработкой ограничений доступа.
- Добавлена publish_post_to_telegram (её раньше не было, но вызывалась).
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
from typing import Optional, Tuple, List, Dict, Any

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

if LOG_LEVEL == "DEBUG":
    logging.getLogger("telegram").setLevel(logging.DEBUG)
    logging.getLogger("telegram.ext").setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.INFO)

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

# --- Cloudflare Worker --------------------------------------------------------
# Базовый URL воркера (без секрета в самом значении):
AICOIN_WORKER_URL = os.getenv(
    "AICOIN_WORKER_URL",
    "https://aicoin-bot-trigger.dfosjam.workers.dev/tg/webhook"
)
# Секрет для публичного GET-триггера (?s=...)
PUBLIC_TRIGGER_SECRET = os.getenv("PUBLIC_TRIGGER_SECRET", "").strip()
# РЕЗЕРВ, если ENV не подхватился
FALLBACK_PUBLIC_TRIGGER_SECRET = "z8PqH0e4jwN3rA1K"
# Старый секрет для заголовка X-Telegram-Bot-Api-Secret-Token (если понадобится POST):
AICOIN_WORKER_SECRET = os.getenv("AICOIN_WORKER_SECRET") or TELEGRAM_BOT_TOKEN_APPROVAL

def _worker_url_with_secret() -> str:
    """Всегда добавляет ?s=<секрет> к URL воркера (берёт из ENV или из fallback)."""
    base = AICOIN_WORKER_URL or ""
    if not base:
        return base
    sec = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
    if not sec:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}s={sec}"

# Жёсткие проверки окружения
missing_env = []
for k in ("TELEGRAM_BOT_TOKEN_APPROVAL","TELEGRAM_APPROVAL_CHAT_ID",
          "TELEGRAM_BOT_TOKEN_CHANNEL","TELEGRAM_CHANNEL_USERNAME_ID"):
    if not os.getenv(k): missing_env.append(k)
if missing_env:
    log.error(f"Не заданы обязательные переменные окружения Telegram: {missing_env}")
    sys.exit(1)
TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)

for k in ("TWITTER_API_KEY","TWITTER_API_SECRET","TWITTER_ACCESS_TOKEN","TWITTER_ACCESS_TOKEN_SECRET"):
    if not os.getenv(k):
        log.error(f"Не заданы обязательные переменные окружения для Twitter: {k}")
        sys.exit(1)

for k in ("ACTION_PAT_GITHUB","ACTION_REPO_GITHUB"):
    if not os.getenv(k):
        log.error(f"Не заданы обязательные переменные окружения GitHub: {k}")
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

client_oa = OpenAI(api_key=OPENAI_API_KEY, max_retries=0, timeout=10)
OPENAI_QUOTA_WARNED = False

TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND  = 600
AUTO_SHUTDOWN_AFTER_SECONDS = 600

DISABLE_WEB_PREVIEW = True

# -----------------------------------------------------------------------------
# ДЕФОЛТНЫЕ ДАННЫЕ ПОСТА
# -----------------------------------------------------------------------------
fallback_images = [
    "https://upload.wikimedia.org/wikipedia/commons/9/99/Sample_User_Icon.png",
    "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/d/d6/Wp-w4-big.jpg"
]

post_data: Dict[str, Any] = {
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
last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None  # datetime | None

ROUTE_TO_PLANNER: set[int] = set()

# -----------------------------------------------------------------------------
# УТИЛИТЫ ЛОГИРОВАНИЯ
# -----------------------------------------------------------------------------
def _planner_snapshot(uid: int) -> str:
    st = PLANNER_STATE.get(uid) or {}
    cur = st.get("current")
    mode = getattr(cur, "mode", "none") if cur else "none"
    step = getattr(cur, "step", "idle") if cur else "idle"
    return f"planner.mode={mode}, planner.step={step}"

def _route_snapshot(uid: int) -> str:
    in_router = uid in ROUTE_TO_PLANNER
    manual = (manual_expected_until and datetime.now(TZ) <= manual_expected_until)
    return f"in_ROUTER={in_router}, manual_expected={bool(manual)}, DND={do_not_disturb['active']}"

def _dbg_where(update: Update) -> str:
    typ = "unknown"
    if update.callback_query:
        typ = f"CB:{update.callback_query.data}"
    elif update.message:
        kinds = []
        if update.message.text: kinds.append("text")
        if update.message.photo: kinds.append("photo")
        if getattr(update.message, 'document', None): kinds.append("doc")
        typ = "MSG:" + "+".join(kinds)
    return typ

# -----------------------------------------------------------------------------
# МЕНЮ/КНОПКИ
# -----------------------------------------------------------------------------
def start_preview_keyboard():
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

# ---------- Хвосты ----------
TW_TAIL_REQUIRED = "🌐 https://getaicoin.com | 💬 @AiCoin_ETH"
TG_LINKS_TAIL = "website: https://getaicoin.com | Twitter: https://x.com/AiCoin_ETH"

def build_tweet_with_tail_275(body_text: str, ai_tags: List[str] | None) -> str:
    """
    Лимит 275. Хвост: сайт + @AiCoin_ETH + (если влезут) базовые/динамические хэштеги.
    """
    MAX_TWEET_SAFE = 275
    tail_required = TW_TAIL_REQUIRED
    tags_str = _dedup_hashtags(MY_HASHTAGS_STR, ai_tags or [])
    tail_full = (tail_required + (f" {tags_str}" if tags_str else "")).strip()
    body = (body_text or "").strip()

    def compose(b, t):
        return f"{b} {t}".strip() if (b and t) else (b or t)

    allowed_for_body = MAX_TWEET_SAFE - (1 if (body and tail_full) else 0) - twitter_len(tail_full)
    if allowed_for_body < 0:
        # не влезают хэштеги — оставляем только обязательный хвост
        tail = tail_required
        allowed_for_body = MAX_TWEET_SAFE - (1 if (body and tail) else 0) - twitter_len(tail)
    else:
        tail = tail_full

    body_trimmed = trim_to_twitter_len(body, allowed_for_body)
    tweet = compose(body_trimmed, tail)

    while twitter_len(tweet) > MAX_TWEET_SAFE and body_trimmed:
        body_trimmed = trim_to_twitter_len(body_trimmed[:-1], allowed_for_body)
        tweet = compose(body_trimmed, tail)

    if twitter_len(tweet) > MAX_TWEET_SAFE:
        tweet = tail_required
    return tweet

def build_telegram_text_no_hashtags(ai_text_en: str) -> str:
    """
    Телеграм без хэштегов. Внизу кликабельные ссылки website | Twitter.
    """
    body = trim_plain_to((ai_text_en or "").strip(), 2000)
    if body:
        return f"{body}\n\n{TG_LINKS_TAIL}"
    return TG_LINKS_TAIL

# ------ Превью/Компоновка ------
def build_twitter_preview(ai_text_en: str, ai_hashtags=None) -> str:
    return build_tweet_with_tail_275(ai_text_en, ai_hashtags or [])

def build_telegram_preview(ai_text_en: str, _ai_hashtags_ignored=None) -> str:
    # Хештеги в Телеграм НЕ используются
    return build_telegram_text_no_hashtags(ai_text_en)

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
# ЕДИНЫЙ ПРЕДПРОСМОТР
# -----------------------------------------------------------------------------
async def send_single_preview(text_en: str, ai_hashtags=None, image_url=None, header: str | None = "Предпросмотр"):
    caption = build_telegram_preview(text_en, ai_hashtags or [])
    hdr = f"<b>{header}</b>\n" if header else ""
    text = f"{hdr}{caption}".strip()

    log.debug(f"[send_single_preview] image_url={bool(image_url)} len(text)={len(text)}")
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
                disable_web_page_preview=False,  # ссылки кликабельные
                reply_markup=start_preview_keyboard()
            )
    except Exception as e:
        log.warning(f"send_single_preview failed, fallback to text: {e}")
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
            reply_markup=start_preview_keyboard()
        )

# -----------------------------------------------------------------------------
# Отправка фото
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
                                       reply_markup=reply_markup, disable_web_page_preview=False)
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
                                       reply_markup=reply_markup, disable_web_page_preview=False)
                return None, None
    except Exception as e:
        log.error(f"Ошибка в send_photo_with_download: {e}")
        await bot.send_message(chat_id=chat_id, text=caption or " ",
                               parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=False)
        return None, None

# -----------------------------------------------------------------------------
# БД истории (дедуп)
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
    log.debug("[init_db] ok")

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
            log.warning(f"save_post_to_history: возможно дубликат/ошибка вставки: {e}")

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

async def ai_generate_content_en(topic_hint: str) -> Tuple[str, List[str], Optional[str]]:
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

try:
    if set_ai_generator:
        set_ai_generator(ai_generate_content_en)
        log.info("Planner AI generator registered.")
    else:
        log.info("Planner AI generator not registered (set_ai_generator not found).")
except Exception as e:
    log.warning(f"Cannot register planner AI generator: {e}")

# -----------------------------------------------------------------------------
# Публикация: Telegram
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text: str, image_url: Optional[str] = None) -> bool:
    """
    Публикуем в канал Telegram (без хэштегов; текст уже содержит хвост ссылок).
    """
    try:
        # Если ссылка на картинку корректна — отправляем фото, иначе текст
        if image_url and image_url.startswith("http"):
            try:
                r = requests.head(image_url, timeout=5)
                if r.ok and r.headers.get("Content-Type","").startswith("image/"):
                    await channel_bot.send_photo(
                        chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                        photo=image_url,
                        caption=text,
                        parse_mode="HTML",
                        disable_notification=False
                    )
                    return True
            except Exception:
                pass
        # Фолбэк: просто текст
        await channel_bot.send_message(
            chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        return True
    except Exception as e:
        log.error(f"Ошибка публикации в Telegram: {e}")
        return False

# -----------------------------------------------------------------------------
# Публикация: Twitter / X
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

def _download_to_temp_file(image_url: str) -> Optional[str]:
    try:
        r = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(r.content); tmp.close()
        return tmp.name
    except Exception as e:
        log.warning(f"Не удалось скачать картинку для Twitter: {e}")
        return None

def publish_post_to_twitter(text_en: str, image_url=None, ai_hashtags=None):
    """
    Публикация в X/Twitter.
    - Жёсткий лимит 275 с учётом хвоста (site + TG + базовые/динамические хэштеги).
    - Гибкая поддержка Tweepy v2: media={"media_ids": [...]} ИЛИ media_ids=[...].
    - Если доступ к эндпоинтам ограничен (403/453) — отдаём понятное сообщение в апрув-чат.
    """
    github_filename = None
    try:
        # Финальный текст
        final_text = build_tweet_with_tail_275(text_en, ai_hashtags or [])

        # --- Загрузка изображения через API v1 (media_upload) ---
        media_ids = None
        if image_url and str(image_url).startswith("http"):
            file_path = _download_to_temp_file(image_url)
            if file_path:
                ok = _try_compress_image_inplace(file_path)
                if not ok:
                    log.warning("Картинку не удалось сжать до лимита — публикуем твит без изображения.")
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

        # --- Публикация через v2 (user context) ---
        # Попытка №1: современный параметр "media"
        try:
            if media_ids:
                twitter_client_v2.create_tweet(text=final_text, media={"media_ids": media_ids})
            else:
                twitter_client_v2.create_tweet(text=final_text)
        except TypeError as e_wrong_sig:
            # Старый tweepy: попробуем media_ids=[]
            log.warning(f"Tweepy create_tweet сигнатура не приняла 'media': {e_wrong_sig} -> retry with media_ids")
            if media_ids:
                twitter_client_v2.create_tweet(text=final_text, media_ids=media_ids)
            else:
                twitter_client_v2.create_tweet(text=final_text)
        except Exception as e_v2:
            # Если v2 не доступен — пробуем v1.1 update_status, зная что он может быть закрыт на вашем тарифе.
            log.warning(f"API v2 не сработал, пробуем через API v1: {e_v2}")
            try:
                if media_ids:
                    twitter_api_v1.update_status(status=final_text, media_ids=media_ids)
                else:
                    twitter_api_v1.update_status(status=final_text)
            except Exception as e_v1:
                # Сообщаем понятнее про лимиты X API
                msg = (
                    "❌ Публикация в X недоступна для текущего уровня доступа API.\n\n"
                    f"Ошибка v2: {e_v2}\nОшибка v1: {e_v1}\n\n"
                    "Поднимите доступ (X API) или включите соответствующие эндпоинты."
                )
                log.error(f"publish_post_to_twitter: {msg}")
                asyncio.create_task(
                    approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=msg)
                )
                # Если изображение было с GitHub — подчистим
                if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
                    github_filename = image_url.split('/')[-1]
                    delete_image_from_github(github_filename)
                return False

        # Удаляем raw-файл из GitHub, если использовали его URL
        if image_url and image_url.startswith(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/"):
            github_filename = image_url.split('/')[-1]
            delete_image_from_github(github_filename)

        return True

    except Exception as e:
        log.error(f"Ошибка публикации в Twitter: {e}")
        asyncio.create_task(approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"❌ Ошибка при публикации в Twitter: {e}"
        ))
        if github_filename:
            delete_image_from_github(github_filename)
        return False

# -----------------------------------------------------------------------------
# TRIGGER WORKER (возвращаем текст воркера)
# -----------------------------------------------------------------------------
async def trigger_worker() -> Tuple[bool, str]:
    """
    Запуск воркера.
    Если есть секрет (из ENV или FALLBACK_PUBLIC_TRIGGER_SECRET) — используем GET ?s=...
    иначе — пробуем старый POST с X-Telegram-Bot-Api-Secret-Token.
    """
    if not AICOIN_WORKER_URL:
        return False, "AICOIN_WORKER_URL не задан."
    try:
        sec = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
        if sec:
            url = _worker_url_with_secret()
            resp = await asyncio.to_thread(requests.get, url, timeout=20)
            if 200 <= resp.status_code < 300:
                body = (resp.text or "").strip()
                return True, (body or f"Воркер ответил {resp.status_code}")
            return False, f"{resp.status_code}: {resp.text[:300]}"
        else:
            ts = int(datetime.now(TZ).timestamp())
            payload = {
                "update_id": ts,
                "message": {
                    "message_id": ts,
                    "date": ts,
                    "chat": {"id": TELEGRAM_APPROVAL_CHAT_ID},
                    "text": "ping-from-approval-bot"
                }
            }
            headers = {}
            if AICOIN_WORKER_SECRET:
                headers["X-Telegram-Bot-Api-Secret-Token"] = AICOIN_WORKER_SECRET

            resp = await asyncio.to_thread(
                requests.post,
                AICOIN_WORKER_URL,
                json=payload,
                headers=headers,
                timeout=20
            )
            if 200 <= resp.status_code < 300:
                body = (resp.text or "").strip()
                return True, (body or f"Воркер ответил {resp.status_code}")
            return False, f"{resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, f"Ошибка: {e}"

# -----------------------------------------------------------------------------
# СОВМЕСТИМОСТЬ СО СТАРЫМ ПАЙПЛАЙНОМ
# -----------------------------------------------------------------------------
def generate_post(topic_hint: str = "General invite and value."):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        text_en = post_data.get("text_en") or ""
        tags = post_data.get("ai_hashtags") or []
        img = post_data.get("image_url")
        return build_telegram_preview(text_en, tags), img
    else:
        text_en, tags, img = loop.run_until_complete(ai_generate_content_en(topic_hint))
        return build_telegram_preview(text_en, tags), img

# -----------------------------------------------------------------------------
# CALLBACKS / INPUT / FLOW
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, last_action_time, manual_expected_until
    query = update.callback_query
    data = query.data
    uid = update.effective_user.id

    log.debug(f"[callback_handler:IN] data={data} | {_planner_snapshot(uid)} | {_route_snapshot(uid)}")
    await query.answer()

    planner_exact = {
        "PLAN_OPEN", "OPEN_PLAN_MODE", "OPEN_GEN_MODE",
        "PLAN_DONE", "GEN_DONE", "PLAN_ADD_MORE", "GEN_ADD_MORE",
        "STEP_BACK", "PLAN_LIST_TODAY", "PLAN_AI_BUILD_NOW",
        "BACK_MAIN_MENU", "ITEM_MENU", "DEL_ITEM", "EDIT_TIME", "EDIT_ITEM"
    }
    planner_prefixes = (
        "PLAN_", "ITEM_MENU:", "DEL_ITEM:", "EDIT_TIME:", "EDIT_ITEM:",
        "EDIT_FIELD:", "AI_FILL_TEXT:", "CLONE_ITEM:", "AI_NEW_FROM:"
    )
    if (data in planner_exact) or any(data.startswith(p) for p in planner_prefixes):
        log.debug(f"[callback_handler] Routed to planner (skip).")
        return

    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post["active"] = True
    pending_post["timer"] = now
    pending_post["timeout"] = TIMER_PUBLISH_EXTEND
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if uid in last_action_time and (now - last_action_time[uid]).seconds < 1:
        log.debug("[callback_handler] Debounced duplicate click")
        return
    last_action_time[uid] = now

    if data == "show_day_plan":
        manual_expected_until = None
        ROUTE_TO_PLANNER.add(uid)
        log.debug(f"[callback_handler] -> open_planner; ROUTE_TO_PLANNER.add({uid})")
        return await open_planner(update, context)

    if data == "shutdown_bot":
        ROUTE_TO_PLANNER.discard(uid)
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        msg = (
            "🔴 Бот выключен.\n"
            f"Следующий пост запланирован: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}\n\n"
            "Чтобы перезапустить обработчик вручную, нажмите «▶️ Старт воркера»."
        )
        # URL-кнопка с секретом:
        start_url = _worker_url_with_secret()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Старт воркера", url=start_url)]])
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=msg, reply_markup=kb)
        finally:
            await asyncio.sleep(1)
            shutdown_bot_and_exit()
        return

    if data in ("cancel_to_main", "BACK_MAIN_MENU"):
        ROUTE_TO_PLANNER.discard(uid)
        log.debug(f"[callback_handler] Back to main; ROUTE_TO_PLANNER.discard({uid})")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "self_post":
        ROUTE_TO_PLANNER.discard(uid)
        log.debug("[callback_handler] self_post -> manual flow; ROUTE cleared")
        try:
            st = PLANNER_STATE.get(uid)
            if st:
                cur = st.get("current")
                if cur:
                    cur.mode = "none"; cur.step = "idle"
                    cur.text = None; cur.topic = None
                    cur.time_str = None; cur.image_url = None
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
        log.debug("[callback_handler] approve -> send_single_preview")
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
        log.debug(f"[callback_handler] publish_flow tg={publish_tg} tw={publish_tw}")
        await publish_flow(publish_tg=publish_tg, publish_tw=publish_tw)
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        log.debug(f"[callback_handler] DND -> {status}")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=get_start_menu())
        return

    if data == "end_day":
        ROUTE_TO_PLANNER.discard(uid)
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        log.debug("[callback_handler] end_day -> DND on & main menu")
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔚 Работа завершена на сегодня.\nСледующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}",
            parse_mode="HTML", reply_markup=get_start_menu())
        return

    if data == "start_worker":
        ok, info = await trigger_worker()
        prefix = "✅ Запуск воркера: " if ok else "❌ Запуск воркера: "
        text_msg = info if (ok and (info or "").strip().startswith("✅")) else (prefix + info)
        try:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=text_msg)
        finally:
            await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        log.debug(f"[callback_handler] start_worker -> {ok} {info}")
        return

    log.debug(f"[callback_handler:OUT] unhandled data={data}")

# --- Ручной ввод ---
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
    uid = update.effective_user.id
    log.debug(f"[handle_manual_input:IN] {_planner_snapshot(uid)} | {_route_snapshot(uid)}")

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
        log.debug("[handle_manual_input:OUT] preview sent")

# --- Публикация ---
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()
    ai_tags = post_data.get("ai_hashtags") or []
    img = post_data.get("image_url") or None

    twitter_text = build_twitter_preview(base_text_en, ai_tags)
    telegram_text = build_telegram_preview(base_text_en, None)

    if do_not_disturb["active"]:
        log.debug("[publish_flow] DND active -> cancel")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

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
            tw_status = publish_post_to_twitter(twitter_text, img, ai_tags)
            if tw_status: await save_post_to_history(twitter_text, img)

    if publish_tg:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram.")
    if publish_tw:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter.")

    log.debug(f"[publish_flow] result tg={tg_status} tw={tw_status}")
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# STARTUP / SHUTDOWN / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    try:
        text_en, ai_tags, img = await ai_generate_content_en("General invite and value.")
    except Exception as e:
        log.warning(f"ai_generate_content_en failed at start: {e}")
        text_en, ai_tags, img = post_data["text_en"], post_data.get("ai_hashtags") or [], post_data.get("image_url")

    post_data["text_en"] = text_en
    post_data["ai_hashtags"] = ai_tags
    post_data["image_url"] = img

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
    log.debug("[main] building Application…")
    app = (
        Application
        .builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    register_planner_handlers(app)

    app.add_handler(CallbackQueryHandler(callback_handler), group=5)
    app.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, message_handler),
        group=10
    )

    asyncio.get_event_loop().create_task(check_inactivity_shutdown())

    log.debug("[main] run_polling…")
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()