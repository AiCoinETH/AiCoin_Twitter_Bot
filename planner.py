# -*- coding: utf-8 -*-
"""
Планировщик с персистентностью в SQLite для twitter_bot.py.

Совместим с ожиданиями бота:
  PLAN_* , ITEM_MENU:, DEL_ITEM:, EDIT_TIME:, EDIT_ITEM:, EDIT_FIELD: (резерв),
  AI_FILL_TEXT:, CLONE_ITEM:, AI_NEW_FROM:, а также PLAN_DONE / GEN_DONE / BACK_MAIN_MENU.

Хранение:
  - Таблица plan_items(user_id, item_id, text, when_hhmm, done, created_at)
  - item_id — локальная последовательность на пользователя (1,2,3,...) — сохраняется
"""

from __future__ import annotations
import re
import asyncio
import logging
import aiosqlite
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Any
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

# ------------------
# Логи / Константы / глобалы
# ------------------
log = logging.getLogger("planner")
# Не переопределяю root-конфиг, но сделаю локальный уровень повышенным:
if log.level == logging.NOTSET:
    log.setLevel(logging.DEBUG)

TZ = ZoneInfo("Europe/Kyiv")
DB_FILE = "planner.db"

USER_STATE: Dict[int, dict] = {}   # ожидания ввода (правка текста/времени/новая тема); ключ: user_id
_ai_generator: Optional[Callable[[str], "asyncio.Future"]] = None
_db_ready = False  # ленивый init


# ------------
# Утилиты логирования
# ------------
def _short(val: Any, n: int = 120) -> str:
    s = str(val)
    return s if len(s) <= n else s[:n] + "…"

def _fmt_arg(v: Any) -> str:
    try:
        from telegram import Update, Bot
        from telegram.ext import CallbackContext
        if isinstance(v, Update):
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
        try:
            log.debug("→ %s(%s%s)", fn.__name__,
                      ", ".join(_fmt_arg(a) for a in args),
                      (", " + ", ".join(f"{k}={_fmt_arg(v)}" for k, v in kwargs.items())) if kwargs else "")
            res = fn(*args, **kwargs)
            log.debug("← %s = %s", fn.__name__, _fmt_arg(res))
            return res
        except Exception:
            log.exception("✖ %s failed", fn.__name__)
            raise
    return wrap

def _trace_async(fn):
    @wraps(fn)
    async def wrap(*args, **kwargs):
        try:
            log.debug("→ %s(%s%s)", fn.__name__,
                      ", ".join(_fmt_arg(a) for a in args),
                      (", " + ", ".join(f"{k}={_fmt_arg(v)}" for k, v in kwargs.items())) if kwargs else "")
            res = await fn(*args, **kwargs)
            log.debug("← %s = %s", fn.__name__, _fmt_arg(res))
            return res
        except Exception:
            log.exception("✖ %s failed", fn.__name__)
            raise
    return wrap


def set_ai_generator(fn: Callable[[str], "asyncio.Future"]) -> None:
    """Бот отдаёт сюда свой AI-генератор (async)."""
    global _ai_generator
    _ai_generator = fn
    log.info("AI generator set: %s", bool(fn))


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
async def _clone_item(uid: int, src: PlanItem) -> PlanItem:
    log.info("Clone request uid=%s src_iid=%s", uid, src.item_id)
    return await _insert_item(uid, text=src.text, when_hhmm=src.when_hhmm)

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
        [InlineKeyboardButton("➕ Новая (пустая)", callback_data="PLAN_ADD_EMPTY"),
         InlineKeyboardButton("✨ Новая от ИИ", callback_data="PLAN_ADD_AI")],
        [InlineKeyboardButton("↩️ Назад", callback_data="BACK_MAIN_MENU"),
         InlineKeyboardButton("✅ Готово", callback_data="PLAN_DONE")],
    ]
    kb = InlineKeyboardMarkup(rows)
    log.debug("Main keyboard built: rows=%d", len(rows))
    return kb

@_trace_sync
def _kb_item(it: PlanItem) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Текст", callback_data=f"EDIT_ITEM:{it.item_id}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{it.item_id}")],
        [InlineKeyboardButton("🤖 ИИ-текст", callback_data=f"AI_FILL_TEXT:{it.item_id}"),
         InlineKeyboardButton("🧬 Клонировать", callback_data=f"CLONE_ITEM:{it.item_id}")],
        [InlineKeyboardButton("✅/🟡 Переключить статус", callback_data=f"TOGGLE_DONE:{it.item_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{it.item_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")],
    ]
    kb = InlineKeyboardMarkup(rows)
    log.debug("Item keyboard built for iid=%s", it.item_id)
    return kb

@_trace_sync
def _kb_gen_topic() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")]])
    log.debug("Topic keyboard built")
    return kb


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
    - Если всё равно не удаётся (или BadRequest иное) — отправляем НОВОЕ сообщение (фоллбэк).
    """
    try:
        log.debug("TG: edit_message_text try")
        await q.edit_message_text(text=text, reply_markup=reply_markup)
        log.debug("TG: edit_message_text OK")
        return
    except RetryAfter as e:
        delay = getattr(e, "retry_after", 2) + 1
        log.warning("TG: edit_message_text flood, sleep=%s", delay)
        await asyncio.sleep(delay)
        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            log.debug("TG: edit_message_text retry OK")
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
                return
            except RetryAfter as e2:
                delay = getattr(e2, "retry_after", 2) + 1
                log.warning("TG: edit_message_reply_markup flood, sleep=%s", delay)
                await asyncio.sleep(delay)
                try:
                    await q.edit_message_reply_markup(reply_markup=reply_markup)
                    log.debug("TG: edit_message_reply_markup retry OK")
                    return
                except Exception as e3:
                    log.error("TG: edit_message_reply_markup retry failed: %s", e3)
                    await _send_new_message_fallback(q, text, reply_markup)
                    return
            except BadRequest as e2:
                if "Message is not modified" in str(e2):
                    log.debug("TG: nothing to modify; pass")
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
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    if data == "PLAN_ADD_AI":
        log.debug("CB: add via AI (request topic)")
        USER_STATE[uid] = {"mode": "waiting_new_topic"}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await edit_or_pass(
            q,
            "🧠 Введи тему/подсказку для новой задачи — сгенерирую текст.\n"
            "Примеры: «анонс AMA», «продвижение сайта», «итоги недели».",
            _kb_gen_topic()
        )
        return

    if data.startswith("ITEM_MENU:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
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
        except Exception:
            await q.answer("Некорректный ID")
            return
        await _delete_item(uid, iid)
        await q.answer("Удалено.")
        log.info("CB: deleted iid=%s", iid)
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data.startswith("CLONE_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        src = await _get_item(uid, iid)
        if not src:
            await q.answer("Нет такой задачи")
            return
        await _clone_item(uid, src)
        await q.answer("Склонировано.")
        log.info("CB: cloned iid=%s", iid)
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    if data.startswith("TOGGLE_DONE:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи")
            return
        await _update_done(uid, iid, not it.done)
        it = await _get_item(uid, iid)
        log.info("CB: toggle done iid=%s -> %s", iid, it.done if it else None)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("EDIT_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        USER_STATE[uid] = {"mode": "edit_text", "item_id": iid}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await edit_or_pass(
            q,
            f"✏️ Введи новый текст для задачи #{iid}",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    if data.startswith("EDIT_TIME:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    if data.startswith("AI_FILL_TEXT:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи")
            return
        hint = it.text or "daily task for Ai Coin"
        log.debug("AI_FILL_TEXT for iid=%s hint=%r", iid, _short(hint))
        if _ai_generator:
            try:
                txt, tags, img = await _ai_generator(hint)
                txt = (txt or "").strip()
                if txt:
                    await _update_text(uid, iid, txt)
                await q.answer("Текст обновлён ИИ.")
            except Exception as e:
                log.exception("AI: generation error")
                await q.answer("Ошибка генерации")
        else:
            log.warning("AI: generator not set")
            await q.answer("ИИ-генератор не подключен")
        it = await _get_item(uid, iid)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    if data.startswith("AI_NEW_FROM:"):
        topic = data.split(":", 1)[1].strip() or "general"
        log.info("AI: new from topic=%r", topic)
        it = await _insert_item(uid, f"(генерация: {topic})")
        if _ai_generator:
            try:
                txt, tags, img = await _ai_generator(topic)
                if txt:
                    await _update_text(uid, it.item_id, (txt or "").strip())
            except Exception:
                log.exception("AI: generation error on create")
        await q.answer("Создано. Укажи время.")
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
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
    st = USER_STATE.get(uid)
    log.debug("MSG router: uid=%s has_state=%s text=%r", uid, bool(st), _short(txt))

    if not st:
        log.debug("MSG: skip (no pending state) uid=%s", uid)
        return

    mode = st.get("mode")
    log.info("MSG: uid=%s mode=%s text=%r", uid, mode, _short(txt, 200))

    if mode == "edit_text":
        iid = int(st.get("item_id"))
        await _update_text(uid, iid, txt)
        it = await _get_item(uid, iid)
        if it and not it.when_hhmm:
            USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
            log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
            await update.message.reply_text(
                f"✏️ Текст обновлён.\n⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
            )
            return
        await update.message.reply_text("✅ Текст обновлён.")
        USER_STATE.pop(uid, None)
        log.debug("State cleared for uid=%s", uid)
        await open_planner(update, context)
        return

    if mode == "edit_time":
        iid = int(st.get("item_id"))
        t = _parse_time(txt)
        if not t:
            await update.message.reply_text("⏰ Формат HH:MM. Можно также 930 или 0930. Попробуй ещё раз.")
            return
        await _update_time(uid, iid, t)
        await update.message.reply_text(f"✅ Время установлено: {t}")
        USER_STATE.pop(uid, None)
        log.debug("State cleared for uid=%s", uid)

        nxt = await _find_next_item(uid, iid)
        if nxt:
            if not nxt.when_hhmm:
                USER_STATE[uid] = {"mode": "edit_time", "item_id": nxt.item_id}
                log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
                await update.message.reply_text(
                    f"➡️ Следующая: #{nxt.item_id}\n⏰ Введи время в формате HH:MM (по Киеву)",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")]])
                )
                return
            else:
                await update.message.reply_text(
                    f"➡️ Следующая задача #{nxt.item_id}\n{_fmt_item(nxt)}",
                    reply_markup=_kb_item(nxt)
                )
                return

        await open_planner(update, context)
        return

    if mode == "waiting_new_topic":
        topic = txt or "general"
        log.info("AI: create new from topic via message: %r", topic)
        it = await _insert_item(uid, f"(генерация: {topic})")
        if _ai_generator:
            try:
                gen_text, tags, img = await _ai_generator(topic)
                if gen_text:
                    await _update_text(uid, it.item_id, gen_text)
                await update.message.reply_text("✨ Создано с помощью ИИ.")
            except Exception:
                log.exception("AI: generation error on message")
                await update.message.reply_text("⚠️ Не удалось сгенерировать, создана пустая задача.")
        else:
            await update.message.reply_text("Создана пустая задача (ИИ недоступен).")

        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        log.debug("State set: uid=%s -> %s", uid, USER_STATE[uid])
        await update.message.reply_text(
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    log.debug("MSG: unknown state -> clearing")
    USER_STATE.pop(uid, None)
    await open_planner(update, context)


# ==== Экспорт для twitter_bot.py ====
@_trace_async
async def planner_add_from_text(uid: int, text: str) -> int:
    """Создаёт новую задачу с текстом и возвращает item_id."""
    it = await _insert_item(uid, text or "")
    log.info("API: planner_add_from_text uid=%s -> iid=%s", uid, it.item_id)
    return it.item_id

@_trace_async
async def planner_prompt_time(uid: int, chat_id: int, bot) -> None:
    """Спрашивает у пользователя время для задачи последней/созданной записи.
       user_id нужен для USER_STATE; chat_id — куда слать сообщение."""
    items = await _get_items(uid)
    if not items:
        log.warning("API: planner_prompt_time — no items for uid=%s", uid)
        return
    iid = items[-1].item_id
    USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
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
    забирал только свои колбэки. BACK_MAIN_MENU/PLAN_DONE/GEN_DONE не ловим.

    ВАЖНО: текстовый хендлер теперь обрабатывает сообщения ТОЛЬКО,
    когда у пользователя есть ожидаемый ввод (USER_STATE).
    """
    log.info("Planner: registering handlers (group=0)")
    app.add_handler(
        CallbackQueryHandler(
            _cb_plan_router,
            pattern=r"^(PLAN_(?!DONE$).+|ITEM_MENU:.*|DEL_ITEM:.*|EDIT_TIME:.*|EDIT_ITEM:.*|EDIT_FIELD:.*|AI_FILL_TEXT:.*|CLONE_ITEM:.*|AI_NEW_FROM:.*|TOGGLE_DONE:.*|show_day_plan)$"
        ),
        group=0
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_router),
        group=0
    )
    log.info("Planner: handlers registered")