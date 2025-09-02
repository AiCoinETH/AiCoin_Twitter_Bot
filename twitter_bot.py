# -*- coding: utf-8 -*-
"""
twitter_bot.py — согласование/публикация в Telegram и X (Twitter).
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
from github import Github

import ai_client

# -----------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("twitter_bot")
log_ai = logging.getLogger("twitter_bot.ai")

# Данные о самом боте (заполняем на старте)
BOT_ID: Optional[int] = None
BOT_USERNAME: Optional[str] = None

# --- Предобъявление глобала, чтобы имя точно существовало в модуле ---
TELEGRAM_APPROVAL_CHAT_ID: Any = None  # может быть int (-100...) или '@username' (str)

# === ПЛАНИРОВЩИК (опционально) ===
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

# Публичный триггер воркера (опционально)
AICOIN_WORKER_URL = os.getenv("AICOIN_WORKER_URL", "https://aicoin-bot-trigger.dfosjam.workers.dev/tg/webhook")
PUBLIC_TRIGGER_SECRET = (os.getenv("PUBLIC_TRIGGER_SECRET") or "").strip()
FALLBACK_PUBLIC_TRIGGER_SECRET = "z8PqH0e4jwN3rA1K"

# Проверка обязательных ENV (мягкая)
_need_env = [
    "TELEGRAM_BOT_TOKEN_APPROVAL", "TELEGRAM_APPROVAL_CHAT_ID",
    "TELEGRAM_BOT_TOKEN_CHANNEL", "TELEGRAM_CHANNEL_USERNAME_ID",
    "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
    "ACTION_PAT_GITHUB", "ACTION_REPO_GITHUB",
]
_missing = [k for k in _need_env if not os.getenv(k)]
if _missing:
    log.error("Не заданы обязательные переменные окружения: %s", _missing)

# Надёжное вычисление chat_id: допускаем -100... и @username
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
    """
    Безопасный доступ к chat_id:
    - поддерживает integer chat_id (включая отрицательные -100... для каналов),
    - поддерживает строковые @username.
    Возвращает кэшированный глобал или перечитывает из ENV, если пуст.
    """
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

# -----------------------------------------------------------------------------
# ГЛОБАЛЫ/БОТЫ/ЧАСОВОЙ ПОЯС
# -----------------------------------------------------------------------------
TZ = ZoneInfo("Europe/Kyiv")
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL) if TELEGRAM_BOT_TOKEN_APPROVAL else None
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL) if TELEGRAM_BOT_TOKEN_CHANNEL else None

# -----------------------------------------------------------------------------
# ГЛОБАЛЫ
# -----------------------------------------------------------------------------
TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND = 600
AUTO_SHUTDOWN_AFTER_SECONDS = 600
VERBATIM_MODE = False
AUTO_AI_IMAGE = False

TW_TAIL_REQUIRED = "🌐 https://getaicoin.com | 🐺 https://t.me/AiCoin_ETH"
TG_TAIL_HTML = '<a href="https://getaicoin.com/">Website</a> | <a href="https://x.com/AiCoin_ETH">Twitter X</a>'

def _worker_url_with_secret() -> str:
    base = AICOIN_WORKER_URL or ""
    sec = (PUBLIC_TRIGGER_SECRET or FALLBACK_PUBLIC_TRIGGER_SECRET).strip()
    if not base:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}s={sec}" if sec else base

# -----------------------------------------------------------------------------
# Twitter API
# -----------------------------------------------------------------------------
def get_twitter_clients():
    if not (TWITTER_API_KEY and TWITTER_API_SECRET and TWITTER_ACCESS_TOKEN and TWITTER_ACCESS_TOKEN_SECRET):
        log.warning("Twitter ENV переменные не заданы полностью — клиенты не будут созданы.")
        return None, None
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

github_client = Github(GITHUB_TOKEN) if GITHUB_TOKEN else None
github_repo = github_client.get_repo(GITHUB_REPO) if (github_client and GITHUB_REPO) else None

# -----------------------------------------------------------------------------
# СТЕЙТ
# -----------------------------------------------------------------------------
post_data: Dict[str, Any] = {
    "text_en": "",
    "ai_hashtags": [],
    "media_kind": "none",
    "media_src": "tg",
    "media_ref": None,
    "media_local_path": None,
    "post_id": 0,
    "is_manual": False,
    "user_tags_override": False
}
prev_data = post_data.copy()

pending_post = {"active": False, "timer": None, "timeout": TIMER_PUBLISH_DEFAULT, "mode": "normal"}
do_not_disturb = {"active": False}

# Доп. глобалы
last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None
ROUTE_TO_PLANNER: set[int] = set()
awaiting_hashtags_until: Optional[datetime] = None

# ---- AI state ----
AI_STATE: Dict[int, Dict[str, Any]] = {}

def ai_state_reset(uid: int):
    AI_STATE[uid] = {"mode": "idle"}
    log_ai.info("AI|state.reset | uid=%s | mode=idle", uid)

def ai_state_set(uid: int, **kwargs):
    st = AI_STATE.get(uid, {"mode": "idle"})
    st.update(kwargs)
    AI_STATE[uid] = st
    log_ai.info("AI|state.set | uid=%s | %s", uid, " ".join([f"{k}={v}" for k, v in kwargs.items()]))

def ai_state_get(uid: int) -> Dict[str, Any]:
    return AI_STATE.get(uid, {"mode": "idle"})

def ai_set_last_topic(uid: int, topic: str):
    st = AI_STATE.get(uid, {"mode": "idle"})
    st["last_topic"] = (topic or "").strip()
    AI_STATE[uid] = st

def ai_get_last_topic(uid: int) -> str:
    return AI_STATE.get(uid, {}).get("last_topic", "").strip()

# -----------------------------------------------------------------------------
# Адресовано ли сообщение нашему боту (для групп/форумов)?
# -----------------------------------------------------------------------------
def _message_addresses_bot(update: Update) -> bool:
    msg = update.message
    if not msg:
        return False
    chat = update.effective_chat
    # 1) Личка
    if getattr(chat, "type", "") == "private":
        return True
    # 2) Реплай на сообщение именно ЭТОГО бота
    try:
        if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot:
            return (BOT_ID is None) or (msg.reply_to_message.from_user.id == BOT_ID)
    except Exception:
        pass
    # 3) Упоминание @username в тексте/подписи
    text = (msg.text or msg.caption or "")
    entities = (msg.entities or []) + (msg.caption_entities or [])
    if BOT_USERNAME and entities:
        for e in entities:
            if e.type == "mention":
                mention = text[e.offset:e.offset+e.length]
                if mention.lstrip("@").lower() == (BOT_USERNAME or "").lower():
                    return True
    return False

# -----------------------------------------------------------------------------
# КНОПКИ / МЕНЮ
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

def _image_confirm_keyboard_for_state() -> InlineKeyboardMarkup:
    if post_data.get("media_kind") in ("image", "video") and post_data.get("media_ref"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📷 Оставить текущее медиа", callback_data="ai_img_keep")],
            [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data="ai_img_gen")],
            [InlineKeyboardButton("📤 Загрузить другое", callback_data="ai_img_upload")],
            [InlineKeyboardButton("🚫 Без изображения", callback_data="ai_img_skip")],
            [InlineKeyboardButton("↩️ Назад к тексту", callback_data="ai_img_back_to_text")]
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data="ai_img_gen")],
            [InlineKeyboardButton("📤 Загрузить свою картинку/видео", callback_data="ai_img_upload")],
            [InlineKeyboardButton("🚫 Без изображения", callback_data="ai_img_skip")],
            [InlineKeyboardButton("↩️ Назад к тексту", callback_data="ai_img_back_to_text")]
        ])

# -----------------------------------------------------------------------------
# БЕЗОПАСНЫЕ SEND/ANSWER
# -----------------------------------------------------------------------------
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
# УТИЛИТЫ ДЛИНЫ/ХЭШТЕГИ/ТЕКСТ
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

def _parse_hashtags_line_user(line: str) -> List[str]:
    if not line:
        return []
    tmp = re.sub(r"[,\u00A0;]+", " ", line.strip())
    raw = [w for w in tmp.split() if w]
    seen, out = set(), []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        k = t.lower()
        if k in seen:
            continue
        seen.add(k); out.append(t)
    return out

def trim_preserving_urls(body: str, max_len: int) -> str:
    body = (body or "").strip()
    if max_len <= 0 or not body:
        return ""
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
    return build_tg_final(text_en, for_photo_caption=False)

# -----------------------------------------------------------------------------
# GitHub helpers
# -----------------------------------------------------------------------------
def upload_image_to_github(image_path, filename):
    if not github_repo:
        log.error("GitHub repo is not configured")
        return None
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
    if not github_repo:
        return
    try:
        contents = github_repo.get_contents(f"{GITHUB_IMAGE_PATH}/{filename}", ref="main")
        github_repo.delete_file(contents.path, "delete image after posting", contents.sha, branch="main")
    except Exception as e:
        log.error(f"Ошибка удаления файла на GitHub: {e}")

# -----------------------------------------------------------------------------
# Загрузка медиа
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
# БД истории (дедуп)
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
            os.remove(tmp.name)
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
# Публикация
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

async def publish_post_to_telegram(text: str | None) -> bool:
    try:
        mk = post_data.get("media_kind", "none")
        msrc = post_data.get("media_src", "tg")
        mref = post_data.get("media_ref")
        final_html = build_tg_final(text or "", for_photo_caption=(mk in ("image","video")))
        if mk == "none" or not mref:
            if not final_html.strip():
                await send_with_start_button(_approval_chat_id(), "⚠️ Telegram: пусто (нет текста и медиа).")
                return False
            await channel_bot.send_message(
                chat_id=TELEGRAM_CHANNEL_USERNAME_ID,
                text=final_html, parse_mode="HTML", disable_web_page_preview=True
            )
            return True
        local_path = await download_to_temp_local(mref, is_telegram=(msrc == "tg"), bot=approval_bot)
        post_data["media_local_path"] = local_path
        if mk == "image":
            with open(local_path, "rb") as f:
                await channel_bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, photo=f,
                    caption=(final_html if final_html.strip() else None), parse_mode="HTML"
                )
        elif mk == "video":
            with open(local_path, "rb") as f:
                await channel_bot.send_video(
                    chat_id=TELEGRAM_CHANNEL_USERNAME_ID, video=f,
                    supports_streaming=True,
                    caption=(final_html if final_html.strip() else None), parse_mode="HTML"
                )
        os.remove(local_path)
        post_data["media_local_path"] = None
        return True
    except Exception as e:
        log.error(f"Ошибка публикации в Telegram: {e}")
        await send_with_start_button(_approval_chat_id(), f"❌ Ошибка публикации в Telegram: {e}")
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False

async def publish_post_to_twitter(final_text_ready: str | None) -> bool:
    try:
        if not twitter_client_v2 or not twitter_api_v1:
            raise RuntimeError("Twitter clients are not configured.")
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
                media = twitter_api_v1.media_upload(filename=local_path, media_category="tweet_video", chunked=True)
                media_ids = [media.media_id_string]
        clean_text = (final_text_ready or "").strip()
        if not media_ids and not clean_text:
            asyncio.create_task(send_with_start_button(
                _approval_chat_id(), "⚠️ В Twitter нечего публиковать: нет ни текста, ни медиа."
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
            _approval_chat_id(), "❌ Twitter: ошибка загрузки. Проверьте права app (Read+Write) и параметры видео."
        ))
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False
    except Exception as e:
        log.error(f"Twitter general error: {e}")
        asyncio.create_task(send_with_start_button(_approval_chat_id(), f"❌ Twitter: {e}"))
        lp = post_data.get("media_local_path")
        if lp:
            try: os.remove(lp)
            except Exception: pass
            post_data["media_local_path"] = None
        return False

# -----------------------------------------------------------------------------
# ПРЕДПРОСМОТР
# -----------------------------------------------------------------------------
async def send_single_preview(text_en: str, ai_hashtags=None, header: str | None = "Предпросмотр"):
    text_for_message = build_telegram_preview(text_en, ai_hashtags or [])
    caption_for_media = build_tg_final(text_en, for_photo_caption=True)
    hdr = f"<b>{html_escape(header)}</b>\n" if header else ""
    hashtags_line = ("<i>Хэштеги:</i> " + html_escape(" ".join(ai_hashtags or []))) if (ai_hashtags) else "<i>Хэштеги:</i> —"
    text_message = f"{hdr}{text_for_message}\n\n{hashtags_line}".strip()

    mk, msrc, mref = post_data.get("media_kind"), post_data.get("media_src"), post_data.get("media_ref")
    try:
        if mk == "video" and mref:
            try:
                await approval_bot.send_video(
                    chat_id=_approval_chat_id(), video=mref, supports_streaming=True,
                    caption=(caption_for_media if caption_for_media.strip() else None),
                    parse_mode="HTML", reply_markup=start_preview_keyboard()
                )
            except Exception:
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
            except Exception:
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
        log.info("Был отправлен предпросмотр.")
    except Exception as e:
        log.warning(f"send_single_preview fallback: {e}")
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=(text_message if text_message else "<i>(пусто — только изображение/видео)</i>"),
            parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=start_preview_keyboard()
        )

# -----------------------------------------------------------------------------
# Генерация ИИ-изображения (по явному согласию)
# -----------------------------------------------------------------------------
async def _generate_ai_image_explicit(topic: str) -> Tuple[Optional[str], Optional[str]]:
    if not hasattr(ai_client, "ai_generate_image"):
        log_ai.info("AI|image.skip | функция ai_generate_image отсутствует в ai_client.")
        return "⚠️ Генерация изображения недоступна (ai_generate_image отсутствует).", None
    try:
        img_path, warn_img = ai_client.ai_generate_image(topic or "")
        if not img_path or not os.path.exists(img_path):
            log_ai.info("AI|image.fail | генерация не вернула файл.")
            return (warn_img or "⚠️ Не удалось сгенерировать изображение ИИ."), None
        filename = f"{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
        raw_url = upload_image_to_github(img_path, filename)
        try:
            os.remove(img_path)
        except Exception:
            pass
        if not raw_url:
            log_ai.info("AI|image.fail | upload to GitHub failed.")
            return (warn_img or "⚠️ Upload image failed."), None
        post_data["media_kind"] = "image"
        post_data["media_src"] = "url"
        post_data["media_ref"] = raw_url
        log_ai.info("AI|image.ok | %s", raw_url)
        return (warn_img or ""), filename
    except Exception as e:
        log_ai.warning("AI|image.exception: %s", e)
        return "⚠️ Ошибка генерации изображения.", None

# -----------------------------------------------------------------------------
# РУЧНОЙ ВВОД («Сделай сам»)
# -----------------------------------------------------------------------------
async def handle_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_expected_until
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    text = (update.message.text or update.message.caption or "").strip()
    media_kind = "none"; media_src = "tg"; media_ref = None

    if getattr(update.message, "photo", None):
        media_kind = "image"; media_ref = update.message.photo[-1].file_id
    elif getattr(update.message, "video", None):
        media_kind = "video"; media_ref = update.message.video.file_id
    elif getattr(update.message, "document", None):
        mime = (update.message.document.mime_type or "")
        fid  = update.message.document.file_id
        if mime.startswith("video/"): media_kind = "video"; media_ref = fid
        elif mime.startswith("image/"): media_kind = "image"; media_ref = fid
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

    await send_single_preview(post_data["text_en"], post_data.get("ai_hashtags") or [], header="Предпросмотр")
    manual_expected_until = None

# -----------------------------------------------------------------------------
# НОВОЕ: ВВОД ДЛЯ ИИ (двухэтапное согласование)
# -----------------------------------------------------------------------------
async def handle_ai_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.now(TZ)
    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    # 1) парсим сообщение как «Сделай сам»
    raw_text = (update.message.text or update.message.caption or "").strip()
    media_kind, media_src, media_ref = "none", "tg", None
    kind_logged = "text"

    if getattr(update.message, "photo", None):
        media_kind, media_ref = "image", update.message.photo[-1].file_id
        kind_logged = "photo"
    elif getattr(update.message, "video", None):
        media_kind, media_ref = "video", update.message.video.file_id
        kind_logged = "video"
    elif getattr(update.message, "document", None):
        mime = (update.message.document.mime_type or "")
        fid  = update.message.document.file_id
        if mime.startswith("video/"): media_kind, media_ref = "video", fid; kind_logged = "video"
        elif mime.startswith("image/"): media_kind, media_ref = "image", fid; kind_logged = "image"
    elif raw_text and raw_text.startswith("http"):
        url = raw_text.split()[0]
        if any(url.lower().endswith(ext) for ext in (".mp4", ".mov", ".m4v", ".webm")):
            media_kind, media_src, media_ref = "video", "url", url
            raw_text = raw_text[len(url):].strip()
            kind_logged = "video_url"
        elif any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
            media_kind, media_src, media_ref = "image", "url", url
            raw_text = raw_text[len(url):].strip()
            kind_logged = "image_url"
    log_ai.info("AI|recv | chat=%s | kind=%s", update.effective_chat.id, kind_logged)

    # 2) тема = текст (или последняя)
    topic = (raw_text or "").strip() or ai_get_last_topic(uid)
    if not topic:
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="⚠️ Отправьте тему поста (любой текст)."
        )
        return

    # 3) генерим текст
    txt, warn_t = ai_client.ai_generate_text(topic)
    post_data["text_en"] = (txt or "").strip()
    ai_set_last_topic(uid, topic)

    # сохраняем медиа, если было прислано
    post_data["media_kind"] = media_kind
    post_data["media_src"]  = media_src
    post_data["media_ref"]  = media_ref

    # 4) ЭТАП 1: "Подходит ли текст?"
    ai_state_set(uid, mode="confirm_text", await_until=(now + timedelta(minutes=5)))
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
# Планировщик — роутинг
# -----------------------------------------------------------------------------
def _planner_active_for(uid: int) -> bool:
    return uid in ROUTE_TO_PLANNER

async def _route_to_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if open_planner:
        return await open_planner(update, context)
    await safe_send_message(
        approval_bot, chat_id=_approval_chat_id(),
        text="⚠️ Планировщик не подключён (planner.py). Работаем в ручном режиме.",
        reply_markup=get_start_menu()
    )
    return

# -----------------------------------------------------------------------------
# CALLBACKS
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

    # --- Планировщик ---
    planner_any = data.startswith((
        "PLAN_", "ITEM_MENU:", "DEL_ITEM:", "EDIT_TIME:", "EDIT_ITEM:",
        "EDIT_FIELD:", "CLONE_ITEM:", "TOGGLE_DONE:", "show_day_plan"
    ))
    planner_exit = data in {"BACK_MAIN_MENU", "PLAN_DONE", "GEN_DONE"}

    if data == "show_day_plan" or planner_any or planner_exit:
        ROUTE_TO_PLANNER.add(uid)
        awaiting_hashtags_until = None
        await _route_to_planner(update, context)
        if planner_exit or data == "BACK_MAIN_MENU":
            ROUTE_TO_PLANNER.discard(uid)
            await safe_send_message(
                approval_bot,
                chat_id=_approval_chat_id(),
                text="Главное меню:",
                reply_markup=get_start_menu()
            )
        return

    # --- Главное меню/базовые действия ---
    if data == "cancel_to_main":
        ROUTE_TO_PLANNER.discard(uid)
        awaiting_hashtags_until = None
        ai_state_reset(uid)
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="Главное меню:", reply_markup=get_start_menu()
        )
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
        ROUTE_TO_PLANNER.discard(uid)
        awaiting_hashtags_until = None
        ai_state_reset(uid)
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

    # ===== ИИ: главное меню ====
    if data == "ai_home":
        ai_state_set(uid, mode="ai_home")
        log_ai.info("AI|home | uid=%s", uid)
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🤖 Режим ИИ. Пришлите тему (можно с медиа или URL). После текста спрошу, нужна ли картинка.",
            reply_markup=ai_home_keyboard()
        )
        return

    if data == "ai_generate":
        ai_state_set(uid, mode="await_topic", await_until=(now + timedelta(minutes=5)))
        log_ai.info("AI|await_topic | uid=%s | until=%s", uid, now + timedelta(minutes=5))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🧠 Введите тему поста (EN/RU/UA). Можно приложить картинку/видео или URL. У меня есть 5 минут.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Тема поста…")
        )
        return

    # ===== ЭТАП 1 (ТЕКСТ): подтверждение/перегенерация/правка =====
    if data == "ai_text_ok":
        ai_state_set(uid, mode="confirm_image", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="🖼 Нужна картинка к посту?",
            reply_markup=_image_confirm_keyboard_for_state()
        )
        return

    if data == "ai_text_regen":
        last_topic = ai_get_last_topic(uid)
        if not last_topic:
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⚠️ Ещё нет сохранённой темы. Нажмите «🧠 Сгенерировать текст по теме».",
                reply_markup=ai_home_keyboard()
            )
        else:
            txt, warn = ai_client.ai_generate_text(last_topic)
            post_data["text_en"] = (txt or "").strip()
            ai_state_set(uid, mode="confirm_text", await_until=(now + timedelta(minutes=5)))
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
        ai_state_set(uid, mode="await_text_edit", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="✏️ Пришлите новый текст поста (EN) одним сообщением (5 минут)."
        )
        return

    # ===== ЭТАП 2 (КАРТИНКА): генерация/загрузка/пропуск =====
    if data == "ai_img_gen":
        topic = ai_get_last_topic(uid) or (post_data.get("text_en") or "")[:200]
        warn_img, filename = await _generate_ai_image_explicit(topic)
        header = "Предпросмотр (текст согласован; изображение сгенерировано)"
        if warn_img:
            header += f" — {warn_img}"
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header=header)
        return

    if data == "ai_img_upload":
        ai_state_set(uid, mode="await_image", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text="📤 Пришлите фото/видео или URL на картинку/видео (5 минут)."
        )
        return

    if data == "ai_img_skip":
        post_data["media_kind"] = "none"
        post_data["media_src"]  = "tg"
        post_data["media_ref"]  = None
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (текст согласован; без изображения)")
        return

    if data == "ai_img_keep":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (текст согласован; текущее медиа сохранено)")
        return

    if data == "ai_img_back_to_text":
        ai_state_set(uid, mode="confirm_text", await_until=(now + timedelta(minutes=5)))
        await safe_send_message(
            approval_bot, chat_id=_approval_chat_id(),
            text=f"<b>Возврат к тексту</b>\n\n{build_telegram_preview(post_data.get('text_en') or '')}\n\nПодходит ли текст?",
            parse_mode="HTML", reply_markup=ai_text_confirm_keyboard()
        )
        return

    # ===== Публикация =====
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

# -----------------------------------------------------------------------------
# Ввод сообщений
# -----------------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_button_pressed_at, manual_expected_until, awaiting_hashtags_until
    uid = update.effective_user.id
    now = datetime.now(TZ)
    last_button_pressed_at = now

    pending_post.update(active=True, timer=now, timeout=TIMER_PUBLISH_EXTEND)
    if pending_post.get("mode") == "placeholder":
        pending_post["mode"] = "normal"

    st = ai_state_get(uid)

    # === ИИ-режим: ожидание темы или дом-экран ===
    if st.get("mode") in {"ai_home", "await_topic"}:
        await_until = st.get("await_until")
        if (await_until is None) or (now <= await_until):
            # Принимаем сообщения так же, как в "Сделай сам":
            #  - в личке: всегда
            #  - в чате согласования: всегда (без @упоминания)
            #  - в других группах: только если адресовано боту (реплай/упоминание)
            chat = update.effective_chat
            in_private = (getattr(chat, "type", "") == "private")

            # Определяем, пришло ли из чата согласования (поддержка id и @username)
            aid = _approval_chat_id()
            from_approval_chat = False
            try:
                if isinstance(aid, int):
                    from_approval_chat = (chat.id == aid)
                else:
                    uname = getattr(chat, "username", None)
                    from_approval_chat = (uname and ("@" + uname.lower()) == str(aid).lower())
            except Exception:
                from_approval_chat = False

            # В личке и в чате согласования — принимаем всё.
            # В остальных чатах — только если адресовано боту (реплай/упоминание).
            if in_private or from_approval_chat or _message_addresses_bot(update):
                return await handle_ai_input(update, context)
            else:
                return
        else:
            ai_state_reset(uid)
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время ожидания темы истекло.",
                reply_markup=get_start_menu()
            )
            return
    # ===== Этап правки текста =====
    if st.get("mode") == "await_text_edit":
        await_until = st.get("await_until")
        if await_until and now <= await_until:
            new_text = (update.message.text or update.message.caption or "").strip()
            log_ai.info("AI|text.edit.recv | uid=%s | len=%s", uid, len(new_text))
            if new_text:
                post_data["text_en"] = new_text
                ai_state_set(uid, mode="confirm_text", await_until=(now + timedelta(minutes=5)))
                await safe_send_message(
                    approval_bot, chat_id=_approval_chat_id(),
                    text=f"<b>Обновлённый текст</b>\n\n{build_telegram_preview(post_data['text_en'])}\n\nПодходит ли текст?",
                    parse_mode="HTML", reply_markup=ai_text_confirm_keyboard()
                )
            else:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Пусто. Пришлите обновлённый текст.")
            return
        else:
            ai_state_reset(uid)
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время редактирования текста истекло.",
                reply_markup=get_start_menu()
            )
            return

    # ===== Этап загрузки медиа =====
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
                ai_state_set(uid, mode="ready_media")
                await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр (медиа согласовано)")
            else:
                await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Пришлите фото/видео или URL на изображение/видео.")
            return
        else:
            ai_state_reset(uid)
            await safe_send_message(
                approval_bot, chat_id=_approval_chat_id(),
                text="⏰ Время согласования медиа истекло.",
                reply_markup=get_start_menu()
            )
            return

    # ===== Хэштеги — ручной ввод =====
    if awaiting_hashtags_until and now <= awaiting_hashtags_until:
        line = (update.message.text or update.message.caption or "").strip()
        tags = _parse_hashtags_line_user(line)
        post_data["ai_hashtags"] = tags
        post_data["user_tags_override"] = True
        awaiting_hashtags_until = None
        cur = " ".join(tags) if tags else "—"
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=f"✅ Хэштеги обновлены: {cur}\nРежим Twitter: обязательные ссылки + твои теги (≤275).")
        return await send_single_preview(post_data.get("text_en") or "", post_data.get("ai_hashtags") or [], header="Предпросмотр")

    # ===== Ручной ввод «Сделай сам» (5 минут) =====
    if manual_expected_until and now <= manual_expected_until:
        return await handle_manual_input(update, context)

    # ===== Планировщик =====
    if _planner_active_for(uid):
        return await _route_to_planner(update, context)

    # ---- никаких автоменю по умолчанию ----
    return

# -----------------------------------------------------------------------------
# Общая публикация
# -----------------------------------------------------------------------------
async def publish_flow(publish_tg: bool, publish_tw: bool):
    base_text_en = (post_data.get("text_en") or "").strip()
    twitter_final_text = (
        build_tweet_user_hashtags_275(base_text_en, post_data.get("ai_hashtags") or [])
        if post_data.get("user_tags_override") else
        build_twitter_text(base_text_en, post_data.get("ai_hashtags") or [])
    )
    telegram_text_preview = build_telegram_preview(base_text_en, None)

    if do_not_disturb["active"]:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="🌙 Режим «Не беспокоить» активен. Публикация отменена.")
        return

    media_hash = await compute_media_hash_from_state()
    tg_status = tw_status = None

    if publish_tg:
        if await is_duplicate_post(telegram_text_preview, media_hash):
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Дубликат для Telegram. Публикация пропущена.")
            tg_status = False
        else:
            tg_status = await publish_post_to_telegram(text=base_text_en)
            if tg_status:
                final_html_saved = build_tg_final(base_text_en, for_photo_caption=(post_data.get("media_kind") in ("image","video")))
                await save_post_to_history(final_html_saved, media_hash)

    if publish_tw:
        if await is_duplicate_post(twitter_final_text, media_hash):
            await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text="⚠️ Дубликат для Twitter. Публикация пропущена.")
            tw_status = False
        else:
            tw_status = await publish_post_to_twitter(twitter_final_text)
            if tw_status:
                await save_post_to_history(twitter_final_text, media_hash)

    if publish_tg:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=("✅ Успешно отправлено в Telegram!" if tg_status else "❌ Не удалось отправить в Telegram."))
    if publish_tw:
        await safe_send_message(approval_bot, chat_id=_approval_chat_id(), text=("✅ Успешно отправлено в Twitter!" if tw_status else "❌ Не удалось отправить в Twitter."))

    return

# -----------------------------------------------------------------------------
# STARTUP / SHUTDOWN / MAIN
# -----------------------------------------------------------------------------
async def on_start(app: Application):
    await init_db()
    post_data["text_en"] = post_data.get("text_en") or ""
    post_data["ai_hashtags"] = post_data.get("ai_hashtags") or []
    post_data["media_kind"] = "none"
    post_data["media_src"] = "tg"
    post_data["media_ref"] = None

    # стартовый предпросмотр
    await send_single_preview(post_data["text_en"], post_data["ai_hashtags"], header="Предпросмотр (ручной режим)")
    log.info("Бот запущен. Предпросмотр отправлен. Планировщик — в planner.py (если подключено).")

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
                    await send_with_start_button(_approval_chat_id(), "🔴 Нет активности 10 минут. Отключаюсь. Нажми «Старт воркера», чтобы перезапустить.")
                except Exception:
                    pass
                shutdown_bot_and_exit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"check_inactivity_shutdown error: {e}")
            try:
                await send_with_start_button(_approval_chat_id(), f"⚠️ Ошибка наблюдателя активности: {e}\nНажми «Старт воркера», чтобы перезапустить.")
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
    log.error(f"TG error: {context.error}")

def main():
    if not TELEGRAM_BOT_TOKEN_APPROVAL:
        log.error("TELEGRAM_BOT_TOKEN_APPROVAL is not set. Exiting.")
        sys.exit(1)

    # builder без .allowed_updates (этого метода нет у ApplicationBuilder)
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )

    # наши хэндлеры (роутинг ИИ/планировщика/ручного ввода)
    app.add_handler(CallbackQueryHandler(callback_handler), group=0)
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.VIDEO | filters.Document.IMAGE,
            message_handler
        ),
        group=0,
    )

    # планировщик
    register_planner_handlers(app)

    app.add_error_handler(on_error)
    asyncio.get_event_loop().create_task(check_inactivity_shutdown())

    # Перед стартом узнаём username/id бота (нужно для фильтра адресации)
    async def _fetch_me():
        global BOT_ID, BOT_USERNAME
        try:
            me = await approval_bot.get_me()
            BOT_ID = me.id
            BOT_USERNAME = me.username
            log.info("BOT: id=%s username=@%s", BOT_ID, BOT_USERNAME)
        except Exception as e:
            log.warning("Could not fetch bot info: %s", e)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_fetch_me())

    # allowed_updates передаем в run_polling
    app.run_polling(
        poll_interval=0.6,
        timeout=2,
        allowed_updates=["message", "callback_query"]
    )

if __name__ == "__main__":
    main()
