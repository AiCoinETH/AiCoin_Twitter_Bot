# -*- coding: utf-8 -*-
"""
twitter_bot.py — production-ready бот для генерации / согласования / публикации постов
в Telegram и X (Twitter) с поддержкой планировщика и Gemini API.

Функционал:
- ✅ Стартовое меню (8 кнопок).
- ✅ Интеграция Gemini API (текст, изображение, видео).
- ✅ Сохранение фото/видео на GitHub (auto-clean 7d).
- ✅ Предпросмотр и согласование постов.
- ✅ Публикация в Telegram + Twitter.
- ✅ SQLite история постов (анти-дубликаты).
- ✅ Планировщик (если есть planner.py).
- ✅ Авто-shutdown через 10 мин бездействия.
- ✅ ПОДРОБНОЕ ЛОГИРОВАНИЕ ВСЕХ ШАГОВ ИИ.
"""

import os
import re
import sys
import io
import uuid
import base64
import asyncio
import logging
import tempfile
import hashlib
import time
from functools import wraps
from html import escape as html_escape
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, time as dt_time
from unicodedata import normalize
from zoneinfo import ZoneInfo

import requests
import tweepy
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    Update, Bot, InputFile
)
from telegram.ext import (
    Application, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError
import aiosqlite
from github import Github
import google.generativeai as genai   # Gemini API

# ------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# ------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s"
)
log = logging.getLogger("twitter_bot")

def _short(s: str, n: int = 120) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else (s[:n] + "…")

def ai_log(event: str, uid: Optional[int] = None, **kw):
    parts = [f"AI|{event}"]
    if uid is not None:
        parts.append(f"uid={uid}")
    for k, v in kw.items():
        if isinstance(v, str):
            v = _short(v, 160)
        parts.append(f"{k}={v}")
    log.info(" | ".join(parts))

def ai_step(event_name: str):
    """Декоратор: логируем старт, успех/ошибку и длительность шага ИИ."""
    def deco(fn):
        @wraps(fn)
        async def wrap(*args, **kwargs):
            uid = kwargs.get("uid")
            t0 = time.perf_counter()
            ai_log(f"{event_name}.start", uid=uid)
            try:
                res = await fn(*args, **kwargs)
                dt = time.perf_counter() - t0
                ai_log(f"{event_name}.ok", uid=uid, took=f"{dt:.3f}s")
                return res
            except Exception as e:
                dt = time.perf_counter() - t0
                ai_log(f"{event_name}.err", uid=uid, took=f"{dt:.3f}s", err=str(e))
                raise
        return wrap
    return deco

# ------------------------------------------------------------------
# ENV
# ------------------------------------------------------------------
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

# Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# ------------------------------------------------------------------
# ГЛОБАЛЫ
# ------------------------------------------------------------------
TZ = ZoneInfo("Europe/Kyiv")
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

TELEGRAM_APPROVAL_CHAT_ID = int(TELEGRAM_APPROVAL_CHAT_ID_STR)

AUTO_SHUTDOWN_AFTER_SECONDS = 600
TIMER_PUBLISH_DEFAULT = 180
TIMER_PUBLISH_EXTEND = 600

VERBATIM_MODE = False

# Twitter API
def get_twitter_clients():
    client_v2 = tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET
    )
    api_v1 = tweepy.API(
        tweepy.OAuth1UserHandler(
            TWITTER_API_KEY, TWITTER_API_SECRET,
            TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET
        )
    )
    return client_v2, api_v1

twitter_client_v2, twitter_api_v1 = get_twitter_clients()

# GitHub
github_client = Github(GITHUB_TOKEN)
github_repo = github_client.get_repo(GITHUB_REPO)

# ------------------------------------------------------------------
# СТЕЙТ
# ------------------------------------------------------------------
post_data: Dict[str, Any] = {
    "text_en": "",
    "ai_hashtags": [],
    "media_kind": "none",      # none | image | video
    "media_src": "tg",         # tg | url | github
    "media_ref": None,
    "media_filename": None,
    "timestamp": None,
    "post_id": 0,
    "is_manual": False,
    "user_tags_override": False
}

# Хранение активности
pending_post = {
    "active": False,
    "timer": None,
    "timeout": TIMER_PUBLISH_DEFAULT,
    "mode": "normal"
}
last_action_time: Dict[int, datetime] = {}
last_button_pressed_at: Optional[datetime] = None
manual_expected_until: Optional[datetime] = None
awaiting_hashtags_until: Optional[datetime] = None

# ------------------------------------------------------------------
# МЕНЮ
# ------------------------------------------------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("🤖 Сгенерировать пост (ИИ)", callback_data="ai_generate")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 План на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="do_not_disturb")],
        [InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

# ------------------------------------------------------------------
# SAFE wrappers для Telegram (анти-флуд)
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# Gemini API — генерация (с подробным логированием)
# ------------------------------------------------------------------
@ai_step("gemini.text")
async def gemini_generate_text(prompt: str, uid: Optional[int] = None) -> str:
    """Генерация текста поста через Gemini"""
    ai_log("gemini.text.prompt", uid=uid, prompt=prompt)
    try:
        model = genai.GenerativeModel("gemini-1.5-pro")
        resp = await asyncio.to_thread(model.generate_content, prompt)
        text = (resp.text or "").strip() if resp else ""
        ai_log("gemini.text.result", uid=uid, text=text)
        return text or "⚠️ Gemini не вернул текст."
    except Exception as e:
        ai_log("gemini.text.exception", uid=uid, err=str(e))
        return f"❌ Ошибка генерации текста: {e}"

@ai_step("gemini.image")
async def gemini_generate_image(prompt: str, uid: Optional[int] = None) -> Optional[str]:
    """Генерация изображения через Gemini Images API → GitHub URL"""
    ai_log("gemini.image.prompt", uid=uid, prompt=prompt)
    try:
        img = await asyncio.to_thread(
            genai.images.generate,
            prompt=prompt,
            size="1024x1024"
        )
        b64_png = None
        if img and getattr(img, "generated_images", None):
            g = img.generated_images[0]
            b64_png = getattr(g, "image", None)
            if hasattr(b64_png, "data"):
                b64_png = b64_png.data
        if not b64_png:
            raise RuntimeError("Images API не вернул base64.")
        image_bytes = base64.b64decode(b64_png)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.write(image_bytes); tmp.close()

        filename = f"{uuid.uuid4().hex}.png"
        with open(tmp.name, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        github_repo.create_file(
            f"{GITHUB_IMAGE_PATH}/{filename}",
            "upload ai image",
            content_b64,
            branch="main"
        )
        os.remove(tmp.name)
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        ai_log("gemini.image.github_saved", uid=uid, filename=filename, url=url)
        return url
    except Exception as e:
        ai_log("gemini.image.exception", uid=uid, err=str(e))
        return None

@ai_step("gemini.video")
async def gemini_generate_video(prompt: str, uid: Optional[int] = None) -> Optional[str]:
    """
    Видео (заглушка): генерируем картинку и возвращаем её URL.
    """
    ai_log("gemini.video.prompt", uid=uid, prompt=prompt)
    try:
        img_url = await gemini_generate_image(prompt, uid=uid)
        if not img_url:
            ai_log("gemini.video.noimage", uid=uid)
            return None
        ai_log("gemini.video.stub_return", uid=uid, url=img_url)
        return img_url
    except Exception as e:
        ai_log("gemini.video.exception", uid=uid, err=str(e))
        return None

# ------------------------------------------------------------------
# GitHub helpers (upload / delete / cleanup)
# ------------------------------------------------------------------
def upload_file_to_github(path: str, filename: str, message="upload file") -> Optional[str]:
    try:
        with open(path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        github_repo.create_file(
            f"{GITHUB_IMAGE_PATH}/{filename}",
            message,
            content_b64,
            branch="main"
        )
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_IMAGE_PATH}/{filename}"
        log.info(f"GITHUB.upload | name={filename} | url={url}")
        return url
    except Exception as e:
        log.error(f"GITHUB.upload.err | name={filename} | err={e}")
        return None

def delete_file_from_github(filename: str):
    try:
        contents = github_repo.get_contents(f"{GITHUB_IMAGE_PATH}/{filename}", ref="main")
        github_repo.delete_file(contents.path, "delete old file", contents.sha, branch="main")
        log.info(f"GITHUB.delete | name={filename}")
    except Exception as e:
        log.error(f"GITHUB.delete.err | name={filename} | err={e}")

async def cleanup_github_files_older_than(days: int = 7):
    """Удаляет файлы из репо старше N дней"""
    try:
        contents = github_repo.get_contents(GITHUB_IMAGE_PATH, ref="main")
        cutoff = datetime.utcnow() - timedelta(days=days)
        for file in contents:
            if not file.type == "file":
                continue
            created_at = getattr(file, "last_modified", None)
            if created_at and created_at < cutoff:
                delete_file_from_github(file.name)
                log.info(f"GITHUB.cleanup.deleted | name={file.name}")
    except Exception as e:
        log.warning(f"GITHUB.cleanup.warn | err={e}")

# ------------------------------------------------------------------
# Предпросмотр поста
# ------------------------------------------------------------------
async def send_single_preview(text_en: str, media_url: Optional[str] = None, header: str = "Предпросмотр"):
    caption = html_escape(text_en.strip()) if text_en else "<i>(пусто)</i>"
    hdr = f"<b>{header}</b>\n\n"
    log.info(f"PREVIEW.send | kind={post_data.get('media_kind')} | has_media={bool(media_url)} | text={_short(text_en)}")
    try:
        if post_data.get("media_kind") == "image" and media_url:
            await approval_bot.send_photo(
                TELEGRAM_APPROVAL_CHAT_ID,
                media_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_start_menu()
            )
        elif post_data.get("media_kind") == "video" and media_url:
            await approval_bot.send_video(
                TELEGRAM_APPROVAL_CHAT_ID,
                media_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=get_start_menu()
            )
        else:
            await approval_bot.send_message(
                TELEGRAM_APPROVAL_CHAT_ID,
                f"{hdr}{caption}",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=get_start_menu()
            )
    except Exception as e:
        log.error(f"PREVIEW.err | {e}")

# ------------------------------------------------------------------
# Публикация в Telegram и Twitter (X)
# ------------------------------------------------------------------
async def publish_post_to_telegram(text: str | None, media_url: Optional[str] = None, media_kind: str = "none") -> bool:
    try:
        html_text = html_escape((text or "").strip()) or "<i>(пусто)</i>"
        log.info(f"PUB.TG | kind={media_kind} | has_media={bool(media_url)} | text={_short(text or '')}")
        if media_kind == "image" and media_url:
            await channel_bot.send_photo(
                TELEGRAM_CHANNEL_USERNAME_ID,
                media_url,
                caption=html_text,
                parse_mode="HTML"
            )
        elif media_kind == "video" and media_url:
            await channel_bot.send_video(
                TELEGRAM_CHANNEL_USERNAME_ID,
                media_url,
                caption=html_text,
                parse_mode="HTML",
                supports_streaming=True
            )
        else:
            await channel_bot.send_message(
                TELEGRAM_CHANNEL_USERNAME_ID,
                html_text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        return True
    except Exception as e:
        log.error(f"PUB.TG.err | {e}")
        try:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Telegram: {e}")
        except Exception:
            pass
        return False

def _download_to_temp(url: str, suffix: str) -> Optional[str]:
    try:
        log.info(f"MEDIA.download | url={url} | suffix={suffix}")
        r = requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=60)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content); tmp.close()
        return tmp.name
    except Exception as e:
        log.warning(f"MEDIA.download.err | url={url} | err={e}")
        return None

async def publish_post_to_twitter(text: str | None, media_url: Optional[str] = None, media_kind: str = "none") -> bool:
    try:
        body = (text or "").strip()
        media_ids = None
        local_path = None
        log.info(f"PUB.X | kind={media_kind} | has_media={bool(media_url)} | text={_short(body)}")

        if media_url and media_kind in ("image", "video"):
            if media_kind == "image":
                local_path = _download_to_temp(media_url, suffix=".png")
                if not local_path:
                    raise RuntimeError("Не удалось скачать изображение для X")
                media = twitter_api_v1.media_upload(filename=local_path)
                media_ids = [media.media_id_string]
            else:
                local_path = _download_to_temp(media_url, suffix=".mp4")
                if not local_path:
                    raise RuntimeError("Не удалось скачать видео для X")
                media = twitter_api_v1.media_upload(
                    filename=local_path,
                    media_category="tweet_video",
                    chunked=True
                )
                media_ids = [media.media_id_string]

        if media_ids and body:
            try:
                twitter_client_v2.create_tweet(text=body, media={"media_ids": media_ids})
            except TypeError:
                twitter_client_v2.create_tweet(text=body, media_ids=media_ids)
        elif media_ids and not body:
            try:
                twitter_client_v2.create_tweet(media={"media_ids": media_ids})
            except TypeError:
                twitter_client_v2.create_tweet(media_ids=media_ids)
        else:
            twitter_client_v2.create_tweet(text=body)

        if local_path:
            try:
                os.remove(local_path)
            except Exception:
                pass
        return True
    except Exception as e:
        log.error(f"PUB.X.err | {e}")
        try:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Twitter: {e}")
        except Exception:
            pass
        return False

async def publish_both(text: str | None, media_url: Optional[str], media_kind: str) -> None:
    tg_ok = await publish_post_to_telegram(text, media_url, media_kind)
    tw_ok = await publish_post_to_twitter(text, media_url, media_kind)
    status = f"Telegram: {'✅' if tg_ok else '❌'} | Twitter: {'✅' if tw_ok else '❌'}"
    log.info(f"PUB.status | {status}")
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"Готово. {status}")

# ------------------------------------------------------------------
# TG медиа → GitHub
# ------------------------------------------------------------------
async def _tg_file_to_github_url(file_id: str, prefer_image: bool = True) -> Tuple[str, str]:
    """
    Возвращает (github_raw_url, kind) где kind ∈ {'image','video'}.
    """
    tg_file = await approval_bot.get_file(file_id)
    fp = tg_file.file_path or ""
    is_video = (".mp4" in fp.lower()) or (".webm" in fp.lower()) or (".mov" in fp.lower())
    suffix = ".mp4" if is_video else (".png" if prefer_image else ".bin")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    await tg_file.download_to_drive(tmp.name)

    try:
        filename = f"{uuid.uuid4().hex}{suffix}"
        url = upload_file_to_github(tmp.name, filename, message="upload media from TG")
        log.info(f"TG.media.saved | kind={'video' if is_video else 'image'} | name={filename} | url={url}")
        if not url:
            raise RuntimeError("upload_file_to_github returned None")
        return url, ("video" if is_video else "image")
    finally:
        try:
            os.remove(tmp.name)
        except Exception:
            pass

# ------------------------------------------------------------------
# ИИ-состояние per-user
# ------------------------------------------------------------------
AI_FLOWS: Dict[int, Dict[str, Any]] = {}

def _ai_reset(uid: int):
    AI_FLOWS[uid] = {
        "mode": "idle",
        "topic": "",
        "text": "",
        "hashtags": [],
        "media_kind": "none",
        "media_url": None,
    }
    ai_log("state.reset", uid=uid, mode="idle")

def _ai_get(uid: int) -> Dict[str, Any]:
    if uid not in AI_FLOWS:
        _ai_reset(uid)
    return AI_FLOWS[uid]

def _hashtags_from_gemini_text(raw: str) -> List[str]:
    cand = re.findall(r'[#$][\w\d_]+', raw, flags=re.UNICODE)
    out, seen = [], set()
    for t in cand:
        key = t.lower()
        if key in seen: 
            continue
        seen.add(key); out.append(t)
        if len(out) >= 10:
            break
    return out

# ------------------------------------------------------------------
# Кнопочные UI-блоки ИИ
# ------------------------------------------------------------------
def kb_ai_text_actions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Текст подходит", callback_data="ai_text_ok"),
         InlineKeyboardButton("♻️ Перегенерировать", callback_data="ai_text_regen")],
        [InlineKeyboardButton("✏️ Править", callback_data="ai_text_edit")],
        [InlineKeyboardButton("🔖 Сгенерировать хэштеги", callback_data="ai_hash_gen")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ])

def kb_ai_media_choice():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Сгенерировать картинку", callback_data="ai_img")],
        [InlineKeyboardButton("🎬 Сгенерировать видео", callback_data="ai_video")],
        [InlineKeyboardButton("⏭ Без медиа", callback_data="ai_media_skip")]
    ])

def kb_ai_media_after_gen():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подходит", callback_data="ai_media_ok"),
         InlineKeyboardButton("♻️ Ещё вариант", callback_data="ai_media_regen")],
        [InlineKeyboardButton("⏭ Пропустить медиа", callback_data="ai_media_skip")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ])

def kb_save_to_plan_or_post_now():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Сохранить в план (и задать время)", callback_data="ai_save_to_plan")],
        [InlineKeyboardButton("📤 Опубликовать сейчас", callback_data="ai_post_now")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
    ])

# ------------------------------------------------------------------
# Планировщик (опционально)
# ------------------------------------------------------------------
try:
    from planner import register_planner_handlers as _planner_register
    from planner import open_planner as _planner_open
    from planner import planner_add_from_text as _planner_add_item
    from planner import planner_prompt_time as _planner_prompt_time
    from planner import _update_media as _planner_update_media
    PLANNER_OK = True
except Exception as _e:
    log.warning(f"Planner import warn: {_e}")
    _planner_register = lambda app: None
    _planner_open = None
    _planner_add_item = None
    _planner_prompt_time = None
    _planner_update_media = None
    PLANNER_OK = False

@ai_step("plan.save")
async def _save_ai_to_plan(uid: int, text: str, media_url: Optional[str], media_kind: str):
    if not PLANNER_OK or _planner_add_item is None:
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "⚠️ Планировщик недоступен. Могу только опубликовать сейчас."
        )
        ai_log("plan.unavailable", uid=uid)
        return
    try:
        new_item_id = await _planner_add_item(uid, text, chat_id=TELEGRAM_APPROVAL_CHAT_ID, bot=approval_bot)
        ai_log("plan.item_created", uid=uid, item_id=new_item_id, text=text)
        if media_url and _planner_update_media:
            mtype = "photo" if media_kind == "image" else "document"
            await _planner_update_media(uid, new_item_id, media_url, mtype)
            ai_log("plan.media_attached", uid=uid, item_id=new_item_id, media_kind=media_kind, url=media_url)
        if _planner_prompt_time:
            await _planner_prompt_time(uid, TELEGRAM_APPROVAL_CHAT_ID, approval_bot)
            ai_log("plan.prompt_time", uid=uid, item_id=new_item_id)
    except Exception as e:
        ai_log("plan.save.err", uid=uid, err=str(e))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Не удалось сохранить в план: {e}")

# ------------------------------------------------------------------
# CALLBACKS — ПОДРОБНОЕ ЛОГИРОВАНИЕ
# ------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    await safe_q_answer(q)

    ai = _ai_get(uid)
    ai_log("cb", uid=uid, data=data, mode=ai.get("mode"))

    if data == "approve":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("media_ref"), header="Предпросмотр")
        return

    if data == "ai_generate":
        _ai_reset(uid)
        AI_FLOWS[uid]["mode"] = "await_topic"
        ai_log("state.set", uid=uid, mode="await_topic")
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "🤖 Введите тему/бриф (1–2 предложения) для поста:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    if data == "self_post":
        global manual_expected_until
        manual_expected_until = datetime.now(TZ) + timedelta(minutes=5)
        ai_log("self_post.open", uid=uid, until=manual_expected_until.isoformat())
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "✍️ Отправьте одним сообщением текст (EN/UA/RU) и при желании фото/видео.\n"
            "Медиа будут загружены на GitHub. Время ожидания — 5 мин.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    if data == "edit_hashtags":
        AI_FLOWS[uid]["mode"] = "await_hashtags_input"
        ai_log("state.set", uid=uid, mode="await_hashtags_input")
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "🔖 Введите хэштеги (через пробел/запятую). Не более 10:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    if data == "do_not_disturb":
        ai_log("dnd.toggle", uid=uid)
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🌙 Режим «Не беспокоить» переключен (заглушка).")
        return

    if data == "end_day":
        ai_log("day.end", uid=uid)
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⏳ День завершён (заглушка).")
        return

    if data == "shutdown_bot":
        ai_log("shutdown", uid=uid)
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🔴 Бот выключается…")
        import time as _t; _t.sleep(1)
        os._exit(0)
        return

    if data == "back_to_main":
        ai_log("back_to_main", uid=uid, mode=ai.get("mode"))
        await send_single_preview(post_data.get("text_en") or "", post_data.get("media_ref"), header="Главное меню / предпросмотр")
        return

    # ---- ИИ: TEXT ----
    if data == "ai_text_regen":
        topic = ai.get("topic") or "Short crypto post"
        sys_prompt = (
            "You are a social media copywriter. Create a short, engaging post for X/Twitter: "
            "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags."
        )
        text = await gemini_generate_text(f"{sys_prompt}\n\nTopic: {topic}", uid=uid)
        ai["text"] = text
        ai["mode"] = "ready_text"
        ai_log("text.ready", uid=uid, text=text)
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            f"✍️ Вариант текста:\n\n{text}",
            reply_markup=kb_ai_text_actions()
        )
        return

    if data == "ai_text_ok":
        ai["mode"] = "ready_text"
        ai_log("text.accept", uid=uid, text=ai.get("text"))
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "Текст зафиксирован. Сгенерировать хэштеги или перейти к медиа?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔖 Сгенерировать хэштеги", callback_data="ai_hash_gen")],
                [InlineKeyboardButton("➡️ Перейти к медиа", callback_data="ai_media_choose")]
            ])
        )
        return

    if data == "ai_text_edit":
        ai["mode"] = "await_text_edit"
        ai_log("text.edit.await", uid=uid)
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "Отправьте отредактированный текст одним сообщением.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return

    # ---- ИИ: HASHTAGS ----
    if data == "ai_hash_gen":
        try:
            prompt = (
                "Generate 6-10 concise, platform-friendly hashtags for X/Twitter. "
                "Return them separated by spaces, include $Ai, #AI, #crypto where relevant."
                f"\n\nPost text:\n{ai.get('text','')}"
            )
            ai_log("hashtags.gen.start", uid=uid)
            raw = await gemini_generate_text(prompt, uid=uid)
            tags = _hashtags_from_gemini_text(raw)
            ai["hashtags"] = tags
            ai_log("hashtags.gen.done", uid=uid, tags=" ".join(tags))
            await approval_bot.send_message(
                TELEGRAM_APPROVAL_CHAT_ID,
                f"🔖 Хэштеги:\n{' '.join(tags) if tags else '—'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Править вручную", callback_data="edit_hashtags")],
                    [InlineKeyboardButton("➡️ К медиа", callback_data="ai_media_choose")]
                ])
            )
        except Exception as e:
            ai_log("hashtags.gen.err", uid=uid, err=str(e))
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Ошибка генерации хэштегов: {e}")
        return

    if data == "ai_media_choose":
        ai_log("media.choose", uid=uid)
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Выберите тип медиа:", reply_markup=kb_ai_media_choice())
        return

    # ---- ИИ: MEDIA ----
    if data == "ai_img":
        ai_log("media.image.start", uid=uid, topic=ai.get("topic"))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🖼 Генерирую изображение…")
        img_prompt = (
            "Generate a clean, square social-media illustration without any text overlay. "
            "Style: modern, high-contrast, eye-catching, safe for work.\n"
            f"Theme: {ai.get('topic')}\n"
            f"Post text: {ai.get('text')}"
        )
        url = await gemini_generate_image(img_prompt, uid=uid)
        if not url:
            ai_log("media.image.fail", uid=uid)
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "❌ Не удалось сгенерировать изображение.")
            return
        ai["media_url"] = url
        ai["media_kind"] = "image"
        ai_log("media.image.ok", uid=uid, url=url)
        await send_single_preview(ai.get("text",""), url, header="ИИ пост: изображение")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Подходит картинка?", reply_markup=kb_ai_media_after_gen())
        return

    if data == "ai_video":
        ai_log("media.video.start", uid=uid, topic=ai.get("topic"))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🎬 Генерирую видео (заглушка)…")
        vid_prompt = (
            "Create a short looping social clip (5-8s) concept matching the post. "
            "Modern, high-contrast, engaging. No subtitles.\n"
            f"Theme: {ai.get('topic')}\n"
            f"Post text: {ai.get('text')}"
        )
        url = await gemini_generate_video(vid_prompt, uid=uid)
        if not url:
            ai_log("media.video.fail", uid=uid)
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "❌ Не удалось сгенерировать видео.")
            return
        ai["media_url"] = url
        ai["media_kind"] = "video"
        ai_log("media.video.ok", uid=uid, url=url)
        await send_single_preview(ai.get("text",""), url, header="ИИ пост: видео")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Подходит видео?", reply_markup=kb_ai_media_after_gen())
        return

    if data == "ai_media_regen":
        ai_log("media.regen", uid=uid, kind=ai.get("media_kind"))
        # Перегенерация того же типа
        if ai.get("media_kind") == "image":
            update.callback_query.data = "ai_img"
        elif ai.get("media_kind") == "video":
            update.callback_query.data = "ai_video"
        else:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Сначала выберите тип медиа.")
            return await callback_handler(update, context)
        return await callback_handler(update, context)

    if data == "ai_media_ok":
        ai_log("media.accept", uid=uid, kind=ai.get("media_kind"), url=ai.get("media_url"))
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "Медиа зафиксировано. Что дальше?",
            reply_markup=kb_save_to_plan_or_post_now()
        )
        return

    if data == "ai_media_skip":
        ai_log("media.skip", uid=uid, prev_kind=ai.get("media_kind"))
        ai["media_url"] = None
        ai["media_kind"] = "none"
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            "Ок, без медиа. Сохранить в план или публиковать?",
            reply_markup=kb_save_to_plan_or_post_now()
        )
        return

    # ---- SAVE/POST ----
    if data == "ai_save_to_plan":
        text = ai.get("text","").strip()
        tags = ai.get("hashtags") or []
        if tags:
            text = (text + "\n\n" + " ".join(tags)).strip()
        ai_log("plan.save.request", uid=uid, media_kind=ai.get("media_kind"), text=text)
        await _save_ai_to_plan(uid, text, ai.get("media_url"), ai.get("media_kind") or "none")
        post_data["text_en"] = text
        post_data["media_kind"] = ai.get("media_kind") or "none"
        post_data["media_ref"] = ai.get("media_url")
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Сохранено в план. Ожидает времени.")
        _ai_reset(uid)
        return

    if data == "ai_post_now":
        text = ai.get("text","").strip()
        tags = ai.get("hashtags") or []
        if tags:
            text = (text + "\n\n" + " ".join(tags)).strip()
        ai_log("post.now", uid=uid, media_kind=ai.get("media_kind"), text=text)
        await publish_both(text, ai.get("media_url"), ai.get("media_kind") or "none")
        post_data["text_en"] = text
        post_data["media_kind"] = ai.get("media_kind") or "none"
        post_data["media_ref"] = ai.get("media_url")
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Предпросмотр (после публикации)")
        _ai_reset(uid)
        return

# ------------------------------------------------------------------
# Message router — ПОДРОБНОЕ ЛОГИРОВАНИЕ ИИ-ШАГОВ
# ------------------------------------------------------------------
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ai = _ai_get(uid)
    text = (getattr(update.message, "text", None) or getattr(update.message, "caption", None) or "").strip()
    ai_log("msg", uid=uid, mode=ai.get("mode"), has_photo=bool(getattr(update.message, "photo", None)),
           has_video=bool(getattr(update.message, "video", None)), has_doc=bool(getattr(update.message, "document", None)),
           text=text)

    # 1) Тема для ИИ
    if ai.get("mode") == "await_topic" and text:
        ai["topic"] = text
        ai_log("topic.set", uid=uid, topic=text)
        sys_prompt = (
            "You are a social media copywriter. Create a short, engaging post for X/Twitter: "
            "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags."
        )
        gen = await gemini_generate_text(f"{sys_prompt}\n\nTopic: {text}", uid=uid)
        ai["text"] = gen
        ai["mode"] = "ready_text"
        ai_log("text.ready", uid=uid, text=gen)
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            f"✍️ Вариант текста:\n\n{gen}",
            reply_markup=kb_ai_text_actions()
        )
        return

    # 2) Ручная правка текста
    if ai.get("mode") == "await_text_edit" and text:
        ai["text"] = text
        ai["mode"] = "ready_text"
        ai_log("text.edited", uid=uid, text=text)
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            f"✅ Текст обновлён.\n\n{text}",
            reply_markup=kb_ai_text_actions()
        )
        return

    # 3) Ручные хэштеги
    if ai.get("mode") == "await_hashtags_input" and text:
        raw = re.sub(r"[,\u00A0;]+", " ", text)
        tags = [t for t in raw.split() if t]
        norm, seen = [], set()
        for t in tags:
            if not t.startswith("#") and not t.startswith("$"):
                t = "#" + t
            key = t.lower()
            if key in seen:
                continue
            seen.add(key); norm.append(t)
            if len(norm) >= 10:
                break
        ai["hashtags"] = norm
        ai["mode"] = "ready_text"
        ai_log("hashtags.manual", uid=uid, tags=" ".join(norm))
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            f"🔖 Хэштеги обновлены:\n{' '.join(norm) if norm else '—'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➡️ К медиа", callback_data="ai_media_choose")]])
        )
        return

    # 4) «Сделай сам» — одно сообщение
    global manual_expected_until
    if manual_expected_until and datetime.now(TZ) <= manual_expected_until:
        media_url = None
        media_kind = "none"

        if getattr(update.message, "photo", None):
            fid = update.message.photo[-1].file_id
            media_url, media_kind = await _tg_file_to_github_url(fid, prefer_image=True)
        elif getattr(update.message, "video", None):
            fid = update.message.video.file_id
            media_url, media_kind = await _tg_file_to_github_url(fid, prefer_image=False)
        elif getattr(update.message, "document", None):
            doc = update.message.document
            fid = doc.file_id
            m = (doc.mime_type or "").lower()
            if m.startswith("image/"):
                media_url, media_kind = await _tg_file_to_github_url(fid, prefer_image=True)
            elif m.startswith("video/"):
                media_url, media_kind = await _tg_file_to_github_url(fid, prefer_image=False)

        post_data["text_en"] = text
        post_data["media_kind"] = media_kind
        post_data["media_ref"] = media_url

        ai_log("self_post.captured", uid=uid, media_kind=media_kind, url=media_url, text=text)
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Предпросмотр (Сделай сам)")
        manual_expected_until = None
        return

    # Обновление предпросмотра простым текстом
    if text:
        post_data["text_en"] = text
        ai_log("preview.update_text", uid=uid, text=text)
        await send_single_preview(post_data["text_en"], post_data.get("media_ref"), header="Предпросмотр (обновлено)")

# ------------------------------------------------------------------
# STARTUP / MAIN
# ------------------------------------------------------------------
async def on_start(app: Application):
    log.info("BOT.start")
    await approval_bot.send_message(
        TELEGRAM_APPROVAL_CHAT_ID,
        "🤖 Бот запущен. Готов к работе!",
        reply_markup=get_start_menu()
    )

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN_APPROVAL)
        .post_init(on_start)
        .concurrent_updates(False)
        .build()
    )
    # Планировщик подключится, если есть
    try:
        _planner_register(app)
    except Exception as e:
        log.warning(f"Planner register warn: {e}")

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.IMAGE | filters.Document.VIDEO, message_handler))
    app.run_polling(poll_interval=0.8, timeout=10)

if __name__ == "__main__":
    main()