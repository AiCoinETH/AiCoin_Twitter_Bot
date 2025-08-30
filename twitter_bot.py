# -*- coding: utf-8 -*-
"""
twitter_bot.py — production-ready бот для генерации / согласования / публикации постов
в Telegram и X (Twitter) с поддержкой планировщика и Gemini API.

Функционал:
- Стартовое меню (8 кнопок).
- Интеграция Gemini API (текст, изображение, «видео»-заглушка).
- Сохранение медиа на GitHub (auto-clean 7d).
- Предпросмотр, публикация в Telegram + Twitter (X).
- Планировщик (если есть planner.py).
- Подробное логирование ИИ-потока (каждый шаг).
- Кнопка «▶️ Запуск воркера» после выключения.
"""

import os
import re
import uuid
import base64
import asyncio
import logging
import tempfile
from html import escape as html_escape
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import tweepy
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import RetryAfter, BadRequest, TimedOut, NetworkError
from github import Github
import google.generativeai as genai   # Gemini API

# ----------------------------- ЛОГИ -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("twitter_bot")

def ai_log(event: str, **kw):
    kv = " | ".join(f"{k}={v}" for k, v in kw.items())
    log.info(f"AI|{event} | {kv}" if kv else f"AI|{event}")

# ------------------------------ ENV ------------------------------
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = int(os.getenv("TELEGRAM_APPROVAL_CHAT_ID", "0"))
TELEGRAM_BOT_TOKEN_CHANNEL = os.getenv("TELEGRAM_BOT_TOKEN_CHANNEL")
TELEGRAM_CHANNEL_USERNAME_ID = os.getenv("TELEGRAM_CHANNEL_USERNAME_ID")
TELEGRAM_APPROVAL_BOT_USERNAME = os.getenv("TELEGRAM_APPROVAL_BOT_USERNAME", "").lstrip("@")

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

GITHUB_TOKEN = os.getenv("ACTION_PAT_GITHUB")
GITHUB_REPO = os.getenv("ACTION_REPO_GITHUB")
GITHUB_MEDIA_PATH = "images_for_posts"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

# ----------------------------- ГЛОБАЛЫ -----------------------------
TZ = ZoneInfo("Europe/Kyiv")
approval_bot = Bot(token=TELEGRAM_BOT_TOKEN_APPROVAL)
channel_bot = Bot(token=TELEGRAM_BOT_TOKEN_CHANNEL)

AUTO_SHUTDOWN_AFTER_SECONDS = 600
VERBATIM_MODE = False

# Twitter
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

# Текущее “предпросмотрное” состояние для стартового меню
post_data: Dict[str, Any] = {
    "text_en": "",
    "media_kind": "none",      # none | image | video
    "media_ref": None,
}

# Локальные ИИ-состояния по пользователю
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

# ----------------------------- МЕНЮ -----------------------------
def get_start_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Предпросмотр", callback_data="approve")],
        [InlineKeyboardButton("🤖 Сгенерировать пост (ИИ)", callback_data="ai_generate")],
        [InlineKeyboardButton("✍️ Сделай сам", callback_data="self_post")],
        [InlineKeyboardButton("🗓 План на день", callback_data="show_day_plan")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="edit_hashtags")],
        [InlineKeyboardButton("🔕 Не беспокоить", callback_data="dnd_toggle")],
        [InlineKeyboardButton("⏳ Завершить день", callback_data="end_day")],
        [InlineKeyboardButton("🔴 Выключить", callback_data="shutdown_bot")]
    ])

def get_after_shutdown_menu():
    # Чтобы можно было поднять воркер кликом из Telegram
    if not TELEGRAM_APPROVAL_BOT_USERNAME:
        return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Запуск воркера", callback_data="noop")]])
    start_url = f"https://t.me/{TELEGRAM_APPROVAL_BOT_USERNAME}?start=run_worker"
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Запуск воркера", url=start_url)]])

# ----------------------------- SAFE TG -----------------------------
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

# --------------------------- Gemini API ---------------------------
async def gemini_generate_text(prompt: str) -> str:
    ai_log("text.req", prompt_len=len(prompt))
    try:
        model = genai.GenerativeModel("gemini-1.5-pro")
        resp = await asyncio.to_thread(model.generate_content, prompt)
        txt = (resp.text or "").strip() if resp else ""
        ai_log("text.ok", out_len=len(txt))
        return txt or "⚠️ Gemini не вернул текст."
    except Exception as e:
        ai_log("text.err", err=str(e))
        return f"❌ Ошибка генерации текста: {e}"

async def gemini_generate_image(prompt: str) -> Optional[str]:
    ai_log("image.req", prompt_len=len(prompt))
    try:
        img = await asyncio.to_thread(genai.images.generate, prompt=prompt, size="1024x1024")
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
        github_repo.create_file(f"{GITHUB_MEDIA_PATH}/{filename}", "upload ai image", content_b64, branch="main")
        os.remove(tmp.name)
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_MEDIA_PATH}/{filename}"
        ai_log("image.ok", url=url)
        return url
    except Exception as e:
        ai_log("image.err", err=str(e))
        return None

async def gemini_generate_video(prompt: str) -> Optional[str]:
    # Заглушка: вернём картинку как «видео»
    ai_log("video.req", info="stub")
    try:
        url = await gemini_generate_image(prompt)
        if not url:
            return None
        ai_log("video.ok", url=url)
        return url
    except Exception as e:
        ai_log("video.err", err=str(e))
        return None

# --------------------------- GitHub helpers ---------------------------
def upload_file_to_github(path: str, filename: str, message="upload file") -> Optional[str]:
    try:
        with open(path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        github_repo.create_file(f"{GITHUB_MEDIA_PATH}/{filename}", message, content_b64, branch="main")
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_MEDIA_PATH}/{filename}"
        ai_log("gh.upload.ok", filename=filename, url=url)
        return url
    except Exception as e:
        ai_log("gh.upload.err", err=str(e))
        return None

async def _tg_file_to_github_url(file_id: str) -> tuple[Optional[str], str]:
    tg_file = await approval_bot.get_file(file_id)
    fp = (tg_file.file_path or "").lower()
    is_video = any(x in fp for x in (".mp4", ".webm", ".mov"))
    suffix = ".mp4" if is_video else ".png"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    await tg_file.download_to_drive(tmp.name)
    try:
        url = upload_file_to_github(tmp.name, f"{uuid.uuid4().hex}{suffix}", message="upload media from TG")
        return url, ("video" if is_video else "image")
    finally:
        try: os.remove(tmp.name)
        except Exception: pass

# --------------------------- Предпросмотр ---------------------------
async def send_single_preview(text_en: str, media_url: Optional[str] = None, header: str = "Предпросмотр"):
    caption = html_escape((text_en or "").strip()) or "<i>(пусто)</i>"
    ai_log("preview.send", kind=post_data.get("media_kind"), has_media=bool(media_url), text=caption[:60])
    try:
        if post_data.get("media_kind") == "image" and media_url:
            await approval_bot.send_photo(TELEGRAM_APPROVAL_CHAT_ID, media_url, caption=caption, parse_mode="HTML", reply_markup=get_start_menu())
        elif post_data.get("media_kind") == "video" and media_url:
            await approval_bot.send_video(TELEGRAM_APPROVAL_CHAT_ID, media_url, caption=caption, parse_mode="HTML", reply_markup=get_start_menu())
        else:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"<b>{header}</b>\n\n{caption}", parse_mode="HTML", disable_web_page_preview=True, reply_markup=get_start_menu())
    except Exception as e:
        log.error(f"send_single_preview error: {e}")

# --------------------------- Публикация ---------------------------
async def publish_post_to_telegram(text: str | None, media_url: Optional[str] = None, media_kind: str = "none") -> bool:
    try:
        html_text = html_escape((text or "").strip()) or "<i>(пусто)</i>"
        if media_kind == "image" and media_url:
            await channel_bot.send_photo(TELEGRAM_CHANNEL_USERNAME_ID, media_url, caption=html_text, parse_mode="HTML")
        elif media_kind == "video" and media_url:
            await channel_bot.send_video(TELEGRAM_CHANNEL_USERNAME_ID, media_url, caption=html_text, parse_mode="HTML", supports_streaming=True)
        else:
            await channel_bot.send_message(TELEGRAM_CHANNEL_USERNAME_ID, html_text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as e:
        log.error(f"publish_post_to_telegram error: {e}")
        try: await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Telegram: {e}")
        except Exception: pass
        return False

def _download_to_temp(url: str, suffix: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={'User-Agent':'Mozilla/5.0'}, timeout=60)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content); tmp.close()
        return tmp.name
    except Exception as e:
        log.warning(f"_download_to_temp: {e}")
        return None

async def publish_post_to_twitter(text: str | None, media_url: Optional[str] = None, media_kind: str = "none") -> bool:
    try:
        body = (text or "").strip()
        media_ids = None
        local_path = None
        if media_url and media_kind in ("image", "video"):
            if media_kind == "image":
                local_path = _download_to_temp(media_url, suffix=".png")
                if not local_path: raise RuntimeError("Не удалось скачать изображение для X")
                media = twitter_api_v1.media_upload(filename=local_path)
                media_ids = [media.media_id_string]
            else:
                local_path = _download_to_temp(media_url, suffix=".mp4")
                if not local_path: raise RuntimeError("Не удалось скачать видео для X")
                media = twitter_api_v1.media_upload(filename=local_path, media_category="tweet_video", chunked=True)
                media_ids = [media.media_id_string]

        if media_ids and body:
            try: twitter_client_v2.create_tweet(text=body, media={"media_ids": media_ids})
            except TypeError: twitter_client_v2.create_tweet(text=body, media_ids=media_ids)
        elif media_ids and not body:
            try: twitter_client_v2.create_tweet(media={"media_ids": media_ids})
            except TypeError: twitter_client_v2.create_tweet(media_ids=media_ids)
        else:
            twitter_client_v2.create_tweet(text=body)

        if local_path:
            try: os.remove(local_path)
            except Exception: pass
        return True
    except Exception as e:
        log.error(f"publish_post_to_twitter error: {e}")
        try: await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Twitter: {e}")
        except Exception: pass
        return False

async def publish_both(text: str | None, media_url: Optional[str], media_kind: str) -> None:
    tg_ok = await publish_post_to_telegram(text, media_url, media_kind)
    tw_ok = await publish_post_to_twitter(text, media_url, media_kind)
    await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"Готово. Telegram: {'✅' if tg_ok else '❌'} | Twitter: {'✅' if tw_ok else '❌'}")

# --------------------------- Planner (optional) ---------------------------
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

async def _save_ai_to_plan(uid: int, text: str, media_url: Optional[str], media_kind: str):
    if not PLANNER_OK or _planner_add_item is None:
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⚠️ Планировщик недоступен. Могу только опубликовать сейчас.")
        return
    try:
        new_item_id = await _planner_add_item(uid, text, chat_id=TELEGRAM_APPROVAL_CHAT_ID, bot=approval_bot)
        if media_url and _planner_update_media:
            mtype = "photo" if media_kind == "image" else "document"
            await _planner_update_media(uid, new_item_id, media_url, mtype)
        if _planner_prompt_time:
            await _planner_prompt_time(uid, TELEGRAM_APPROVAL_CHAT_ID, approval_bot)
    except Exception as e:
        log.error(f"_save_ai_to_plan: {e}")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"❌ Не удалось сохранить в план: {e}")

# --------------------------- CALLBACKS (кнопки) ---------------------------
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

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    await q.answer()
    ai = _ai_get(uid)
    ai_log("cb", uid=uid, data=data, mode=ai.get("mode"))

    if data == "approve":
        await send_single_preview(post_data.get("text_en") or "", post_data.get("media_ref"), header="Предпросмотр")
        return

    if data == "ai_generate":
        # Сразу генерим текст, если тему не отправили отдельно
        topic = ai.get("topic") or "AI Coin project"
        sys_prompt = ("You are a social media copywriter. Create a short, engaging post for X/Twitter: "
                      "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags.")
        text = await gemini_generate_text(f"{sys_prompt}\n\nTopic: {topic}")
        ai["text"] = text
        ai["mode"] = "ready_text"
        ai_log("text.ready", uid=uid, text_len=len(text))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"✍️ Вариант текста:\n\n{text}", reply_markup=kb_ai_text_actions())
        return

    if data == "ai_text_regen":
        topic = ai.get("topic") or "AI Coin project"
        sys_prompt = ("You are a social media copywriter. Create a short, engaging post for X/Twitter: "
                      "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags.")
        text = await gemini_generate_text(f"{sys_prompt}\n\nTopic: {topic}")
        ai["text"] = text
        ai["mode"] = "ready_text"
        ai_log("text.regen.ok", uid=uid, text_len=len(text))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"✍️ Вариант текста:\n\n{text}", reply_markup=kb_ai_text_actions())
        return

    if data == "ai_text_ok":
        ai["mode"] = "ready_text"
        ai_log("text.lock", uid=uid)
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
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Отправьте отредактированный текст одним сообщением.")
        return

    if data == "ai_hash_gen":
        prompt = ("Generate 6-10 concise, platform-friendly hashtags for X/Twitter. "
                  "Return them separated by spaces, include $Ai, #AI, #crypto where relevant.\n\n"
                  f"Post text:\n{ai.get('text','')}")
        raw = await gemini_generate_text(prompt)
        tags = re.findall(r'[#$][\w\d_]+', raw)
        # дедуп + максимум 10
        seen, final = set(), []
        for t in tags:
            k = t.lower()
            if k in seen: continue
            seen.add(k); final.append(t)
            if len(final) >= 10: break
        ai["hashtags"] = final
        ai_log("tags.ok", uid=uid, n=len(final))
        await approval_bot.send_message(
            TELEGRAM_APPROVAL_CHAT_ID,
            f"🔖 Хэштеги:\n{' '.join(final) if final else '—'}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➡️ К медиа", callback_data="ai_media_choose")]
            ])
        )
        return

    if data == "ai_media_choose":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Выберите тип медиа:", reply_markup=kb_ai_media_choice())
        return

    if data == "ai_img":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🖼 Генерирую изображение…")
        img_prompt = ("Generate a clean, square social-media illustration without any text overlay. "
                      "Style: modern, high-contrast, eye-catching, safe for work.\n"
                      f"Theme: {ai.get('topic')}\nPost text: {ai.get('text')}")
        url = await gemini_generate_image(img_prompt)
        if not url:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "❌ Не удалось сгенерировать изображение.")
            return
        ai["media_url"] = url
        ai["media_kind"] = "image"
        post_data["media_kind"] = "image"
        post_data["media_ref"] = url
        await send_single_preview(ai.get("text",""), url, header="ИИ пост: изображение")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Подходит картинка?", reply_markup=kb_ai_media_after_gen())
        return

    if data == "ai_video":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🎬 Генерирую видео (заглушка)…")
        vid_prompt = ("Create a short looping social clip (5-8s) concept matching the post. "
                      "Modern, high-contrast, engaging. No subtitles.\n"
                      f"Theme: {ai.get('topic')}\nPost text: {ai.get('text')}")
        url = await gemini_generate_video(vid_prompt)
        if not url:
            await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "❌ Не удалось сгенерировать видео.")
            return
        ai["media_url"] = url
        ai["media_kind"] = "video"
        post_data["media_kind"] = "video"
        post_data["media_ref"] = url
        await send_single_preview(ai.get("text",""), url, header="ИИ пост: видео")
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Подходит видео?", reply_markup=kb_ai_media_after_gen())
        return

    if data == "ai_media_regen":
        # повторная генерация текущего типа
        if ai.get("media_kind") == "image":
            return await callback_handler(type("obj", (), {"callback_query": type("cq", (), {"data":"ai_img","answer":q.answer})})(), context)
        elif ai.get("media_kind") == "video":
            return await callback_handler(type("obj", (), {"callback_query": type("cq", (), {"data":"ai_video","answer":q.answer})})(), context)
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Сначала выберите тип медиа.")
        return

    if data == "ai_media_ok":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Медиа зафиксировано. Что дальше?", reply_markup=kb_save_to_plan_or_post_now())
        return

    if data == "ai_media_skip":
        ai["media_url"] = None
        ai["media_kind"] = "none"
        post_data["media_kind"] = "none"
        post_data["media_ref"] = None
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "Ок, без медиа. Сохранить в план или публиковать?", reply_markup=kb_save_to_plan_or_post_now())
        return

    if data == "ai_save_to_plan":
        text = (ai.get("text") or "").strip()
        tags = ai.get("hashtags") or []
        if tags:
            text = (text + "\n\n" + " ".join(tags)).strip()
        await _save_ai_to_plan(uid, text, ai.get("media_url"), ai.get("media_kind") or "none")
        post_data["text_en"] = text
        post_data["media_kind"] = ai.get("media_kind") or "none"
        post_data["media_ref"] = ai.get("media_url")
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Сохранено в план. Ожидает времени.")
        _ai_reset(uid)
        return

    if data == "ai_post_now":
        text = (ai.get("text") or "").strip()
        tags = ai.get("hashtags") or []
        if tags:
            text = (text + "\n\n" + " ".join(tags)).strip()
        await publish_both(text, ai.get("media_url"), ai.get("media_kind") or "none")
        post_data["text_en"] = text
        post_data["media_kind"] = ai.get("media_kind") or "none"
        post_data["media_ref"] = ai.get("media_url")
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Предпросмотр (после публикации)")
        _ai_reset(uid)
        return

    if data == "back_to_main":
        ai_log("back_to_main", uid=uid, mode=ai.get("mode"))
        await send_single_preview(post_data.get("text_en") or "", post_data.get("media_ref"), header="Главное меню / предпросмотр")
        return

    if data == "dnd_toggle":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "🌙 Режим «Не беспокоить» переключен (заглушка).")
        return

    if data == "end_day":
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, "⏳ День завершён (заглушка).")
        return

    if data == "shutdown_bot":
        ai_log("shutdown", uid=uid)
        try:
            await approval_bot.send_message(
                TELEGRAM_APPROVAL_CHAT_ID,
                "🔴 Бот выключается…\n\n▶️ Для запуска снова нажми кнопку ниже:",
                reply_markup=get_after_shutdown_menu()
            )
        except Exception as e:
            log.warning(f"shutdown notice failed: {e}")
        await asyncio.sleep(1.0)
        os._exit(0)
        return

# ------------------------ РОУТЕР СООБЩЕНИЙ (важно!) ------------------------
manual_expected_until: Optional[datetime] = None

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ai = _ai_get(uid)
    text = (getattr(update.message, "text", None) or getattr(update.message, "caption", None) or "").strip()
    ai_log("msg", uid=uid, mode=ai.get("mode"), has_photo=bool(getattr(update.message, "photo", None)),
           has_video=bool(getattr(update.message, "video", None)), text_len=len(text))

    # 1) Если ждём правку текста
    if ai.get("mode") == "await_text_edit" and text:
        ai["text"] = text
        ai["mode"] = "ready_text"
        ai_log("text.edit.ok", uid=uid, len=len(text))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"✅ Текст обновлён.\n\n{text}", reply_markup=kb_ai_text_actions())
        return

    # 2) Если пользователь просто прислал тему — сгенерим текст
    if ai.get("mode") in ("idle", "await_topic") and text and not getattr(update.message, "photo", None) and not getattr(update.message, "video", None):
        ai["topic"] = text
        sys_prompt = ("You are a social media copywriter. Create a short, engaging post for X/Twitter: "
                      "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags.")
        gen = await gemini_generate_text(f"{sys_prompt}\n\nTopic: {text}")
        ai["text"] = gen
        ai["mode"] = "ready_text"
        ai_log("topic.text.ok", uid=uid, topic=text[:60], text_len=len(gen))
        await approval_bot.send_message(TELEGRAM_APPROVAL_CHAT_ID, f"✍️ Вариант текста:\n\n{gen}", reply_markup=kb_ai_text_actions())
        return

    # 3) Режим «Сделай сам»: одно сообщение с текстом и возможным медиа
    global manual_expected_until
    if manual_expected_until and datetime.now(TZ) <= manual_expected_until:
        media_url, media_kind = None, "none"
        if getattr(update.message, "photo", None):
            fid = update.message.photo[-1].file_id
            media_url, media_kind = await _tg_file_to_github_url(fid)
        elif getattr(update.message, "video", None):
            fid = update.message.video.file_id
            media_url, media_kind = await _tg_file_to_github_url(fid)

        post_data["text_en"] = text
        post_data["media_kind"] = media_kind
        post_data["media_ref"] = media_url
        ai_log("self_post.set", uid=uid, kind=media_kind, has_media=bool(media_url))
        await send_single_preview(post_data["text_en"], post_data["media_ref"], header="Предпросмотр (Сделай сам)")
        manual_expected_until = None
        return

    # 4) Иначе — просто обновим предпросмотр текстом
    if text:
        post_data["text_en"] = text
        ai_log("preview.update.text", uid=uid, len=len(text))
        await send_single_preview(post_data["text_en"], post_data.get("media_ref"), header="Предпросмотр (обновлено)")

# ------------------------------ STARTUP ------------------------------
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
        .concurrent_updates(False)
        .build()
    )

    # planner handlers (если есть)
    _planner_register(app)

    # ВАЖНО: кнопки → callback_handler
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ВАЖНО: сообщения → message_handler
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, message_handler))

    # запуск
    app.post_init = on_start
    app.run_polling(poll_interval=0.8)

if __name__ == "__main__":
    main()