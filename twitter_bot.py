# -*- coding: utf-8 -*-
"""
twitter_bot.py — согласование/публикация в Telegram и X (Twitter).

Ключевые правки:
- Надёжный watchdog (выключен по умолчанию; включается через ENV AUTO_SHUTDOWN_AFTER_SECONDS>0)
- Инициализация "последней активности" на старте, чтобы бот не вырубался сам
- Аккуратный обработчик старых callback'ов (Query is too old)
- Безопасные отправки в TG (ретраи) и корректные уведомления
- Обработчики для "подобрать хэштеги" и "план на день" (если planner подключён)
- Дедуп публикаций с TTL, хэши медиа, обрезка текста, обязательные хвосты
- ИИ-режим теперь читает обычные сообщения (как «Сделай сам») — управляется ENV AI_ACCEPT_ANY_MESSAGE
- Единая «сводка» об итогах публикации (успех/ошибка) для всех разделов
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
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

import requests
import tweepy
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot, ForceReply
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError
import aiosqlite

# GitHub (устранён DeprecationWarning по Auth)
from github import Github
try:
    from github import Auth as _GhAuth
except Exception:
    _GhAuth = None

import ai_client

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s"
)
log = logging.getLogger("twitter_bot")
log_ai = logging.getLogger("twitter_bot.ai")
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("telegram").setLevel(logging.INFO)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

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
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")  # не обязателен

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_IMAGE_PATH = os.getenv("GH_IMAGES_DIR", "images_for_posts")

AICOIN_WORKER_URL = os.getenv("AICOIN_WORKER_URL", "https://aicoin-bot-trigger.dfosjam.workers.dev/tg/webhook")
PUBLIC_TRIGGER_SECRET = (os.getenv("PUBLIC_TRIGGER_SECRET") or "").strip()
FALLBACK_PUBLIC_TRIGGER_SECRET = "z8PqH0e4jwN3rA1K"

# НОВОЕ: ИИ-режим читает обычные сообщения (как «Сделай сам»)
AI_ACCEPT_ANY_MESSAGE = (os.getenv("AI_ACCEPT_ANY_MESSAGE", "1") or "1").strip() \
    not in ("0", "false", "False", "no", "No")

_need_env = [
    "TELEGRAM_BOT_TOKEN_APPROVAL", "TELEGRAM_APPROVAL_CHAT_ID",
    "TELEGRAM_BOT_TOKEN_CHANNEL", "TELEGRAM_CHANNEL_USERNAME_ID",
    "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
    "ACTION_PAT_GITHUB", "ACTION_REPO_GITHUB",
]
_missing = [k for k in _need_env if not os.getenv(k)]
if _missing:
    log.error("Не заданы обязательные переменные окружения: %s", _missing)

# -----------------------------------------------------------------------------
# БОТ/ЧАТ/ЧАСОВОЙ ПОЯС
# -----------------------------------------------------------------------------
TZ = ZoneInfo("Europe/Kyiv")

approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL) if TELEGRAM_BOT_TOKEN_APPROVAL else None
channel_bot  = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL) if TELEGRAM_BOT_TOKEN_CHANNEL else None

BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None

# Чат согласования: может быть int (-100...) или '@username'
TELEGRAM_APPROVAL_CHAT_ID: Any = None
_raw_chat = (TELEGRAM_APPROVAL_CHAT_ID_STR or os.getenv("TELEGRAM_APPROVAL_CHAT_ID") or "").strip()
if _raw_chat.startswith("@"):
    TELEGRAM_APPROVAL_CHAT_ID = _raw_chat
    log.info("ENV: TELEGRAM_APPROVAL_CHAT_ID=%s (username)", TELEGRAM_APPROVAL_CHAT_ID)
else:
    try:
        TELEGRAM_APPROVAL_CHAT_ID = int(_raw_chat) if _raw_chat else 0
        log.info("ENV: TELEGRAM_APPROVAL_CHAT_ID=%s", TELEGRAM_APPROVAL_CHAT_ID)
    except Exception as _e:
        TELEGRAM_APPROVAL_CHAT_ID = 0
        log.error("ENV TELEGRAM_APPROVAL_CHAT_ID некорректен: %s", _e)

def _approval_chat_id() -> Any:
    global TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_APPROVAL_CHAT_ID_STR
    if isinstance(TELEGRAM_APPROVAL_CHAT_ID, int) and TELEGRAM_APPROVAL_CHAT_ID != 0:
        return TELEGRAM_APPROVAL_CHAT_ID
    if isinstance(TELEGRAM_APPROVAL_CHAT_ID, str) and TELEGRAM_APPROVAL_CHAT_ID.strip():
        return TELEGRAM_APPROVAL_CHAT_ID.strip()
    raw = (os.getenv("TELEGRAM_APPROVAL_CHAT_ID") or (TELEGRAM_APPROVAL_CHAT_ID_STR or "")).strip()
    if not raw:
        log.error("Approval chat id is not set (empty).")
        return 0
    if raw.startswith("@"):
        TELEGRAM_APPROVAL_CHAT_ID = raw
        return TELEGRAM_APPROVAL_CHAT_ID
    try:
        TELEGRAM_APPROVAL_CHAT_ID = int(raw)
        return TELEGRAM_APPROVAL_CHAT_ID
    except Exception:
        TELEGRAM_APPROVAL_CHAT_ID = 0
        log.error("Approval chat id is invalid (cannot parse).")
        return 0

# (Оставлено для совместимости: опционально ограничить пользователя)
try:
    APPROVAL_USER_ID = int(os.getenv("TELEGRAM_APPROVAL_USER_ID", "0") or "0")
except Exception:
    APPROVAL_USER_ID = 0

def _is_approved_user(update: Update) -> bool:
    if not update or not getattr(update, "effective_user", None):
        return False
    if APPROVAL_USER_ID and update.effective_user and update.effective_user.id != APPROVAL_USER_ID:
        return False
    return True

# Планировщик (опционально)
try:
    from planner import register_planner_handlers, open_planner
    log.info("Planner module loaded")
except Exception as _e:
    log.warning("Planner module not available: %s", _e)
    register_planner_handlers = lambda app: None
    open_planner = None

# -----------------------------------------------------------------------------
# ВОТЧДОГ (по умолчанию ВЫКЛЮЧЕН)
# -----------------------------------------------------------------------------
try:
    AUTO_SHUTDOWN_AFTER_SECONDS = int(os.getenv("AUTO_SHUTDOWN_AFTER_SECONDS", "0") or "0")
except Exception:
    AUTO_SHUTDOWN_AFTER_SECONDS = 0
ENABLE_WATCHDOG = AUTO_SHUTDOWN_AFTER_SECONDS > 0

# -----------------------------------------------------------------------------
# СТЕЙТЫ/НАСТРОЙКИ ПУБЛИКАЦИЙ
# -----------------------------------------------------------------------------
VERBATIM_MODE = False
AUTO_AI_IMAGE = False

TW_TAIL_REQUIRED = "🌐 https://getaicoin.com | 🐺 https://t.me/AiCoin_ETH"
TG_TAIL_HTML     = '<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/AiCoin_ETH">Twitter X</a>'

def _worker_url_with_secret() -> str:
    base = AICOIN_WORKER_URL or ""
    sec  = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
    if not base: return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}s={sec}" if sec else base

# Twitter API
def get_twitter_clients():
    if not (TWITTER_API_KEY and TWITTER_API_SECRET and TWITTER_ACCESS_TOKEN and TWITTER_ACCESS_TOKEN_SECRET):
        log.warning("Twitter ENV переменные заданы не полностью — клиенты не будут созданы.")
        return None, None
    client_v2 = tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
        bearer_token=TWITTER_BEARER_TOKEN
    )
    api_v1 = tweepy.API(
        tweepy.OAuth1UserHandler(
            TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return client_v2, api_v1

twitter_client_v2, twitter_api_v1 = get_twitter_clients()

# GitHub
try:
    if _GhAuth and GITHUB_TOKEN:
        _gh_auth = _GhAuth.Token(GITHUB_TOKEN)
        github_client = Github(auth=_gh_auth)
    else:
        github_client = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None
except Exception:
    github_client = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None

github_repo = github_client.get_repo(GITHUB_REPO) if (github_client and GITHUB_REPO) else None

# -----------------------------------------------------------------------------
# СТЕЙТ ПО ПОСТУ
# -----------------------------------------------------------------------------
post_data: Dict[str, Any] = {
    "text_en": "",
    "ai_hashtags": [],
    "media_kind": "none",     # none | image | video
    "media_src":  "tg",       # tg | url
    "media_ref":  None,       # file_id (tg) или url (url)
    "media_local_path": None, # путь временного файла
    "post_id": 0,
    "is_manual": False,
    "user_tags_override": False
}

pending_post = {"active": False, "timer": None, "timeout": 180, "mode": "normal"}
do_not_disturb = {"active": False}

last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None
awaiting_hashtags_until: Optional[datetime] = None
ROUTE_TO_PLANNER: set[int] = set()

# AI стейт
AI_STATE_G: Dict[str, Any] = {"mode": "idle"}
def ai_state_reset():
    AI_STATE_G.clear(); AI_STATE_G.update({"mode": "idle"})
    log_ai.info("AI|state.reset | mode=idle")

def ai_state_set(**kwargs):
    AI_STATE_G.update(kwargs)
    log_ai.info("AI|state.set | %s", " ".join(f"{k}={v}" for k, v in kwargs.items()))

def ai_state_get() -> Dict[str, Any]:
    return AI_STATE_G

def ai_set_last_topic(topic: str):
    AI_STATE_G["last_topic"] = (topic or "").strip()

def ai_get_last_topic() -> str:
    return AI_STATE_G.get("last_topic", "").strip()

# -----------------------------------------------------------------------------
# Адресовано ли сообщение нашему боту? (для групп)
# -----------------------------------------------------------------------------
def _message_addresses_bot(update: Update) -> bool:
    msg = update.message
    if not msg:
        return False
    chat = update.effective_chat
    if getattr(chat, "type", "") == "private":
        return True
    try:
        if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
            return (BOT_ID is None) or (msg.reply_to_message.from_user.id == BOT_ID)
    except Exception:
        pass
    text = (msg.text or msg.caption or "")
    entities = (msg.entities or []) + (msg.caption_entities or []) if msg else []
    if BOT_USERNAME and entities:
        for e in entities:
            if e.type == "mention":
                mention = text[e.offset:e.offset+e.length]
                if mention.lstrip("@").lower() == (BOT_USERNAME or "").lower():
                    return True
    return False

# -----------------------------------------------------------------------------
# Безопасные отправки и вспомогательные UI
# -----------------------------------------------------------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 ИИ", callback_data="ai_home")],
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_preview_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ПОСТ!", callback_data="post_both")],
        [InlineKeyboardButton("Пост в Twitter", callback_data="post_twitter"),
         InlineKeyboardButton("Пост в Telegram", callback_data="post_telegram")],
        [InlineKeyboardButton("✏️ Править текст", callback_data="ai_text_edit"),
         InlineKeyboardButton("🖼️ Изменить медиа", callback_data="ai_image_edit")],
        [InlineKeyboardButton("🤖 ИИ", callback_data="ai_home"),
         InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post"),
         InlineKeyboardButton("🗓 План на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb"),
         InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def start_worker_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Старт воркера", url=_worker_url_with_secret())]])

def ai_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 Сгенерировать текст по теме", callback_data="ai_generate")],
        [InlineKeyboardButton("🔁 Перегенерировать по последней теме", callback_data="ai_text_regen")],
        [InlineKeyboardButton("🔖 Подобрать хэштеги по текущему тексту", callback_data="ai_hashtags_suggest")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
    ])

def ai_text_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Текст ок", callback_data="ai_text_ok"),
         InlineKeyboardButton("🔁 Ещё вариант", callback_data="ai_text_regen")],
        [InlineKeyboardButton("✏️ Править текст", callback_data="ai_text_edit")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
    ])

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
    if bot is None:
        log.error("Bot is not initialized — cannot send message. kwargs=%s", kwargs)
        return None
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

async def send_with_start_button(chat_id: Any, text: str):
    try:
        await safe_send_message(approval_bot, chat_id=chat_id, text=text, reply_markup=start_worker_keyboard())
    except Exception:
        await safe_send_message(approval_bot, chat_id=chat_id, text=text)

# -----------------------------------------------------------------------------
# ДЕТЕКТОР языка «на английском»
# -----------------------------------------------------------------------------
_EN_PATTERNS = [
    r"\benglish\b", r"\bin\s+english\b", r"\bwrite\s+in\s+english\b",
    r"\bEN\b", r"\bENG\b",
    r"на\s+английск(ом|ий|ом языке)", r"по-английски", r"английском\s+языке"
]
def wants_english(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in _EN_PATTERNS)

# -----------------------------------------------------------------------------
# Уведомления «бот работает»
# -----------------------------------------------------------------------------
async def ai_progress(text: str):
    try:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=text)
    except Exception as e:
        log_ai.warning("AI|progress send fail: %s", e)

# -----------------------------------------------------------------------------
# ТИХ (Twitter length), хвосты, хэштеги
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
        if not t: return ""
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        return t
    def ok(t: str) -> bool:
        tl = t.lower()
        return ("ai" in tl) or ("crypto" in tl) or tl.startswith("$ai")
    for g in groups:
        if not g: continue
        items = g.split() if isinstance(g, str) else list(g)
        for raw in items:
            tag = norm(raw)
            if not tag or not ok(tag): continue
            k = tag.lower()
            if k in seen: continue
            seen.add(k); out.append(tag)
    return " ".join(out)

def _parse_hashtags_line_user(line: str) -> List[str]:
    if not line: return []
    tmp = re.sub(r"[,\u00A0;]+", " ", line.strip())
    raw = [w for w in tmp.split() if w]
    seen, out = set(), []
    for t in raw:
        t = t.strip()
        if not t: continue
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        k = t.lower()
        if k in seen: continue
        seen.add(k); out.append(t)
    return out

def trim_preserving_urls(body: str, max_len: int) -> str:
    body = (body or "").strip()
    if max_len <= 0 or not body: return ""
    parts, last = [], 0
    for m in _URL_RE.finditer(body):
        if m.start() > last:
            parts.append((False, body[last:m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    if last < len(body):
        parts.append((False, body[last:]))
    out = ""
    for is_url, seg in parts:
        if not seg: continue
        if is_url:
            cand = (out + (" " if out else "") + seg).strip()
            if twitter_len(cand) <= max_len:
                out = cand
            else:
                continue
        else:
            if twitter_len(out) >= max_len: break
            remain = max_len - twitter_len(out) - (1 if out else 0)
            if remain <= 0: break
            chunk = seg.strip()
            if not chunk: continue
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

def _tail_block(ai_tags: List[str] | None) -> str:
    tags_str = _dedup_hashtags(MY_HASHTAGS_STR, ai_tags or [])
    return (TW_TAIL_REQUIRED + (f" {tags_str}" if tags_str else "")).strip()

def build_tweet_with_tail_275(body_text: str, ai_tags: List[str] | None) -> str:
    MAX_TWEET = 275
    body = (body_text or "").strip()
    tail_full = _tail_block(ai_tags)
    tail_req  = TW_TAIL_REQUIRED
    tail = tail_full if twitter_len(tail_full) <= MAX_TWEET else tail_req
    sep = 1 if (body and tail) else 0
    allowed = max(0, MAX_TWEET - twitter_len(tail) - sep)
    body_trimmed = trim_to_twitter_len(body, allowed)
    was_trimmed_initial = twitter_len(body) > twitter_len(body_trimmed)
    if was_trimmed_initial and tail:
        allowed2 = max(0, MAX_TWEET - twitter_len(tail) - sep - 2)
        body_trimmed = trim_to_twitter_len(body, allowed2)
        tweet = f"{body_trimmed} … {tail}".strip() if body_trimmed else tail
    else:
        tweet = f"{body_trimmed} {tail}".strip() if (body_trimmed and tail) else (body_trimmed or tail)
    if twitter_len(tweet) > MAX_TWEET:
        if tail != tail_req:
            tail = tail_req
            was_trimmed = twitter_len(body) > allowed
            if was_trimmed:
                allowed2 = max(0, MAX_TWEET - twitter_len(tail) - (1 if body else 0) - 2)
                body_trimmed = trim_to_twitter_len(body, allowed2)
                tweet = f"{body_trimmed} … {tail}".strip() if body_trimmed else tail
            else:
                allowed = max(0, MAX_TWEET - twitter_len(tail) - (1 if body else 0))
                body_trimmed = trim_to_twitter_len(body, allowed)
                tweet = f"{body_trimmed} {tail}".strip() if (body_trimmed and tail) else (body_trimmed or tail)
    if twitter_len(tweet) > MAX_TWEET:
        tweet = tail_req
    return tweet

def build_tweet_user_hashtags_275(body_text: str, user_tags: List[str] | None) -> str:
    MAX_TWEET = 275
    body = (body_text or "").strip()
    tags = user_tags or []
    tags_str = " ".join(tags).strip()
    tail_links = TW_TAIL_REQUIRED.strip()
    tail_full = (tail_links + (f" {tags_str}" if tags_str else "")).strip()
    sep = 1 if (body and tail_full) else 0
    allowed = max(0, MAX_TWEET - twitter_len(tail_full) - sep)
    body_trimmed = trim_preserving_urls(body, allowed)
    was_trimmed = twitter_len(body) > twitter_len(body_trimmed)
    if was_trimmed:
        allowed2 = max(0, MAX_TWEET - twitter_len(tail_full) - sep - 2)
        body_trimmed = trim_preserving_urls(body, allowed2)
        tweet = f"{body_trimmed} … {tail_full}".strip() if body_trimmed else tail_full
    else:
        tweet = f"{body_trimmed} {tail_full}".strip() if (body_trimmed and tail_full) else (body_trimmed or tail_full)
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
# TG лимиты и сборка финального HTML
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
# GitHub helpers (аплоад/удаление)
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path: str, filename: Optional[str] = None) -> Optional[str]:
    if not github_repo:
        log.error("GitHub repo is not configured")
        return None
    try:
        ext = os.path.splitext(image_path)[1] or ".png"
        if not filename:
            filename = f"{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
        rel_path = f"{GITHUB_IMAGE_PATH}/{filename}"
        with open(image_path, "rb") as img_file:
            content_b64 = base64.b64encode(img_file.read()).decode("utf-8")
        github_repo.create_file(rel_path, "upload image for post", content_b64, branch="main")
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{rel_path}"
    except Exception as e:
        log.error(f"Ошибка загрузки файла на GitHub: {e}")
        return None

def delete_image_from_github(filename):
    if not github_repo:
        return
    try:
        contents = github_repo.get_contents(f"{GITHUB_IMAGE_PATH}/{filename}", ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
    except Exception as e:
        log.error(f"Ошибка удаления файла на GitHub: {e}")

# -----------------------------------------------------------------------------
# Загрузка медиа (универсальная)
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Вспомогательные функции URL-медиа
# -----------------------------------------------------------------------------
def _guess_ext_from_headers_and_url(ctype: str | None, url: str | None, default_img_ext: str = ".jpg") -> str:
    mapping = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-m4v": ".m4v",
    }
    if ctype:
        c = ctype.split(";")[0].strip().lower()
        if c in mapping:
            return mapping[c]
    if url:
        low = url.lower()
        for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".m4v", ".webm"):
            if low.endswith(e):
                return e
    return default_img_ext

def _sniff_ext_from_bytes(head: bytes, fallback: str) -> str:
    """
    Определяем расширение по сигнатуре первых байт. Исправлено: без обрыва строки b"".
    """
    try:
        b = head or b""
        if len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if len(b) >= 3 and b[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if len(b) >= 6 and b[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if len(b) >= 12 and b[8:12] == b"WEBP":
            return ".webp"
        if len(b) >= 12 and b[4:8] == b"ftyp":
            return ".mp4"
        if len(b) >= 4 and b[:4] == b"RIFF":
            return ".webm"
    except Exception:
        pass
    return fallback

def _download_to_temp_file(url: str, suffix: Optional[str] = None) -> Optional[str]:
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=60)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type")
        guessed = _guess_ext_from_headers_and_url(ctype, url, default_img_ext=".jpg")
        ext = suffix if suffix else guessed
        head = r.content[:64]
        ext = _sniff_ext_from_bytes(head, ext)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
        tmp.write(r.content)
        tmp.close()
        log.info("MEDIA|download url=%s ctype=%s -> %s bytes=%s", url, ctype, tmp.name, len(r.content))
        return tmp.name
    except Exception as e:
        log.warning("MEDIA|download fail url=%s err=%s", url, e)
        return None
# -----------------------------------------------------------------------------
# Публикация в X (Twitter)
# -----------------------------------------------------------------------------
def _twitter_media_category(kind: str) -> str | None:
    if kind == "video":
        return "tweet_video"
    if kind == "image":
        return None
    return None

async def publish_post_to_twitter(final_text_ready: str | None) -> bool:
    try:
        if not twitter_client_v2 or not twitter_api_v1:
            raise RuntimeError("Twitter clients are not configured (check API keys and tokens).")

        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")
        clean_text = (final_text_ready or "").strip()

        log.info("TW|publish start kind=%s src=%s text_len=%s", mk, msrc, len(clean_text))

        media_ids = None
        local_path = None

        if mk in ("image", "video") and mref:
            if msrc == "url":
                local_path = _download_to_temp_file(mref)
                if not local_path:
                    raise RuntimeError("Не удалось получить медиа (URL) для X")
            else:
                local_path = await download_to_temp_local(mref, is_telegram=True, bot=approval_bot)
            post_data["media_local_path"] = local_path
            size = os.path.getsize(local_path) if (local_path and os.path.exists(local_path)) else -1
            log.info("TW|media local=%s size=%s", local_path, size)

            if mk == "image":
                media = twitter_api_v1.media_upload(filename=local_path)
                media_ids = [media.media_id_string]
                log.info("TW|media_upload image ok id=%s", media.media_id_string)
            else:
                category = _twitter_media_category(mk)
                media = twitter_api_v1.media_upload(filename=local_path, chunked=True, media_category=category)
                media_ids = [media.media_id_string]
                log.info("TW|media_upload video ok id=%s", media.media_id_string)

        tweet_resp = None
        if media_ids and not clean_text:
            try:
                tweet_resp = twitter_client_v2.create_tweet(media={"media_ids": media_ids})
            except TypeError:
                tweet_resp = twitter_client_v2.create_tweet(media_ids=media_ids)
        elif not media_ids and clean_text:
            tweet_resp = twitter_client_v2.create_tweet(text=clean_text)
        else:
            try:
                tweet_resp = twitter_client_v2.create_tweet(text=clean_text, media={"media_ids": media_ids})
            except TypeError:
                tweet_resp = twitter_client_v2.create_tweet(text=clean_text, media_ids=media_ids)

        try:
            tw_id = None
            if tweet_resp and hasattr(tweet_resp, "data") and isinstance(tweet_resp.data, dict):
                tw_id = tweet_resp.data.get("id")
            log.info("TW|create_tweet ok id=%s", tw_id)
        except Exception:
            log.info("TW|create_tweet ok (no-id-parsed) resp=%s", str(tweet_resp)[:200])

        if local_path:
            try:
                os.remove(local_path)
                log.info("TW|tmp removed %s", local_path)
            except Exception as rm_e:
                log.warning("TW|tmp remove fail %s", rm_e)
            post_data["media_local_path"] = None

        return True

    except tweepy.TweepyException as e:
        log.error("TW|TweepyException: %s", e)
        await send_with_start_button(
            _approval_chat_id(),
            "❌ X/Twitter: ошибка загрузки. Проверь права app (Read+Write) и соответствие медиа требованиям."
        )
        lp = post_data.get("media_local_path")
        if lp:
            try:
                os.remove(lp)
                log.info("TW|cleanup tmp removed %s", lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False
    except Exception as e:
        log.error("TW|general error: %s", e)
        await send_with_start_button(_approval_chat_id(), f"❌ X/Twitter: {e}")
        lp = post_data.get("media_local_path")
        if lp:
            try:
                os.remove(lp)
                log.info("TW|cleanup tmp removed %s", lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Публикация в Telegram
# -----------------------------------------------------------------------------
async def publish_post_to_telegram(text: str | None) -> bool:
    try:
        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")
        log.info("TG|publish start kind=%s src=%s ref=%s", mk, msrc, (str(mref)[:120] if mref else None))

        final_html = build_tg_final(text or "", for_photo_caption=(mk in ("image","video")))
        if mk == "none" or not mref:
            if not final_html.strip():
                await send_with_start_button(_approval_chat_id(), "⚠️ Telegram: пусто (нет текста и медиа).")
                return False
            msg = await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=final_html, parse_mode="HTML", disable_web_page_preview=True
            )
            log.info("TG|send_message ok id=%s len=%s", getattr(msg, "message_id", None), len(final_html))
            return True

        # Подготовка локального файла
        if msrc == "url":
            local_path = _download_to_temp_file(mref)
            if not local_path:
                raise RuntimeError("Не удалось скачать медиа из URL для Telegram")
        else:
            tg_file = await approval_bot.get_file(mref)
            ext = ".mp4" if (tg_file.file_path or "").lower().endswith(".mp4") else (".jpg" if mk == "image" else ".bin")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            await tg_file.download_to_drive(tmp.name)
            local_path = tmp.name
            log.info("TG|get_file id=%s path=%s kind=%s", mref, local_path, mk)

        post_data["media_local_path"] = local_path

        if mk == "image":
            with open(local_path, "rb") as f:
                msg = await channel_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, photo=f,
                    caption=(final_html if final_html.strip() else None), parse_mode="HTML"
                )
            log.info("TG|send_photo ok id=%s", getattr(msg, "message_id", None))
        elif mk == "video":
            with open(local_path, "rb") as f:
                msg = await channel_bot.send_video(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, video=f,
                    supports_streaming=True,
                    caption=(final_html if final_html.strip() else None), parse_mode="HTML"
                )
            log.info("TG|send_video ok id=%s", getattr(msg, "message_id", None))
        else:
            msg = await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=final_html, parse_mode="HTML", disable_web_page_preview=True
            )
            log.info("TG|send_message(fallback) ok id=%s", getattr(msg, "message_id", None))

        try:
            os.remove(local_path)
            log.info("TG|tmp removed %s", local_path)
        except Exception as rm_e:
            log.warning("TG|tmp remove fail %s", rm_e)
        post_data["media_local_path"] = None
        return True

    except Exception as e:
        log.error("TG|publish error: %s", e)
        await send_with_start_button(_approval_chat_id(), f"❌ Ошибка публикации в Telegram: {e}")
        lp = post_data.get("media_local_path")
        if lp:
            try:
                os.remove(lp)
                log.info("TG|cleanup tmp removed %s", lp)
            except Exception:
                pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# Очистка ИИ-текста и обрезка к целевой длине
# -----------------------------------------------------------------------------
def _get_len_config() -> Tuple[int, int]:
    try:
        target = int(os.getenv("TARGET_CHAR_LEN", "666") or "666")
    except Exception:
        target = 666
    try:
        tol = int(os.getenv("TARGET_CHAR_TOL", "20") or "20")
    except Exception:
        tol = 20
    return max(0, target), max(0, tol)

_BRACKETS_RE = re.compile(r"\[[^\]]*\]")

def sanitize_ai_text(s: str) -> str:
    s = _BRACKETS_RE.sub("", s or "")
    s = re.sub(r"(Website\s*\|\s*Twitter\s*X)\s*", "", s, flags=re.I)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def adjust_text_to_target_length(text: str) -> str:
    target, tol = _get_len_config()
    low, high = max(0, target - tol), target + tol
    s = (text or "").strip()
    if len(s) <= high:
        return s
    cut = s[:high]
    m = re.search(r"[.!?…](\s|$).*$", cut)
    if m:
        cut = cut[:m.start()+1]
    return cut.strip()

# -----------------------------------------------------------------------------
# БД истории/дедуп (асинхронная, авто-очистка > TTL)
# -----------------------------------------------------------------------------
DB_FILE = "post_history.db"
DEDUP_TTL_DAYS = int(os.getenv("DEDUP_TTL_DAYS", "15") or "15")

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
        cutoff = (datetime.now(TZ) - timedelta(days=DEDUP_TTL_DAYS)).isoformat()
        await db.execute("DELETE FROM posts WHERE timestamp < ?", (cutoff,))
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
        log.debug("HASH|media: none")
        return None
    try:
        if src == "url":
            r = requests.get(ref, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            r.raise_for_status()
            b = r.content
            h = sha256_hex(b)
            log.info("HASH|media[url] kind=%s bytes=%s sha256=%s", kind, len(b), h[:16])
            return h
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
            h = sha256_hex(b)
            log.info("HASH|media[tg] kind=%s bytes=%s sha256=%s", kind, len(b), h[:16])
            return h
    except Exception as e:
        log.warning("HASH|fail: %s", e)
        return None

async def is_duplicate_post(text: str, media_hash: Optional[str]) -> bool:
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None
    async with aiosqlite.connect(DB_FILE) as db:
        cutoff = (datetime.now(TZ) - timedelta(days=DEDUP_TTL_DAYS)).isoformat()
        await db.execute("DELETE FROM posts WHERE timestamp < ?", (cutoff,))
        await db.commit()
        q = "SELECT 1 FROM posts WHERE COALESCE(text_hash,'') = COALESCE(?, '') AND COALESCE(image_hash,'') = COALESCE(?, '') LIMIT 1"
        async with db.execute(q, (text_hash, media_hash or None)) as cur:
            row = await cur.fetchone()
            is_dup = row is not None
            log.info("DEDUP|text_hash=%s img_hash=%s -> %s", (text_hash or "")[:12], (media_hash or "")[:12], is_dup)
            return is_dup

async def save_post_to_history(text: str, media_hash: Optional[str]):
    text_norm = normalize_text_for_hashing(text)
    text_hash = sha256_hex(text_norm.encode("utf-8")) if text_norm else None
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute("INSERT INTO posts (text, text_hash, timestamp, image_hash) VALUES (?, ?, ?, ?)",
                             (text, text_hash, datetime.now(TZ).isoformat(), media_hash or None))
            await db.commit()
            log.info("HISTORY|saved text_hash=%s img_hash=%s", (text_hash or "")[:12], (media_hash or "")[:12])
        except Exception as e:
            log.warning("HISTORY|insert fail (возможно дубликат): %s", e)

# -----------------------------------------------------------------------------
# Предпросмотр (с медиа/без)
# -----------------------------------------------------------------------------
async def send_single_preview(text_en: str, ai_hashtags=None, header: str | None = "Предпросмотр"):
    text_for_message = build_telegram_preview(text_en, ai_hashtags or [])
    caption_for_media = build_tg_final(text_en, for_photo_caption=True)
    hdr = f"<b>{html_escape(header)}</b>\n" if header else ""
    hashtags_line = ("<i>Хэштеги:</i> " + html_escape(" ".join(ai_hashtags or []))) if (ai_hashtags) else "<i>Хэштеги:</i> —"
    text_message = f"{hdr}{text_for_message}\n\n{hashtags_line}".strip()

    mk, msrc, mref = post_data.get("media_kind"), post_data.get("media_src"), post_data.get("media_ref")
    log.info("PREVIEW|kind=%s src=%s ref=%s", mk, msrc, (str(mref)[:100] if mref else None))
    try:
        if mk == "video" and mref:
            try:
                await approval_bot.send_video(
                    chat_id=_approval_chat_id(), video=mref, supports_streaming=True,
                    caption=(caption_for_media if caption_for_media.strip() else None),
                    parse_mode="HTML", reply_markup=start_preview_keyboard()
                )
                log.info("PREVIEW|video ok")
            except Exception as ee:
                log.warning("PREVIEW|video inline fail: %s; fallback text", ee)
                await safe_send_message(
                    approval_bot, chat_id=_approval_chat_id(),
                    text=text_message, parse_mode="HTML",
                    reply_markup=start_preview_keyboard()
                )
        elif mk == "image" and mref:
            try:
                await approval_bot.send_photo(
                    chat_id=_approval_chat_id(), photo=mref,
                    caption=(caption_for_media if caption_for_media.strip() else None),
                    parse_mode="HTML", reply_markup=start_preview_keyboard()
                )
                log.info("PREVIEW|image ok")
            except Exception as ee:
                log.warning("PREVIEW|image inline fail: %s; fallback text", ee)
                await safe_send_message(
                    approval_bot, chat_id=_approval_chat_id(),
                    text=text_message, parse_mode="HTML",
                    reply_markup=start_preview_keyboard()
                )
        else:
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=start_preview_keyboard()
            )
            log.info("PREVIEW|text-only ok")
    except Exception as e:
        log.warning("PREVIEW|fallback text due to: %s", e)
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=start_preview_keyboard()
        )

# -----------------------------------------------------------------------------
# Генерация ИИ-изображения (через ai_client) + аплоад в GitHub
# -----------------------------------------------------------------------------
async def _generate_ai_image_explicit(topic: str) -> Tuple[Optional[str], Optional[str]]:
    if not hasattr(ai_client, "ai_generate_image"):
        log_ai.info("AI|image.skip | функция ai_generate_image отсутствует.")
        return "⚠️ Генерация изображения недоступна (ai_generate_image отсутствует).", None
    try:
        await ai_progress("🖼 Бот генерирует изображение…")
        img_path, warn_img = ai_client.ai_generate_image(topic or "")
        if not img_path or not os.path.exists(img_path):
            log_ai.info("AI|image.fail | генерация не вернула файл.")
            return (warn_img or "⚠️ Не удалось сгенерировать изображение ИИ."), None

        await ai_progress("📤 Загружаю изображение…")
        raw_url = upload_image_to_github(img_path, filename=None)
        try:
            os.remove(img_path)
        except Exception:
            pass
        if not raw_url:
            log_ai.info("AI|image.fail | upload to GitHub failed.")
            return (warn_img or "⚠️ Upload image failed."), None

        post_data["media_kind"] = "image"
        post_data["media_src"]  = "url"
        post_data["media_ref"]  = raw_url
        log_ai.info("AI|image.ok | url=%s", raw_url)
        await ai_progress("✅ Изображение готово.")
        return (warn_img or ""), raw_url
    except Exception as e:
        log_ai.warning("AI|image.exception: %s", e)
        return "⚠️ Ошибка генерации изображения.", None

# -----------------------------------------------------------------------------
# Общие помощники твит-текста (пользовательские теги vs авто)
# -----------------------------------------------------------------------------
def build_twitter_payload_text(base_text_en: str) -> str:
    if post_data.get("user_tags_override"):
        return build_tweet_user_hashtags_275(base_text_en, post_data.get("ai_hashtags") or [])
    return build_twitter_text(base_text_en, post_data.get("ai_hashtags") or [])
# -----------------------------------------------------------------------------
# Общая публикация (Telegram + X) с дедупом + сводное уведомление
# -----------------------------------------------------------------------------
def build_twitter_payload_text(base_text_en: str) -> str:
    if post_data.get("user_tags_override"):
        return build_tweet_user_hashtags_275(base_text_en, post_data.get("ai_hashtags") or [])
    return build_twitter_text(base_text_en, post_data.get("ai_hashtags") or [])

async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()

    twitter_final_text = build_twitter_payload_text(base_text_en)
    telegram_text_preview = build_telegram_preview(base_text_en, None)

    if do_not_disturb["active"]:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

    media_hash = await compute_media_hash_from_state()
    tg_status = tw_status = None
    tg_dup = tw_dup = False

    if publish_tg:
        if await is_duplicate_post(telegram_text_preview, media_hash):
            tg_dup = True
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=base_text_en)
            if tg_status:
                final_html_saved = build_tg_final(base_text_en, for_photo_caption=(post_data.get("media_kind") in ("image","video")))
                await save_post_to_history(final_html_saved, media_hash)

    if publish_tw:
        if await is_duplicate_post(twitter_final_text, media_hash):
            tw_dup = True
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Дубликат для X (Twitter). Публикация пропущена.")
            tw_status = False
        else:
            tw_status = await publish_post_to_twitter(twitter_final_text)
            if tw_status:
                await save_post_to_history(twitter_final_text, media_hash)

    # Сводка (единое уведомление «успех/ошибка/пропуск»)
    def fmt(name: str, status, dup: bool) -> str:
        if status is True:
            return f"{name}: ✅ опубликовано"
        if dup:
            return f"{name}: ⏭️ дубликат"
        if status is False:
            return f"{name}: ❌ ошибка"
        return f"{name}: —"

    if publish_tg or publish_tw:
        summary = "📣 Итоги публикации:\n" + "\n".join([
            fmt("Telegram", tg_status, tg_dup) if publish_tg else "Telegram: —",
            fmt("Twitter",  tw_status, tw_dup) if publish_tw else "Twitter: —",
        ])
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=summary)

# -----------------------------------------------------------------------------
# Этап: ввод темы/контента для ИИ (принимает текст/медиа/URL) — логика как «Сделай сам»
# -----------------------------------------------------------------------------
async def handle_ai_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_approved_user(update):
        return
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=600)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    raw_text = (update.message.text or update.message.caption or "").strip()
    media_kind, media_src, media_ref = "none", "tg", None
    kind_logged = "text"

    # Медиа/URL — полностью повторяет поведение «Сделай сам»
    if getattr(update.message, "photo", None):
        media_kind, media_ref = "image", update.message.photo[-1].file_id; kind_logged = "photo"
    elif getattr(update.message, "video", None):
        media_kind, media_ref = "video", update.message.video.file_id; kind_logged = "video"
    elif getattr(update.message, "document", None):
        mime = (update.message.document.mime_type or ""); fid  = update.message.document.file_id
        if mime.startswith("video/"): media_kind, media_ref = "video", fid; kind_logged = "video"
        elif mime.startswith("image/"): media_kind, media_ref = "image", fid; kind_logged = "image"
    elif raw_text and raw_text.startswith("http"):
        url = raw_text.split()[0]
        if any(url.lower().endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
            media_kind, media_src, media_ref = "video", "url", url; raw_text = raw_text[len(url):].strip(); kind_logged = "video_url"
        elif any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
            media_kind, media_src, media_ref = "image", "url", url; raw_text = raw_text[len(url):].strip(); kind_logged = "image_url"

    log_ai.info("AI|recv | chat=%s | kind=%s | len=%s | head=%r", update.effective_chat.id, kind_logged, len(raw_text), raw_text[:120])

    topic = (raw_text or "").strip() or ai_get_last_topic()
    if not topic:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Отправьте тему поста (любой текст).")
        return

    # Принудительный EN, если явно попросили
    locale_hint = "en" if wants_english(topic) else None
    if locale_hint == "en" and not re.search(r"[A-Za-z]", topic):
        topic = f"{topic} (write in English)"
    ai_set_last_topic(topic)

    await ai_progress("🧠 Бот генерирует текст…")

    # Генерация текста
    try:
        txt, warn_t = ai_client.ai_generate_text(topic)
        if locale_hint == "en" and re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", txt or ""):
            try:
                txt = ai_client.generate_text(topic, locale_hint="en")
                warn_t = (warn_t or "")
                if "forced EN" not in warn_t:
                    warn_t = (warn_t + " | forced EN").strip(" |")
            except Exception:
                pass
    except Exception as e:
        log_ai.warning("AI|text exception: %s", e)
        txt, warn_t = "", f"local text fallback ({e})"

    txt = adjust_text_to_target_length(sanitize_ai_text(txt or ""))
    post_data["text_en"] = txt
    post_data["media_kind"] = media_kind
    post_data["media_src"]  = media_src
    post_data["media_ref"]  = media_ref

    ai_state_set(mode="confirm_text", await_until=(now + timedelta(minutes=5)))
    header = "ИИ сгенерировал текст"
    if warn_t:
        header += f" — {warn_t}"
    msg = (
        f"<b>{html_escape(header)}</b>\n\n"
        f"{build_telegram_preview(post_data['text_en'])}\n\n"
        f"Подходит ли текст?"
    )
    await safe_send_message(
        approval_bot, chat_id=_approval_chat_id(),
        text=msg, parse_mode="HTML",
        reply_markup=ai_text_confirm_keyboard()
    )

# -----------------------------------------------------------------------------
# Ручной режим («Сделай сам»)
# -----------------------------------------------------------------------------
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
    if not _is_approved_user(update):
        return
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=600)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    text = (update.message.text or update.message.caption or "").strip()
    media_kind = "none"; media_src = "tg"; media_ref = None

    if getattr(update.message, "photo", None):
        media_kind = "image"; media_ref = update.message.photo[-1].file_id
        log_ai.info("SELF|recv photo | chat=%s", update.effective_chat.id)
    elif getattr(update.message, "video", None):
        media_kind = "video"; media_ref = update.message.video.file_id
        log_ai.info("SELF|recv video | chat=%s", update.effective_chat.id)
    elif getattr(update.message, "document", None):
        mime = (update.message.document.mime_type or "")
        fid  = update.message.document.file_id
        if mime.startswith("video/"): media_kind = "video"; media_ref = fid; log_ai.info("SELF|recv doc.video | chat=%s", update.effective_chat.id)
        elif mime.startswith("image/"): media_kind = "image"; media_ref = fid; log_ai.info("SELF|recv doc.image | chat=%s", update.effective_chat.id)
    elif text and text.startswith("http"):
        url = text.split()[0]
        if any(url.lower().endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
            media_kind = "video"; media_src = "url"; media_ref = url
            text = text[len(url):].strip()
            log_ai.info("SELF|recv video_url | chat=%s", update.effective_chat.id)
        elif any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
            media_kind = "image"; media_src = "url"; media_ref = url
            text = text[len(url):].strip()
            log_ai.info("SELF|recv image_url | chat=%s", update.effective_chat.id)
    else:
        log_ai.info("SELF|recv text | chat=%s | len=%s | head=%r", update.effective_chat.id, len(text), text[:120])

    text = adjust_text_to_target_length(sanitize_ai_text(text))
    post_data["text_en"] = text
    post_data["media_kind"] = media_kind
    post_data["media_src"]  = media_src
    post_data["media_ref"]  = media_ref
    post_data["media_local_path"] = None
    post_data["post_id"] += 1
    post_data["is_manual"] = True

    await send_single_preview(post_data["text_en"], post_data.get("ai_hashtags") or [], header="Предпросмотр")
    manual_expected_until = None

# -----------------------------------------------------------------------------
# CALLBACKS
# -----------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        q = update.callback_query
    except Exception:
        return
    if not q:
        return

    if not _is_approved_user(update):
        await safe_q_answer(q)
        return

    global last_button_pressed_at, last_action_time, manual_expected_until, awaiting_hashtags_until
    data = q.data

    ok = await safe_q_answer(q)  # «Query is too old» обрабатываем мягко

    now = datetime.now(TZ)
    last_button_pressed_at = now
    pending_post.update(active=True, timer=now, timeout=600)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    if 0 in last_action_time and (now - last_action_time[0]).seconds < 1:
        return
    last_action_time[0] = now

    if data == "cancel_to_main":
        ROUTE_TO_PLANNER.clear()
        awaiting_hashtags_until = None
        ai_state_set(mode="idle")
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="Главное меню:", reply_markup=get_start_menu())
        return

    if data == "shutdown_bot":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        msg = f"🔴 Бот выключен.\nСледующий пост: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}"
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=msg, reply_markup=start_worker_keyboard())
        await asyncio.sleep(1)
        shutdown_bot_and_exit()
        return

    if data == "self_post":
        ROUTE_TO_PLANNER.clear()
        awaiting_hashtags_until = None
        ai_state_set(mode="idle")
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="✍️ Введите текст поста (EN) и (опционально) приложите фото/видео одним сообщением:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="cancel_to_main")]
            ])
        )
        manual_expected_until = now + timedelta(minutes=5)
        return

    if data == "approve":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр")
        return

    if data == "edit_hashtags":
        awaiting_hashtags_until = now + timedelta(minutes=3)
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🔖 Пришлите строку с тегами. Пример: <code>#AiCoin #AI $Ai #crypto</code>",
            parse_mode="HTML"
        )
        return

    if data == "ai_home":
        ai_state_set(mode="ai_home")
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🤖 Режим ИИ. Пришлите тему (можно с медиа или URL). После текста спрошу, нужна ли картинка.",
            reply_markup=ai_home_keyboard()
        )
        return

    if data == "ai_generate":
        ai_state_set(mode="await_topic", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🧠 Введите тему поста (EN/RU/UA). Можно приложить картинку/видео или URL. У меня есть 5 минут.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Тема поста…")
        )
        return

    if data == "ai_hashtags_suggest":
        base = (post_data.get("text_en") or "").strip()
        if base and hasattr(ai_client, "suggest_hashtags"):
            try:
                tags = ai_client.suggest_hashtags(base) or []
                tags = _parse_hashtags_line_user(" ".join(tags))
                post_data["ai_hashtags"] = tags
                post_data["user_tags_override"] = False
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(),
                                        text=f"✅ Хэштеги подобраны: {' '.join(tags) if tags else '—'}")
                await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (теги авто)")
            except Exception as e:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=f"⚠️ Не удалось сгенерировать хэштеги: {e}")
        else:
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(),
                                    text="ℹ️ Сначала дайте текст поста (или нажмите «Сделай сам»), затем повторите подбор хэштегов.")
        return

    # === ТЕКСТ подтверждение/редактирование ===
    if data == "ai_text_ok":
        ai_state_set(mode="confirm_image", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🖼 Нужна картинка к посту?",
            reply_markup=_image_confirm_keyboard_for_state()
        )
        return

    if data == "ai_text_regen":
        last_topic = ai_get_last_topic()
        if not last_topic:
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⚠️ Ещё нет сохранённой темы. Нажмите «🧠 Сгенерировать текст по теме».",
                reply_markup=ai_home_keyboard()
            )
        else:
            await ai_progress("🧠 Бот генерирует текст…")
            try:
                txt, warn = ai_client.ai_generate_text(last_topic)
                if wants_english(last_topic) and re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", txt or ""):
                    try:
                        txt = ai_client.generate_text(last_topic, locale_hint="en")
                        warn = (warn or "")
                        if "forced EN" not in (warn or ""):
                            warn = (warn + " | forced EN").strip(" |")
                    except Exception:
                        pass
            except Exception as e:
                txt, warn = "", f"regen fallback ({e})"
            txt = adjust_text_to_target_length(sanitize_ai_text(txt))
            post_data["text_en"] = txt
            ai_state_set(mode="confirm_text", await_until=(now + timedelta(minutes=5)))
            hdr = "ИИ перегенерировал текст"
            if warn:
                hdr += f" — {warn}"
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text=f"<b>{html_escape(hdr)}</b>\n\n{build_telegram_preview(post_data['text_en'])}\n\nПодходит ли текст?",
                parse_mode="HTML", reply_markup=ai_text_confirm_keyboard()
            )
        return

    if data == "ai_text_edit":
        ai_state_set(mode="await_text_edit", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="✏️ Пришлите новый текст поста (EN) одним сообщением (5 минут)."
        )
        return

    # === КАРТИНКА ===
    if data == "ai_image_edit":
        ai_state_set(mode="confirm_image", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🖼 Что делаем с медиа?",
            reply_markup=_image_confirm_keyboard_for_state()
        )
        return

    if data == "ai_img_gen":
        topic = ai_get_last_topic() or (post_data.get("text_en") or "")[:200]
        warn_img, url = await _generate_ai_image_explicit(topic)
        header = "Предпросмотр (текст согласован; изображение сгенерировано)"
        if warn_img:
            header += f" — {warn_img}"
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header=header)
        return

    if data == "ai_img_upload":
        ai_state_set(mode="await_image", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="📤 Пришлите фото/видео или URL на картинку/видео (5 минут)."
        )
        return

    if data == "ai_img_skip":
        post_data["media_kind"] = "none"
        post_data["media_src"]  = "tg"
        post_data["media_ref"]  = None
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (без изображения)")
        return

    if data == "ai_img_keep":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (текущее медиа сохранено)")
        return

    if data == "ai_img_back_to_text":
        ai_state_set(mode="confirm_text", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=f"<b>Возврат к тексту</b>\n\n{build_telegram_preview(post_data.get('text_en') or '')}\n\nПодходит ли текст?",
            parse_mode="HTML", reply_markup=ai_text_confirm_keyboard()
        )
        return

    # === Публикация ===
    if data in ("post_twitter", "post_telegram", "post_both"):
        await publish_flow(publish_tg=(data != "post_twitter"), publish_tw=(data != "post_telegram"))
        return

    if data == "do_not_disturb":
        do_not_disturb["active"] = not do_not_disturb["active"]
        status = "включён" if do_not_disturb["active"] else "выключен"
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=f"🌙 Режим «Не беспокоить» {status}.",
            reply_markup=get_start_menu()
        )
        return

    if data == "end_day":
        do_not_disturb["active"] = True
        tomorrow = datetime.combine(datetime.now(TZ).date() + timedelta(days=1), dt_time(hour=9, tzinfo=TZ))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=f"🔚 Работа завершена. Следующая публикация: {tomorrow.strftime('%Y-%m-%d %H:%M %Z')}",
            parse_mode="HTML", reply_markup=get_start_menu()
        )
        return

    if data == "show_day_plan":
        if open_planner:
            try:
                await open_planner(approval_bot, _approval_chat_id())
            except Exception as e:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=f"⚠️ Planner недоступен: {e}")
        else:
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="ℹ️ Planner не подключён.")
        return
# -----------------------------------------------------------------------------
# Роутинг обычных сообщений (обновлено: ИИ читает как «Сделай сам» при AI_ACCEPT_ANY_MESSAGE=1)
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_approved_user(update):
        return

    global last_button_pressed_at, manual_expected_until, awaiting_hashtags_until
    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post.update(active=True, timer=now, timeout=600)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    st = ai_state_get()

    if st.get("mode") in {"ai_home", "await_topic"}:
        await_until = st.get("await_until")
        if (await_until is None) or (now <= await_until):
            chat = update.effective_chat
            in_private = (getattr(chat, "type", "") == "private")
            # Новое: если AI_ACCEPT_ANY_MESSAGE=1 — принимаем в группах без упоминания
            if in_private or AI_ACCEPT_ANY_MESSAGE or _message_addresses_bot(update):
                return await handle_ai_input(update, context)
            else:
                return
        else:
            ai_state_set(mode="idle")
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время ожидания темы истекло.",
                reply_markup=get_start_menu()
            )
            return

    if st.get("mode") == "await_text_edit":
        await_until = st.get("await_until")
        if await_until and now <= await_until:
            new_text = (update.message.text or update.message.caption or "").strip()
            log_ai.info("AI|text.edit.recv | len=%s | head=%r", len(new_text), (new_text or "")[:120])
            if new_text:
                post_data["text_en"] = adjust_text_to_target_length(sanitize_ai_text(new_text))
                ai_state_set(mode="confirm_text", await_until=(now + timedelta(minutes=5)))
                await safe_send_message(
                    approval_bot, chat_id=_approval_chat_id(),
                    text=f"<b>Обновлённый текст</b>\n\n{build_telegram_preview(post_data['text_en'])}\n\nПодходит ли текст?",
                    parse_mode="HTML", reply_markup=ai_text_confirm_keyboard()
                )
            else:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Пусто. Пришлите обновлённый текст.")
            return
        else:
            ai_state_set(mode="idle")
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время редактирования текста истекло.",
                reply_markup=get_start_menu()
            )
            return

    if st.get("mode") == "await_image":
        await_until = st.get("await_until")
        if await_until and now <= await_until:
            text = (update.message.text or update.message.caption or "").strip()
            mk, msrc, mref = "none", "tg", None
            if getattr(update.message, "photo", None):
                mk, mref = "image", update.message.photo[-1].file_id
            elif getattr(update.message, "video", None):
                mk, mref = "video", update.message.video.file_id
            elif getattr(update.message, "document", None):
                mime = (update.message.document.mime_type or "")
                fid  = update.message.document.file_id
                if mime.startswith("video/"): mk, mref = "video", fid
                elif mime.startswith("image/"): mk, mref = "image", fid
            elif text and text.startswith("http"):
                url = text.split()[0]
                if any(url.lower().endswith(ext) for ext in (".mp4",".mov",".m4v",".webm")):
                    mk, msrc, mref = "video", "url", url
                elif any(url.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".gif",".webp")):
                    mk, msrc, mref = "image", "url", url
            if mk != "none" and mref:
                post_data["media_kind"] = mk
                post_data["media_src"]  = msrc
                post_data["media_ref"]  = mref
                ai_state_set(mode="ready_media")
                await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (медиа согласовано)")
            else:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Пришлите фото/видео или URL на изображение/видео.")
            return
        else:
            ai_state_set(mode="idle")
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время согласования медиа истекло.",
                reply_markup=get_start_menu()
            )
            return

    if awaiting_hashtags_until and now <= awaiting_hashtags_until:
        line = (update.message.text or update.message.caption or "").strip()
        tags = _parse_hashtags_line_user(line)
        post_data["ai_hashtags"] = tags
        post_data["user_tags_override"] = True
        awaiting_hashtags_until = None
        cur = " ".join(tags) if tags else "—"
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=f"✅ Хэштеги обновлены: {cur}\nРежим Twitter: обязательные ссылки + твои теги (≤275).")
        return await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр")

    if manual_expected_until and now <= manual_expected_until:
        return await handle_manual_input(update, context)

    # По умолчанию — игнор
    return

# -----------------------------------------------------------------------------
# STARTUP / WATCHDOG / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    post_data["text_en"] = post_data.get("text_en") or ""
    post_data["ai_hashtags"] = post_data.get("ai_hashtags") or []
    post_data["media_kind"] = "none"
    post_data["media_src"] = "tg"
    post_data["media_ref"] = None

    # Превью на старте
    await send_single_preview(post_data["text_en"], post_data["ai_hashtags"], header="Предпросмотр (ручной режим)")

    # Считаем, что активность была только что
    global last_button_pressed_at
    last_button_pressed_at = datetime.now(TZ)

    log.info("START|bot launched; preview sent. Planner — см. planner.py (если подключен).")

async def check_inactivity_shutdown():
    if not ENABLE_WATCHDOG:
        return
    global last_button_pressed_at
    while True:
        try:
            await asyncio.sleep(5)
            if last_button_pressed_at is None:
                continue
            idle = (datetime.now(TZ) - last_button_pressed_at).total_seconds()
            if idle >= AUTO_SHUTDOWN_AFTER_SECONDS:
                try:
                    mins = max(1, int(AUTO_SHUTDOWN_AFTER_SECONDS // 60))
                    await send_with_start_button(
                        _approval_chat_id(),
                        f"🔴 Нет активности {mins} мин. Отключаюсь. Нажми «Старт воркера», чтобы перезапустить."
                    )
                except Exception:
                    pass
                shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("WATCHDOG|error: %s", e)
            try:
                await send_with_start_button(
                    _approval_chat_id(),
                    f"⚠️ Ошибка наблюдателя активности: {e}\nНажми «Старт воркера», чтобы перезапустить."
                )
            except Exception:
                pass

def shutdown_bot_and_exit():
    try:
        asyncio.create_task(send_with_start_button(
            _approval_chat_id(),
            "🔴 Бот полностью выключен. Нажми «Старт воркера», чтобы перезапустить."
        ))
    except Exception:
        pass
    import time; time.sleep(2)
    os._exit(0)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("TG|error: %s", context.error)

def main():
    if not TELEGRAM_BOT_TOKEN_APPROVAL:
        log.error("TELEGRAM_BOT_TOKEN_APPROVAL is not set. Exiting.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(CallbackQueryHandler(callback_handler), group=0)
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.VIDEO | filters.Document.IMAGE,
            message_handler
        ),
        group=0,
    )

    register_planner_handlers(app)
    app.add_error_handler(on_error)

    if ENABLE_WATCHDOG:
        asyncio.get_event_loop().create_task(check_inactivity_shutdown())

    async def _fetch_me():
        global BOT_ID, BOT_USERNAME
        try:
            me = await approval_bot.get_me()
            BOT_ID = me.id
            BOT_USERNAME = me.username
            log.info("BOT|me id=%s username=@%s", BOT_ID, BOT_USERNAME)
        except Exception as e:
            log.warning("BOT|get_me fail: %s", e)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_fetch_me())

    app.run_polling(
        poll_interval=0.6,
        timeout=2,
        allowed_updates=["message", "callback_query"]
    )

if __name__ == "__main__":
    main()