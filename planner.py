# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, Message, CallbackQuery, InputMediaPhoto
)
from telegram.ext import (
    Application, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import BadRequest

# -------------------------
# ПАМЯТЬ СЕССИЙ ПЛАНИРОВЩИКА
# -------------------------
USER_STATE: Dict[int, Dict[str, Any]] = {}

# Модель поста в очереди планировщика (темы/контент/время/картинки)
@dataclass
class PlannedItem:
    topic: Optional[str] = None
    text: Optional[str] = None
    time_str: Optional[str] = None
    image_url: Optional[str] = None
    # маркеры процесса
    step: str = "idle"   # idle | waiting_topic | waiting_text | waiting_time
    mode: str = "none"   # plan | gen
    queue: List[dict] = field(default_factory=list)

# -------------------------
# КНОПКИ
# -------------------------
def main_planner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 План ИИ (темы→время)", callback_data="OPEN_PLAN_MODE")],
        [InlineKeyboardButton("✨ Генерация (контент→время)", callback_data="OPEN_GEN_MODE")],
        [InlineKeyboardButton("📋 Список на сегодня", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="PLAN_DONE")]
    ])

def step_buttons_done_add_cancel(prefix: str) -> InlineKeyboardMarkup:
    # prefix: PLAN_ | GEN_
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}DONE"),
            InlineKeyboardButton("➕ Добавить", callback_data=f"{prefix}ADD_MORE"),
        ],
        [InlineKeyboardButton("↩️ Отмена (шаг назад)", callback_data="STEP_BACK")]
    ])

def cancel_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отмена", callback_data="STEP_BACK")]])

# -------------------------
# БЕЗОПАСНОЕ РЕДАКТИРОВАНИЕ
# -------------------------
async def _safe_edit_or_send(q: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup]=None, parse_mode: Optional[str]=None):
    """
    Универсально обновляет UI:
    - если исходное сообщение с text -> edit_message_text
    - если исходное сообщение с caption (фото) -> edit_message_caption
    - если редактировать нельзя -> отправляет новое сообщение
    """
    m: Message = q.message
    try:
        if m and (m.text or m.html_text):
            return await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        elif m and (m.caption or getattr(m, "caption_html", None)):
            return await q.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            raise BadRequest("no text/caption to edit")
    except BadRequest:
        return await m.chat.send_message(text, reply_markup=reply_markup, parse_mode=parse_mode)

# -------------------------
# ОТКРЫТИЕ ПЛАНИРОВЩИКА (вызывается из twitter_bot)
# -------------------------
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    # инициализация сессии
    if uid not in USER_STATE:
        USER_STATE[uid] = {"mode": "none", "items": [], "last_msg_id": None}

    if q:
        await _safe_edit_or_send(q, "Планировщик: выбери режим.", reply_markup=main_planner_menu())
    else:
        chat_id = update.effective_chat.id
        await context.bot.send_message(chat_id, "Планировщик: выбери режим.", reply_markup=main_planner_menu())

# -------------------------
# ВСПОМОГАТЕЛЬНОЕ
# -------------------------
def _ensure(uid: int) -> PlannedItem:
    row = USER_STATE.get(uid) or {}
    if "current" not in row:
        row["current"] = PlannedItem()
        USER_STATE[uid] = row
    return row["current"]

def _push(uid: int, item: PlannedItem):
    row = USER_STATE[uid]
    row.setdefault("items", [])
    row["items"].append({
        "mode": item.mode,
        "topic": item.topic,
        "text": item.text,
        "time": item.time_str,
        "image_url": item.image_url,
        "added_at": datetime.utcnow().isoformat() + "Z"
    })
    # сбросить current
    row["current"] = PlannedItem()

async def _ask_topic(q: CallbackQuery, mode: str):
    uid = q.from_user.id
    st = _ensure(uid)
    st.mode = mode
    st.step = "waiting_topic"
    text = "Введите тему (или несколько) для поста и отправьте сообщением.\nМожно отменить кнопкой ниже."
    await _safe_edit_or_send(q, text, reply_markup=cancel_only())

async def _ask_text(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.mode = "gen"
    st.step = "waiting_text"
    text = ("Отправьте контент поста (текст). "
            "Если хотите картинку — приложите её к сообщению (одним сообщением с подписью) или пришлите фото отдельно.")
    await _safe_edit_or_send(q, text, reply_markup=cancel_only())

async def _ask_time(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.step = "waiting_time"
    text = "Введите время публикации в формате HH:MM по Киеву (например, 14:30)."
    await _safe_edit_or_send(q, text, reply_markup=cancel_only())

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
    await _safe_edit_or_send(q, "Проверьте данные:\n" + "\n".join(summary), reply_markup=step_buttons_done_add_cancel(prefix))

# -------------------------
# CALLBACKS
# -------------------------
async def cb_open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ask_topic(q, mode="plan")

async def cb_open_gen_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _ask_text(q)

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
            if len(txt) > 60: txt = txt[:57] + "…"
            lines.append(f"{i}) [GEN] {it.get('time') or '—'} — {txt} {img}")
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=main_planner_menu())

async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    st = _ensure(uid)
    # просто сброс текущего шага
    st.step = "idle"
    st.topic = None
    st.text = None
    st.time_str = None
    st.image_url = None
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=main_planner_menu())

async def cb_plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_edit_or_send(q, "Готово. Возвращаюсь в меню.", reply_markup=main_planner_menu())

async def cb_gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_edit_or_send(q, "Готово. Возвращаюсь в меню.", reply_markup=main_planner_menu())

async def cb_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    st = _ensure(uid)
    # повтор по кругу: снова запросить тему/контент
    if st.mode == "plan":
        await _ask_topic(q, mode="plan")
    else:
        await _ask_text(q)

# -------------------------
# INPUT (текст/фото) ПО ШАГАМ
# -------------------------
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод только когда пользователь в режиме планировщика."""
    uid = update.effective_user.id
    st = _ensure(uid)
    if st.mode not in ("plan", "gen"):
        return  # не наш режим — пусть основное приложение обработает

    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()

    # Сбор темы (PLAN)
    if st.step == "waiting_topic":
        if not text:
            return await msg.reply_text("Нужна тема текстом. Попробуй ещё раз.", reply_markup=cancel_only())
        st.topic = text
        await _ask_time(await update.to_callback_query(context.bot))  # эмулируем шаг через callback для единообразия
        return

    # Сбор контента (GEN) + картинка опционально
    if st.step == "waiting_text":
        if msg.photo:
            # если фото с подписью — сохраним картинку и текст
            file_id = msg.photo[-1].file_id
            st.image_url = file_id  # реальный URL загрузит основной бот при публикации
        if text:
            st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("Пришлите текст поста и/или фото.", reply_markup=cancel_only())
        await _ask_time(await update.to_callback_query(context.bot))
        return

    # Время для обоих режимов
    if st.step == "waiting_time":
        # проверим формат HH:MM
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("Неверный формат. Пример: 14:30", reply_markup=cancel_only())
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"
        # показать резюме + кнопки
        fake_cb = await update.to_callback_query(context.bot)  # единый путь через safe edit
        await _show_ready_add_cancel(fake_cb)
        return

# -------------------------
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# -------------------------
def register_planner_handlers(app: Application):
    # Открыть планировщик (колбэк встраивается в основной бот)
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode, pattern="^OPEN_PLAN_MODE$"))
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,  pattern="^OPEN_GEN_MODE$"))
    app.add_handler(CallbackQueryHandler(cb_list_today,     pattern="^PLAN_LIST_TODAY$"))

    app.add_handler(CallbackQueryHandler(cb_step_back,      pattern="^STEP_BACK$"))
    app.add_handler(CallbackQueryHandler(cb_plan_done,      pattern="^(PLAN_DONE)$"))
    app.add_handler(CallbackQueryHandler(cb_gen_done,       pattern="^(GEN_DONE)$"))
    app.add_handler(CallbackQueryHandler(cb_add_more,       pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$"))

    # Ввод пользователем (перехватываем только если он в режиме планировщика)
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, on_user_message))

# -------------------------
# ХЕЛПЕР: превращаем Update в "псевдо" CallbackQuery
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

# Публичный шорткат — нужен выше
async def _update_to_callback_query(update: Update, bot):
    if update.callback_query:
        return update.callback_query
    return await _build_fake_callback_from_message(update.message, bot)

# Патчим метод Update "на лету", чтобы вызывать одинаково
setattr(Update, "to_callback_query", _update_to_callback_query)