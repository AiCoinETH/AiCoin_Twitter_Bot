# -*- coding: utf-8 -*-
"""
Планировщик с персистентностью в SQLite для twitter_bot.py.

Поддерживаемые действия:
  Общие:
    PLAN_OPEN, PLAN_ADD_EMPTY, ITEM_MENU:<id>, DEL_ITEM:<id>, EDIT_TIME:<id>, EDIT_ITEM:<id>,
    TOGGLE_DONE:<id>, SHOW_ITEM:<id>.
  ИИ-вкладка:
    PLAN_AI_OPEN, PLAN_AI_NEW, PLAN_AI_ADD_MANUAL,
    AI_ACCEPT_TEXT:<id>, AI_REGEN_TEXT:<id>, AI_CANCEL:<id>,
    AI_GEN_IMG:<id>, AI_SKIP_IMG:<id>

Сохранение:
  - Таблица plan_items(
        user_id, item_id, text, when_hhmm, done,
        media_file_id, media_type, origin, created_at
    )
    origin: 'manual' | 'ai'

Состояние ввода:
  - (chat_id, user_id) с общечатовым fallback (chat_id, 0)
  - mode ∈ {'edit_text','edit_time','ai_wait_topic','ai_after_text'}
"""

from __future__ import annotations
import re
import json
import asyncio
import logging
import aiosqlite
import os
import io
import base64
import contextlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

# --- Telegram ---
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter

# --- AI SDKs (опционально; обрабатываем отсутствие) ---
try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    from openai import OpenAI as OpenAIClient
except Exception:
    OpenAIClient = None

__all__ = [
    "register_planner_handlers",
    "open_planner",
    "planner_add_from_text",
    "planner_prompt_time",
    "USER_STATE",
]

# ------------------
# Логи / Константы / глобалы
# ------------------
log = logging.getLogger("planner")
if log.level == logging.NOTSET:
    log.setLevel(logging.INFO)

TZ = ZoneInfo("Europe/Kyiv")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planner.db")

STATE: Dict[Tuple[int, int], dict] = {}  # (chat_id,user_id)->state   и (chat_id,0)->state (fallback)
USER_STATE = STATE  # alias

LAST_SIG: Dict[Tuple[int, int], Tuple[str, str]] = {}  # (chat_id, message_id) -> (text, markup_json)
_db_ready = False

# ENV для ИИ
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

_gemini_model_cached = None

# ------------
# Утилиты логирования
# ------------
def _short(val: Any, n: int = 120) -> str:
    s = str(val)
    return s if len(s) <= n else s[:n] + "…"

def _fmt_arg(v: Any) -> str:
    try:
        from telegram import Update as TGUpdate
        if isinstance(v, TGUpdate):
            return f"<Update chat={getattr(getattr(v, 'effective_chat', None), 'id', None)} cb={bool(v.callback_query)}>"
        if v.__class__.__name__ in {"Bot", "Application"}:
            return f"<{v.__class__.__name__}>"
    except Exception:
        pass
    if isinstance(v, PlanItem):
        return f"PlanItem(iid={v.item_id}, time={v.when_hhmm}, done={v.done}, origin={getattr(v,'origin','?')}, text={_short(v.text, 60)!r})"
    if isinstance(v, list) and v and isinstance(v[0], PlanItem):
        return f"[PlanItem×{len(v)}: {', '.join('#'+str(i.item_id) for i in v[:5])}{'…' if len(v)>5 else ''}]"
    if isinstance(v, str):
        return repr(_short(v, 120))
    return _short(v, 120)

def _trace_sync(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        log.debug("→ %s(%s%s)", fn.__name__,
                  ", ".join(_fmt_arg(a) for a in args),
                  (", " + ", ".join(f"{k}={_fmt_arg(v)}" for k, v in kwargs.items())) if kwargs else "")
        res = fn(*args, **kwargs)
        log.debug("← %s = %s", fn.__name__, _fmt_arg(res))
        return res
    return wrap

def _trace_async(fn):
    @wraps(fn)
    async def wrap(*args, **kwargs):
        log.debug("→ %s(%s%s)", fn.__name__,
                  ", ".join(_fmt_arg(a) for a in args),
                  ((", " + ", ".join(f"{k}={_fmt_arg(v)}" for k, v in kwargs.items())) if kwargs else ""))
        res = await fn(*args, **kwargs)
        log.debug("← %s = %s", fn.__name__, _fmt_arg(res))
        return res
    return wrap

# ------------
# Helpers для STATE
# ------------
def _state_keys_from_update(update: Update) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    return (chat_id, user_id), (chat_id, 0)

def set_state_for_update(update: Update, st: dict) -> None:
    k_personal, k_chat = _state_keys_from_update(update)
    STATE[k_personal] = st
    STATE[k_chat] = st
    log.debug("STATE set for %s and %s -> %s", k_personal, k_chat, st)

def get_state_for_update(update: Update) -> Optional[dict]:
    k_personal, k_chat = _state_keys_from_update(update)
    st = STATE.get(k_personal) or STATE.get(k_chat)
    log.debug("STATE get %s or %s -> %s", k_personal, k_chat, st)
    return st

def clear_state_for_update(update: Update) -> None:
    k_personal, k_chat = _state_keys_from_update(update)
    STATE.pop(k_personal, None)
    STATE.pop(k_chat, None)
    log.debug("STATE cleared for %s and %s", k_personal, k_chat)

def set_state_for_ids(chat_id: int, user_id: int, st: dict) -> None:
    STATE[(chat_id, user_id)] = st
    STATE[(chat_id, 0)] = st
    log.debug("STATE set for ids (%s,%s) and (%s,0) -> %s", chat_id, user_id, chat_id, st)

# ------------
# Модель данных
# ------------
@dataclass
class PlanItem:
    user_id: int
    item_id: int        # локальный id внутри пользователя
    text: str
    when_hhmm: Optional[str]  # "HH:MM" | None
    done: bool
    media_file_id: Optional[str] = None  # Telegram file_id
    media_type: Optional[str] = None     # "photo" | "document" | None
    origin: str = "manual"               # "manual" | "ai"

# ------------
# База (SQLite)
# ------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plan_items (
  user_id       INTEGER NOT NULL,
  item_id       INTEGER NOT NULL,
  text          TEXT    NOT NULL DEFAULT '',
  when_hhmm     TEXT,
  done          INTEGER NOT NULL DEFAULT 0,
  media_file_id TEXT,
  media_type    TEXT,
  origin        TEXT    NOT NULL DEFAULT 'manual',
  created_at    TEXT    NOT NULL,
  PRIMARY KEY (user_id, item_id)
);
"""

@_trace_async
async def _migrate_db() -> None:
    """Мягкие миграции на новые поля."""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            for sql in (
                "ALTER TABLE plan_items ADD COLUMN media_file_id TEXT",
                "ALTER TABLE plan_items ADD COLUMN media_type TEXT",
                "ALTER TABLE plan_items ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'",
            ):
                with contextlib.suppress(Exception):
                    await db.execute(sql)
            await db.commit()
    except Exception as e:
        log.warning("DB migrate skipped: %s", e)

@_trace_async
async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    log.info("DB init start: %s", DB_FILE)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_SQL)
        await db.commit()
    await _migrate_db()
    _db_ready = True
    log.info("DB init complete")

@_trace_async
async def _get_items(uid: int, origin: Optional[str] = None) -> List[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        if origin:
            sql = """SELECT user_id, item_id, text, when_hhmm, done, media_file_id, media_type, origin
                     FROM plan_items WHERE user_id=? AND origin=? ORDER BY item_id ASC"""
            cur = await db.execute(sql, (uid, origin))
        else:
            sql = """SELECT user_id, item_id, text, when_hhmm, done, media_file_id, media_type, origin
                     FROM plan_items WHERE user_id=? ORDER BY item_id ASC"""
            cur = await db.execute(sql, (uid,))
        rows = await cur.fetchall()
    return [PlanItem(r["user_id"], r["item_id"], r["text"], r["when_hhmm"], bool(r["done"]),
                     r["media_file_id"], r["media_type"], r["origin"]) for r in rows]

@_trace_async
async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?"
        cur = await db.execute(sql, (uid,))
        row = await cur.fetchone()
        mx = row[0] if row is not None else 0
    return int(mx) + 1

@_trace_async
async def _insert_item(uid: int, text: str = "", when_hhmm: Optional[str] = None, origin: str = "manual") -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = """INSERT INTO plan_items(user_id, item_id, text, when_hhmm, done, media_file_id, media_type, origin, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?)"""
        args = (uid, iid, text or "", when_hhmm, 0, None, None, origin, now)
        await db.execute(sql, args)
        await db.commit()
    return PlanItem(uid, iid, text or "", when_hhmm, False, None, None, origin)

@_trace_async
async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?",
                         (text or "", uid, iid))
        await db.commit()

@_trace_async
async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?",
                         (when_hhmm, uid, iid))
        await db.commit()

@_trace_async
async def _update_done(uid: int, iid: int, done: bool) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET done=? WHERE user_id=? AND item_id=?",
                         (1 if done else 0, uid, iid))
        await db.commit()

@_trace_async
async def _update_media(uid: int, iid: int, file_id: Optional[str], mtype: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE plan_items SET media_file_id=?, media_type=? WHERE user_id=? AND item_id=?",
                         (file_id, mtype, uid, iid))
        await db.commit()

@_trace_async
async def _delete_item(uid: int, iid: int) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM plan_items WHERE user_id=? AND item_id=?", (uid, iid))
        await db.commit()

@_trace_async
async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        sql = """SELECT user_id, item_id, text, when_hhmm, done, media_file_id, media_type, origin
                 FROM plan_items WHERE user_id=? AND item_id=?"""
        cur = await db.execute(sql, (uid, iid))
        row = await cur.fetchone()
    if not row:
        return None
    return PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]),
                    row["media_file_id"], row["media_type"], row["origin"])

# -------------------------
# ИИ (Gemini — текст, OpenAI — изображение)
# -------------------------
def _gemini_model():
    global _gemini_model_cached
    if _gemini_model_cached:
        return _gemini_model_cached
    if not (genai and GEMINI_API_KEY):
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_model_cached = genai.GenerativeModel(GEMINI_MODEL)
        return _gemini_model_cached
    except Exception as e:
        log.error("Gemini init failed: %s", e)
        return None

async def _ai_generate_text(topic: str) -> Optional[str]:
    model = _gemini_model()
    if not model:
        log.warning("Gemini model not ready (no SDK or no GEMINI_API_KEY)")
        return None
    prompt = (
        "Сгенерируй короткий твит на русском или украинском по теме ниже. "
        "До 260 символов, живой тон, без хэштегов, без эмодзи в начале, по сути.\n\n"
        f"Тема: {topic}"
    )
    try:
        log.info("AI: generating text with Gemini, model=%s, topic=%s", GEMINI_MODEL, _short(topic, 80))
        resp = await asyncio.wait_for(asyncio.to_thread(model.generate_content, prompt), timeout=25)
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            log.warning("AI: empty text from Gemini")
            return None
        if len(text) > 270:
            text = text[:260].rstrip() + "…"
        log.info("AI: generated text len=%d", len(text))
        return text
    except asyncio.TimeoutError:
        log.error("AI: Gemini text timeout")
        return None
    except Exception as e:
        log.exception("AI: Gemini text error: %s", e)
        return None

async def _ai_generate_image_bytes(prompt: str) -> Optional[bytes]:
    if not OPENAI_API_KEY or not OpenAIClient:
        log.warning("OpenAI client not ready (no OPENAI_API_KEY or no SDK)")
        return None
    try:
        client = OpenAIClient(api_key=OPENAI_API_KEY)
        full_prompt = (prompt or "") + "\nРеалистичный квадратный арт для поста, без текста на изображении."
        log.info("AI: generating image with OpenAI, prompt=%s", _short(full_prompt, 120))
        result = await asyncio.wait_for(
            asyncio.to_thread(client.images.generate, model="gpt-image-1", prompt=full_prompt, size="1024x1024"),
            timeout=45
        )
        b64 = result.data[0].b64_json
        return base64.b64decode(b64)
    except asyncio.TimeoutError:
        log.error("AI: OpenAI image timeout")
        return None
    except Exception as e:
        log.exception("AI: OpenAI image error: %s", e)
        return None

# -------------------------
# Рендеринг и клавиатуры UI
# -------------------------
@_trace_sync
def _fmt_item(i: PlanItem) -> str:
    t = f"[{i.when_hhmm}]" if i.when_hhmm else "[—]"
    d = "✅" if i.done else "🟡"
    cam = " 📷" if i.media_file_id else ""
    mark = " 🤖" if getattr(i, "origin", "manual") == "ai" else ""
    txt = (i.text or "").strip() or "(пусто)"
    return f"{d} {t} {txt}{cam}{mark}"

@_trace_async
async def _kb_main(uid: int) -> InlineKeyboardMarkup:
    items = await _get_items(uid)
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"ITEM_MENU:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("➕ Новая (пустая)", callback_data="PLAN_ADD_EMPTY")],
        [InlineKeyboardButton("🧠 План ИИ", callback_data="PLAN_AI_OPEN")],
        [InlineKeyboardButton("↩️ Назад", callback_data="BACK_MAIN_MENU")],
    ]
    return InlineKeyboardMarkup(rows)

@_trace_async
async def _kb_ai(uid: int) -> InlineKeyboardMarkup:
    items = await _get_items(uid, origin="ai")
    rows: List[List[InlineKeyboardButton]] = []
    if items:
        for it in items:
            rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"ITEM_MENU:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("➕ Новая (ИИ)", callback_data="PLAN_AI_NEW")],
        [InlineKeyboardButton("➕ Новая (вручную)", callback_data="PLAN_AI_ADD_MANUAL")],
        [InlineKeyboardButton("⬅️ В общий список", callback_data="PLAN_OPEN")],
    ]
    return InlineKeyboardMarkup(rows)

@_trace_sync
def _kb_item(it: PlanItem) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Текст", callback_data=f"EDIT_ITEM:{it.item_id}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{it.item_id}")],
    ]
    if it.origin == "ai":
        rows.insert(0, [InlineKeyboardButton("🔁 ИИ: ещё вариант", callback_data=f"AI_REGEN_TEXT:{it.item_id}")])
    if it.media_file_id:
        rows.append([InlineKeyboardButton("👁 Показать медиа", callback_data=f"SHOW_ITEM:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("✅/🟡 Переключить статус", callback_data=f"TOGGLE_DONE:{it.item_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{it.item_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")],
    ]
    return InlineKeyboardMarkup(rows)

@_trace_sync
def _kb_cancel_to_list() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])

@_trace_sync
def _kb_add_more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Еще одна задача", callback_data="PLAN_ADD_EMPTY")],
        [InlineKeyboardButton("🧠 Ещё ИИ-задача", callback_data="PLAN_AI_NEW")],
        [InlineKeyboardButton("✅ Готово", callback_data="PLAN_OPEN")]
    ])

@_trace_sync
def _kb_ai_text_actions(iid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Текст ок", callback_data=f"AI_ACCEPT_TEXT:{iid}"),
         InlineKeyboardButton("🔁 Ещё вариант", callback_data=f"AI_REGEN_TEXT:{iid}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"AI_CANCEL:{iid}")]
    ])

@_trace_sync
def _kb_ai_image_question(iid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data=f"AI_GEN_IMG:{iid}")],
        [InlineKeyboardButton("⏭ Пропустить — к времени", callback_data=f"AI_SKIP_IMG:{iid}")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]
    ])

# ---------------
# Парсеры/хелперы
# ---------------
_TIME_RE_COLON = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

@_trace_sync
def _parse_time(s: str) -> Optional[str]:
    s0 = s
    s = (s or "").strip().replace(" ", "")
    m = _TIME_RE_COLON.match(s)
    if m:
        hh, mm = m.groups()
        return f"{int(hh):02d}:{int(mm):02d}"
    if s.isdigit() and len(s) in (3, 4):
        hh, mm = (s[0], s[1:]) if len(s) == 3 else (s[:2], s[2:])
        try:
            hh_i, mm_i = int(hh), int(mm)
            if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
                return f"{hh_i:02d}:{mm_i:02d}"
        except ValueError:
            pass
    log.debug("Time parse failed: %r", s0)
    return None

# ---------------
# Безопасные действия TG
# ---------------
@_trace_async
async def _safe_q_answer(q) -> bool:
    try:
        await q.answer()
        return True
    except BadRequest as e:
        if "query is too old" in str(e).lower():
            return False
        return False
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        try:
            await q.answer()
            return True
        except Exception:
            return False
    except Exception:
        return False

@_trace_async
async def _send_new_message_fallback(q, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        chat_id = q.message.chat_id if q and q.message else None
        if chat_id is None:
            return
        await q.message.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        with contextlib.suppress(Exception):
            await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=reply_markup)
    except Exception:
        pass

@_trace_async
async def edit_or_pass(q, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        msg = getattr(q, "message", None)
        if msg:
            key = (msg.chat_id, msg.message_id)
            markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
            new_sig = (text or "", markup_json)
            if LAST_SIG.get(key) == new_sig:
                return
        await q.edit_message_text(text=text, reply_markup=reply_markup)
        if msg:
            LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", markup_json)
        return
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            msg = getattr(q, "message", None)
            if msg:
                markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
                LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", markup_json)
            return
        except Exception:
            await _send_new_message_fallback(q, text, reply_markup)
            return
    except BadRequest as e:
        s = str(e)
        if "Message is not modified" in s:
            with contextlib.suppress(Exception):
                await q.edit_message_reply_markup(reply_markup=reply_markup)
                msg = getattr(q, "message", None)
                if msg:
                    markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
                    LAST_SIG[(msg.chat_id, msg.message_id)] = ((msg.text or ""), markup_json)
                return
        await _send_new_message_fallback(q, text, reply_markup)
        return
    except Exception:
        await _send_new_message_fallback(q, text, reply_markup)
        return

# -----------------------------
# Публичный entry-point для бота
# -----------------------------
@_trace_async
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть/обновить экран планировщика (ТОЛЬКО по явной кнопке)."""
    uid = update.effective_user.id
    kb = await _kb_main(uid)
    text = "🗓 ПЛАН НА ДЕНЬ\nВыбирай задачу или добавь новую."
    if update.callback_query:
        await edit_or_pass(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=kb)

# --------------------------------------
# Роутер callback-кнопок (group=0)
# --------------------------------------
@_trace_async
async def _cb_plan_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = (q.data or "").strip()
    await _safe_q_answer(q)

    # Общие экраны
    if data in ("PLAN_OPEN", "PLAN_LIST", "show_day_plan"):
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data == "PLAN_ADD_EMPTY":
        it = await _insert_item(uid, "", origin="manual")
        set_state_for_update(update, {"mode": "edit_text", "item_id": it.item_id, "uid": uid})
        await edit_or_pass(q, f"✏️ Введи текст для задачи #{it.item_id}", _kb_cancel_to_list())
        return

    if data.startswith("ITEM_MENU:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Задача не найдена"); return
        await edit_or_pass(q, f"📝 Задача #{it.item_id}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("DEL_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID"); return
        await _delete_item(uid, iid)
        await q.answer("Удалено.")
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data.startswith("TOGGLE_DONE:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи"); return
        await _update_done(uid, iid, not it.done)
        it = await _get_item(uid, iid)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("EDIT_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID"); return
        set_state_for_update(update, {"mode": "edit_text", "item_id": iid, "uid": uid})
        await edit_or_pass(q, f"✏️ Введи новый текст для задачи #{iid}", _kb_cancel_to_list())
        return

    if data.startswith("EDIT_TIME:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID"); return
        set_state_for_update(update, {"mode": "edit_time", "item_id": iid, "uid": uid})
        await edit_or_pass(q, f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)", _kb_cancel_to_list())
        return

    if data.startswith("SHOW_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it or not it.media_file_id:
            await q.answer("Медиа нет"); return
        caption = f"📝 #{it.item_id} {_fmt_item(it)}"
        if it.media_type == "photo":
            await q.message.bot.send_photo(chat_id=q.message.chat_id, photo=it.media_file_id, caption=caption)
        else:
            await q.message.bot.send_document(chat_id=q.message.chat_id, document=it.media_file_id, caption=caption)
        await edit_or_pass(q, f"📝 Задача #{it.item_id}\n{_fmt_item(it)}", _kb_item(it))
        return

    # --- ИИ-вкладка ---
    if data == "PLAN_AI_OPEN":
        await edit_or_pass(q, "🧠 ПЛАН ИИ", await _kb_ai(uid))
        return

    if data == "PLAN_AI_ADD_MANUAL":
        it = await _insert_item(uid, "", origin="manual")
        set_state_for_update(update, {"mode": "edit_text", "item_id": it.item_id, "uid": uid})
        await edit_or_pass(q, f"✏️ Введи текст для задачи #{it.item_id}", _kb_cancel_to_list())
        return

    if data == "PLAN_AI_NEW":
        # создаём пустой ИИ-элемент, ждём тему
        it = await _insert_item(uid, "", origin="ai")
        set_state_for_update(update, {"mode": "ai_wait_topic", "item_id": it.item_id, "uid": uid})
        await edit_or_pass(q, f"🧠 Введи тему для ИИ-задачи #{it.item_id}", _kb_cancel_to_list())
        return

    if data.startswith("AI_REGEN_TEXT:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it or it.origin != "ai":
            await q.answer("Нет такой ИИ-задачи"); return
        # регенерируем из последней принятой темы = текущий текст как подсказка темы
        topic = it.text or "Крипто, рынки, инвестиции"
        await edit_or_pass(q, f"🧠 Генерирую текст…", _kb_cancel_to_list())
        log.info("UI: AI_REGEN for iid=%s by uid=%s", iid, uid)
        text = await _ai_generate_text(topic)
        if not text:
            await edit_or_pass(q, "❌ Не удалось сгенерировать текст. Попробуй ещё раз.", _kb_ai(uid))
            return
        await _update_text(uid, iid, text)
        await edit_or_pass(q, f"🧪 Черновик #{iid} (ИИ):\n\n{text}", _kb_ai_text_actions(iid))
        return

    if data.startswith("AI_ACCEPT_TEXT:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it or it.origin != "ai":
            await q.answer("Нет такой ИИ-задачи"); return
        # спрашиваем про картинку
        set_state_for_update(update, {"mode": "ai_after_text", "item_id": iid, "uid": uid})
        await edit_or_pass(q, f"👍 Текст принят для #{iid}.\nСгенерировать изображение?", _kb_ai_image_question(iid))
        return

    if data.startswith("AI_CANCEL:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        await _delete_item(uid, iid)
        await edit_or_pass(q, "❎ Черновик ИИ удалён.", await _kb_ai(uid))
        return

    if data.startswith("AI_GEN_IMG:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи"); return
        log.info("UI: AI_GEN_IMG for iid=%s by uid=%s", iid, uid)
        await edit_or_pass(q, f"🖼 Генерирую изображение…", _kb_cancel_to_list())
        img = await _ai_generate_image_bytes(it.text)
        if not img:
            await edit_or_pass(q, "❌ Не удалось сгенерировать изображение. Перейти к установке времени?", InlineKeyboardMarkup([
                [InlineKeyboardButton("⏰ Ввести время", callback_data=f"EDIT_TIME:{iid}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="PLAN_OPEN")]
            ]))
            return
        # отправляем фото и сохраняем file_id
        bio = io.BytesIO(img)
        bio.name = f"ai_{iid}.png"
        msg = await q.message.bot.send_photo(chat_id=q.message.chat_id, photo=InputFile(bio), caption=f"🧠 Изображение для #{iid}")
        if msg.photo:
            file_id = msg.photo[-1].file_id
            await _update_media(uid, iid, file_id, "photo")
        # спрашиваем время
        set_state_for_update(update, {"mode": "edit_time", "item_id": iid, "uid": uid})
        await q.message.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
            reply_markup=_kb_cancel_to_list()
        )
        return

    if data.startswith("AI_SKIP_IMG:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID"); return
        set_state_for_update(update, {"mode": "edit_time", "item_id": iid, "uid": uid})
        await edit_or_pass(q, f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)", _kb_cancel_to_list())
        return

# --------------------------------------
# Текстовые/медийные сообщения (ввод для режимов)
# --------------------------------------
@_trace_async
async def _msg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    incoming_uid = update.effective_user.id
    msg = update.message
    txt = (getattr(msg, "text", None) or "").strip()
    st = get_state_for_update(update)
    log.debug("MSG router: incoming_uid=%s has_state=%s text=%r", incoming_uid, bool(st), _short(txt))

    if not st:
        return

    mode = st.get("mode")
    iid = int(st.get("item_id", 0))
    owner_uid = int(st.get("uid", incoming_uid))

    if iid == 0:
        clear_state_for_update(update)
        await msg.reply_text("Что-то пошло не так. Пожалуйста, попробуй ещё раз.")
        return

    if mode == "edit_text":
        final_text = txt
        file_id = None
        mtype = None

        if msg.photo:
            file_id = msg.photo[-1].file_id
            mtype = "photo"
            if not final_text:
                final_text = (msg.caption or "").strip() or "Фото"
        elif msg.document:
            mime = (msg.document.mime_type or "")
            if mime.startswith("image/"):
                file_id = msg.document.file_id
                mtype = "document"
                if not final_text:
                    final_text = (msg.caption or "").strip() or "Изображение"

        await _update_text(owner_uid, iid, final_text or "")
        if file_id:
            await _update_media(owner_uid, iid, file_id, mtype)

        set_state_for_update(update, {"mode": "edit_time", "item_id": iid, "uid": owner_uid})
        await msg.reply_text(
            "✅ Сохранено!\n⏰ Теперь введи время публикации в формате HH:MM (по Киеву)",
            reply_markup=_kb_cancel_to_list()
        )
        return

    if mode == "edit_time":
        t = _parse_time(txt)
        if not t:
            await msg.reply_text("⏰ Формат HH:MM. Можно также 930 или 0930. Попробуй ещё раз.")
            return
        await _update_time(owner_uid, iid, t)
        clear_state_for_update(update)
        await msg.reply_text(
            f"✅ Время установлено: {t}\n\nДобавить ещё одну задачу или закончить?",
            reply_markup=_kb_add_more()
        )
        return

    if mode == "ai_wait_topic":
        topic = txt
        if not topic:
            await msg.reply_text("✍️ Введи, пожалуйста, тему (текстом).")
            return
        await msg.reply_text("🧠 Генерирую текст…")
        text = await _ai_generate_text(topic)
        if not text:
            await msg.reply_text("❌ Не удалось сгенерировать текст. Попробуй другую тему.")
            return
        await _update_text(owner_uid, iid, text)
        # показываем черновик и ждём решение
        await msg.reply_text(f"🧪 Черновик #{iid} (ИИ):\n\n{text}", reply_markup=_kb_ai_text_actions(iid))
        # остаёмся в режиме ожидания кнопок, но режим меняем на 'ai_after_text' для логики
        set_state_for_update(update, {"mode": "ai_after_text", "item_id": iid, "uid": owner_uid})
        return

    if mode == "ai_after_text":
        # В этом режиме ожидались кнопки, а не ввод текста — подскажем действия.
        await msg.reply_text(
            "Выбери действие ниже:",
            reply_markup=_kb_ai_text_actions(iid)
        )
        return

    # неизвестный режим
    clear_state_for_update(update)

# ==== Экспорт для twitter_bot.py ====
@_trace_async
async def planner_add_from_text(uid: int, text: str, chat_id: int = None, bot = None) -> int:
    """Создаёт новую задачу с текстом и возвращает item_id. Если передан chat_id и bot, сразу запрашивает время."""
    it = await _insert_item(uid, text or "", origin="manual")
    if chat_id is not None and bot is not None:
        set_state_for_ids(chat_id, uid, {"mode": "edit_time", "item_id": it.item_id, "uid": uid})
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Текст сохранён!\n⏰ Теперь введи время для публикации в формате HH:MM (по Киеву)",
            reply_markup=_kb_cancel_to_list()
        )
    return it.item_id

@_trace_async
async def planner_prompt_time(uid: int, chat_id: int, bot) -> None:
    """Спрашивает у пользователя время для задачи последней/созданной записи."""
    items = await _get_items(uid)
    if not items:
        return
    iid = items[-1].item_id
    set_state_for_ids(chat_id, uid, {"mode": "edit_time", "item_id": iid, "uid": uid})
    await bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
        reply_markup=_kb_cancel_to_list()
    )

# --------------------------------------
# Регистрация хендлеров в PTB (group=0)
# --------------------------------------
@_trace_sync
def register_planner_handlers(app: Application) -> None:
    """
    Регистрируем РАНЬШЕ основного бота (group=0), чтобы планировщик
    забирал только свои колбэки. BACK_MAIN_MENU не ловим — это основной бот.
    Текст/медиа обрабатываем ТОЛЬКО при наличии STATE.
    """
    log.info("Planner: registering handlers (group=0)")

    app.add_handler(
        CallbackQueryHandler(
            _cb_plan_router,
            pattern=(
                r"^(?:"
                r"show_day_plan$|PLAN_OPEN$|PLAN_ADD_EMPTY$|ITEM_MENU:\d+$|DEL_ITEM:\d+$|EDIT_TIME:\d+$|EDIT_ITEM:\d+$|"
                r"TOGGLE_DONE:\d+$|SHOW_ITEM:\d+$|"
                r"PLAN_AI_OPEN$|PLAN_AI_NEW$|PLAN_AI_ADD_MANUAL$|"
                r"AI_ACCEPT_TEXT:\d+$|AI_REGEN_TEXT:\d+$|AI_CANCEL:\d+$|AI_GEN_IMG:\d+$|AI_SKIP_IMG:\d+$"
                r")$"
            )
        ),
        group=0
    )
    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_router), group=0)
    # Фото
    app.add_handler(MessageHandler(filters.PHOTO, _msg_router), group=0)
    # Документ-изображение (image/*)
    with contextlib.suppress(Exception):
        app.add_handler(MessageHandler(filters.Document.IMAGE, _msg_router), group=0)

    log.info("Planner: handlers registered")