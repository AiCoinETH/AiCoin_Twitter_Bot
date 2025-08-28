# -*- coding: utf-8 -*-
"""
Планировщик с ИИ-мастером (Gemini) и персистентностью в SQLite для twitter_bot.py.

Главное исправление: защита от ошибки SQLite "file is not a database".
Если обнаружена повреждённая/левого формата БД, файл автоматически переносится
в quarantine с суффиксом .bad-YYYYMMDDHHMMSS, затем БД создаётся заново.

Поддерживаемые действия (основные):
  PLAN_OPEN, PLAN_ADD_EMPTY, ITEM_MENU:<id>, DEL_ITEM:<id>, EDIT_TIME:<id>, EDIT_ITEM:<id>,
  TOGGLE_DONE:<id>, SHOW_ITEM:<id>, BACK_MAIN_MENU (обрабатывает основной бот)

Ветка ИИ-плана:
  AI_PLAN_OPEN, AI_TOPIC, AI_TXT_APPROVE, AI_TXT_REGEN, AI_EDIT_TEXT,
  AI_IMG_GEN, AI_IMG_APPROVE, AI_IMG_REGEN, AI_IMG_SKIP,
  AI_SAVE_AND_TIME, AI_DONE_ADD_MORE, AI_DONE_FINISH

Хранение:
  - Таблица plan_items(user_id, item_id, text, when_hhmm, done, media_file_id, media_type, created_at, source)
  - source: 'manual' | 'ai'
  - item_id — локальная последовательность на пользователя (1,2,3,...) — сохраняется

Состояние ввода:
  - Привязка по (chat_id, user_id) с общечатовым fallback (chat_id, 0)
"""

from __future__ import annotations
import re
import os
import io
import json
import time
import base64
import asyncio
import logging
import sqlite3
import shutil
import aiosqlite
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter

# ===== Gemini =====
_GEMINI_OK = False
try:
    import google.generativeai as genai
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        _GEMINI_OK = True
    else:
        _GEMINI_OK = False
except Exception:
    _GEMINI_OK = False

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

# Где хранить БД (по умолчанию — рядом с файлом)
DB_DIR = os.getenv("PLANNER_DB_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DB_DIR, exist_ok=True)
DB_FILE = os.path.join(DB_DIR, "planner.db")

STATE: Dict[Tuple[int, int], dict] = {}  # (chat_id,user_id)->state   и (chat_id,0)->state (fallback)
USER_STATE = STATE  # alias

LAST_SIG: Dict[Tuple[int, int], Tuple[str, str]] = {}  # (chat_id, message_id) -> (text, markup_json)
LAST_EDIT_AT: Dict[Tuple[int, int], float] = {}        # (chat_id, message_id) -> ts
MIN_EDIT_GAP = 0.8
_db_ready = False

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
        return f"PlanItem(iid={v.item_id}, time={v.when_hhmm}, done={v.done}, src={getattr(v,'source','?')}, text={_short(v.text, 60)!r})"
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
# STATE helpers
# ------------
def _state_keys_from_update(update: Update) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = update.effective_user.id if update.effective_user else 0
    return (chat_id, user_id), (chat_id, 0)

def set_state_for_update(update: Update, st: dict) -> None:
    k_personal, k_chat = _state_keys_from_update(update)
    STATE[k_personal] = st
    STATE[k_chat] = st

def get_state_for_update(update: Update) -> Optional[dict]:
    k_personal, k_chat = _state_keys_from_update(update)
    return STATE.get(k_personal) or STATE.get(k_chat)

def clear_state_for_update(update: Update) -> None:
    k_personal, k_chat = _state_keys_from_update(update)
    STATE.pop(k_personal, None)
    STATE.pop(k_chat, None)

def set_state_for_ids(chat_id: int, user_id: int, st: dict) -> None:
    STATE[(chat_id, user_id)] = st
    STATE[(chat_id, 0)] = st

# ------------
# Data model
# ------------
@dataclass
class PlanItem:
    user_id: int
    item_id: int
    text: str
    when_hhmm: Optional[str]
    done: bool
    media_file_id: Optional[str] = None
    media_type: Optional[str] = None
    source: str = "manual"  # 'manual' or 'ai'

# ------------
# SQLite
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
  created_at    TEXT    NOT NULL,
  source        TEXT    NOT NULL DEFAULT 'manual',
  PRIMARY KEY (user_id, item_id)
);
"""

# --- Вспомогательные функции по инициализации/ремонту БД ---

def _quarantine_bad_db() -> Optional[str]:
    """Переименовать текущий DB_FILE в *.bad-<ts> и вернуть новый путь (или None, если файла нет)."""
    if os.path.exists(DB_FILE):
        ts = datetime.now(TZ).strftime("%Y%m%d%H%M%S")
        bad_path = f"{DB_FILE}.bad-{ts}"
        try:
            os.replace(DB_FILE, bad_path)
            log.warning("Planner DB quarantined: %s -> %s", DB_FILE, bad_path)
            return bad_path
        except Exception as e:
            log.error("Failed to quarantine DB %s: %s", DB_FILE, e)
    return None

async def _create_schema() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute(CREATE_SQL)
        await db.commit()

@_trace_async
async def _migrate_db() -> None:
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            for sql in (
                "ALTER TABLE plan_items ADD COLUMN media_file_id TEXT",
                "ALTER TABLE plan_items ADD COLUMN media_type TEXT",
                "ALTER TABLE plan_items ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
            ):
                try:
                    await db.execute(sql)
                except Exception:
                    pass
            await db.commit()
    except Exception as e:
        log.warning("DB migrate skipped: %s", e)

@_trace_async
async def _ensure_db() -> None:
    """Убедиться, что БД существует и валидна. При ошибке 'file is not a database' — авто-ремонт."""
    global _db_ready
    if _db_ready:
        return
    log.info("Planner DB path: %s", DB_FILE)

    # Если на месте БД вдруг директория — в карантин
    if os.path.isdir(DB_FILE):
        _quarantine_bad_db()

    try:
        await _create_schema()
    except Exception as e:
        msg = str(e).lower()
        if isinstance(e, sqlite3.DatabaseError) or "file is not a database" in msg:
            _quarantine_bad_db()
            await _create_schema()
        else:
            raise
    await _migrate_db()
    _db_ready = True

# --- CRUD ---

@_trace_async
async def _get_items(uid: int) -> List[PlanItem]:
    await _ensure_db()
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            sql = """SELECT user_id, item_id, text, when_hhmm, done, media_file_id, media_type, source
                     FROM plan_items WHERE user_id=? ORDER BY COALESCE(when_hhmm,'99:99'), item_id"""
            cur = await db.execute(sql, (uid,))
            rows = await cur.fetchall()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db()
            await _create_schema()
            async with aiosqlite.connect(DB_FILE) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(sql, (uid,))
                rows = await cur.fetchall()
        else:
            raise
    return [PlanItem(r["user_id"], r["item_id"], r["text"], r["when_hhmm"], bool(r["done"]),
                     r["media_file_id"], r["media_type"], r["source"]) for r in rows]

@_trace_async
async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    sql = "SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            cur = await db.execute(sql, (uid,))
            row = await cur.fetchone()
            mx = row[0] if row is not None else 0
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db()
            await _create_schema()
            async with aiosqlite.connect(DB_FILE) as db:
                cur = await db.execute(sql, (uid,))
                row = await cur.fetchone()
                mx = row[0] if row is not None else 0
        else:
            raise
    return int(mx) + 1

@_trace_async
async def _insert_item(uid: int, text: str = "", when_hhmm: Optional[str] = None, source: str = "manual") -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    sql = """INSERT INTO plan_items(user_id, item_id, text, when_hhmm, done, media_file_id, media_type, created_at, source)
             VALUES (?,?,?,?,?,?,?,?,?)"""
    args = (uid, iid, text or "", when_hhmm, 0, None, None, now, source)
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, args)
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db()
            await _create_schema()
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute(sql, args)
                await db.commit()
        else:
            raise
    return PlanItem(uid, iid, text or "", when_hhmm, False, None, None, source)

@_trace_async
async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    sql = "UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, (text or "", uid, iid))
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
        else:
            raise

@_trace_async
async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    sql = "UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, (when_hhmm, uid, iid))
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
        else:
            raise

@_trace_async
async def _update_done(uid: int, iid: int, done: bool) -> None:
    await _ensure_db()
    sql = "UPDATE plan_items SET done=? WHERE user_id=? AND item_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, (1 if done else 0, uid, iid))
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
        else:
            raise

@_trace_async
async def _update_media(uid: int, iid: int, file_id: Optional[str], mtype: Optional[str]) -> None:
    await _ensure_db()
    sql = "UPDATE plan_items SET media_file_id=?, media_type=? WHERE user_id=? AND item_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, (file_id, mtype, uid, iid))
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
        else:
            raise

@_trace_async
async def _delete_item(uid: int, iid: int) -> None:
    await _ensure_db()
    sql = "DELETE FROM plan_items WHERE user_id=? AND item_id=?"
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(sql, (uid, iid))
            await db.commit()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
        else:
            raise

@_trace_async
async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    sql = """SELECT user_id, item_id, text, when_hhmm, done, media_file_id, media_type, source
             FROM plan_items WHERE user_id=? AND item_id=?"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, (uid, iid))
            row = await cur.fetchone()
    except sqlite3.DatabaseError as e:
        if "file is not a database" in str(e).lower():
            _quarantine_bad_db(); await _create_schema()
            async with aiosqlite.connect(DB_FILE) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(sql, (uid, iid))
                row = await cur.fetchone()
        else:
            raise
    if not row:
        return None
    return PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]),
                    row["media_file_id"], row["media_type"], row["source"])

# -------------------------
# Рендеринг и клавиатуры UI
# -------------------------
@_trace_sync
def _fmt_item(i: PlanItem) -> str:
    t = f"[{i.when_hhmm}]" if i.when_hhmm else "[—]"
    d = "✅" if i.done else "🟡"
    cam = " 📷" if i.media_file_id else ""
    src = "🤖" if i.source == "ai" else "✋"
    txt = (i.text or "").strip() or "(пусто)"
    return f"{d} {t} {src} {txt}{cam}"

@_trace_async
async def _kb_main(uid: int) -> InlineKeyboardMarkup:
    items = await _get_items(uid)
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"ITEM_MENU:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("➕ Новая (моя)", callback_data="PLAN_ADD_EMPTY"),
         InlineKeyboardButton("🧠 План ИИ", callback_data="AI_PLAN_OPEN")],
        [InlineKeyboardButton("↩️ Назад", callback_data="BACK_MAIN_MENU")],
    ]
    return InlineKeyboardMarkup(rows)

@_trace_sync
def _kb_item(it: PlanItem) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Текст", callback_data=f"EDIT_ITEM:{it.item_id}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{it.item_id}")],
    ]
    if it.media_file_id:
        rows.append([InlineKeyboardButton("👁 Показать", callback_data=f"SHOW_ITEM:{it.item_id}")])
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
        [InlineKeyboardButton("➕ Ещё одна", callback_data="PLAN_ADD_EMPTY"),
         InlineKeyboardButton("🧠 Ещё ИИ-пост", callback_data="AI_PLAN_OPEN")],
        [InlineKeyboardButton("✅ Готово", callback_data="PLAN_OPEN")]
    ])

# ----- ИИ мастер UI -----
@_trace_sync
def _kb_ai_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Создать ИИ-пост", callback_data="AI_TOPIC")],
        [InlineKeyboardButton("⬅️ Назад к плану", callback_data="PLAN_OPEN")],
    ])

@_trace_sync
def _kb_ai_text_actions() -> InlineKeyboardMarkup:
    # оставлено для совместимости, но основной поток идёт через предпросмотр
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Текст подходит", callback_data="AI_TXT_APPROVE"),
         InlineKeyboardButton("🔁 Перегенерировать", callback_data="AI_TXT_REGEN")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="AI_PLAN_OPEN")],
    ])

@_trace_sync
def _kb_ai_image_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data="AI_IMG_GEN")],
        [InlineKeyboardButton("⏭ Пропустить картинку", callback_data="AI_IMG_SKIP")],
    ])

@_trace_sync
def _kb_ai_image_after_gen() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Изображение ок", callback_data="AI_IMG_APPROVE"),
         InlineKeyboardButton("🔁 Ещё вариант", callback_data="AI_IMG_REGEN")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="AI_PLAN_OPEN")],
    ])

# >>>>>>>>>> НОВОЕ: клавиатура предпросмотра
@_trace_sync
def _kb_ai_preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="AI_EDIT_TEXT"),
         InlineKeyboardButton("🔁 Реген текста", callback_data="AI_TXT_REGEN")],
        [InlineKeyboardButton("🖼 Сгенерировать изображение", callback_data="AI_IMG_GEN"),
         InlineKeyboardButton("⏭ Без изображения", callback_data="AI_IMG_SKIP")],
        [InlineKeyboardButton("💾 Сохранить и выбрать время", callback_data="AI_SAVE_AND_TIME")],
        [InlineKeyboardButton("⬅️ Отмена", callback_data="AI_PLAN_OPEN")],
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
# Telegram safe ops
# ---------------
@_trace_async
async def _send_new_message_fallback(q, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        chat_id = q.message.chat_id if q and q.message else None
        if chat_id is None:
            return
        await q.message.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        try:
            await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=reply_markup)
        except Exception:
            pass
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
            ts = time.time()
            last_ts = LAST_EDIT_AT.get(key, 0.0)
            if ts - last_ts < MIN_EDIT_GAP:
                await asyncio.sleep(MIN_EDIT_GAP - (ts - last_ts))
        await q.edit_message_text(text=text, reply_markup=reply_markup)
        if msg:
            LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True))
            LAST_EDIT_AT[(msg.chat_id, msg.message_id)] = time.time()
        return
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 2) + 1)
        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            msg = getattr(q, "message", None)
            if msg:
                LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True))
                LAST_EDIT_AT[(msg.chat_id, msg.message_id)] = time.time()
            return
        except Exception:
            await _send_new_message_fallback(q, text, reply_markup)
            return
    except BadRequest as e:
        s = str(e)
        if "Message is not modified" in s:
            try:
                await q.edit_message_reply_markup(reply_markup=reply_markup)
                msg = getattr(q, "message", None)
                if msg:
                    LAST_SIG[(msg.chat_id, msg.message_id)] = ((msg.text or ""), json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True))
                    LAST_EDIT_AT[(msg.chat_id, msg.message_id)] = time.time()
                return
            except Exception:
                await _send_new_message_fallback(q, text, reply_markup)
                return
        if "query is too old" in s.lower():
            await _send_new_message_fallback(q, text, reply_markup)
            return
        await _send_new_message_fallback(q, text, reply_markup)
        return
    except Exception:
        await _send_new_message_fallback(q, text, reply_markup)
        return

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

# -----------------------------
# Публичный entry-point
# -----------------------------
@_trace_async
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = await _kb_main(uid)
    text = "🗓 ПЛАН НА ДЕНЬ\nВыбирай задачу, добавь новую или запусти 🧠 План ИИ."
    if update.callback_query:
        await edit_or_pass(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=kb)

# --------------------------------------
# Callback router (group=0)
# --------------------------------------
@_trace_async
async def _cb_plan_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = (q.data or "").strip()
    await _safe_q_answer(q)

    # Главный экран
    if data in ("PLAN_OPEN", "PLAN_LIST", "show_day_plan"):
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    # ----- ИИ-план: домашний экран -----
    if data == "AI_PLAN_OPEN":
        set_state_for_update(update, {"mode": "ai_home", "uid": uid})
        await edit_or_pass(q, "🧠 План ИИ\nТы можешь создать ИИ-пост по теме.\nНажми «Создать ИИ-пост».", _kb_ai_home())
        return

    # Старт ввода темы
    if data == "AI_TOPIC":
        set_state_for_update(update, {"mode": "ai_topic", "uid": uid})
        await edit_or_pass(q, "🧠 Введи ТЕМУ поста (1–2 предложения).\nПосле этого я сгенерирую текст.", _kb_cancel_to_list())
        return

    # >>>>>>>>>> НОВОЕ: подтверждение текста ведёт в предпросмотр
    if data == "AI_TXT_APPROVE":
        st = get_state_for_update(update) or {}
        ai_text = (st.get("ai_text") or "").strip()
        if not ai_text:
            await edit_or_pass(q, "Пока нет текста. Введи тему и сгенерируй снова.", _kb_ai_home())
            return
        st["mode"] = "ai_preview"
        set_state_for_update(update, st)
        await edit_or_pass(q, f"📝 Предпросмотр:\n\n{ai_text}", _kb_ai_preview())
        return

    # >>>>>>>>>> НОВОЕ: регенерация текста
    if data == "AI_TXT_REGEN":
        st = get_state_for_update(update) or {}
        topic = (st.get("ai_topic") or "").strip()
        if not topic:
            await edit_or_pass(q, "Сначала укажи тему.", _kb_ai_home())
            return
        await edit_or_pass(q, "🧠 Генерирую новый вариант текста…", _kb_cancel_to_list())
        try:
            if not _GEMINI_OK:
                raise RuntimeError("Не задан GEMINI_API_KEY.")
            sys_prompt = (
                "You are a social media copywriter. Create a short, engaging post for X/Twitter: "
                "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags."
            )
            model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=sys_prompt)
            resp = await asyncio.to_thread(model.generate_content, [topic], request_options={"timeout": 45})
            text_out = (getattr(resp, "text", None) or "").strip() or "Не удалось получить текст. Попробуй ещё раз."
            st["ai_text"] = text_out
            st["mode"] = "ai_preview"
            set_state_for_update(update, st)
            await edit_or_pass(q, f"✍️ Обновлённый текст:\n\n{text_out}", _kb_ai_preview())
        except Exception as e:
            await edit_or_pass(q, f"Ошибка генерации: {e}", _kb_ai_home())
        return

    # >>>>>>>>>> НОВОЕ: переход к ручной правке текста
    if data == "AI_EDIT_TEXT":
        st = get_state_for_update(update) or {}
        st["mode"] = "ai_edit_text"
        set_state_for_update(update, st)
        await edit_or_pass(q, "✏️ Отправь новый текст поста одним сообщением.", _kb_cancel_to_list())
        return

    # Генерация изображения
    if data == "AI_IMG_GEN":
        st = get_state_for_update(update) or {}
        if st.get("busy_ai_image"):
            await edit_or_pass(q, "⚙️ Уже генерирую изображение…", _kb_ai_image_after_gen())
            return
        st["busy_ai_image"] = True
        set_state_for_update(update, st)

        topic = (st.get("ai_topic") or "").strip()
        text_for_img = (st.get("ai_text") or "").strip()
        prompt_img = f"Create a square social-media illustration matching this post:\nTopic: {topic}\nPost text: {text_for_img}\nClean, eye-catching, no text overlay."
        photo_file_id = None

        try:
            if not _GEMINI_OK:
                raise RuntimeError("Gemini API key missing.")
            model = genai.GenerativeModel("gemini-1.5-flash")
            img_resp = await asyncio.to_thread(model.generate_content, [{"text": prompt_img}], request_options={"timeout": 60})
            b64 = None
            for part in getattr(img_resp, "candidates", []) or []:
                for p in getattr(part, "content", {}).get("parts", []):
                    if hasattr(p, "inline_data") and getattr(p.inline_data, "mime_type", "").startswith("image/"):
                        b64 = p.inline_data.data
                        break
                if b64:
                    break
            if not b64:
                model2 = genai.GenerativeModel("gemini-1.5-flash")
                img2 = await asyncio.to_thread(model2.generate_content, [{"text": prompt_img + "\nReturn an image as base64 PNG inline_data."}], request_options={"timeout": 60})
                for part in getattr(img2, "candidates", []) or []:
                    for p in getattr(part, "content", {}).get("parts", []):
                        if hasattr(p, "inline_data") and getattr(p.inline_data, "mime_type", "").startswith("image/"):
                            b64 = p.inline_data.data
                            break
                    if b64:
                        break
            if not b64:
                raise RuntimeError("Модель не вернула изображение.")

            image_bytes = base64.b64decode(b64)
            bio = io.BytesIO(image_bytes)
            bio.name = "ai_image.png"
            msg = await q.message.bot.send_photo(chat_id=q.message.chat_id, photo=InputFile(bio), caption="🖼 Вариант изображения")
            if msg and msg.photo:
                photo_file_id = msg.photo[-1].file_id
            st = get_state_for_update(update) or {}
            st["busy_ai_image"] = False
            st["ai_image_file_id"] = photo_file_id
            set_state_for_update(update, st)
            await edit_or_pass(q, "Изображение сгенерировано. Подходит?", _kb_ai_image_after_gen())
            return
        except Exception as e:
            st = get_state_for_update(update) or {}
            st["busy_ai_image"] = False
            set_state_for_update(update, st)
            await edit_or_pass(q, f"Не удалось сгенерировать изображение: {e}\nМожешь попробовать ещё раз или пропустить.", _kb_ai_image_actions())
            return

    # Ещё вариант изображения
    if data == "AI_IMG_REGEN":
        st = get_state_for_update(update) or {}
        st.pop("ai_image_file_id", None)
        set_state_for_update(update, st)
        update.callback_query.data = "AI_IMG_GEN"
        await _cb_plan_router(update, context)
        return

    # Изображение принято
    if data == "AI_IMG_APPROVE":
        st = get_state_for_update(update) or {}
        st["mode"] = "ai_ready_to_save"
        set_state_for_update(update, st)
        await edit_or_pass(q, "Супер! Сохранить в план и задать время?", InlineKeyboardMarkup([
            [InlineKeyboardButton("💾 Сохранить и выбрать время", callback_data="AI_SAVE_AND_TIME")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="AI_PLAN_OPEN")],
        ]))
        return

    # Пропуск изображения — сразу к сохранению
    if data == "AI_IMG_SKIP":
        st = get_state_for_update(update) or {}
        st["ai_image_file_id"] = None
        st["mode"] = "ai_ready_to_save"
        set_state_for_update(update, st)
        await edit_or_pass(q, "Ок, без изображения. Сохранить в план и выбрать время?", InlineKeyboardMarkup([
            [InlineKeyboardButton("💾 Сохранить и выбрать время", callback_data="AI_SAVE_AND_TIME")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="AI_PLAN_OPEN")],
        ]))
        return

    # Сохранение ИИ-поста как item и запрос времени
    if data == "AI_SAVE_AND_TIME":
        st = get_state_for_update(update) or {}
        ai_text = (st.get("ai_text") or "").strip()
        if not ai_text:
            await edit_or_pass(q, "Нет текста. Начнём заново?", _kb_ai_home())
            return
        it = await _insert_item(uid, ai_text, None, source="ai")
        if st.get("ai_image_file_id"):
            await _update_media(uid, it.item_id, st["ai_image_file_id"], "photo")
        set_state_for_update(update, {"mode": "edit_time", "item_id": it.item_id, "uid": uid})
        await edit_or_pass(q, f"💾 Сохранено как задача #{it.item_id}.\n⏰ Введи время в формате HH:MM (по Киеву).", _kb_cancel_to_list())
        return

    # ----- Обычный план: добавить пустую -----
    if data == "PLAN_ADD_EMPTY":
        it = await _insert_item(uid, "", None, source="manual")
        set_state_for_update(update, {"mode": "edit_text", "item_id": it.item_id, "uid": uid})
        await edit_or_pass(q, f"✏️ Введи текст для задачи #{it.item_id}", _kb_cancel_to_list())
        return

    # Карточка айтема
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

# --------------------------------------
# Текстовые/медийные сообщения (ввод для режимов)
# --------------------------------------
@_trace_async
async def _msg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    incoming_uid = update.effective_user.id
    txt = (getattr(update.message, "text", None) or "").strip()
    st = get_state_for_update(update)

    if not st:
        return

    mode = st.get("mode")
    iid = int(st.get("item_id", 0))
    owner_uid = int(st.get("uid", incoming_uid))

    # ===== Ветка ИИ: ввод темы =====
    if mode == "ai_topic":
        topic = txt
        await update.message.reply_text("🧠 Генерирую текст…")
        try:
            if not _GEMINI_OK:
                raise RuntimeError("Не задан GEMINI_API_KEY.")
            sys_prompt = (
                "You are a social media copywriter. Create a short, engaging post for X/Twitter: "
                "limit ~230 chars, 1–2 sentences, 1 emoji max, include a subtle hook, no hashtags."
            )
            model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=sys_prompt)
            resp = await asyncio.to_thread(model.generate_content, [topic], request_options={"timeout": 45})
            text_out = (getattr(resp, "text", None) or "").strip()
            if not text_out:
                text_out = "Не удалось получить текст. Попробуй изменить тему."
            st["mode"] = "ai_preview"              # <<<< изменить режим
            st["ai_topic"] = topic
            st["ai_text"] = text_out
            set_state_for_update(update, st)
            await update.message.reply_text(f"✍️ Вариант текста:\n\n{text_out}", reply_markup=_kb_ai_preview())  # <<<< показать предпросмотр
        except Exception as e:
            await update.message.reply_text(f"Ошибка генерации: {e}", reply_markup=_kb_ai_home())
        return

    # >>>>>>>>>> НОВОЕ: ручная правка ИИ-текста
    if mode == "ai_edit_text":
        new_text = txt
        if not new_text:
            await update.message.reply_text("Текст пуст. Отправь содержимое поста сообщением.", reply_markup=_kb_cancel_to_list())
            return
        st["ai_text"] = new_text
        st["mode"] = "ai_preview"
        set_state_for_update(update, st)
        await update.message.reply_text(f"✅ Обновил текст.\n\n{new_text}", reply_markup=_kb_ai_preview())
        return

    # ===== Ветка ИИ/Обычная: установка времени =====
    if mode == "edit_time" and iid != 0:
        t = _parse_time(txt)
        if not t:
            await update.message.reply_text("⏰ Формат HH:MM. Можно также 930 или 0930. Попробуй ещё раз.")
            return
        await _update_time(owner_uid, iid, t)
        clear_state_for_update(update)
        await update.message.reply_text(
            f"✅ Время установлено: {t}\n\nДобавить ещё?",
            reply_markup=_kb_add_more()
        )
        return

    # ===== Обычный режим: ввод/редакт текста =====
    if mode == "edit_text" and iid != 0:
        final_text = txt
        file_id = None
        mtype = None

        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            mtype = "photo"
            if not final_text:
                final_text = (update.message.caption or "").strip() or "Фото"
        elif update.message.document:
            mime = (update.message.document.mime_type or "")
            if mime.startswith("image/"):
                file_id = update.message.document.file_id
                mtype = "document"
                if not final_text:
                    final_text = (update.message.caption or "").strip() or "Изображение"

        await _update_text(owner_uid, iid, final_text or "")
        if file_id:
            await _update_media(owner_uid, iid, file_id, mtype)

        set_state_for_update(update, {"mode": "edit_time", "item_id": iid, "uid": owner_uid})
        await update.message.reply_text(
            "✅ Сохранено!\n⏰ Теперь введи время в формате HH:MM (по Киеву)",
            reply_markup=_kb_cancel_to_list()
        )
        return

    # неизвестный режим
    clear_state_for_update(update)

# ==== Экспорт для twitter_bot.py ====
@_trace_async
async def planner_add_from_text(uid: int, text: str, chat_id: int = None, bot = None) -> int:
    it = await _insert_item(uid, text or "", source="manual")
    if chat_id is not None and bot is not None:
        set_state_for_ids(chat_id, uid, {"mode": "edit_time", "item_id": it.item_id, "uid": uid})
        await bot.send_message(
            chat_id=chat_id,
            text="✅ Текст сохранён!\n⏰ Теперь введи время (HH:MM, Киев)",
            reply_markup=_kb_cancel_to_list()
        )
    return it.item_id

@_trace_async
async def planner_prompt_time(uid: int, chat_id: int, bot) -> None:
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
    log.info("Planner: registering handlers (group=0)")
    log.info("Planner DB path (again): %s", DB_FILE)

    app.add_handler(
        CallbackQueryHandler(
            _cb_plan_router,
            pattern=(
                r"^(?:"
                r"show_day_plan$|PLAN_OPEN$|PLAN_ADD_EMPTY$|"
                r"ITEM_MENU:\d+$|DEL_ITEM:\d+$|EDIT_TIME:\d+$|EDIT_ITEM:\d+$|TOGGLE_DONE:\d+$|SHOW_ITEM:\d+$|"
                r"AI_PLAN_OPEN$|AI_TOPIC$|AI_TXT_APPROVE$|AI_TXT_REGEN$|AI_EDIT_TEXT$|"
                r"AI_IMG_GEN$|AI_IMG_APPROVE$|AI_IMG_REGEN$|AI_IMG_SKIP$|"
                r"AI_SAVE_AND_TIME$"
                r")$"
            )
        ),
        group=0
    )

    # Текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_router), group=0)
    # Фото
    app.add_handler(MessageHandler(filters.PHOTO, _msg_router), group=0)
    # Документ-изображение
    try:
        app.add_handler(MessageHandler(filters.Document.IMAGE, _msg_router), group=0)
    except Exception:
        pass

    log.info("Planner: handlers registered")