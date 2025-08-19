# -*- coding: utf-8 -*-
"""
twitter_bot.py — согласование/генерация/публикация в Telegram и X (Twitter).

Обновления (этой версии):
- 🗓 Планировщик: события планирования идут в open_planner() и НЕ попадают в «Сделай сам».
- 🧹 Полностью убран OpenAI и все зависимости от ИИ.
- ✅ Кнопка «🔖 Хэштеги» доступна ВСЕГДА (предпросмотр, «Сделай сам», главное меню).
- ✅ Ввод/редактирование хэштегов отдельным сообщением (5 мин окно), с дедупом; в X есть режим override.
- ✅ Telegram: хвост добавляется ВСЕГДА (и не дублируется), с учётом лимитов caption=1024 и message=4096.
- ✅ Twitter:
    VERBATIM_MODE=True  -> публикуем РОВНО текст пользователя (без хвостов).
    VERBATIM_MODE=False -> добавляем хвост (🌐 site | 🐺 Telegram) + дедуп-хэштеги; лимит 275.
- ✅ Видео: принимаем photo / video / document(video); Telegram — send_video; X — chunked upload v1.1.
- ✅ FIX: Twitter video — убран run_until_complete (ошибка "event loop is already running"), публикация в X async.
- ✅ Главный экран ВСЕГДА содержит кнопку «▶️ Старт воркера».
- ✅ «Сделай сам» перехватывает сообщения только 5 минут после нажатия.
- 🛠 GitHub upload — через base64 (PyGithub требует base64-строку).
- 🛠 Убрана повторная сборка твита: финальный текст X формируется 1 раз и не модифицируется в publish_post_to_twitter().
- 🆕 Режим override: «обязательные ссылки + пользовательские хэштеги (≤275)» (без автотегов).
- 🆕 Twitter TRIM POLICY: если тело обрезается — ВСЕГДА добавляем « … » перед блоком «ссылки и хэштеги», чтобы хвост не терялся.
- 🆕 Анти-флуд в Telegram: безопасные обёртки для answerCallbackQuery/sendMessage, обработчик ошибок, сниженный polling.
"""

import os
import re
import sys
import uuid
import base64
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
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError
import aiosqlite
from github import Github

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("twitter_bot")

# === ПЛАНИРОВЩИК (опционально) ===
# Перенесено после инициализации логгера; безопасный импорт + дефолты.
try:
    from planner import register_planner_handlers, open_planner
    log.info("Planner module loaded")
except Exception as _e:
    log.warning("Planner module not available: %s", _e)
    register_planner_handlers = lambda app: None
    open_planner = None

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

AICOIN_WORKER_URL = os.getenv("AICOIN_WORKER_URL", "https://aicoin-bot-trigger.dfosjam.workers.dev/tg/webhook")
PUBLIC_TRIGGER_SECRET = (os.getenv("PUBLIC_TRIGGER_SECRET") or "").strip()
AICOIN_WORKER_SECRET = os.getenv("AICOIN_WORKER_SECRET") or TELEGRAM_BOT_TOKEN_APPROVAL
FALLBACK_PUBLIC_TRIGGER_SECRET = "z8PqH0e4jwN3rA1K"

need_env = [
    "TELEGRAM_BOT_TOKEN_APPROVAL", "TELEGRAM_APPROVAL_CHAT_ID",
    "TELEGRAM_BOT_TOKEN_CHANNEL", "TELEGRAM_CHANNEL_USERNAME_ID",
    "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
    "ACTION_PAT_GITHUB", "ACTION_REPO_GITHUB"
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
    "ai_hashtags": [],           # редактируемая пользователем коллекция
    "media_kind": "none",        # "none" | "image" | "video"
    "media_src": "tg",           # "tg" | "url"
    "media_ref": None,
    "media_local_path": None,
    "timestamp": None,
    "post_id": 0,
    "is_manual": False,
    "user_tags_override": False  # если True, X собирается из обязательных ссылок + твоих хэштегов (без автотегов)
}
prev_data = post_data.copy()

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT, "mode": "normal"}
do_not_disturb = {"active": False}
last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None
ROUTE_TO_PLANNER: set[int] = set()  # трекер «я в планировщике»

# — ожидание ввода хэштегов (отдельное окно 5 минут)
awaiting_hashtags_until: Optional[datetime] = None

# -----------------------------------------------------------------------------
# УТИЛИТЫ ДЛИНЫ / ДЕДУП ХЭШТЕГОВ (для X)
# -----------------------------------------------------------------------------
_TCO_LEN = 23
_URL_RE = re.compile(r'https?://\S+', flags=re.UNICODE)
MY_HASHTAGS_STR = "#AiCoin #AI $Ai #crypto"

def twitter_len(s: str) -> int:
    if not s:
        return 0
    s = normalize("NFC", s)
    return len(_URL_RE.sub('X' * _TCO_LEN, s))

def trim_to_twitter_len(s: str, max_len: int) -> str:
    if not s:
        return s
    s = normalize("NFC", s).strip()
    if twitter_len(s) <= max_len:
        return s
    ell = '…'
    while s and twitter_len(s + ell) > max_len:
        s = s[:-1]
    return (s + ell).rstrip()

def _dedup_hashtags(*groups):
    seen, out = set(), []
    def norm(t: str) -> str:
        t = t.strip()
        if not t:
            return ""
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        return t
    def ok(t: str) -> bool:
        tl = t.lower()
        return ("ai" in tl) or ("crypto" in tl) or tl.startswith("$ai")
    for g in groups:
        if not g:
            continue
        items = g.split() if isinstance(g, str) else list(g)
        for raw in items:
            tag = norm(raw)
            if not tag or not ok(tag):
                continue
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key); out.append(tag)
    return " ".join(out)

def _parse_hashtags_line(line: str) -> List[str]:
    if not line:
        return []
    tmp = re.sub(r"[,\u00A0;]+", " ", line.strip())
    raw = [w for w in tmp.split() if w]
    filtered = _dedup_hashtags(raw).split()
    return filtered

# ======== Пользовательские теги (без тематического фильтра) ========
def _normalize_hashtag_any(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    if not (t.startswith("#") or t.startswith("$")):
        t = "#" + t
    return t

def _dedup_any_hashtags(tags: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for raw in tags:
        h = _normalize_hashtag_any(raw)
        if not h:
            continue
        key = h.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out

def _parse_hashtags_line_user(line: str) -> List[str]:
    if not line:
        return []
    tmp = re.sub(r"[,\u00A0;]+", " ", line.strip())
    raw = [w for w in tmp.split() if w]
    return _dedup_any_hashtags(raw)

# ======== Трим с сохранением целых URL (в теле) ========
def trim_preserving_urls(body: str, max_len: int) -> str:
    body = (body or "").strip()
    if max_len <= 0 or not body:
        return ""
    parts = []
    last = 0
    for m in _URL_RE.finditer(body):
        if m.start() > last:
            parts.append((False, body[last:m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    if last < len(body):
        parts.append((False, body[last:]))

    out = ""
    for is_url, seg in parts:
        if not seg:
            continue
        if is_url:
            cand = (out + (" " if out else "") + seg).strip()
            if twitter_len(cand) <= max_len:
                out = cand
            else:
                continue
        else:
            if twitter_len(out) >= max_len:
                break
            remain = max_len - twitter_len(out) - (1 if out else 0)
            if remain <= 0:
                break
            chunk = seg.strip()
            if not chunk:
                continue
            if twitter_len((out + (" " if out else "") + chunk).strip()) <= max_len:
                out = (out + (" " if out else "") + chunk).strip()
            else:
                acc = ""
                for ch in chunk:
                    test = (out + (" " if out else "") + acc + ch).strip()
                    if twitter_len(test) <= max_len:
                        acc += ch
                    else:
                        break
                if acc:
                    out = (out + (" " if out else "") + acc).strip()
                break
    return out.strip()

# ======== Сборка хвоста и твита (основной режим) ========
def _tail_block(ai_tags: List[str] | None) -> str:
    tags_str = _dedup_hashtags(MY_HASHTAGS_STR, ai_tags or [])
    return (TW_TAIL_REQUIRED + (f" {tags_str}" if tags_str else "")).strip()

def build_tweet_with_tail_275(body_text: str, ai_tags: List[str] | None) -> str:
    """
    Политика: лимит 275, если тело пришлось обрезать — добавляем ' … ' перед блоком «ссылки+хэштеги».
    Хвост не исчезает никогда. Если комбинированный хвост длинный — деградируем до обязательных ссылок.
    """
    MAX_TWEET = 275
    body = (body_text or "").strip()

    tail_full = _tail_block(ai_tags)
    tail_req  = TW_TAIL_REQUIRED

    # если "полный" хвост сам по себе > лимита — возьмём только обязательные ссылки
    tail = tail_full if twitter_len(tail_full) <= MAX_TWEET else tail_req

    # первая попытка: без учёта ' … '
    sep = 1 if (body and tail) else 0
    allowed = MAX_TWEET - twitter_len(tail) - sep
    allowed = max(0, allowed)

    # трим по twitter_len
    body_trimmed = trim_to_twitter_len(body, allowed)
    was_trimmed_initial = twitter_len(body) > twitter_len(body_trimmed)

    # если пришлось резать — выделим 2 символа под " …"
    if was_trimmed_initial and tail:
        allowed2 = MAX_TWEET - twitter_len(tail) - sep - 2  # пробел + '…'
        allowed2 = max(0, allowed2)
        body_trimmed = trim_to_twitter_len(body, allowed2)
        # финальная сборка с " … "
        tweet = f"{body_trimmed} … {tail}".strip() if body_trimmed else tail
    else:
        # сборка без " … "
        tweet = f"{body_trimmed} {tail}".strip() if (body_trimmed and tail) else (body_trimmed or tail)

    # страховка по длине
    if twitter_len(tweet) > MAX_TWEET:
        # попробуем деградировать до минимального хвоста
        if tail != tail_req:
            tail = tail_req
            was_trimmed = twitter_len(body) > allowed
            if was_trimmed:
                allowed2 = MAX_TWEET - twitter_len(tail) - (1 if body else 0) - 2
                allowed2 = max(0, allowed2)
                body_trimmed = trim_to_twitter_len(body, allowed2)
                tweet = f"{body_trimmed} … {tail}".strip() if body_trimmed else tail
            else:
                allowed = MAX_TWEET - twitter_len(tail) - (1 if body else 0)
                allowed = max(0, allowed)
                body_trimmed = trim_to_twitter_len(body, allowed)
                tweet = f"{body_trimmed} {tail}".strip() if (body_trimmed and tail) else (body_trimmed or tail)

    if twitter_len(tweet) > MAX_TWEET:
        tweet = tail_req  # крайний случай — только обязательные ссылки

    return tweet

# ======== Режим «обязательные ссылки + пользовательские теги» (override) ========
def build_tweet_user_hashtags_275(body_text: str, user_tags: List[str] | None) -> str:
    """
    - сохраняем URLs и текст из body_text (URL не рвём)
    - добавляем ОБЯЗАТЕЛЬНЫЕ ССЫЛКИ (TW_TAIL_REQUIRED) + ТОЛЬКО пользовательские хэштеги
    - общий лимит 275 (учёт t.co=23)
    - если тело урезали — ставим ' … ' перед хвостом (ссылки+теги)
    """
    MAX_TWEET = 275
    body = (body_text or "").strip()

    # пользовательские теги, без тематического фильтра + дедуп
    tags = _dedup_any_hashtags(user_tags or [])
    tags_str = " ".join(tags).strip()

    tail_links = TW_TAIL_REQUIRED.strip()
    tail_full = (tail_links + (f" {tags_str}" if tags_str else "")).strip()

    # первая попытка (без учёта ' … ')
    sep = 1 if (body and tail_full) else 0
    allowed = MAX_TWEET - twitter_len(tail_full) - sep
    allowed = max(0, allowed)

    body_trimmed = trim_preserving_urls(body, allowed)
    was_trimmed = twitter_len(body) > twitter_len(body_trimmed)

    if was_trimmed:
        # учитываем место под ' … '
        allowed2 = MAX_TWEET - twitter_len(tail_full) - sep - 2
        allowed2 = max(0, allowed2)
        body_trimmed = trim_preserving_urls(body, allowed2)
        tweet = f"{body_trimmed} … {tail_full}".strip() if body_trimmed else tail_full
    else:
        tweet = f"{body_trimmed} {tail_full}".strip() if (body_trimmed and tail_full) else (body_trimmed or tail_full)

    # если не влезли — урезаем теги, ссылки неприкасаемы
    if twitter_len(tweet) > MAX_TWEET:
        kept = []
        for t in tags:
            test_tail = (tail_links + (" " + " ".join(kept + [t]) if kept or t else "")).strip()
            test_sep = " … " if was_trimmed and body_trimmed else (" " if body_trimmed else "")
            test_tweet = (f"{body_trimmed}{test_sep}{test_tail}").strip() if body_trimmed else test_tail
            if twitter_len(test_tweet) <= MAX_TWEET:
                kept.append(t)
            else:
                break
        tail_full = (tail_links + (" " + " ".join(kept) if kept else "")).strip()
        tweet = (f"{body_trimmed} … {tail_full}".strip() if (was_trimmed and body_trimmed)
                 else (f"{body_trimmed} {tail_full}".strip() if (body_trimmed and tail_full) else (body_trimmed or tail_full)))

    # крайний случай — только ссылки (и сколько влезло тегов)
    if twitter_len(tweet) > MAX_TWEET:
        kept = []
        for t in tags:
            test_tail = (tail_links + (" " + " ".join(kept + [t]) if kept or t else "")).strip()
            if twitter_len(test_tail) <= MAX_TWEET:
                kept.append(t)
            else:
                break
        tweet = (tail_links + (" " + " ".join(kept) if kept else "")).strip()

    return tweet

def build_twitter_text(text_en: str, ai_hashtags=None) -> str:
    return (text_en or "").strip() if VERBATIM_MODE else build_tweet_with_tail_275(text_en, ai_hashtags or [])

# -----------------------------------------------------------------------------
# TG: гарантированный хвост в финале публикации
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
    return build_tg_final(text_en, for_photo_caption=False)

# -----------------------------------------------------------------------------
# GitHub helpers (для предпросмотра TG-фото)
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path, filename):
    """ВАЖНО: PyGithub.create_file ожидает base64-строку."""
    try:
        with open(image_path, "rb") as img_file:
            content_b64 = base64.b64encode(img_file.read()).decode("utf-8")
        github_repo.create_file(
            f"{GITHUB_IMAGE_PATH}/{filename}",
            "upload image for post",
            content_b64,
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
                log.warning(f"download_image_async TG failed: {e}")
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
    filename = f"{uuid.uuid4().hex}.jpg}"
    # исправим случайную скобку если вдруг — но правильный вариант ниже:
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
    if not text:
        return ""
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
# КНОПКИ / МЕНЮ
# -----------------------------------------------------------------------------
def _worker_url_with_secret() -> str:
    base = AICOIN_WORKER_URL or ""
    if not base:
        return base
    sec = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}s={sec}" if sec else base

def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Старт воркера", url=_worker_url_with_secret())],
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 План на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить на сегодня", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter"),
         InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags"),
         InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 План на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb"),
         InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_worker_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Старт воркера", url=_worker_url_with_secret())]])

# ---- Безопасные обёртки от флуд-контроля/старых callback'ов ----
async def safe_q_answer(q) -> bool:
    try:
        await q.answer()
        return True
    except BadRequest as e:
        if "Query is too old" in str(e):
            log.warning("Callback too old; ignored.")
            return False
        raise
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await q.answer()
            return True
        except Exception:
            return False

async def safe_send_message(bot: Bot, **kwargs):
    for _ in range(3):
        try:
            return await bot.send_message(**kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError):
            await asyncio.sleep(1)
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return None
            raise
    return None

async def send_with_start_button(chat_id: int, text: str):
    try:
        await safe_send_message(approval_bot, chat_id=chat_id, text=text, reply_markup=start_worker_keyboard())
    except Exception:
        await safe_send_message(approval_bot, chat_id=chat_id, text=text)

# -----------------------------------------------------------------------------
# Публикация в Telegram — хвост ВСЕГДА
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

        local_path = await download_to_temp_local(mref, is_telegram=(msrc == "tg"), bot=approval_bot)
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
            try:
                os.remove(lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Публикация в Twitter/X (текст/картинка/видео) — без повторной сборки текста.
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

async def publish_post_to_twitter(final_text_ready: str | None, _image_url_unused: str | None = None) -> bool:
    try:
        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")

        media_ids = None
        local_path = None

        if mk in ("image", "video") and mref:
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

        clean_text = (final_text_ready or "").strip()

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
            try:
                os.remove(local_path)
            except Exception:
                pass
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
            try:
                os.remove(lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False
    except Exception as e:
        log.error(f"Twitter general error: {e}")
        asyncio.create_task(send_with_start_button(
            TELEGRAM_APPROVAL_CHAT_ID, f"❌ Twitter: {e}"
        ))
        lp = post_data.get("media_local_path")
        if lp:
            try:
                os.remove(lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Предпросмотр
# -----------------------------------------------------------------------------
async def send_photo_with_download(bot, chat_id, url_or_file_id, caption=None, reply_markup=None):
    try:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=url_or_file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
        return msg, None
    except Exception as e:
        log.error(f"Ошибка в send_photo_with_download: {e}")
        msg = await bot.send_message(
            chat_id=chat_id,
            text=caption or " ",
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=False
        )
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
    hashtags_line = ("<i>Хэштеги:</i> " + html_escape(" ".join(ai_hashtags or []))) if (ai_hashtags) else "<i>Хэштеги:</i> —"
    text_message = f"{hdr}{text_for_message}\n\n{hashtags_line}".strip()

    preview_media_ref = None
    if post_data.get("media_kind") == "image":
        if post_data.get("media_src") == "url":
            preview_media_ref = post_data.get("media_ref")   # внешний URL
        elif post_data.get("media_src") == "tg":
            preview_media_ref = post_data.get("media_ref")   # Telegram file_id

    try:
        if post_data.get("media_kind") == "video" and post_data.get("media_ref"):
            await send_video_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                post_data.get("media_ref"),
                caption=(caption_for_media if caption_for_media.strip() else None),
                reply_markup=start_preview_keyboard()
            )
        elif preview_media_ref:
            await send_photo_with_download(
                approval_bot,
                TELEGRAM_APPROVAL_CHAT_ID,
                preview_media_ref,
                caption=(caption_for_media if caption_for_media.strip() else None),
                reply_markup=start_preview_keyboard()
            )
        else:
            await safe_send_message(
                approval_bot,
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=start_preview_keyboard()
            )
    except Exception as e:
        log.warning(f"send_single_preview fallback: {e}")
        await safe_send_message(
            approval_bot,
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
    # мягкое уведомление, если модуль не подключён
    try:
        await safe_send_message(
            approval_bot,
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
            text="⚠️ Планировщик не подключён (planner.py). Работаем в ручном режиме.",
            reply_markup=get_start_menu()
        )
    except Exception:
        pass
    return

# -----------------------------------------------------------------------------
# CALLBACKS / INPUT
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, last_action_time, manual_expected_until, awaiting_hashtags_until
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    await safe_q_answer(q)

    now = datetime.now(TZ)
    last_button_pressed_at = now
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if uid in last_action_time and (now - last_action_time[uid]).seconds < 1:
        return
    last_action_time[uid] = now

    # --- Планировщик: явные команды/префиксы ---
    planner_any = data.startswith(("PLAN_", "ITEM_MENU:", "DEL_ITEM:", "EDIT_TIME:", "EDIT_ITEM:", "EDIT_FIELD:", "CLONE_ITEM:", "TOGGLE_DONE:", "show_day_plan"))
    planner_exit = data in {"BACK_MAIN_MENU", "PLAN_DONE", "GEN_DONE"}

    if data == "show_day_plan" or planner_any or planner_exit:
        ROUTE_TO_PLANNER.add(uid)
        awaiting_hashtags_until = None
        await _route_to_planner(update, context)
        if planner_exit or data == "BACK_MAIN_MENU":
            ROUTE_TO_PLANNER.discard(uid)
            await safe_send_message(
                approval_bot,
                chat_id=TELEGRAM_APPROVAL_CHAT_ID,
                text="Главное меню:",
                reply_markup=get_start_menu()
            )
        return

    if data == "cancel_to_main":
        ROUTE_TO_PLANNER.discard(uid)
        awaiting_hashtags_until = None
        await safe_send_message(chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", bot=approval_bot, reply_markup=get_start_menu())
        return

    if data == "shutdown_bot":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        msg = f"🔴 Бот выключен.\nСледующий пост: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}"
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=msg, reply_markup=start_worker_keyboard())
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data == "self_post":
        ROUTE_TO_PLANNER.discard(uid)
        awaiting_hashtags_until = None
        await safe_send_message(
            approval_bot,
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

    # === ХЭШТЕГИ ===
    if data == "edit_hashtags":
        awaiting_hashtags_until = now + timedelta(minutes=5)
        cur = " ".join(post_data.get("ai_hashtags") or [])
        hint = (
            "🔖 Отправьте строку с хэштегами (через пробел/запятую).\n"
            "Я учту любые теги, удалю дубли. В Twitter можно включить режим «обязательные ссылки + твои теги». \n"
            f"Сейчас: {cur if cur else '—'}"
        )
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=hint, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Очистить хэштеги", callback_data="clear_hashtags")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="approve")]
        ]))
        return

    if data == "clear_hashtags":
        post_data["ai_hashtags"] = []
        post_data["user_tags_override"] = False
        awaiting_hashtags_until = None
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="✅ Хэштеги очищены. Режим Twitter вернулся к стандартному (хвост + автотеги).")
        await send_single_preview(post_data.get("text_en") or "", [], image_url=None, header="Предпросмотр")
        return

    # Публикация
    if data in ("post_twitter", "post_telegram", "post_both"):
        await publish_flow(publish_tg=(data != "post_twitter"), publish_tw=(data != "post_telegram"))
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🌙 Режим «Не беспокоить» {status}.", reply_markup=get_start_menu())
        return

    if data == "end_day":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"🔚 Работа завершена. Следующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}", parse_mode="HTML", reply_markup=get_start_menu())
        return

# Ручной ввод — текст + фото/видео/док-видео; предпросмотр
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    text = (update.message.text or update.message.caption or "").strip()

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
        if any(url.lower().endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
            media_kind = "video"; media_src = "url"; media_ref = url
            text = text[len(url):].strip()
        elif any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
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

    # формируем финальный текст для X один раз
    if post_data.get("user_tags_override"):
        twitter_final_text = build_tweet_user_hashtags_275(base_text_en, post_data.get("ai_hashtags") or [])
    else:
        twitter_final_text = build_twitter_text(base_text_en, post_data.get("ai_hashtags") or [])
    telegram_text_preview = build_telegram_preview(base_text_en, None)

    if do_not_disturb["active"]:
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

    media_hash = await compute_media_hash_from_state()

    tg_status = tw_status = None

    if publish_tg:
        if await is_duplicate_post(telegram_text_preview, media_hash):
            await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=base_text_en)
            if tg_status:
                final_html_saved = build_tg_final(base_text_en, for_photo_caption=(post_data.get("media_kind") in ("image","video")))
                await save_post_to_history(final_html_saved, media_hash)

    if publish_tw:
        if await is_duplicate_post(twitter_final_text, media_hash):
            await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="⚠️ Дубликат для Twitter. Публикация пропущена.")
            tw_status = False
        else:
            tw_status = await publish_post_to_twitter(twitter_final_text, None)
            if tw_status:
                await save_post_to_history(twitter_final_text, media_hash)

    if publish_tg:
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=("✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram."))
    if publish_tw:
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=("✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter."))

    await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# Роутер сообщений
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, manual_expected_until, awaiting_hashtags_until
    uid = update.effective_user.id
    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    # если пользователь в планировщике — всё туда
    if _planner_active_for(uid):
        return await _route_to_planner(update, context)

    # если ждём хэштеги — обработаем здесь
    if awaiting_hashtags_until and now <= awaiting_hashtags_until:
        line = (update.message.text or update.message.caption or "").strip()
        tags = _parse_hashtags_line_user(line)
        post_data["ai_hashtags"] = tags
        post_data["user_tags_override"] = True
        awaiting_hashtags_until = None
        cur = " ".join(tags) if tags else "—"
        await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text=f"✅ Хэштеги обновлены: {cur}\nРежим Twitter: обязательные ссылки + твои теги (≤275).")
        return await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], image_url=None, header="Предпросмотр")

    # «Сделай сам» — только в течение 5 минут после кнопки
    if manual_expected_until and now <= manual_expected_until:
        return await handle_manual_input(update, context)

    # иначе — главное меню
    await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())

# -----------------------------------------------------------------------------
# STARTUP / SHUTDOWN / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    # Режим без ИИ: стартуем с пустыми полями и сразу показываем предпросмотр/меню
    post_data["text_en"] = post_data.get("text_en") or ""
    post_data["ai_hashtags"] = post_data.get("ai_hashtags") or []
    post_data["media_kind"] = "none"
    post_data["media_src"] = "tg"
    post_data["media_ref"] = None

    await send_single_preview(post_data["text_en"], post_data["ai_hashtags"], image_url=None, header="Предпросмотр (ручной режим)")
    await safe_send_message(approval_bot, chat_id=TELEGRAM_APPROVAL_CHAT_ID, text="Главное меню:", reply_markup=get_start_menu())
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
            log.warning(f"check_inactivity_shutdown error: {e}")
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

# ---- Глобальный обработчик ошибок Telegram ----
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"TG error: {context.error}")

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    # планировщик регистрирует свои хендлеры ПЕРВЫМ
    register_planner_handlers(app)

    # наши хендлеры — в высоких группах, чтобы planner.py ловил раньше при своей логике
    app.add_handler(CallbackQueryHandler(callback_handler), group=50)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.VIDEO | filters.Document.IMAGE, message_handler), group=50)

    # обработчик ошибок
    app.add_error_handler(on_error)

    asyncio.get_event_loop().create_task(check_inactivity_shutdown())

    # снизили частоту polling, чтобы не ловить Flood control
    app.run_polling(poll_interval=0.6, timeout=2)

if __name__ == "__main__":
    main()