# -*- coding: utf-8 -*-
"""
twitter_bot.py — согласование/генерация/публикация в Telegram и X (Twitter).

ВАЖНОЕ НОВОЕ:
- 🔖 Кнопка «Хэштеги» доступна всегда (в предпросмотре и в главном меню).
- Хэштеги можно вводить в любом месте: "#AiCoin #AI $Ai #crypto #DeFi".
- В Twitter (VERBATIM_MODE=False) хвост + дедуп‑хэштеги под лимит 275.
- В Telegram хвост гарантирован, хэштеги в текст не навязываем (по желанию).
"""

import os
import re
import sys
import uuid
import asyncio
import logging
import tempfile
from html import escape as html_escape
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

import requests
import tweepy
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import aiosqlite
from github import Github
from openai import OpenAI  # openai>=1.35.0

# === ПЛАНИРОВЩИК (опционально) ===
try:
    from planner import register_planner_handlers, open_planner, set_ai_generator, USER_STATE as PLANNER_STATE
except Exception:
    register_planner_handlers = lambda app: None
    open_planner = None
    set_ai_generator = None
    PLANNER_STATE = {}

# -----------------------------------------------------------------------------
# ЛОГИ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("twitter_bot")

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

AICOIN_WORKER_URL = os.getenv("AICOIN_WORKER_URL", "https://aicoin-bot-trigger.dfosjam.workers.dev/tg/webhook")
PUBLIC_TRIGGER_SECRET = (os.getenv("PUBLIC_TRIGGER_SECRET") or "").strip()
AICOIN_WORKER_SECRET = os.getenv("AICOIN_WORKER_SECRET") or TELEGRAM_BOT_TOKEN_APPROVAL
FALLBACK_PUBLIC_TRIGGER_SECRET = "z8PqH0e4jwN3rA1K"

need_env = [
    "TELEGRAM_BOT_TOKEN_APPROVAL", "TELEGRAM_APPROVAL_CHAT_ID",
    "TELEGRAM_BOT_TOKEN_CHANNEL", "TELEGRAM_CHANNEL_USERNAME_ID",
    "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
    "ACTION_PAT_GITHUB", "ACTION_REPO_GITHUB", "OPENAI_API_KEY"
]
missing = [k for k in need_env if not os.getenv(k)]
if missing:
    log.error(f"Не заданы обязательные переменные окружения: {missing}")
    sys.exit(1)

TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)

# -----------------------------------------------------------------------------
# ГЛОБАЛЫ
# -----------------------------------------------------------------------------
TZ = ZoneInfo("Europe/Kyiv")
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

client_oa = OpenAI(api_key=OPENAI_API_KEY, max_retries=0, timeout=10)
OPENAI_QUOTA_WARNED = False

TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND = 600
AUTO_SHUTDOWN_AFTER_SECONDS = 600

VERBATIM_MODE = False  # X: как написал — так и публикуем (False = с хвостом)

# -----------------------------------------------------------------------------
# ХВОСТЫ
# -----------------------------------------------------------------------------
TW_TAIL_REQUIRED = "🌐 https://getaicoin.com | 🐺 https://t.me/AiCoin_ETH"
TG_TAIL_HTML = '<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/AiCoin_ETH">Twitter X</a>'

# -----------------------------------------------------------------------------
# Twitter API клиенты
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
            TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return client_v2, api_v1

twitter_client_v2, twitter_api_v1 = get_twitter_clients()

# GitHub (для предпросмотра изображений из TG)
github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

# -----------------------------------------------------------------------------
# СТЕЙТ
# -----------------------------------------------------------------------------
post_data: Dict[str, Any] = {
    "text_en": "",
    "ai_hashtags": [],          # <— ХЭШТЕГИ ТУТ
    "media_kind": "none",       # "none" | "image" | "video"
    "media_src": "tg",          # "tg" | "url"
    "media_ref": None,
    "media_local_path": None,
    "timestamp": None,
    "post_id": 0,
    "is_manual": False
}
prev_data = post_data.copy()

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT, "mode": "normal"}
do_not_disturb = {"active": False}
last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None
ROUTE_TO_PLANNER: set[int] = set()

# -----------------------------------------------------------------------------
# УТИЛИТЫ ДЛИНЫ / ДЕДУП ХЭШТЕГОВ (для X)
# -----------------------------------------------------------------------------
_TCO_LEN = 23
_URL_RE = re.compile(r'https?://\S+', flags=re.UNICODE)
MY_HASHTAGS_STR = "#AiCoin #AI $Ai #crypto"

def twitter_len(s: str) -> int:
    if not s: return 0
    s = normalize("NFC", s)
    return len(_URL_RE.sub('X' * _TCO_LEN, s))

def trim_to_twitter_len(s: str, max_len: int) -> str:
    if not s: return s
    s = normalize("NFC", s).strip()
    if twitter_len(s) <= max_len: return s
    ell = '…'
    while s and twitter_len(s + ell) > max_len:
        s = s[:-1]
    return (s + ell).rstrip()

def _parse_tags_to_list(raw: str) -> List[str]:
    if not raw: return []
    # допускаем "#AiCoin, ai, $ai" и т.п.
    raw = raw.replace(",", " ").replace(";", " ")
    out = []
    for tok in raw.split():
        tok = tok.strip()
        if not tok: continue
        if not (tok.startswith("#") or tok.startswith("$")):
            tok = "#" + tok
        # фильтруем пустые и слишком длинные
        if len(tok) <= 50:
            out.append(tok)
    return out

def _dedup_hashtags(*groups):
    seen, out = set(), []
    def ok(t: str) -> bool:
        tl = t.lower()
        return ("ai" in tl) or ("crypto" in tl) or tl.startswith("$ai")
    for g in groups:
        if not g: continue
        items = g.split() if isinstance(g, str) else list(g)
        for raw in items:
            tag = raw.strip()
            if not tag: continue
            if not (tag.startswith("#") or tag.startswith("$")):
                tag = "#" + tag
            if not ok(tag): continue
            key = tag.lower()
            if key in seen: continue
            seen.add(key); out.append(tag)
    return " ".join(out)

def build_tweet_with_tail_275(body_text: str, ai_tags: List[str] | None) -> str:
    MAX_TWEET_SAFE = 275
    tail_required = TW_TAIL_REQUIRED
    tags_str = _dedup_hashtags(MY_HASHTAGS_STR, ai_tags or [])
    tail_full = (tail_required + (f" {tags_str}" if tags_str else "")).strip()
    body = (body_text or "").strip()

    def compose(b, t): return f"{b} {t}".strip() if (b and t) else (b or t)

    allowed_for_body = MAX_TWEET_SAFE - (1 if (body and tail_full) else 0) - twitter_len(tail_full)
    if allowed_for_body < 0:
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

def build_twitter_text(text_en: str, ai_hashtags=None) -> str:
    return (text_en or "").strip() if VERBATIM_MODE else build_tweet_with_tail_275(text_en, ai_hashtags or [])

# -----------------------------------------------------------------------------
# TG: гарантированный хвост
# -----------------------------------------------------------------------------
TG_CAPTION_MAX = 1024
TG_TEXT_MAX = 4096

def _has_tail(html_text_lower: str) -> bool:
    return ("getaicoin.com" in html_text_lower) and ("x.com/aicoin_eth" in html_text_lower)

def build_tg_final(body_text: str | None, for_photo_caption: bool) -> str:
    body_raw = (body_text or "").strip()
    body_html = html_escape(body_raw)
    tail_html = TG_TAIL_HTML
    limit = TG_CAPTION_MAX if for_photo_caption else TG_TEXT_MAX

    current_full = body_html
    if not _has_tail(current_full.lower()):
        sep = ("\n\n" if body_html else "")
        reserved = len(sep) + len(tail_html)
        allowed_for_body = max(0, limit - reserved)
        if len(body_html) > allowed_for_body:
            body_html = body_html[:allowed_for_body].rstrip()
        current_full = (f"{body_html}{sep}{tail_html}").strip()

    if len(current_full) > limit:
        current_full = current_full[:limit].rstrip()
    return current_full

def build_telegram_preview(text_en: str, _ai_hashtags_ignored=None) -> str:
    # Хэштеги не добавляем насильно в Telegram — только если автор сам включил их в текст
    return build_tg_final(text_en, for_photo_caption=False)

# -----------------------------------------------------------------------------
# GitHub helpers (для предпросмотра TG‑фото)
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path, filename):
    try:
        with open(image_path, "rb") as img_file:
            content = img_file.read()
        github_repo.create_file(
            f"{GITHUB_IMAGE_PATH}/{filename}",
            "upload image for post",
            content,
            branch="main"
        )
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
    except Exception as e:
        log.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None

def delete_image_from_github(filename):
    try:
        contents = github_repo.get_contents(f"{GITHUB_IMAGE_PATH}/{filename}", ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
    except Exception as e:
        log.error(f"Ошибка удаления файла на GitHub: {e}")

# -----------------------------------------------------------------------------
# Загрузка файлов
# -----------------------------------------------------------------------------
async def download_image_async(url_or_file_id, is_telegram_file=False, bot=None, retries=3):
    if is_telegram_file:
        for _ in range(retries):
            try:
                file = await bot.get_file(url_or_file_id)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                await file.download_to_drive(tmp.name)
                return tmp.name
            except Exception as e:
                await asyncio.sleep(1)
        raise Exception("Не удалось скачать файл из Telegram")
    else:
        r = requests.get(url_or_file_id, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        tmp.write(r.content); tmp.close()
        return tmp.name

async def download_to_temp_local(path_or_file_id: str, is_telegram: bool, bot: Bot) -> str:
    if is_telegram:
        tg_file = await bot.get_file(path_or_file_id)
        suffix = ".mp4" if (tg_file.file_path or "").lower().endswith(".mp4") else ".bin"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        await tg_file.download_to_drive(tmp.name)
        return tmp.name
    else:
        r = requests.get(path_or_file_id, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        r.raise_for_status()
        suf = ".mp4" if path_or_file_id.lower().endswith(".mp4") else ".bin"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suf)
        tmp.write(r.content); tmp.close()
        return tmp.name

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
# БД истории (дедуп по тексту+медиа)
# -----------------------------------------------------------------------------
DB_FILE = "post_history.db"

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

async def compute_media_hash_from_state() -> Optional[str]:
    kind = post_data.get("media_kind")
    src  = post_data.get("media_src")
    ref  = post_data.get("media_ref")
    if not kind or kind == "none" or not ref:
        return None
    try:
        if src == "url":
            r = requests.get(ref, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            r.raise_for_status()
            return sha256_hex(r.content)
        else:
            tg_file = await approval_bot.get_file(ref)
            tmp = tempfile.NamedTemporaryFile(delete=False)
            await tg_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                b = f.read()
            try:
                os.remove(tmp.name)
            except Exception:
                pass
            return sha256_hex(b)
    except Exception as e:
        log.warning(f"compute_media_hash_from_state fail: {e}")
        return None

async def is_duplicate_post(text: str, media_hash: Optional[str]) -> bool:
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None
    async with aiosqlite.connect(DB_FILE) as db:
        q = "SELECT 1 FROM posts WHERE COALESCE(text_hash,'') = COALESCE(?, '') AND COALESCE(image_hash,'') = COALESCE(?, '') LIMIT 1"
        async with db.execute(q, (text_hash, media_hash or None)) as cur:
            row = await cur.fetchone()
            return row is not None

async def save_post_to_history(text: str, media_hash: Optional[str]):
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute("INSERT INTO posts (text, text_hash, timestamp, image_hash) VALUES (?, ?, ?, ?)",
                             (text, text_hash, datetime.now(TZ).isoformat(), media_hash or None))
            await db.commit()
        except Exception as e:
            log.warning(f"save_post_to_history: возможно дубликат/ошибка вставки: {e}")

# -----------------------------------------------------------------------------
# ИИ (стартовый предпросмотр)
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
        return "Ai Coin fuses AI with blockchain to turn community ideas into real actions. Join builders shaping the next wave of crypto utility."

async def ai_generate_content_en(topic_hint: str) -> Tuple[str, List[str], Optional[str]]:
    text_prompt = (
        "Create a short social promo (1–3 sentences) about Ai Coin: an AI-integrated crypto project where holders can propose ideas, "
        "AI analyzes them, and the community votes on-chain. Tone: inspiring, community-first, no jargon. "
        f"Emphasize: {topic_hint}."
    )
    text_en = _oa_chat_text(text_prompt)

    extra_tags_prompt = (
        "Give me 3 short, relevant crypto+AI hashtags for a social post about Ai Coin (no duplicates of #AiCoin, #AI, #crypto, $Ai), "
        "single line, space-separated, each begins with #, only AI/crypto topics."
    )
    tags_line = _oa_chat_text(extra_tags_prompt)
    ai_tags = [t for t in tags_line.split() if t.startswith("#") and len(t) > 1][:4]

    return (text_en, ai_tags, None)

if set_ai_generator:
    try:
        set_ai_generator(ai_generate_content_en)
        log.info("Planner AI generator registered.")
    except Exception as e:
        log.warning(f"Cannot register planner AI generator: {e}")

# -----------------------------------------------------------------------------
# КНОПКИ / МЕНЮ
# -----------------------------------------------------------------------------
def _worker_url_with_secret() -> str:
    base = AICOIN_WORKER_URL or ""
    if not base: return base
    sec = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}s={sec}" if sec else base

def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Старт воркера", url=_worker_url_with_secret())],
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],  # <—
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter"),
         InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],  # <—
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post"),
         InlineKeyboardButton("🗓 ИИ план на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb"),
         InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_worker_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Старт воркера", url=_worker_url_with_secret())]])

async def send_with_start_button(chat_id: int, text: str):
    try:
        await approval_bot.send_message(chat_id=chat_id, text=text, reply_markup=start_worker_keyboard())
    except Exception:
        await approval_bot.send_message(chat_id=chat_id, text=text)

# -----------------------------------------------------------------------------
# Публикация в Telegram
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text: str | None, _image_url_ignored: Optional[str] = None) -> bool:
    try:
        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")
        final_html = build_tg_final(text or "", for_photo_caption=(mk in ("image","video")))

        if mk == "none" or not mref:
            if not final_html.strip():
                await send_with_start_button(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Telegram: пусто (нет текста и медиа).")
                return False
            await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            return True

        local_path = await download_to_temp_local(mref, is_telegram=(msrc=="tg"), bot=approval_bot)
        post_data["media_local_path"] = local_path

        if mk == "image":
            with open(local_path, "rb") as f:
                await channel_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, photo=f,
                    caption=(final_html if final_html.strip() else None),
                    parse_mode="HTML"
                )
        elif mk == "video":
            with open(local_path, "rb") as f:
                await channel_bot.send_video(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, video=f,
                    supports_streaming=True,
                    caption=(final_html if final_html.strip() else None),
                    parse_mode="HTML"
                )
        else:
            await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=final_html, parse_mode="HTML", disable_web_page_preview=True
            )

        try:
            os.remove(local_path)
        except Exception:
            pass
        post_data["media_local_path"] = None
        return True

    except Exception as e:
        log.error(f"Ошибка публикации в Telegram: {e}")
        await send_with_start_button(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Ошибка публикации в Telegram: {e}")
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Публикация в Twitter/X
# -----------------------------------------------------------------------------
def _download_to_temp_file(url: str, suffix: str = ".bin") -> Optional[str]:
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content); tmp.close()
        return tmp.name
    except Exception as e:
        log.warning(f"Не удалось скачать медиа для Twitter: {e}")
        return None

async def publish_post_to_twitter(text_en: str | None, _image_url_unused: str | None = None, ai_hashtags=None) -> bool:
    try:
        final_text = build_twitter_text(text_en or "", ai_hashtags or [])
        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")

        media_ids = None
        local_path = None

        if mk in ("image","video") and mref:
            if msrc == "url":
                suf = ".mp4" if mk == "video" else ".jpg"
                local_path = _download_to_temp_file(mref, suffix=suf)
                if not local_path:
                    raise RuntimeError("Не удалось получить медиа из URL для X")
            else:
                local_path = await download_to_temp_local(mref, is_telegram=True, bot=approval_bot)

            post_data["media_local_path"] = local_path

            if mk == "image":
                media = twitter_api_v1.media_upload(filename=local_path)
                media_ids = [media.media_id_string]
            else:
                media = twitter_api_v1.media_upload(
                    filename=local_path,
                    media_category="tweet_video",
                    chunked=True
                )
                media_ids = [media.media_id_string]

        clean_text = (final_text or "").strip()

        if not media_ids and not clean_text:
            asyncio.create_task(send_with_start_button(
                TELEGRAM_APPROVAL_CHAT_ID,
                "⚠️ В Twitter нечего публиковать: нет ни текста, ни медиа."
            ))
            return False

        if media_ids and not clean_text:
            try:
                twitter_client_v2.create_tweet(media={"media_ids": media_ids})
            except TypeError:
                twitter_client_v2.create_tweet(media_ids=media_ids)
        elif not media_ids and clean_text:
            twitter_client_v2.create_tweet(text=clean_text)
        else:
            try:
                twitter_client_v2.create_tweet(text=clean_text, media={"media_ids": media_ids})
            except TypeError:
                twitter_client_v2.create_tweet(text=clean_text, media_ids=media_ids)

        if local_path:
            try: os.remove(local_path)
            except Exception: pass
            post_data["media_local_path"] = None

        return True

    except tweepy.TweepyException as e:
        log.error(f"Twitter TweepyException: {e}")
        asyncio.create_task(send_with_start_button(
            TELEGRAM_APPROVAL_CHAT_ID,
            "❌ Twitter: ошибка загрузки. Проверь права app (Read+Write) и параметры видео (H.264/AAC, ≤~140s)."
        ))
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False
    except Exception as e:
        log.error(f"Twitter general error: {e}")
        asyncio.create_task(send_with_start_button(
            TELEGRAM_APPROVAL_CHAT_ID, f"❌ Twitter: {e}"
        ))
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Предпросмотр
# -----------------------------------------------------------------------------
async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    try:
        if not str(url_or_file_id).startswith("http"):
            url = await process_telegram_photo(url_or_file_id, bot)
            return await bot.send_photo(chat_id=chat_id, photo=url, caption=caption, parse_mode="HTML", reply_markup=reply_markup), url.split('/')[-1]
        else:
            try:
                response = requests.get(url_or_file_id, timeout=10)
                response.raise_for_status()
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                tmp.write(response.content); tmp.close()
                with open(tmp.name, "rb") as img:
                    msg = await bot.send_photo(chat_id=chat_id, photo=img, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
                os.remove(tmp.name)
                return msg, None
            except Exception:
                msg = await bot.send_message(chat_id=chat_id, text=caption or "", parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=False)
                return msg, None
    except Exception as e:
        log.error(f"Ошибка в send_photo_with_download: {e}")
        msg = await bot.send_message(chat_id=chat_id, text=caption or " ", parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=False)
        return msg, None

async def send_video_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    try:
        if not str(url_or_file_id).startswith("http"):
            try:
                msg = await bot.send_video(
                    chat_id=chat_id,
                    video=url_or_file_id,
                    supports_streaming=True,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
                return msg, None
            except Exception:
                tg_file = await bot.get_file(url_or_file_id)
                suffix = ".mp4" if (tg_file.file_path or "").lower().endswith(".mp4") else ".bin"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                await tg_file.download_to_drive(tmp.name)
                with open(tmp.name, "rb") as f:
                    msg = await bot.send_video(
                        chat_id=chat_id, video=f,
                        supports_streaming=True,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                os.remove(tmp.name)
                return msg, None
        else:
            try:
                response = requests.get(url_or_file_id, timeout=60, headers={'User-Agent':'Mozilla/5.0'})
                response.raise_for_status()
                suf = ".mp4" if url_or_file_id.lower().endswith(".mp4") else ".bin"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suf)
                tmp.write(response.content); tmp.close()
                with open(tmp.name, "rb") as f:
                    msg = await bot.send_video(
                        chat_id=chat_id, video=f,
                        supports_streaming=True,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                os.remove(tmp.name)
                return msg, None
            except Exception:
                msg = await bot.send_message(chat_id=chat_id, text=(caption or url_or_file_id), parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=False)
                return msg, None
    except Exception as e:
        log.error(f"Ошибка в send_video_with_download: {e}")
        msg = await bot.send_message(chat_id=chat_id, text=(caption or " "), parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=False)
        return msg, None

async def send_single_preview(text_en: str, ai_hashtags=None, image_url=None, header: str | None = "Предпросмотр"):
    text_for_message = build_telegram_preview(text_en, ai_hashtags or [])
    caption_for_media = build_tg_final(text_en, for_photo_caption=True)

    hdr = f"<b>{html_escape(header)}</b>\n" if header else ""
    text_message = f"{hdr}{text_for_message}".strip()

    preview_image_url = None
    if post_data.get("media_kind") == "image":
        if post_data.get("media_src") == "url":
            preview_image_url = post_data.get("media_ref")
        elif post_data.get("media_src") == "tg":
            try:
                preview_image_url = await process_telegram_photo(post_data.get("media_ref"), approval_bot)
            except Exception:
                preview_image_url = None

    try:
        if post_data.get("media_kind") == "video" and post_data.get("media_ref"):
            await send_video_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data.get("media_ref"),
                caption=(caption_for_media if caption_for_media.strip() else None),
                reply_markup=start_preview_keyboard()
            )
        elif preview_image_url:
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                preview_image_url,
                caption=(caption_for_media if caption_for_media.strip() else None),
                reply_markup=start_preview_keyboard()
            )
        else:
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=start_preview_keyboard()
            )
    except Exception as e:
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=start_preview_keyboard()
        )

# -----------------------------------------------------------------------------
# Планировщик — снимки и роутинг
# -----------------------------------------------------------------------------
def _planner_active_for(uid: int) -> bool:
    return uid in ROUTE_TO_PLANNER

async def _route_to_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if open_planner:
        return await open_planner(update, context)
    return

# -----------------------------------------------------------------------------
# РЕДАКТОР ХЭШТЕГОВ (любой момент)
# -----------------------------------------------------------------------------
async def ask_edit_hashtags(chat_id: int):
    current = " ".join(post_data.get("ai_hashtags") or [])
    msg = (
        "🔖 Пришли хэштеги одной строкой (пример):\n"
        "<code>#AiCoin #AI $Ai #crypto #DeFi</code>\n\n"
        f"Текущие: <i>{html_escape(current) if current else '—'}</i>"
    )
    await approval_bot.send_message(
        chat_id=chat_id, text=msg, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="approve")]])
    )

# -----------------------------------------------------------------------------
# CALLBACKS / INPUT
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, last_action_time, manual_expected_until
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    await q.answer()

    now = datetime.now(TZ)
    last_button_pressed_at = now
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if uid in last_action_time and (now - last_action_time[uid]).seconds < 1:
        return
    last_action_time[uid] = now

    # Планировщик роутинг
    planner_any = data.startswith(("PLAN_", "ITEM_MENU:", "DEL_ITEM:", "EDIT_TIME:", "EDIT_ITEM:", "EDIT_FIELD:", "AI_FILL_TEXT:", "CLONE_ITEM:", "AI_NEW_FROM:"))
    planner_exit = data in {"BACK_MAIN_MENU", "PLAN_DONE", "GEN_DONE"}

    if data == "show_day_plan" or planner_any or planner_exit:
        ROUTE_TO_PLANNER.add(uid)
        await _route_to_planner(update, context)
        if planner_exit or data == "BACK_MAIN_MENU":
            ROUTE_TO_PLANNER.discard(uid)
            await approval_bot.send_message(
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Главное меню:",
                reply_markup=get_start_menu()
            )
        return

    if data == "cancel_to_main":
        ROUTE_TO_PLANNER.discard(uid)
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "shutdown_bot":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        msg = f"🔴 Бот выключен.\nСледующий пост: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=msg, reply_markup=start_worker_keyboard())
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data == "self_post":
        ROUTE_TO_PLANNER.discard(uid)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="✍️ Введите текст поста (EN) и (опционально) приложите фото/видео одним сообщением:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
            ])
        )
        manual_expected_until = now + timedelta(minutes=5)
        return

    if data == "approve":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], image_url=None, header="Предпросмотр")
        return

    if data == "edit_hashtags":
        await ask_edit_hashtags(TELEGRAM_APPROVAL_CHAT_ID)
        return

    if data in ("post_twitter","post_telegram","post_both"):
        await publish_flow(publish_tg=(data!="post_twitter"), publish_tw=(data!="post_telegram"))
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=get_start_menu())
        return

    if data == "end_day":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"🔚 Работа завершена. Следующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}", parse_mode="HTML", reply_markup=get_start_menu())
        return

# Ручной ввод — текст + медиа И/ИЛИ ввод хэштегов строкой
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    raw_text = (update.message.text or update.message.caption or "").strip()

    # Если пользователь прислал строку-хэштеги в режиме редактирования:
    if raw_text and (raw_text.startswith("#") or raw_text.lower().startswith(("tags:", "теги:", "хэштеги:"))):
        # срежем префикс tags:
        cleaned = raw_text
        for pref in ("tags:", "теги:", "хэштеги:"):
            if cleaned.lower().startswith(pref):
                cleaned = cleaned[len(pref):].strip()
        post_data["ai_hashtags"] = _parse_tags_to_list(cleaned)
        await approval_bot.send_message(
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text=f"🔖 Хэштеги обновлены: {' '.join(post_data['ai_hashtags']) or '—'}",
            reply_markup=start_preview_keyboard()
        )
        manual_expected_until = None
        return

    text = raw_text
    media_kind = "none"
    media_src  = "tg"
    media_ref  = None

    if getattr(update.message, "photo", None):
        media_kind = "image"
        media_ref  = update.message.photo[-1].file_id
    elif getattr(update.message, "video", None):
        media_kind = "video"
        media_ref  = update.message.video.file_id
    elif getattr(update.message, "document", None):
        mime = (update.message.document.mime_type or "")
        fid  = update.message.document.file_id
        if mime.startswith("video/"):
            media_kind = "video"; media_ref = fid
        elif mime.startswith("image/"):
            media_kind = "image"; media_ref = fid
    elif text and text.startswith("http"):
        url = text.split()[0]
        if any(url.lower().endswith(ext) for ext in (".mp4",".mov",".m4v",".webm")):
            media_kind = "video"; media_src = "url"; media_ref = url
            text = text[len(url):].strip()
        elif any(url.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".gif",".webp")):
            media_kind = "image"; media_src = "url"; media_ref = url
            text = text[len(url):].strip()

    post_data["text_en"] = text
    post_data["media_kind"] = media_kind
    post_data["media_src"]  = media_src
    post_data["media_ref"]  = media_ref
    post_data["media_local_path"] = None
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    await send_single_preview(post_data["text_en"], post_data.get("ai_hashtags") or [], image_url=None, header="Предпросмотр")
    manual_expected_until = None

# Общая публикация
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()

    twitter_text = build_twitter_text(base_text_en, post_data.get("ai_hashtags") or [])
    telegram_text_preview = build_telegram_preview(base_text_en, None)

    if do_not_disturb["active"]:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

    media_hash = await compute_media_hash_from_state()
    tg_status = tw_status = None

    if publish_tg:
        if await is_duplicate_post(telegram_text_preview, media_hash):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=base_text_en)
            if tg_status:
                final_html_saved = build_tg_final(base_text_en, for_photo_caption=(post_data.get("media_kind") in ("image","video")))
                await save_post_to_history(final_html_saved, media_hash)

    if publish_tw:
        if await is_duplicate_post(twitter_text, media_hash):
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Дубликат для Twitter. Публикация пропущена.")
            tw_status = False
        else:
            tw_status = await publish_post_to_twitter(twitter_text, None, post_data.get("ai_hashtags") or [])
            if tw_status:
                await save_post_to_history(twitter_text, media_hash)

    if publish_tg:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram.")
    if publish_tw:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter.")

    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# Роутер сообщений
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, manual_expected_until
    uid = update.effective_user.id
    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if _planner_active_for(uid):
        return await _route_to_planner(update, context)

    if manual_expected_until and now <= manual_expected_until:
        return await handle_manual_input(update, context)

    await approval_bot.send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# STARTUP / SHUTDOWN / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    try:
        text_en, ai_tags, _ = await ai_generate_content_en("General invite and value.")
    except Exception as e:
        log.warning(f"ai_generate_content_en failed at start: {e}")
        text_en, ai_tags = post_data["text_en"], post_data.get("ai_hashtags") or []

    post_data["text_en"] = text_en or ""
    post_data["ai_hashtags"] = ai_tags or []
    post_data["media_kind"] = "none"
    post_data["media_src"] = "tg"
    post_data["media_ref"] = None

    await send_single_preview(post_data["text_en"], post_data["ai_hashtags"], image_url=None, header="Предпросмотр")
    log.info("Бот запущен. Отправлен предпросмотр. Планирование — в planner.py (если подключено).")

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
                    await send_with_start_button(TELEGRAM_APPROVAL_CHAT_ID, "🔴 Нет активности 10 минут. Отключаюсь. Нажми «Старт воркера», чтобы перезапустить.")
                except Exception:
                    pass
                shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await send_with_start_button(TELEGRAM_APPROVAL_CHAT_ID, f"⚠️ Ошибка наблюдателя активности: {e}\nНажми «Старт воркера», чтобы перезапустить.")
            except Exception:
                pass

def shutdown_bot_and_exit():
    try:
        asyncio.create_task(send_with_start_button(
            TELEGRAM_APPROVAL_CHAT_ID,
            "🔴 Бот полностью выключен. Нажми «Старт воркера», чтобы перезапустить."
        ))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    register_planner_handlers(app)  # планировщик

    app.add_handler(CallbackQueryHandler(callback_handler), group=50)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.VIDEO | filters.Document.IMAGE, message_handler), group=50)

    asyncio.get_event_loop().create_task(check_inactivity_shutdown())
    app.run_polling(poll_interval=0.12, timeout=1)

if __name__ == "__main__":
    main()