# -*- coding: utf-8 -*-
"""
Мини-модуль "План ИИ" (отдельная ветка от обычного планировщика).

Поток (🤖 Создать с ИИ):
  1) Пользователь вводит тему.
  2) Генерация текста через Gemini (google-generativeai).
  3) Пользователь принимает или просит регенерацию.
  4) Опционально сгенерировать черновик изображения (PIL) или пропустить.
  5) Запрос времени HH:MM (Киев).
  6) Сохранение в plan_items с флагом is_ai=1, показ «Добавить ещё / Готово».

ENV:
  GEMINI_API_KEY

Интеграция:
  из twitter_bot.py вызови register_planner_ai_handlers(app)
  и выведи кнопку в своём меню, которая шлёт callback_data="PLAN_AI_OPEN".
"""

from __future__ import annotations
import os
import re
import io
import json
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    InputFile,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter

# ========= Логи/константы =========
log = logging.getLogger("planner_ai")
if log.level == logging.NOTSET:
    log.setLevel(logging.INFO)

TZ = ZoneInfo("Europe/Kyiv")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planner.db")

# ========= Состояние =========
STATE: Dict[Tuple[int, int], dict] = {}  # ключ: (chat_id, user_id)
LAST_SIG: Dict[Tuple[int, int], Tuple[str, str]] = {}  # anti-dup edit signature

def _keys(update: Update) -> Tuple[Tuple[int,int], Tuple[int,int]]:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    return (chat_id, user_id), (chat_id, 0)

def _set_state(update: Update, st: dict) -> None:
    k1, k2 = _keys(update)
    STATE[k1] = st
    STATE[k2] = st

def _get_state(update: Update) -> Optional[dict]:
    k1, k2 = _keys(update)
    return STATE.get(k1) or STATE.get(k2)

def _clear_state(update: Update) -> None:
    k1, k2 = _keys(update)
    STATE.pop(k1, None)
    STATE.pop(k2, None)

# ========= DB =========
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plan_items (
  user_id       INTEGER NOT NULL,
  item_id       INTEGER NOT NULL,
  text          TEXT    NOT NULL DEFAULT '',
  when_hhmm     TEXT,
  done          INTEGER NOT NULL DEFAULT 0,
  media_file_id TEXT,
  media_type    TEXT,
  created_at    TEXT    NOT NULL,
  is_ai         INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, item_id)
);
"""

_db_ready = False

async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_SQL)
        # мягкая миграция is_ai
        try:
            await db.execute("ALTER TABLE plan_items ADD COLUMN is_ai INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        await db.commit()
    _db_ready = True

@dataclass
class PlanItem:
    user_id: int
    item_id: int
    text: str
    when_hhmm: Optional[str]
    done: bool
    media_file_id: Optional[str]
    media_type: Optional[str]
    is_ai: bool

async def _get_ai_items(uid: int) -> List[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT user_id,item_id,text,when_hhmm,done,media_file_id,media_type,is_ai
               FROM plan_items WHERE user_id=? AND is_ai=1 ORDER BY item_id ASC""",
            (uid,),
        )
        rows = await cur.fetchall()
    return [PlanItem(r["user_id"], r["item_id"], r["text"], r["when_hhmm"], bool(r["done"]),
                     r["media_file_id"], r["media_type"], bool(r["is_ai"])) for r in rows]

async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?", (uid,))
        row = await cur.fetchone()
        mx = int(row[0]) if row else 0
    return mx + 1

async def _insert_ai_item(uid: int, text: str) -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """INSERT INTO plan_items(user_id,item_id,text,when_hhmm,done,media_file_id,media_type,created_at,is_ai)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (uid, iid, text or "", None, 0, None, None, now, 1),
        )
        await db.commit()
    return PlanItem(uid, iid, text or "", None, False, None, None, True)

async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?", (text or "", uid, iid))
        await db.commit()

async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?", (when_hhmm, uid, iid))
        await db.commit()

async def _update_media(uid: int, iid: int, file_id: Optional[str], mtype: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET media_file_id=?,media_type=? WHERE user_id=? AND item_id=?", (file_id, mtype, uid, iid))
        await db.commit()

async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""SELECT user_id,item_id,text,when_hhmm,done,media_file_id,media_type,is_ai
                                  FROM plan_items WHERE user_id=? AND item_id=?""", (uid, iid))
        r = await cur.fetchone()
    if not r:
        return None
    return PlanItem(r["user_id"], r["item_id"], r["text"], r["when_hhmm"], bool(r["done"]),
                    r["media_file_id"], r["media_type"], bool(r["is_ai"]))

# ========= UI =========
def _fmt_item(i: PlanItem) -> str:
    t = f"[{i.when_hhmm}]" if i.when_hhmm else "[—]"
    cam = " 📷" if i.media_file_id else ""
    return f"{'🤖' if i.is_ai else '📝'} {t} {(i.text or '(пусто)')[:60]}{cam}"

async def _kb_ai_main(uid: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for it in await _get_ai_items(uid):
        rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"AI_SHOW:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("🤖 Создать с ИИ", callback_data="AI_NEW")],
        [InlineKeyboardButton("➕ Создать вручную", callback_data="AI_NEW_MANUAL")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="PLAN_OPEN")],  # вернёмся в твой основной план
    ]
    return InlineKeyboardMarkup(rows)

def _kb_ai_topic_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Сгенерировать ещё", callback_data="AI_TXT_REGEN")],
        [InlineKeyboardButton("✅ Подходит", callback_data="AI_TXT_OK")],
        [InlineKeyboardButton("❌ Отмена", callback_data="PLAN_AI_OPEN")],
    ])

def _kb_ai_image_controls() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ Сгенерировать картинку", callback_data="AI_IMG_GEN")],
        [InlineKeyboardButton("⏭️ Пропустить картинку", callback_data="AI_IMG_SKIP")],
    ])

def _kb_add_more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ещё одна ИИ-публикация", callback_data="AI_NEW")],
        [InlineKeyboardButton("⬅️ К списку ИИ", callback_data="PLAN_AI_OPEN")],
    ])

# ========= Безопасные TG операции =========
async def _safe_answer(q) -> None:
    try:
        await q.answer()
    except Exception:
        pass

async def _edit_or_send(q, text: str, kb: Optional[InlineKeyboardMarkup]=None):
    try:
        await q.edit_message_text(text=text, reply_markup=kb)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            try:
                await q.edit_message_reply_markup(reply_markup=kb)
                return
            except Exception:
                pass
        # отправим новое
        try:
            await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=kb)
        except Exception:
            pass
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        try:
            await q.edit_message_text(text=text, reply_markup=kb)
        except Exception:
            try:
                await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=kb)
            except Exception:
                pass
    except Exception:
        try:
            await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=kb)
        except Exception:
            pass

# ========= Вспомогательные =========
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
def _parse_time(s: str) -> Optional[str]:
    s = (s or "").strip().replace(" ", "")
    m = _TIME_RE.match(s)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    if s.isdigit() and len(s) in (3,4):
        hh, mm = (s[0], s[1:]) if len(s)==3 else (s[:2], s[2:])
        try:
            hi, mi = int(hh), int(mm)
            if 0<=hi<=23 and 0<=mi<=59:
                return f"{hi:02d}:{mi:02d}"
        except ValueError:
            pass
    return None

# ========= Gemini (текст) =========
_GEMINI_READY = False
def _gemini_model():
    global _GEMINI_READY
    import google.generativeai as genai
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY не задан в GitHub Secrets/ENV")
    if not _GEMINI_READY:
        genai.configure(api_key=key)
        _GEMINI_READY = True
    # быстрая и дешевая модель
    return genai.GenerativeModel("gemini-1.5-flash")

async def _gen_post_text(topic: str) -> str:
    """
    Генерирует твит/пост под тему.
    """
    model = _gemini_model()
    prompt = (
        "Сгенерируй короткий пост для X (Twitter) на русском языке. "
        "Желательно 1-2 абзаца, до 240 символов, без хэштегов, без эмодзи-спама. "
        "Тема: " + topic.strip()
    )
    # синк-обёртка в async
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
    text = (resp.text or "").strip()
    return text[:500] if text else "Не удалось получить текст. Попробуй ещё раз."

# ========= Черновик изображения (PIL) =========
def _render_image_with_text(text: str) -> bytes:
    W, H = 1200, 675
    img = Image.new("RGB", (W, H), (25, 27, 31))
    draw = ImageDraw.Draw(img)
    # Шрифт по умолчанию (без внешних файлов)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 42)
    except Exception:
        font = ImageFont.load_default()

    margin = 80
    max_width = W - margin*2

    # Перенос строк
    words = text.replace("\n", " ").split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    # ограничим количество строк
    lines = lines[:8]

    y = (H - (len(lines)*56)) // 2
    for ln in lines:
        w = draw.textlength(ln, font=font)
        x = (W - w) // 2
        draw.text((x, y), ln, fill=(235, 235, 235), font=font)
        y += 56

    # логотипчик «AI»
    draw.rectangle([(W-130, H-70), (W-30, H-30)], fill=(60, 64, 70))
    try:
        font2 = ImageFont.truetype("DejaVuSans.ttf", 28)
    except Exception:
        font2 = ImageFont.load_default()
    draw.text((W-120, H-66), "AI DRAFT", fill=(200,200,200), font=font2)

    bio = io.BytesIO()
    img.save(bio, format="JPEG", quality=90)
    bio.seek(0)
    return bio.read()

# ========= Экран «План ИИ» =========
async def open_ai_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = await _kb_ai_main(uid)
    text = "🧠 ПЛАН ИИ\nПосмотри список или создай новую публикацию."
    if update.callback_query:
        await _safe_answer(update.callback_query)
        await _edit_or_send(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=kb)

# ========= Роутер callback =========
async def _cb_ai_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = (q.data or "").strip()
    await _safe_answer(q)

    if data == "PLAN_AI_OPEN":
        await _edit_or_send(q, "🧠 ПЛАН ИИ", await _kb_ai_main(uid))
        return

    if data == "AI_NEW":
        _set_state(update, {"mode": "ai_topic"})
        await _edit_or_send(q, "✍️ Введи тему для ИИ-поста (1–2 строки).", None)
        return

    if data == "AI_NEW_MANUAL":
        # минимально: создаём пустой ИИ-пост, дальше спросим время
        it = await _insert_ai_item(uid, "")
        _set_state(update, {"mode": "ai_time", "iid": it.item_id})
        await _edit_or_send(q, f"⏰ Введи время для публикации #{it.item_id} в формате HH:MM (по Киеву).", None)
        return

    if data == "AI_TXT_REGEN":
        st = _get_state(update) or {}
        topic = st.get("topic") or ""
        if not topic:
            await _edit_or_send(q, "Не вижу темы. Начни заново: PLAN_AI_OPEN → 🤖 Создать с ИИ.", await _kb_ai_main(uid))
            return
        await _edit_or_send(q, "⏳ Генерирую текст…", None)
        text = await _gen_post_text(topic)
        _set_state(update, {"mode": "ai_text_ready", "topic": topic, "text": text})
        await q.message.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"🔎 Черновик текста:\n\n{text}",
            reply_markup=_kb_ai_topic_controls()
        )
        return

    if data == "AI_TXT_OK":
        st = _get_state(update) or {}
        text = st.get("text") or ""
        if not text:
            await _edit_or_send(q, "Текст пуст. Попробуй сгенерировать заново.", await _kb_ai_main(uid))
            return
        it = await _insert_ai_item(uid, text)
        _set_state(update, {"mode": "ai_img_step", "iid": it.item_id, "text": text})
        await _edit_or_send(q, "Хочешь сгенерировать картинку к посту?", _kb_ai_image_controls())
        return

    if data == "AI_IMG_GEN":
        st = _get_state(update) or {}
        iid = st.get("iid")
        text = st.get("text") or ""
        if not iid:
            await _edit_or_send(q, "Не вижу активной задачи. Вернись в список.", await _kb_ai_main(uid))
            return
        await _edit_or_send(q, "🖼️ Генерирую картинку…", None)
        img_bytes = _render_image_with_text(text[:140] or "AI Post")
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        await q.message.bot.send_photo(chat_id=q.message.chat_id, photo=InputFile(bio, filename="ai_draft.jpg"), caption="Черновик изображения")
        # предложим принять/пропустить
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Принять картинку", callback_data="AI_IMG_OK")],
            [InlineKeyboardButton("🔁 Сгенерировать ещё", callback_data="AI_IMG_GEN")],
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="AI_IMG_SKIP")],
        ])
        await q.message.bot.send_message(chat_id=q.message.chat_id, text="Как поступаем с картинкой?", reply_markup=kb)
        # отметим, что у нас есть последний img в памяти (не сохраняем, пока не «ОК»)
        _set_state(update, {**st, "last_img": img_bytes})
        return

    if data == "AI_IMG_OK":
        st = _get_state(update) or {}
        iid = st.get("iid")
        last_img: Optional[bytes] = st.get("last_img")
        if not iid:
            await _edit_or_send(q, "Не вижу активной задачи.", await _kb_ai_main(uid))
            return
        if last_img:
            # заливаем в TG, берём file_id и сохраняем в БД
            bio = io.BytesIO(last_img); bio.seek(0)
            sent = await q.message.bot.send_photo(chat_id=q.message.chat_id, photo=InputFile(bio, filename="ai_final.jpg"), caption=f"Фото прикреплено к #{iid}")
            # возьмём file_id из последнего media
            try:
                file_id = sent.photo[-1].file_id
            except Exception:
                file_id = None
            if file_id:
                await _update_media(uid, iid, file_id, "photo")
        # далее спросим время
        _set_state(update, {"mode": "ai_time", "iid": iid})
        await _edit_or_send(q, f"⏰ Введи время для публикации #{iid} в формате HH:MM (по Киеву).", None)
        return

    if data == "AI_IMG_SKIP":
        st = _get_state(update) or {}
        iid = st.get("iid")
        if not iid:
            await _edit_or_send(q, "Не вижу активной задачи.", await _kb_ai_main(uid))
            return
        _set_state(update, {"mode": "ai_time", "iid": iid})
        await _edit_or_send(q, f"⏰ Введи время для публикации #{iid} в формате HH:MM (по Киеву).", None)
        return

    if data.startswith("AI_SHOW:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await _edit_or_send(q, "Некорректный ID.", await _kb_ai_main(uid))
            return
        it = await _get_item(uid, iid)
        if not it or not it.is_ai:
            await _edit_or_send(q, "Пост не найден.", await _kb_ai_main(uid))
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку ИИ", callback_data="PLAN_AI_OPEN")]])
        await _edit_or_send(q, f"#{it.item_id} {('🤖 ' if it.is_ai else '')}{_fmt_item(it)}\n\n{it.text}", kb)
        return

# ========= Роутер сообщений (текст) =========
async def _msg_ai_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = _get_state(update)
    if not st:
        return
    mode = st.get("mode")
    txt = (getattr(update.message, "text", None) or "").strip()

    # Ввод темы
    if mode == "ai_topic":
        topic = txt
        await update.message.reply_text("⏳ Генерирую текст…")
        try:
            draft = await _gen_post_text(topic)
        except Exception as e:
            log.error("Gemini error: %s", e)
            await update.message.reply_text("❌ Не удалось получить ответ от Gemini. Проверь API-ключ и попробуй ещё раз.")
            return
        _set_state(update, {"mode": "ai_text_ready", "topic": topic, "text": draft})
        await update.message.reply_text(
            f"🔎 Черновик текста:\n\n{draft}",
            reply_markup=_kb_ai_topic_controls()
        )
        return

    # Ввод времени
    if mode == "ai_time":
        t = _parse_time(txt)
        if not t:
            await update.message.reply_text("⏰ Формат HH:MM (можно 930/0930). Попробуй ещё раз.")
            return
        iid = st.get("iid")
        if not iid:
            _clear_state(update)
            await update.message.reply_text("Что-то пошло не так. Открой список ИИ и начни заново.")
            return
        await _update_time(update.effective_user.id, iid, t)
        _clear_state(update)
        await update.message.reply_text(f"✅ Сохранено! Публикация #{iid} в {t}.", reply_markup=_kb_add_more())
        return

# ========= Публичный entry =========
async def open_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await open_ai_plan(update, context)

def register_planner_ai_handlers(app: Application) -> None:
    """
    Регистрируем обработчики этой ветки РАНЬШЕ или ВМЕСТЕ с остальными (group=0).
    Главное — добавить кнопку в твоём основном меню, которая шлёт callback_data="PLAN_AI_OPEN".
    """
    log.info("Planner-AI: registering handlers")
    app.add_handler(CallbackQueryHandler(_cb_ai_router, pattern=r"^(PLAN_AI_OPEN|AI_NEW|AI_NEW_MANUAL|AI_TXT_REGEN|AI_TXT_OK|AI_IMG_GEN|AI_IMG_OK|AI_IMG_SKIP|AI_SHOW:\d+)$"), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_ai_router), group=0)
    log.info("Planner-AI: handlers registered")