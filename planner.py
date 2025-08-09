# -*- coding: utf-8 -*-
"""
planner.py — единый файл планировщика:
- Кнопки: "🤖 План ИИ" (PLAN_OPEN), внутри режимы: ПЛАН и ГЕНЕРАЦИЯ
- FSM для двух сценариев
- Шаг назад с удалением последнего сообщения бота
- Хранение в SQLite (posts_plan) + дедупликация
- Интеграция: import register_planner_handlers(app) в первом файле
- Можно и запускать как отдельного бота (if __name__ == "__main__")
"""

import os
import json
import re
import asyncio
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------------------------------------------------------
# ENV
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN_APPROVAL = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TELEGRAM_APPROVAL_CHAT_ID = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")  # строка/число
ADMIN_ID = int(TELEGRAM_APPROVAL_CHAT_ID) if TELEGRAM_APPROVAL_CHAT_ID and TELEGRAM_APPROVAL_CHAT_ID.isdigit() else None

# -----------------------------------------------------------------------------
# CONST
# -----------------------------------------------------------------------------
DB_FILE = "schedule.db"
KYIV = ZoneInfo("Europe/Kyiv")

# callback data
CB_PLAN_OPEN       = "PLAN_OPEN"
CB_OPEN_PLAN_MODE  = "OPEN_PLAN_MODE"
CB_OPEN_GEN_MODE   = "OPEN_GEN_MODE"
CB_PLAN_DONE       = "PLAN_DONE"
CB_GEN_DONE        = "GEN_DONE"
CB_PLAN_ADD_MORE   = "PLAN_ADD_MORE"
CB_GEN_ADD_MORE    = "GEN_ADD_MORE"
CB_STEP_BACK       = "STEP_BACK"

# -----------------------------------------------------------------------------
# IN-MEMORY FSM
# -----------------------------------------------------------------------------
# USER_STATE: uid -> dict
# {
#   "mode": "plan"|"gen",
#   "step": "...",
#   "buffer": [ { "time_iso":"...", "theme":"...", "images":[...] }, ... ],
#   "current": { "theme":"", "time_iso":"", "images":[...] },
#   "last_bot_msg_ids": [int, ...]
# }
USER_STATE = {}

def _state(uid) -> dict:
    return USER_STATE.setdefault(uid, {
        "mode": None,
        "step": None,
        "buffer": [],
        "current": {},
        "last_bot_msg_ids": []
    })

def _push_bot_msg(update_or_ctx, msg):
    """Сохраняем id последнего сообщения бота для удаления при STEP_BACK."""
    try:
        uid = update_or_ctx.effective_user.id
    except Exception:
        return
    st = _state(uid)
    if msg and getattr(msg, "message_id", None):
        st["last_bot_msg_ids"].append(msg.message_id)

async def _delete_last_bot_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _state(uid)
    if not st["last_bot_msg_ids"]:
        return
    mid = st["last_bot_msg_ids"].pop()
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------
INIT_SQL = """
CREATE TABLE IF NOT EXISTS posts_plan (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  time       TEXT NOT NULL,       -- ISO (Europe/Kyiv или UTC, договоримся)
  type       TEXT NOT NULL,       -- 'theme' | 'content'
  theme      TEXT,
  text       TEXT,
  images     TEXT,                -- JSON array
  status     TEXT NOT NULL DEFAULT 'pending',
  dedupe_key TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_plan_dedupe ON posts_plan(dedupe_key);
"""

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        for stmt in INIT_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                await db.execute(s)
        await db.commit()

def normalize_text(s: str) -> str:
    s = s or ""
    s = s.strip().lower()
    # уберём двойные пробелы
    s = re.sub(r"\s+", " ", s)
    return s

def compute_dedupe_key(item: dict) -> str:
    """
    - type='theme': sha256( normalize(theme) + '|' + yyyy-mm-dd )
    - type='content': sha256( normalize(text) [+ '|' + img_hash] )
    """
    itype = item.get("type")
    if itype == "theme":
        theme = normalize_text(item.get("theme", ""))
        # Привязываем к дню, чтобы не дублировать одинаковую тему в один день
        try:
            dt = datetime.fromisoformat(item["time_iso"])
        except Exception:
            dt = datetime.now(KYIV)
        day = dt.astimezone(KYIV).date().isoformat()
        base = f"{theme}|{day}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()
    else:
        text = normalize_text(item.get("text", ""))
        base = text
        # Если хотим ещё сильнее отсекать дубль по первой картинке — добавим:
        imgs = item.get("images") or []
        if imgs:
            base = f"{base}|{imgs[0]}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

async def save_items_to_db(items: list[dict], created_by: str | None):
    """Сохраняем элементы (theme/content) транзакционно, с дедупом."""
    now_iso = datetime.now(KYIV).isoformat()
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute("BEGIN")
            for it in items:
                dedupe_key = compute_dedupe_key(it)
                images_json = json.dumps(it.get("images") or [])
                await db.execute(
                    """
                    INSERT INTO posts_plan(time, type, theme, text, images, status, dedupe_key, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        it["time_iso"],
                        it["type"],
                        it.get("theme"),
                        it.get("text"),
                        images_json,
                        dedupe_key,
                        now_iso,
                        str(created_by) if created_by else None
                    )
                )
            await db.commit()
            return True, None
        except aiosqlite.IntegrityError as e:
            await db.rollback()
            return False, "Дубликат: похожая запись уже есть на этот день."
        except Exception as e:
            await db.rollback()
            return False, f"Ошибка сохранения: {e}"

# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
def main_planner_menu():
    kb = [
        [InlineKeyboardButton("🗓 План ИИ", callback_data=CB_OPEN_PLAN_MODE)],
        [InlineKeyboardButton("✨ Генерация", callback_data=CB_OPEN_GEN_MODE)],
    ]
    return InlineKeyboardMarkup(kb)

def decide_menu(mode: str):
    # mode = 'plan' or 'gen' — разные callback на DONE/ADD
    if mode == "plan":
        done = CB_PLAN_DONE
        addm = CB_PLAN_ADD_MORE
    else:
        done = CB_GEN_DONE
        addm = CB_GEN_ADD_MORE
    kb = [
        [InlineKeyboardButton("✅ Готово", callback_data=done)],
        [InlineKeyboardButton("➕ Добавить", callback_data=addm)],
        [InlineKeyboardButton("↩️ Отменить", callback_data=CB_STEP_BACK)],
    ]
    return InlineKeyboardMarkup(kb)

# -----------------------------------------------------------------------------
# Guards
# -----------------------------------------------------------------------------
def _is_admin(update: Update) -> bool:
    if not ADMIN_ID:
        return True
    return update.effective_user and update.effective_user.id == ADMIN_ID

async def _guard_non_admin(update: Update):
    await update.effective_message.reply_text("Доступ ограничен.")

# -----------------------------------------------------------------------------
# Handlers: OPEN
# -----------------------------------------------------------------------------
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускается по кнопке PLAN_OPEN."""
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await init_db()
    q = update.callback_query
    if q:
        await q.answer()
        msg = await q.edit_message_text("Планировщик: выбери режим.", reply_markup=main_planner_menu())
        _push_bot_msg(update, msg)
    else:
        msg = await update.message.reply_text("Планировщик: выбери режим.", reply_markup=main_planner_menu())
        _push_bot_msg(update, msg)

# -----------------------------------------------------------------------------
# PLAN mode
# -----------------------------------------------------------------------------
async def open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    uid = update.effective_user.id
    st = _state(uid)
    st["mode"] = "plan"
    st["step"] = "plan_theme_wait"
    st["buffer"].clear()
    st["current"] = {}
    if update.callback_query:
        await update.callback_query.answer()
        msg = await update.callback_query.edit_message_text("Режим ПЛАНА. Введите тему или нажмите Отменить.")
    else:
        msg = await update.message.reply_text("Режим ПЛАНА. Введите тему или нажмите Отменить.")
    _push_bot_msg(update, msg)

async def plan_on_text_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "plan":
        return

    # Отмена словом
    if update.message.text and update.message.text.strip().lower() in ("отменить", "отмена", "cancel"):
        # шаг назад
        await step_back(update, context)
        return

    if st["step"] == "plan_theme_wait":
        if not update.message.text:
            return
        st["current"] = {"theme": update.message.text.strip()}
        st["step"] = "plan_time_wait"
        msg = await update.message.reply_text("Ок. Введите время (HH:MM, Киев) или Отменить.")
        _push_bot_msg(update, msg)
        return

    if st["step"] == "plan_time_wait":
        if not update.message.text:
            return
        m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", update.message.text.strip())
        if not m:
            msg = await update.message.reply_text("Формат времени HH:MM. Попробуйте ещё раз или нажмите Отменить.")
            _push_bot_msg(update, msg)
            return
        hh, mm = map(int, m.groups())
        now = datetime.now(KYIV)
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt < now:
            dt += timedelta(days=1)
        st["current"]["time_iso"] = dt.isoformat()
        st["current"]["type"] = "theme"
        st["current"]["images"] = []
        st["step"] = "plan_decide"

        summary = f"Тема: {st['current']['theme']}\nВремя: {dt.strftime('%Y-%m-%d %H:%M')}"
        msg = await update.message.reply_text(summary, reply_markup=decide_menu("plan"))
        _push_bot_msg(update, msg)
        return

# -----------------------------------------------------------------------------
# GEN mode
# -----------------------------------------------------------------------------
async def open_gen_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    uid = update.effective_user.id
    st = _state(uid)
    st["mode"] = "gen"
    st["step"] = "gen_theme_images_wait"
    st["buffer"].clear()
    st["current"] = {"images": []}
    if update.callback_query:
        await update.callback_query.answer()
        msg = await update.callback_query.edit_message_text("Режим ГЕНЕРАЦИИ. Введите тему и (опц.) пришлите 1–4 фото. Напишите 'ГОТОВО' когда закончите, или 'Отменить'.")
    else:
        msg = await update.message.reply_text("Режим ГЕНЕРАЦИИ. Введите тему и (опц.) пришлите 1–4 фото. Напишите 'ГОТОВО', или 'Отменить'.")
    _push_bot_msg(update, msg)

async def gen_on_text_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "gen":
        return

    # Отмена словом
    if update.message.text and update.message.text.strip().lower() in ("отменить", "отмена", "cancel"):
        await step_back(update, context)
        return

    if st["step"] == "gen_theme_images_wait":
        # Фото?
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            imgs = st["current"].setdefault("images", [])
            if len(imgs) < 4:
                imgs.append(file_id)
                msg = await update.message.reply_text(f"Изображение добавлено ({len(imgs)}/4). Добавьте ещё или напишите 'ГОТОВО'.")
                _push_bot_msg(update, msg)
            else:
                msg = await update.message.reply_text("Достигнут лимит 4 изображения. Напишите 'ГОТОВО' или 'Отменить'.")
                _push_bot_msg(update, msg)
            return

        # Текст?
        if update.message.text:
            text = update.message.text.strip()
            if text.upper() == "ГОТОВО":
                # Проверим, что тема есть
                if not st["current"].get("theme"):
                    msg = await update.message.reply_text("Сначала введите тему (текст), затем 'ГОТОВО'.")
                    _push_bot_msg(update, msg)
                    return
                st["step"] = "gen_time_wait"
                msg = await update.message.reply_text("Ок. Введите время (HH:MM, Киев) или Отменить.")
                _push_bot_msg(update, msg)
                return
            else:
                st["current"]["theme"] = text
                msg = await update.message.reply_text("Тема зафиксирована. Пришлите фото (необязательно) и напишите 'ГОТОВО', либо 'Отменить'.")
                _push_bot_msg(update, msg)
                return

    if st["step"] == "gen_time_wait":
        if not update.message.text:
            return
        m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", update.message.text.strip())
        if not m:
            msg = await update.message.reply_text("Формат времени HH:MM. Попробуйте ещё раз или нажмите Отменить.")
            _push_bot_msg(update, msg)
            return
        hh, mm = map(int, m.groups())
        now = datetime.now(KYIV)
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt < now:
            dt += timedelta(days=1)
        st["current"]["time_iso"] = dt.isoformat()
        st["current"]["type"] = "theme"   # в генерации тоже тип 'theme' (текст генернём позже)
        st["step"] = "gen_decide"

        imgs_cnt = len(st["current"].get("images") or [])
        summary = f"Тема: {st['current']['theme']}\nВремя: {dt.strftime('%Y-%m-%d %H:%M')}\nКартинок: {imgs_cnt}"
        msg = await update.message.reply_text(summary, reply_markup=decide_menu("gen"))
        _push_bot_msg(update, msg)
        return

# -----------------------------------------------------------------------------
# Callback buttons: DONE / ADD MORE / STEP BACK
# -----------------------------------------------------------------------------
async def plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await update.callback_query.answer()
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "plan":
        return
    items = list(st["buffer"])
    cur = st.get("current") or {}
    if cur.get("time_iso") and cur.get("theme"):
        items.append(cur)
    if not items:
        msg = await update.callback_query.edit_message_text("Нет данных для сохранения.")
        _push_bot_msg(update, msg)
        return
    ok, err = await save_items_to_db(items, created_by=str(uid))
    if ok:
        # reset
        USER_STATE.pop(uid, None)
        msg = await update.callback_query.edit_message_text("План сохранён ✅")
        _push_bot_msg(update, msg)
    else:
        msg = await update.callback_query.edit_message_text(err or "Ошибка сохранения.")
        _push_bot_msg(update, msg)

async def gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await update.callback_query.answer()
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "gen":
        return
    items = list(st["buffer"])
    cur = st.get("current") or {}
    if cur.get("time_iso") and cur.get("theme"):
        items.append(cur)
    if not items:
        msg = await update.callback_query.edit_message_text("Нет данных для сохранения.")
        _push_bot_msg(update, msg)
        return
    ok, err = await save_items_to_db(items, created_by=str(uid))
    if ok:
        USER_STATE.pop(uid, None)
        msg = await update.callback_query.edit_message_text("Запланировано ✅")
        _push_bot_msg(update, msg)
    else:
        msg = await update.callback_query.edit_message_text(err or "Ошибка сохранения.")
        _push_bot_msg(update, msg)

async def plan_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await update.callback_query.answer()
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "plan":
        return
    cur = st.get("current") or {}
    if cur.get("time_iso") and cur.get("theme"):
        st["buffer"].append(cur)
    st["current"] = {}
    st["step"] = "plan_theme_wait"
    msg = await update.callback_query.edit_message_text("Добавляем ещё. Введите тему или Отменить.")
    _push_bot_msg(update, msg)

async def gen_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await update.callback_query.answer()
    uid = update.effective_user.id
    st = _state(uid)
    if st["mode"] != "gen":
        return
    cur = st.get("current") or {}
    if cur.get("time_iso") and cur.get("theme"):
        # зафиксируем текущий
        st["buffer"].append(cur)
    st["current"] = {"images": []}
    st["step"] = "gen_theme_images_wait"
    msg = await update.callback_query.edit_message_text("Добавляем ещё. Введите тему и (опц.) фото. Напишите 'ГОТОВО' или Отменить.")
    _push_bot_msg(update, msg)

async def step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка STEP_BACK и словесная 'Отменить': шаг назад + удаление последнего сообщения бота."""
    if update.callback_query:
        await update.callback_query.answer()
    uid = update.effective_user.id
    st = _state(uid)
    mode = st.get("mode")
    step = st.get("step")

    # Удаляем последнее сообщение бота
    await _delete_last_bot_msg(update, context)

    if mode == "plan":
        if step == "plan_decide":
            st["step"] = "plan_time_wait"
            msg = await update.effective_message.reply_text("Шаг назад. Введите время (HH:MM) или Отменить.")
            _push_bot_msg(update, msg)
        elif step == "plan_time_wait":
            st["step"] = "plan_theme_wait"
            st["current"].pop("time_iso", None)
            msg = await update.effective_message.reply_text("Шаг назад. Введите тему или Отменить.")
            _push_bot_msg(update, msg)
        else:
            # выходим в меню
            st["mode"] = None
            st["step"] = None
            st["buffer"].clear()
            st["current"] = {}
            msg = await update.effective_message.reply_text("Возврат в меню планировщика.", reply_markup=main_planner_menu())
            _push_bot_msg(update, msg)
        return

    if mode == "gen":
        if step == "gen_decide":
            st["step"] = "gen_time_wait"
            msg = await update.effective_message.reply_text("Шаг назад. Введите время (HH:MM) или Отменить.")
            _push_bot_msg(update, msg)
        elif step == "gen_time_wait":
            st["step"] = "gen_theme_images_wait"
            st["current"].pop("time_iso", None)
            msg = await update.effective_message.reply_text("Шаг назад. Введите тему, добавьте фото и напишите 'ГОТОВО', или Отменить.")
            _push_bot_msg(update, msg)
        else:
            st["mode"] = None
            st["step"] = None
            st["buffer"].clear()
            st["current"] = {}
            msg = await update.effective_message.reply_text("Возврат в меню планировщика.", reply_markup=main_planner_menu())
            _push_bot_msg(update, msg)
        return

    # Если вне режима
    msg = await update.effective_message.reply_text("Ок.", reply_markup=main_planner_menu())
    _push_bot_msg(update, msg)

# -----------------------------------------------------------------------------
# LIST TODAY (по желанию; простая справка)
# -----------------------------------------------------------------------------
async def list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await _guard_non_admin(update)
    await init_db()
    today = datetime.now(KYIV).date()
    start = datetime(today.year, today.month, today.day, 0, 0, tzinfo=KYIV).isoformat()
    end   = datetime(today.year, today.month, today.day, 23, 59, tzinfo=KYIV).isoformat()

    rows = []
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT id, time, type, theme, text, images, status FROM posts_plan WHERE time BETWEEN ? AND ? ORDER BY time ASC",
            (start, end)
        ) as cur:
            async for r in cur:
                rows.append(r)

    if not rows:
        msg = await update.effective_message.reply_text("На сегодня записей нет.")
        _push_bot_msg(update, msg)
        return

    lines = []
    for rid, t, typ, theme, text, images, status in rows:
        label = theme or (text[:40] + "..." if text else "")
        lines.append(f"#{rid} • {t[-8:-3]} • {typ} • {status} • {label}")

    msg = await update.effective_message.reply_text("План на сегодня:\n" + "\n".join(lines))
    _push_bot_msg(update, msg)

# -----------------------------------------------------------------------------
# REGISTRATION
# -----------------------------------------------------------------------------
def register_planner_handlers(app: Application):
    # Открытие планировщика
    app.add_handler(CallbackQueryHandler(open_planner,      pattern=f"^{CB_PLAN_OPEN}$"))
    # Меню режимов
    app.add_handler(CallbackQueryHandler(open_plan_mode,    pattern=f"^{CB_OPEN_PLAN_MODE}$"))
    app.add_handler(CallbackQueryHandler(open_gen_mode,     pattern=f"^{CB_OPEN_GEN_MODE}$"))
    # План: текстовый обработчик
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, plan_on_text_or_photo))
    # Генерация: текст/фото
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, gen_on_text_or_photo))
    # Кнопки управления
    app.add_handler(CallbackQueryHandler(plan_done,         pattern=f"^{CB_PLAN_DONE}$"))
    app.add_handler(CallbackQueryHandler(gen_done,          pattern=f"^{CB_GEN_DONE}$"))
    app.add_handler(CallbackQueryHandler(plan_add_more,     pattern=f"^{CB_PLAN_ADD_MORE}$"))
    app.add_handler(CallbackQueryHandler(gen_add_more,      pattern=f"^{CB_GEN_ADD_MORE}$"))
    app.add_handler(CallbackQueryHandler(step_back,         pattern=f"^{CB_STEP_BACK}$"))
    # Доп.: список на сегодня (можно повесить на отдельную кнопку в твоём меню)
    # Пример: app.add_handler(CommandHandler("today", list_today))

# -----------------------------------------------------------------------------
# STANDALONE RUN (optional)
# -----------------------------------------------------------------------------
async def _startup(app: Application):
    await init_db()

def run_standalone():
    if not TELEGRAM_BOT_TOKEN_APPROVAL:
        raise RuntimeError("TELEGRAM_BOT_TOKEN_APPROVAL is not set.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN_APPROVAL).build()
    register_planner_handlers(app)
    app.post_init = _startup
    app.run_polling()

if __name__ == "__main__":
    run_standalone()