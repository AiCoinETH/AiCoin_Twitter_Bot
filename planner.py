# -*- coding: utf-8 -*-
"""
Планировщик с персистентностью в SQLite для twitter_bot.py.

Поддерживаемые действия:
  PLAN_* , ITEM_MENU:, DEL_ITEM:, EDIT_TIME:, EDIT_ITEM:,
  TOGGLE_DONE:, а также BACK_MAIN_MENU для возврата в основной бот.

Хранение:
  - Таблица plan_items(user_id, item_id, text, when_hhmm, done, created_at)
  - item_id — локальная последовательность на пользователя (1,2,3,...) — сохраняется

Состояние ввода:
  - Привязка по (chat_id, user_id) с общечатовым fallback (chat_id, 0)
"""

from __future__ import annotations
import re
import json
import asyncio
import logging
import aiosqlite
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, RetryAfter

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
DB_FILE = "planner.db"

# Состояние ожиданий ввода (правка текста/времени/новая тема)
STATE: Dict[Tuple[int, int], dict] = {}

# Экспортируемое имя для совместимости с импортом в twitter_bot.py
USER_STATE = STATE  # alias

# Для анти-дубликатов правок сообщений (защита от 400 "Message is not modified")
LAST_SIG: Dict[Tuple[int, int], Tuple[str, str]] = {}  # (chat_id, message_id) -> (text, markup_json)

_db_ready = False  # ленивый init

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
        if v.__class__.__name__ in {"Bot", "Application", "CallbackContext"}:
            return f"<{v.__class__.__name__}>"
    except Exception:
        pass
    if isinstance(v, PlanItem):
        return f"PlanItem(iid={v.item_id}, time={v.when_hhmm}, done={v.done}, text={_short(v.text, 60)!r})"
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
                  (", " + ", ".join(f"{k}={_fmt_arg(v)}" for k, v in kwargs.items())) if kwargs else "")
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
    item_id: int        # локальный порядковый id внутри пользователя
    text: str
    when_hhmm: Optional[str]  # "HH:MM" | None
    done: bool

# ------------
# База (SQLite)
# ------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plan_items (
  user_id     INTEGER NOT NULL,
  item_id     INTEGER NOT NULL,
  text        TEXT    NOT NULL DEFAULT '',
  when_hhmm   TEXT,
  done        INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (user_id, item_id)
);
"""

@_trace_async
async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        log.debug("DB already ready")
        return
    log.info("DB init start: %s", DB_FILE)
    async with aiosqlite.connect(DB_FILE) as db:
        log.debug("SQL exec: CREATE TABLE")
        await db.execute(CREATE_SQL)
        await db.commit()
    _db_ready = True
    log.info("DB init complete")

@_trace_async
async def _get_items(uid: int) -> List[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? ORDER BY item_id ASC"
        log.debug("SQL: %s | args=(%s,)", sql, uid)
        cur = await db.execute(sql, (uid,))
        rows = await cur.fetchall()
    items = [PlanItem(r["user_id"], r["item_id"], r["text"], r["when_hhmm"], bool(r["done"])) for r in rows]
    log.debug("Loaded %d items for uid=%s", len(items), uid)
    return items

@_trace_async
async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?"
        log.debug("SQL: %s | args=(%s,)", sql, uid)
        cur = await db.execute(sql, (uid,))
        (mx,) = await cur.fetchone()
    nxt = int(mx) + 1
    log.debug("Next item_id=%s for uid=%s", nxt, uid)
    return nxt

@_trace_async
async def _insert_item(uid: int, text: str = "", when_hhmm: Optional[str] = None) -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "INSERT INTO plan_items(user_id, item_id, text, when_hhmm, done, created_at) VALUES (?,?,?,?,?,?)"
        args = (uid, iid, text or "", when_hhmm, 0, now)
        log.debug("SQL: %s | args=%s", sql, args)
        await db.execute(sql, args)
        await db.commit()
    item = PlanItem(uid, iid, text or "", when_hhmm, False)
    log.info("Inserted item: %s", _fmt_arg(item))
    return item

@_trace_async
async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?"
        args = (text or "", uid, iid)
        log.debug("SQL: %s | args=%s", sql, (repr(_short(text)), uid, iid))
        await db.execute(sql, args)
        await db.commit()
    log.info("Text updated for uid=%s iid=%s", uid, iid)

@_trace_async
async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?"
        args = (when_hhmm, uid, iid)
        log.debug("SQL: %s | args=%s", sql, args)
        await db.execute(sql, args)
        await db.commit()
    log.info("Time updated for uid=%s iid=%s -> %s", uid, iid, when_hhmm)

@_trace_async
async def _update_done(uid: int, iid: int, done: bool) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "UPDATE plan_items SET done=? WHERE user_id=? AND item_id=?"
        args = (1 if done else 0, uid, iid)
        log.debug("SQL: %s | args=%s", sql, args)
        await db.execute(sql, args)
        await db.commit()
    log.info("Done toggled for uid=%s iid=%s -> %s", uid, iid, done)

@_trace_async
async def _delete_item(uid: int, iid: int) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        sql = "DELETE FROM plan_items WHERE user_id=? AND item_id=?"
        args = (uid, iid)
        log.debug("SQL: %s | args=%s", sql, args)
        await db.execute(sql, args)
        await db.commit()
    log.info("Deleted uid=%s iid=%s", uid, iid)

@_trace_async
async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? AND item_id=?"
        log.debug("SQL: %s | args=(%s,%s)", sql, uid, iid)
        cur = await db.execute(sql, (uid, iid))
        row = await cur.fetchone()
    if not row:
        log.debug("Item not found uid=%s iid=%s", uid, iid)
        return None
    item = PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]))
    log.debug("Fetched: %s", _fmt_arg(item))
    return item

@_trace_async
async def _find_next_item(uid: int, after_iid: int) -> Optional[PlanItem]:
    """Найти следующую задачу по item_id."""
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        sql = ("SELECT user_id, item_id, text, when_hhmm, done FROM plan_items "
               "WHERE user_id=? AND item_id>? ORDER BY item_id ASC LIMIT 1")
        log.debug("SQL: %s | args=(%s,%s)", sql, uid, after_iid)
        cur = await db.execute(sql, (uid, after_iid))
        row = await cur.fetchone()
    if not row:
        log.debug("No next item after iid=%s for uid=%s", after_iid, uid)
        return None
    nxt = PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]))
    log.debug("Next item: %s", _fmt_arg(nxt))
    return nxt

# -------------------------
# Рендеринг и клавиатуры UI
# -------------------------
@_trace_sync
def _fmt_item(i: PlanItem) -> str:
    t = f"[{i.when_hhmm}]" if i.when_hhmm else "[—]"
    d = "✅" if i.done else "🟡"
    txt = (i.text or "").strip() or "(пусто)"
    return f"{d} {t} {txt}"

@_trace_async
async def _kb_main(uid: int) -> InlineKeyboardMarkup:
    items = await _get_items(uid)
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"ITEM_MENU:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("➕ Новая (пустая)", callback_data="PLAN_ADD_EMPTY")],
        [InlineKeyboardButton("↩️ Назад", callback_data="BACK_MAIN_MENU")],
    ]
    kb = InlineKeyboardMarkup(rows)
    log.debug("Main keyboard built: rows=%d", len(rows))
    return kb

@_trace_sync
def _kb_item(it: PlanItem) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Текст", callback_data=f"EDIT_ITEM:{it.item_id}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{it.item_id}")],
        [InlineKeyboardButton("✅/🟡 Переключить статус", callback_data=f"TOGGLE_DONE:{it.item_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{it.item_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")],
    ]
    kb = InlineKeyboardMarkup(rows)
    log.debug("Item keyboard built for iid=%s", it.item_id)
    return kb

@_trace_sync
def _kb_cancel_to_list() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])

# ---------------
# Парсеры/хелперы
# ---------------
_TIME_RE_COLON = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")  # строго HH:MM

@_trace_sync
def _parse_time(s: str) -> Optional[str]:
    """
    Принимает:
      - 'HH:MM' (00:00–23:59)
      - '930'  / '0930' -> '09:30'
      - '1230' -> '12:30'
    Возвращает 'HH:MM' или None.
    """
    original = s
    s = (s or "").strip().replace(" ", "")
    m = _TIME_RE_COLON.match(s)
    if m:
        hh, mm = m.groups()
        res = f"{int(hh):02d}:{int(mm):02d}"
        log.debug("Time parsed (colon) %r -> %s", original, res)
        return res
    if s.isdigit() and len(s) in (3, 4):
        if len(s) == 3:
            hh, mm = s[0], s[1:]
        else:
            hh, mm = s[:2], s[2:]
        try:
            hh_i, mm_i = int(hh), int(mm)
            if 0 <= hh_i <= 23 and 0 <= mm_i <= 59:
                res = f"{hh_i:02d}:{mm_i:02d}"
                log.debug("Time parsed (digits) %r -> %s", original, res)
                return res
        except ValueError:
            pass
    log.debug("Time parse failed: %r", original)
    return None

# ---------------
# Безопасные действия TG
# ---------------
@_trace_async
async def _safe_q_answer(q) -> bool:
    try:
        await q.answer()
        log.debug("answerCallbackQuery OK")
        return True
    except BadRequest as e:
        if "query is too old" in str(e).lower():
            log.warning("TG: callback too old; ignore.")
            return False
        log.error("TG: answerCallbackQuery bad request: %s", e)
        return False
    except RetryAfter as e:
        delay = getattr(e, "retry_after", 2) + 1
        log.warning("TG: answerCallbackQuery flood, sleep=%s", delay)
        await asyncio.sleep(delay)
        try:
            await q.answer()
            log.debug("answerCallbackQuery retry OK")
            return True
        except Exception as e2:
            log.error("TG: answerCallbackQuery retry failed: %s", e2)
            return False
    except Exception as e:
        log.error("TG: answerCallbackQuery unknown error: %s", e)
        return False

@_trace_async
async def _send_new_message_fallback(q, text: str, reply_markup: InlineKeyboardMarkup):
    """Фоллбэк: если редактировать нельзя — шлём новое сообщение туда же."""
    try:
        chat_id = q.message.chat_id if q and q.message else None
        if chat_id is None:
            log.warning("TG: no message/chat in callback for fallback send")
            return
        await q.message.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        log.debug("TG: fallback message sent")
    except RetryAfter as e:
        delay = getattr(e, "retry_after", 2) + 1
        log.warning("TG: send_message flood, sleep=%s", delay)
        await asyncio.sleep(delay)
        try:
            await q.message.bot.send_message(chat_id=q.message.chat_id, text=text, reply_markup=reply_markup)
            log.debug("TG: fallback message retry sent")
        except Exception as e2:
            log.error("TG: fallback send retry failed: %s", e2)
    except Exception as e:
        log.error("TG: fallback send error: %s", e)

@_trace_async
async def edit_or_pass(q, text: str, reply_markup: InlineKeyboardMarkup):
    """
    Безопасно редактируем сообщение.
    - Если «Message is not modified» — пробуем изменить только разметку.
    - Если флад-контроль — ждём и пробуем ещё раз.
    - Если ничего не поменялось (anti-dup) — тихо пропускаем.
    - Если всё равно не удаётся — отправляем НОВОЕ сообщение (фоллбэк).
    """
    try:
        msg = getattr(q, "message", None)
        if msg:
            key = (msg.chat_id, msg.message_id)
            markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
            new_sig = (text or "", markup_json)
            if LAST_SIG.get(key) == new_sig:
                log.debug("TG: nothing to modify; pass (anti-dup)")
                return

        log.debug("TG: edit_message_text try")
        await q.edit_message_text(text=text, reply_markup=reply_markup)
        log.debug("TG: edit_message_text OK")

        if msg:
            LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", markup_json)
        return
    except RetryAfter as e:
        delay = getattr(e, "retry_after", 2) + 1
        log.warning("TG: edit_message_text flood, sleep=%s", delay)
        await asyncio.sleep(delay)
        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            log.debug("TG: edit_message_text retry OK")
            msg = getattr(q, "message", None)
            if msg:
                markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
                LAST_SIG[(msg.chat_id, msg.message_id)] = (text or "", markup_json)
            return
        except Exception as e2:
            log.error("TG: edit_message_text retry failed: %s", e2)
            await _send_new_message_fallback(q, text, reply_markup)
            return
    except BadRequest as e:
        s = str(e)
        if "Message is not modified" in s:
            try:
                log.debug("TG: edit_message_reply_markup only")
                await q.edit_message_reply_markup(reply_markup=reply_markup)
                log.debug("TG: edit_message_reply_markup OK")
                msg = getattr(q, "message", None)
                if msg:
                    markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
                    LAST_SIG[(msg.chat_id, msg.message_id)] = ((msg.text or ""), markup_json)
                return
            except RetryAfter as e2:
                delay = getattr(e2, "retry_after", 2) + 1
                log.warning("TG: edit_message_reply_markup flood, sleep=%s", delay)
                await asyncio.sleep(delay)
                try:
                    await q.edit_message_reply_markup(reply_markup=reply_markup)
                    log.debug("TG: edit_message_reply_markup retry OK")
                    msg = getattr(q, "message", None)
                    if msg:
                        markup_json = json.dumps(reply_markup.to_dict() if reply_markup else {}, ensure_ascii=False, sort_keys=True)
                        LAST_SIG[(msg.chat_id, msg.message_id)] = ((msg.text or ""), markup_json)
                    return
                except Exception as e3:
                    log.error("TG: edit_message_reply_markup retry failed: %s", e3)
                    await _send_new_message_fallback(q, text, reply_markup)
                    return
            except BadRequest as e2:
                if "Message is not modified" in str(e2):
                    log.debug("TG: nothing to modify; pass (branch)")
                    return
                log.error("TG: edit_message_reply_markup bad request: %s", e2)
                await _send_new_message_fallback(q, text, reply_markup)
                return
        log.warning("TG: edit_message_text bad request -> fallback, err=%s", e)
        await _send_new_message_fallback(q, text, reply_markup)
        return
    except Exception as e:
        log.error("TG: edit_message_text unknown error -> fallback: %s", e)
        await _send_new_message_fallback(q, text, reply_markup)
        return

# -----------------------------
# Публичный entry-point для бота
# -----------------------------
@_trace_async
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть/обновить экран планировщика."""
    uid = update.effective_user.id
    log.info("Planner: open for uid=%s (cb=%s)", uid, bool(update.callback_query))
    kb = await _kb_main(uid)
    text = "🗓 ПЛАН НА ДЕНЬ\nВыбирай задачу или добавь новую."
    if update.callback_query:
        await edit_or_pass(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=kb)
    log.debug("Planner: open done for uid=%s", uid)

# --------------------------------------
# Внутренний роутер callback-кнопок (group=0)
# --------------------------------------
@_trace_async
async def _cb_plan_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = (q.data or "").strip()
    log.info("CB router: uid=%s data=%r", uid, data)

    await _safe_q_answer(q)

    if data in ("PLAN_OPEN", "PLAN_LIST", "show_day_plan"):
        log.debug("CB: open list")
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data == "PLAN_ADD_EMPTY":
        log.debug("CB: add empty")
        it = await _insert_item(uid, "")
        set_state_for_update(update, {"mode": "edit_text", "item_id": it.item_id})
        await edit_or_pass(
            q,
            f"✏️ Введи текст для задачи #{it.item_id}",
            _kb_cancel_to_list()
        )
        return

    if data.startswith("ITEM_MENU:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            log.warning("CB: ITEM_MENU parse error: %r", data)
            await q.answer("Некорректный ID")
            return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Задача не найдена")
            return
        log.debug("CB: open item menu iid=%s", iid)
        await edit_or_pass(q, f"📝 Задача #{it.item_id}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("DEL_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID")
            return
        await _delete_item(uid, iid)
        await q.answer("Удалено.")
        log.info("CB: deleted iid=%s", iid)
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data.startswith("TOGGLE_DONE:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID")
            return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи")
            return
        await _update_done(uid, iid, not it.done)
        it = await _get_item(uid, iid) # Обновляем объект, т.к. состояние поменялось
        log.info("CB: toggle done iid=%s -> %s", iid, it.done if it else None)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("EDIT_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID")
            return
        set_state_for_update(update, {"mode": "edit_text", "item_id": iid})
        await edit_or_pass(
            q,
            f"✏️ Введи новый текст для задачи #{iid}",
            _kb_cancel_to_list()
        )
        return

    if data.startswith("EDIT_TIME:"):
        try:
            iid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await q.answer("Некорректный ID")
            return
        set_state_for_update(update, {"mode": "edit_time", "item_id": iid})
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
            _kb_cancel_to_list()
        )
        return

    if data.startswith("PLAN_"):
        log.debug("CB: fallback open planner for %r", data)
        await open_planner(update, context)

# --------------------------------------
# Текстовые сообщения (ввод для режимов)
# --------------------------------------
@_trace_async
async def _msg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    st = get_state_for_update(update)
    log.debug("MSG router: uid=%s has_state=%s text=%r", uid, bool(st), _short(txt))

    if not st:
        log.debug("MSG: skip (no pending state) uid=%s", uid)
        return

    mode = st.get("mode")
    iid = int(st.get("item_id", 0))
    
    # Защита от некорректного iid
    if iid == 0:
        log.warning("MSG router: invalid item_id in state, clearing state for uid=%s", uid)
        clear_state_for_update(update)
        await update.message.reply_text("Что-то пошло не так. Пожалуйста, попробуй ещё раз.")
        return

    log.info("MSG: uid=%s mode=%s text=%r", uid, mode, _short(txt, 200))

    if mode == "edit_text":
        await _update_text(uid, iid, txt)
        it = await _get_item(uid, iid)
        
        # Всегда очищаем состояние ввода текста
        clear_state_for_update(update)
        
        if it and not it.when_hhmm:
            # Если время не установлено, переходим в режим ввода времени
            set_state_for_update(update, {"mode": "edit_time", "item_id": iid})
            await update.message.reply_text(
                f"✏️ Текст обновлён.\n⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
                reply_markup=_kb_cancel_to_list()
            )
        else:
            # Если время уже было установлено, просто возвращаемся в планировщик
            await update.message.reply_text("✅ Текст обновлён.")
            await open_planner(update, context)
        return

    if mode == "edit_time":
        t = _parse_time(txt)
        if not t:
            await update.message.reply_text("⏰ Формат HH:MM. Можно также 930 или 0930. Попробуй ещё раз.")
            return
        
        await _update_time(uid, iid, t)
        await update.message.reply_text(f"✅ Время установлено: {t}")
        clear_state_for_update(update)

        nxt = await _find_next_item(uid, iid)
        if nxt and not nxt.when_hhmm:
            set_state_for_update(update, {"mode": "edit_time", "item_id": nxt.item_id})
            await update.message.reply_text(
                f"➡️ Следующая: #{nxt.item_id}\n⏰ Введи время в формате HH:MM (по Киеву)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")]])
            )
            return

        await open_planner(update, context)
        return

    log.debug("MSG: unknown state -> clearing")
    clear_state_for_update(update)
    await open_planner(update, context)

# ==== Экспорт для twitter_bot.py ====
@_trace_async
async def planner_add_from_text(uid: int, text: str, chat_id: int = None, bot = None) -> int:
    """Создаёт новую задачу с текстом и возвращает item_id. Если передан chat_id и bot, сразу запрашивает время."""
    it = await _insert_item(uid, text or "")
    log.info("API: planner_add_from_text uid=%s -> iid=%s", uid, it.item_id)
    
    # Если переданы chat_id и bot, сразу запрашиваем время
    if chat_id is not None and bot is not None:
        set_state_for_ids(chat_id, uid, {"mode": "edit_time", "item_id": it.item_id})
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            reply_markup=kb
        )
        log.info("API: immediately prompted for time uid=%s iid=%s", uid, it.item_id)
    
    return it.item_id

@_trace_async
async def planner_prompt_time(uid: int, chat_id: int, bot) -> None:
    """Спрашивает у пользователя время для задачи последней/созданной записи.
       user_id нужен для STATE; chat_id — куда слать сообщение."""
    items = await _get_items(uid)
    if not items:
        log.warning("API: planner_prompt_time — no items for uid=%s", uid)
        return
    iid = items[-1].item_id
    set_state_for_ids(chat_id, uid, {"mode": "edit_time", "item_id": iid})
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
    await bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
        reply_markup=kb
    )
    log.info("API: planner_prompt_time uid=%s iid=%s (prompt sent)", uid, iid)

# --------------------------------------
# Регистрация хендлеров в PTB (group=0)
# --------------------------------------
@_trace_sync
def register_planner_handlers(app: Application) -> None:
    """
    Регистрируем РАНЬШЕ основного бота (group=0), чтобы планировщик
    забирал только свои колбэки. BACK_MAIN_MENU не ловим, т.к. это возврат в основной бот.

    Текстовый хендлер обрабатывает сообщения ТОЛЬКО,
    когда у пользователя есть ожидаемый ввод (STATE).
    """
    log.info("Planner: registering handlers (group=0)")
    app.add_handler(
        CallbackQueryHandler(
            _cb_plan_router,
            pattern=r"^(PLAN_|ITEM_MENU:|DEL_ITEM:|EDIT_TIME:|EDIT_ITEM:|TOGGLE_DONE:|show_day_plan$)"
        ),
        group=0
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_router),
        group=0
    )
    log.info("Planner: handlers registered")
    