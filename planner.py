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
from telegram.error import BadRequest

# ------------------
# Константы / глобалы
# ------------------
TZ = ZoneInfo("Europe/Kyiv")
DB_FILE = "planner.db"

USER_STATE: Dict[int, dict] = {}   # ожидания ввода (правка текста/времени/новая тема); ключ: user_id
_ai_generator: Optional[Callable[[str], "asyncio.Future"]] = None
_db_ready = False  # ленивый init

def set_ai_generator(fn: Callable[[str], "asyncio.Future"]) -> None:
    """Бот отдаёт сюда свой AI-генератор (async)."""
    global _ai_generator
    _ai_generator = fn

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
    if _db_ready: return
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(CREATE_SQL)
        await db.commit()
    _db_ready = True

async def _get_items(uid: int) -> List[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? ORDER BY item_id ASC",
            (uid,)
        )
        rows = await cur.fetchall()
    return [
        PlanItem(user_id=r["user_id"], item_id=r["item_id"], text=r["text"],
                 when_hhmm=r["when_hhmm"], done=bool(r["done"]))
        for r in rows
    ]

async def _next_item_id(uid: int) -> int:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COALESCE(MAX(item_id),0) FROM plan_items WHERE user_id=?", (uid,))
        (mx,) = await cur.fetchone()
    return int(mx) + 1

async def _insert_item(uid: int, text: str = "", when_hhmm: Optional[str] = None) -> PlanItem:
    iid = await _next_item_id(uid)
    now = datetime.now(TZ).isoformat()
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO plan_items(user_id, item_id, text, when_hhmm, done, created_at) VALUES (?,?,?,?,?,?)",
            (uid, iid, text or "", when_hhmm, 0, now)
        )
        await db.commit()
    return PlanItem(uid, iid, text or "", when_hhmm, False)

async def _update_text(uid: int, iid: int, text: str) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET text=? WHERE user_id=? AND item_id=?",
            (text or "", uid, iid)
        )
        await db.commit()

async def _update_time(uid: int, iid: int, when_hhmm: Optional[str]) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET when_hhmm=? WHERE user_id=? AND item_id=?",
            (when_hhmm, uid, iid)
        )
        await db.commit()

async def _update_done(uid: int, iid: int, done: bool) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE plan_items SET done=? WHERE user_id=? AND item_id=?",
            (1 if done else 0, uid, iid)
        )
        await db.commit()

async def _delete_item(uid: int, iid: int) -> None:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM plan_items WHERE user_id=? AND item_id=?", (uid, iid))
        await db.commit()

async def _get_item(uid: int, iid: int) -> Optional[PlanItem]:
    await _ensure_db()
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, item_id, text, when_hhmm, done FROM plan_items WHERE user_id=? AND item_id=?",
            (uid, iid)
        )
        row = await cur.fetchone()
    if not row: return None
    return PlanItem(row["user_id"], row["item_id"], row["text"], row["when_hhmm"], bool(row["done"]))

async def _clone_item(uid: int, src: PlanItem) -> PlanItem:
    return await _insert_item(uid, text=src.text, when_hhmm=src.when_hhmm)

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
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="PLAN_OPEN")]])

# ---------------
# Парсеры/хелперы
# ---------------
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

def _parse_time(s: str) -> Optional[str]:
    s = (s or "").strip()
    m = _TIME_RE.match(s)
    if not m:
        return None
    hh, mm = m.groups()
    return f"{int(hh):02d}:{int(mm):02d}"

# ---------------
# Безопасное редактирование сообщения
# ---------------
async def edit_or_pass(q, text: str, reply_markup: InlineKeyboardMarkup):
    """Безопасно редактируем сообщение. Если «Message is not modified» — молча игнорируем."""
    try:
        await q.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            try:
                await q.edit_message_reply_markup(reply_markup=reply_markup)
            except BadRequest as e2:
                if "Message is not modified" in str(e2):
                    return
                raise
            return
        raise

# -----------------------------
# Публичный entry-point для бота
# -----------------------------
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть/обновить экран планировщика."""
    uid = update.effective_user.id
    kb = await _kb_main(uid)
    text = "🗓 ПЛАН НА ДЕНЬ\nВыбирай задачу или добавь новую."
    if update.callback_query:
        await edit_or_pass(update.callback_query, text, kb)
    else:
        await update.effective_message.reply_text(text=text, reply_markup=kb)

# --------------------------------------
# Внутренний роутер callback-кнопок (group=0)
# --------------------------------------
async def _cb_plan_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = q.data or ""

    # показать список
    if data in ("PLAN_OPEN", "PLAN_LIST", "show_day_plan"):
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    # добавление пустой — сразу спросить время
    if data == "PLAN_ADD_EMPTY":
        it = await _insert_item(uid, "")
        await q.answer("Добавлено. Укажи время.")
        USER_STATE[uid] = {"mode": "edit_time", "item_id": it.item_id}
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{it.item_id} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # запрос генерации новой темы от ИИ (сначала тема, потом время)
    if data == "PLAN_ADD_AI":
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
            await q.answer("Некорректный ID")
            return
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Задача не найдена")
            return
        await edit_or_pass(q, f"📝 Задача #{it.item_id}\n{_fmt_item(it)}", _kb_item(it))
        return

    # удалить
    if data.startswith("DEL_ITEM:"):
        iid = int(data.split(":", 1)[1])
        await _delete_item(uid, iid)
        await q.answer("Удалено.")
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    # клон
    if data.startswith("CLONE_ITEM:"):
        iid = int(data.split(":", 1)[1])
        src = await _get_item(uid, iid)
        if not src:
            await q.answer("Нет такой задачи")
            return
        await _clone_item(uid, src)
        await q.answer("Склонировано.")
        await edit_or_pass(q, "🗓 ПЛАН НА ДЕНЬ", await _kb_main(uid))
        return

    # переключить статус done
    if data.startswith("TOGGLE_DONE:"):
        iid = int(data.split(":", 1)[1])
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи")
            return
        await _update_done(uid, iid, not it.done)
        it = await _get_item(uid, iid)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    # правка текста
    if data.startswith("EDIT_ITEM:"):
        iid = int(data.split(":", 1)[1])
        USER_STATE[uid] = {"mode": "edit_text", "item_id": iid}
        await edit_or_pass(
            q,
            f"✏️ Введи новый текст для задачи #{iid}",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # правка времени
    if data.startswith("EDIT_TIME:"):
        iid = int(data.split(":", 1)[1])
        USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
        await edit_or_pass(
            q,
            f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
        )
        return

    # автозаполнение текста ИИ
    if data.startswith("AI_FILL_TEXT:"):
        iid = int(data.split(":", 1)[1])
        it = await _get_item(uid, iid)
        if not it:
            await q.answer("Нет такой задачи")
            return
        hint = it.text or "daily task for Ai Coin"
        if _ai_generator:
            try:
                txt, tags, img = await _ai_generator(hint)
                txt = (txt or "").strip()
                if txt:
                    await _update_text(uid, iid, txt)
                await q.answer("Текст обновлён ИИ.")
            except Exception:
                await q.answer("Ошибка генерации")
        else:
            await q.answer("ИИ-генератор не подключен")
        it = await _get_item(uid, iid)
        await edit_or_pass(q, f"📝 Задача #{iid}\n{_fmt_item(it)}", _kb_item(it))
        return

    # создание новой задачи сразу от ИИ
    if data.startswith("AI_NEW_FROM:"):
        topic = data.split(":", 1)[1].strip() or "general"
        it = await _insert_item(uid, f"(генерация: {topic})")
        if _ai_generator:
            try:
                txt, tags, img = await _ai_generator(topic)
                if txt:
                    await _update_text(uid, it.item_id, txt)
            except Exception:
                pass
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
        await open_planner(update, context)

# --------------------------------------
# Текстовые сообщения (ввод для режимов)
# --------------------------------------
async def _msg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = USER_STATE.get(uid)
    txt = (update.message.text or "").strip()

    if not st:
        # если не ждём ввода — просто показать список
        await open_planner(update, context)
        return

    mode = st.get("mode")
    if mode == "edit_text":
        iid = int(st.get("item_id"))
        await _update_text(uid, iid, txt)
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
        await open_planner(update, context)
        return

    if mode == "waiting_new_topic":
        topic = txt or "general"
        it = await _insert_item(uid, f"(генерация: {topic})")
        if _ai_generator:
            try:
                gen_text, tags, img = await _ai_generator(topic)
                if gen_text:
                    await _update_text(uid, it.item_id, gen_text)
                await update.message.reply_text("✨ Создано с помощью ИИ.")
            except Exception:
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
    USER_STATE.pop(uid, None)
    await open_planner(update, context)

# ==== Экспорт для twitter_bot.py ====
async def planner_add_from_text(uid: int, text: str) -> int:
    """Создаёт новую задачу с текстом и возвращает item_id."""
    it = await _insert_item(uid, text or "")
    return it.item_id

async def planner_prompt_time(uid: int, chat_id: int, bot) -> None:
    """Спрашивает у пользователя время для задачи последней/созданной записи.
       user_id нужен для USER_STATE; chat_id — куда слать сообщение."""
    # в простейшем виде — найдём последнюю задачу
    items = await _get_items(uid)
    if not items:
        return
    iid = items[-1].item_id
    USER_STATE[uid] = {"mode": "edit_time", "item_id": iid}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Отмена", callback_data="PLAN_OPEN")]])
    await bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Введи время для задачи #{iid} в формате HH:MM (по Киеву)",
        reply_markup=kb
    )

# --------------------------------------
# Регистрация хендлеров в PTB (group=0)
# --------------------------------------
def register_planner_handlers(app: Application) -> None:
    """
    Регистрируем РАНЬШЕ основного бота (group=0), чтобы планировщик
    забирал только свои колбэки. BACK_MAIN_MENU/PLAN_DONE/GEN_DONE не ловим.
    """
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