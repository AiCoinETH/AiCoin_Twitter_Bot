# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, Message, CallbackQuery
)
from telegram.ext import (
    Application, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import BadRequest

# -------------------------
# ПАМЯТЬ СЕССИЙ ПЛАНИРОВЩИКА
# -------------------------
USER_STATE: Dict[int, Dict[str, Any]] = {}

@dataclass
class PlannedItem:
    topic: Optional[str] = None
    text: Optional[str] = None
    time_str: Optional[str] = None
    image_url: Optional[str] = None
    step: str = "idle"   # idle | waiting_topic | waiting_text | waiting_time
    mode: str = "none"   # plan | gen

# -------------------------
# КНОПКИ
# -------------------------
def main_planner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 План ИИ (темы→время)", callback_data="OPEN_PLAN_MODE")],
        [InlineKeyboardButton("✨ Генерация (контент→время)", callback_data="OPEN_GEN_MODE")],
        [InlineKeyboardButton("📋 Список на сегодня", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]  # новая кнопка
    ])

def step_buttons_done_add_cancel(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}DONE"),
            InlineKeyboardButton("➕ Добавить", callback_data=f"{prefix}ADD_MORE"),
        ],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")],  # тоже добавляем сюда
        [InlineKeyboardButton("↩️ Отмена (шаг назад)", callback_data="STEP_BACK")]
    ])

def cancel_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")],
        [InlineKeyboardButton("↩️ Отмена", callback_data="STEP_BACK")]
    ])

# -------------------------
# ХЕЛПЕРЫ СОСТОЯНИЯ
# -------------------------
def _ensure(uid: int) -> PlannedItem:
    row = USER_STATE.get(uid) or {}
    if "current" not in row:
        row["current"] = PlannedItem()
        row.setdefault("items", [])
        USER_STATE[uid] = row
    return row["current"]

def _push(uid: int, item: PlannedItem):
    USER_STATE[uid]["items"].append({
        "mode": item.mode,
        "topic": item.topic,
        "text": item.text,
        "time": item.time_str,
        "image_url": item.image_url,
        "added_at": datetime.utcnow().isoformat() + "Z"
    })
    USER_STATE[uid]["current"] = PlannedItem()

def _can_finalize(item: PlannedItem) -> bool:
    if not item.time_str:
        return False
    if item.mode == "plan":
        return bool(item.topic)
    if item.mode == "gen":
        return bool(item.text or item.image_url)
    return False

# -------------------------
# БЕЗОПАСНОЕ РЕДАКТИРОВАНИЕ
# -------------------------
async def _safe_edit_or_send(q: CallbackQuery, text: str,
                             reply_markup: Optional[InlineKeyboardMarkup]=None,
                             parse_mode: Optional[str]="HTML"):
    m: Message = q.message
    try:
        if m and (m.text is not None):
            return await q.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                                             disable_web_page_preview=True)
        if m and (m.caption is not None):
            return await q.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        raise BadRequest("no editable text/caption")
    except BadRequest:
        return await m.chat.send_message(text=text, reply_markup=reply_markup, parse_mode=parse_mode,
                                         disable_web_page_preview=True)

# -------------------------
# ОТКРЫТИЕ ПЛАНИРОВЩИКА
# -------------------------
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    USER_STATE.setdefault(uid, {"mode": "none", "items": [], "current": PlannedItem()})
    if q:
        await _safe_edit_or_send(q, "Планировщик: выбери режим.", reply_markup=main_planner_menu())
    else:
        await context.bot.send_message(update.effective_chat.id, "Планировщик: выбери режим.",
                                       reply_markup=main_planner_menu())

# -------------------------
# ПРОСЬБЫ/ШАГИ
# -------------------------
async def _ask_topic(q: CallbackQuery, mode: str):
    uid = q.from_user.id
    st = _ensure(uid)
    st.mode = mode
    st.step = "waiting_topic"
    await _safe_edit_or_send(q, "Введите тему (или несколько) для поста и отправьте сообщением.", reply_markup=cancel_only())

async def _ask_text(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.mode = "gen"
    st.step = "waiting_text"
    await _safe_edit_or_send(q, "Отправьте контент поста (текст) и/или фото.", reply_markup=cancel_only())

async def _ask_time(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.step = "waiting_time"
    await _safe_edit_or_send(q, "Введите время публикации в формате HH:MM по Киеву (например, 14:30).", reply_markup=cancel_only())

async def _show_ready_add_cancel(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    prefix = "PLAN_" if st.mode == "plan" else "GEN_"
    summary = []
    if st.mode == "plan":
        summary.append(f"Тема: {st.topic or '—'}")
    else:
        summary.append(f"Текст: {st.text or '—'}")
        summary.append(f"Картинка: {'есть' if st.image_url else 'нет'}")
    summary.append(f"Время: {st.time_str or '—'}")
    await _safe_edit_or_send(q, "Проверьте данные:\n" + "\n".join(summary),
                             reply_markup=step_buttons_done_add_cancel(prefix))

# -------------------------
# CALLBACKS
# -------------------------
async def cb_open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ask_topic(update.callback_query, mode="plan")

async def cb_open_gen_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ask_text(update.callback_query)

async def cb_list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    items = USER_STATE.get(uid, {}).get("items", [])
    if not items:
        return await _safe_edit_or_send(q, "На сегодня пока пусто.", reply_markup=main_planner_menu())
    lines = []
    for i, it in enumerate(items, 1):
        if it["mode"] == "plan":
            lines.append(f"{i}) [PLAN] {it.get('time') or '—'} — {it.get('topic')}")
        else:
            img = "🖼" if it.get("image_url") else "—"
            txt = (it.get("text") or "").strip()
            if len(txt) > 60:
                txt = txt[:57] + "…"
            lines.append(f"{i}) [GEN] {it.get('time') or '—'} — {txt} {img}")
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=main_planner_menu())

async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    USER_STATE[uid]["current"] = PlannedItem()
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=main_planner_menu())

async def cb_back_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from twitter_bot import get_start_menu
    q = update.callback_query
    await _safe_edit_or_send(q, "Главное меню:", reply_markup=get_start_menu())

async def _finalize_current_and_back(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    if _can_finalize(st):
        _push(uid, st)
        return await _safe_edit_or_send(q, "Сохранено. Что дальше?", reply_markup=main_planner_menu())
    else:
        return await _safe_edit_or_send(q, "Нечего сохранять — заполните данные и время.", reply_markup=main_planner_menu())

async def cb_plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _finalize_current_and_back(update.callback_query)

async def cb_gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _finalize_current_and_back(update.callback_query)

async def cb_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    st = _ensure(uid)
    if _can_finalize(st):
        _push(uid, st)
    if st.mode == "plan":
        await _ask_topic(q, mode="plan")
    else:
        await _ask_text(q)

# -------------------------
# INPUT
# -------------------------
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _ensure(uid)
    if st.mode not in ("plan", "gen"):
        return
    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()
    if st.step == "waiting_topic":
        if not text:
            return await msg.reply_text("Нужна тема текстом.", reply_markup=cancel_only())
        st.topic = text
        fake_cb = await update.to_callback_query(context.bot)
        return await _ask_time(fake_cb)
    if st.step == "waiting_text":
        if msg.photo:
            st.image_url = msg.photo[-1].file_id
        if text:
            st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("Пришлите текст и/или фото.", reply_markup=cancel_only())
        fake_cb = await update.to_callback_query(context.bot)
        return await _ask_time(fake_cb)
    if st.step == "waiting_time":
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("Неверный формат. Пример: 14:30", reply_markup=cancel_only())
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"
        fake_cb = await update.to_callback_query(context.bot)
        return await _show_ready_add_cancel(fake_cb)

# -------------------------
# РЕГИСТРАЦИЯ
# -------------------------
def register_planner_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode, pattern="^OPEN_PLAN_MODE$"))
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,  pattern="^OPEN_GEN_MODE$"))
    app.add_handler(CallbackQueryHandler(cb_list_today,     pattern="^PLAN_LIST_TODAY$"))
    app.add_handler(CallbackQueryHandler(cb_step_back,      pattern="^STEP_BACK$"))
    app.add_handler(CallbackQueryHandler(cb_back_main_menu, pattern="^BACK_MAIN_MENU$"))
    app.add_handler(CallbackQueryHandler(cb_plan_done,      pattern="^PLAN_DONE$"))
    app.add_handler(CallbackQueryHandler(cb_gen_done,       pattern="^GEN_DONE$"))
    app.add_handler(CallbackQueryHandler(cb_add_more,       pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$"))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, on_user_message))

# -------------------------
# FAKE CallbackQuery
# -------------------------
async def _build_fake_callback_from_message(message: Message, bot) -> CallbackQuery:
    cq = CallbackQuery(
        id="fake",
        from_user=message.from_user,
        chat_instance="",
        message=message,
        bot=bot
    )
    return cq

async def _update_to_callback_query(update: Update, bot):
    if update.callback_query:
        return update.callback_query
    return await _build_fake_callback_from_message(update.message, bot)

setattr(Update, "to_callback_query", _update_to_callback_query)