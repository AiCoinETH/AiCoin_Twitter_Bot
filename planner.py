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
from typing import Callable, Dict, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Europe/Kyiv")
DB_FILE = "planner.db"

USER_STATE: Dict[int, dict] = {}   # ожидания ввода (правка текста/времени/новая тема); ключ: user_id
_ai_generator: Optional[Callable[[str], "asyncio.Future"]] = None
_db_ready = False  # ленивый init


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

async def _ensure_db() -> None:
    global _db_ready
    if _db_ready:
        return
    log.info("DB init start: %s", DB_FILE)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_SQL)
        await db.commit()
    _db_ready = True
    log.info("DB init complete")

async def _get_items(uid: int) -> List[PlanItem]:
    await _ensure_db()
    log.debug("DB: get items for uid=%s", uid)
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? ORDER BY item_id ASC",
            (uid,)
        )
        rows = await cur.fetchall()
    items = [
        PlanItem(user_id=r["user_id"], item_id=r["item_id"], text=r["text"],
                 when_hhmm=r["when_hhmm"], done=bool(r["done"]))
        for r in rows
    ]
    log.debug("DB: get items -> %d rows", len(items))
    return items

async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    log.debug("DB: get next item_id for uid=%s", uid)
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?", (uid,))
        (mx,) = await cur.fetchone()
    nxt = int(mx) + 1
    log.debug("DB: next item_id=%s", nxt)
    return nxt

async def _insert_item(uid: int, text: str = "", when_hhmm: Optional[str] = None) -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    log.info("DB: insert item uid=%s iid=%s time=%s", uid, iid, when_hhmm)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO plan_items(user_id, item_id, text, when_hhmm, done, created_at) VALUES (?,?,?,?,?,?)",
            (uid, iid, text or "", when_hhmm, 0, now)
        )
        await db.commit()
    return PlanItem(uid, iid, text or "", when_hhmm, False)

async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    log.info("DB: update text uid=%s iid=%s len=%s", uid, iid, len(text or ""))
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?",
            (text or "", uid, iid)
        )
        await db.commit()

async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    log.info("DB: update time uid=%s iid=%s time=%s", uid, iid, when_hhmm)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?",
            (when_hhmm, uid, iid)
        )
        await db.commit()

async def _update_done(uid: int, iid: int, done: bool) -> None:
    await _ensure_db()
    log.info("DB: update done uid=%s iid=%s -> %s", uid, iid, done)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET done=? WHERE user_id=? AND item_id=?",
            (1 if done else 0, uid, iid)
        )
        await db.commit()

async def _delete_item(uid: int, iid: int) -> None:
    await _ensure_db()
    log.info("DB: delete item uid=%s iid=%s", uid, iid)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM plan_items WHERE user_id=? AND item_id=?", (uid, iid))
        await db.commit()

async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    log.debug("DB: get item uid=%s iid=%s", uid, iid)
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? AND item_id=?",
            (uid, iid)
        )
        row = await cur.fetchone()
    if not row:
        log.debug("DB: get item -> None")
        return None
    item = PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]))
    log.debug("DB: get item -> %s", item)
    return item

async def _clone_item(uid: int, src: PlanItem) -> PlanItem:
    log.info("DB: clone item uid=%s src_iid=%s", uid, src.item_id)
    return await _insert_item(uid, text=src.text, when_hhmm=src.when_hhmm)

async def _find_next_item(uid: int, after_iid: int) -> Optional[PlanItem]:
    """Найти следующую задачу по item_id."""
    await _ensure_db()
    log.debug("DB: find next item uid=%s after_iid=%s", uid, after_iid)
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items "
            "WHERE user_id=? AND item_id>? ORDER BY item_id ASC LIMIT 1",
            (uid, after_iid)
        )
        row = await cur.fetchone()
    if not row:
        log.debug("DB: find next item -> None")
        return None
    nxt = PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]))
    log.debug("DB: find next item -> iid=%s", nxt.item_id)
    return nxt


# -------------------------
# Рендеринг и клавиатуры UI
# -------------------------
def _fmt_item(i: PlanItem) -> str:
    t = f"[{i.when_hhmm}]" if i.when_hhmm else "[—]"
    d = "✅" if i.done else "🟡"
    txt = (i.text or "").strip() or "(пусто)"
    return f"{d} {t} {txt}"

async def _kb_main(uid: int) -> InlineKeyboardMarkup:
    items = await _get_items(uid)
    log.debug("UI: build main keyboard for uid=%s, items=%d", uid, len(items))
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        rows.append([InlineKeyboardButton(_fmt_item(it), callback_data=f"ITEM_MENU:{it.item_id}")])
    rows += [
        [InlineKeyboardButton("➕ Новая (пустая)", callback_data="PLAN_ADD_EMPTY"),
         InlineKeyboardButton("✨ Новая от ИИ", callback_data="PLAN_ADD_AI")],
        [InlineKeyboardButton("↩️ Назад", callback_data="BACK_MAIN_MENU"),
         InlineKeyboardButton("✅ Готово", callback_data="PLAN_DONE")],
    ]
    return InlineKeyboardMarkup(rows)

def _kb_item(it: PlanItem) -> InlineKeyboardMarkup:
    log.debug("UI: build item keyboard iid=%s", it.item_id)
    rows = [
        [InlineKeyboardButton("✏️ Текст", callback_data=f"EDIT_ITEM:{it.item_id}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{it.item_id}")],
        [InlineKeyboardButton("🤖 ИИ-текст", callback_data=f"AI_FILL_TEXT:{it.item_id}"),
         InlineKeyboardButton("🧬 Клонировать", callback_data=f"CLONE_ITEM:{it.item_id}")],
        [InlineKeyboardButton("✅/🟡 Переключить статус", callback_data=f"TOGGLE_DONE:{it.item_id}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{it.item_id}")],
        [InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")],
    ]
    return InlineKeyboardMarkup(rows)

def _kb_gen_topic() -> InlineKeyboardMarkup:
    log.debug("UI: build topic keyboard")
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")]])


# ---------------
# Парсеры/хелперы
# ---------------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):?([0-5]\d)$")  # допускаем '930' -> '09:30'

def _parse_time(s: str) -> Optional[str]:
    s = (s or "").strip().replace(" ", "")
    m = _TIME_RE.match(s)
    if not m:
        log.debug("Time parse failed: %r", s)
        return None
    hh, mm = m.groups()
    res = f"{int(hh):02d}:{int(mm):02d}"
    log.debug("Time parsed: %r -> %s", s, res)
    return res


# ---------------
# Безопасные действия TG
# ---------------
async def _safe_q_answer(q) -> bool:
    try:
        await q.answer()
        return True
    except BadRequest as e:
        # Частый кейс в логах: "callback query is too old"
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
            return True
        except Exception as e2:
            log.error("TG: answerCallbackQuery retry failed: %s", e2)
            return False
    except Exception as e:
        log.error("TG: answerCallbackQuery unknown error: %s", e)
        return False

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
        except Exception as e2:
            log.error("TG: fallback send retry failed: %s", e2)
    except Exception as e:
        log.error("TG: fallback send error: %s", e)

async def edit_or_pass(q, text: str, reply_markup: InlineKeyboardMarkup):
    """
    Безопасно редактируем сообщение.
    - Если «Message is not modified» — пробуем изменить только разметку.
    - Если флад-контроль — ждём и пробуем ещё раз.
    - Если всё равно не удаётся (или BadRequest иное) — отправляем НОВОЕ сообщение (фоллбэк).
    """
    try:
        log.debug("TG: edit_message_text")
        await q.edit_message_text(text=text, reply_markup=reply_markup)
        return
    except RetryAfter as e:
        delay = getattr(e, "retry_after", 2) + 1
        log.warning("TG: edit_message_text flood, sleep=%s", delay)
        await asyncio.sleep(delay)
        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            return
        except Exception as e2:
            log.error("TG: edit_message_text retry failed: %s", e2)
            # фоллбэк — новое сообщение
            await _send_new_message_fallback(q, text, reply_markup)
            return
    except BadRequest as e:
        s = str(e)
        if "Message is not modified" in s:
            # Пробуем поменять только клавиатуру
            try:
                log.debug("TG: edit_message_reply_markup only")
                await q.edit_message_reply_markup(reply_markup=reply_markup)
                return
            except RetryAfter as e2:
                delay = getattr(e2, "retry_after", 2) + 1
                log.warning("TG: edit_message_reply_markup flood, sleep=%s", delay)
                await asyncio.sleep(delay)
                try:
                    await q.edit_message_reply_markup(reply_markup=reply_markup)
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
        # Любая другая ошибка — шлём новое сообщение, чтобы не застревать
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
async def _cb_plan_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = (q.data or "").strip()
    log.info("CB router: uid=%s data=%r", uid, data)

    await _safe_q_answer(q)

    # показать список
    if data in ("PLAN_OPEN", "PLAN_LIST", "show_day_plan"):
        log.debug("CB: open list")
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    # добавление пустой — сразу спросить время
    if data == "PLAN_ADD_EMPTY":
        log.debug("CB: add empty")
        it = await _insert_item(uid, "")
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # запрос генерации новой темы от ИИ (сначала тема, потом время)
    if data == "PLAN_ADD_AI":
        log.debug("CB: add via AI (request topic)")
        USER_STATE[uid] = {"mode": "waiting_new_topic"}
        await edit_or_pass(
            q,
            "🧠 Введи тему/подсказку для новой задачи — сгенерирую текст.\n"
            "Примеры: «анонс AMA», «продвижение сайта», «итоги недели».",
            _kb_gen_topic()
        )
        return

    # меню айтема
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

    # удалить
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

    # клон
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

    # переключить статус done
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

    # правка текста
    if data.startswith("EDIT_ITEM:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        USER_STATE[uid] = {"mode": "edit_text", "item_id": iid}
        log.debug("CB: edit text iid=%s", iid)
        await edit_or_pass(
            q,
            f"✏️ Введи новый текст для задачи #{iid}",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # правка времени
    if data.startswith("EDIT_TIME:"):
        try:
            iid = int(data.split(":", 1)[1])
        except Exception:
            await q.answer("Некорректный ID")
            return
        USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
        log.debug("CB: edit time iid=%s", iid)
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # автозаполнение текста ИИ
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
        if _ai_generator:
            try:
                log.info("AI: fill text for iid=%s hint=%r", iid, hint[:80])
                txt, tags, img = await _ai_generator(hint)
                txt = (txt or "").strip()
                if txt:
                    await _update_text(uid, iid, txt)
                await q.answer("Текст обновлён ИИ.")
            except Exception as e:
                log.error("AI: generation error: %s", e)
                await q.answer("Ошибка генерации")
        else:
            log.warning("AI: generator not set")
            await q.answer("ИИ-генератор не подключен")
        it = await _get_item(uid, iid)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    # создание новой задачи сразу от ИИ
    if data.startswith("AI_NEW_FROM:"):
        topic = data.split(":", 1)[1].strip() or "general"
        log.info("AI: new from topic=%r", topic)
        it = await _insert_item(uid, f"(генерация: {topic})")
        if _ai_generator:
            try:
                txt, tags, img = await _ai_generator(topic)
                if txt:
                    await _update_text(uid, it.item_id, (txt or "").strip())
            except Exception as e:
                log.error("AI: generation error on create: %s", e)
        await q.answer("Создано. Укажи время.")
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # PLAN_DONE / GEN_DONE / BACK_MAIN_MENU — не обрабатываем (отдаст основной бот)

    # fallback: любые остальные PLAN_*
    if data.startswith("PLAN_"):
        log.debug("CB: fallback open planner for %r", data)
        await open_planner(update, context)


# --------------------------------------
# Текстовые сообщения (ввод для режимов)
# --------------------------------------
async def _msg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    st = USER_STATE.get(uid)

    # Если не ждём ввода — НЕ перехватываем сообщение (пусть обработает основной бот).
    if not st:
        log.debug("MSG: skip (no pending state) uid=%s text=%r", uid, txt[:80])
        return

    mode = st.get("mode")
    log.info("MSG: uid=%s mode=%s text=%r", uid, mode, txt[:120])

    if mode == "edit_text":
        iid = int(st.get("item_id"))
        await _update_text(uid, iid, txt)
        it = await _get_item(uid, iid)
        # Если у задачи не задано время — сразу спрашиваем
        if it and not it.when_hhmm:
            USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
            await update.message.reply_text(
                f"✏️ Текст обновлён.\n⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
            )
            return
        # иначе — в список
        await update.message.reply_text("✅ Текст обновлён.")
        USER_STATE.pop(uid, None)
        await open_planner(update, context)
        return

    if mode == "edit_time":
        iid = int(st.get("item_id"))
        t = _parse_time(txt)
        if not t:
            await update.message.reply_text("⏰ Формат HH:MM. Попробуй ещё раз.")
            return
        await _update_time(uid, iid, t)
        await update.message.reply_text(f"✅ Время установлено: {t}")
        USER_STATE.pop(uid, None)

        # Переходим к следующей задаче, если есть
        nxt = await _find_next_item(uid, iid)
        if nxt:
            if not nxt.when_hhmm:
                USER_STATE[uid] = {"mode": "edit_time", "item_id": nxt.item_id}
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

        # Если следующей нет — вернёмся к списку
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
            except Exception as e:
                log.error("AI: generation error on message: %s", e)
                await update.message.reply_text("⚠️ Не удалось сгенерировать, создана пустая задача.")
        else:
            await update.message.reply_text("Создана пустая задача (ИИ недоступен).")

        # 👉 сразу спрашиваем время
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        await update.message.reply_text(
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # на всякий
    log.debug("MSG: unknown state, clearing")
    USER_STATE.pop(uid, None)
    await open_planner(update, context)


# ==== Экспорт для twitter_bot.py ====
async def planner_add_from_text(uid: int, text: str) -> int:
    """Создаёт новую задачу с текстом и возвращает item_id."""
    it = await _insert_item(uid, text or "")
    log.info("API: planner_add_from_text uid=%s -> iid=%s", uid, it.item_id)
    return it.item_id

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
    log.info("API: planner_prompt_time uid=%s iid=%s", uid, iid)


# --------------------------------------
# Регистрация хендлеров в PTB (group=0)
# --------------------------------------
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
    # Текст: оставляем общий фильтр, но в _msg_router пропускаем всё,
    # если не ждём ввода (см. USER_STATE).
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _msg_router),
        group=0
    )
    log.info("Planner: handlers registered")