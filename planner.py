# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List

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
    step: str = "idle"   # idle | waiting_topic | waiting_text | waiting_time | confirm
    mode: str = "none"   # plan | gen

# -------------------------
# КНОПКИ
# -------------------------
def main_planner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 План ИИ (темы→время)", callback_data="OPEN_PLAN_MODE")],
        [InlineKeyboardButton("✨ Ручной план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
        [InlineKeyboardButton("🤖 Построить план ИИ сейчас", callback_data="PLAN_AI_BUILD_NOW")],
        [InlineKeyboardButton("📋 Список на сегодня", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
    ])

def step_buttons_done_add_cancel(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}DONE"),
            InlineKeyboardButton("➕ Добавить", callback_data=f"{prefix}ADD_MORE"),
        ],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")],
        [InlineKeyboardButton("↩️ Отмена (шаг назад)", callback_data="STEP_BACK")],
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
    USER_STATE[uid]["current"] = PlannedItem()  # сброс

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
# OPENAI: ПРОВЕРКА ДОСТУПНОСТИ/КВОТЫ
# -------------------------
def _openai_key_present() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))

async def _openai_usable() -> bool:
    """
    Лёгкая проверка: пробуем мини-вызов /chat/completions.
    Если 429 insufficient_quota — вернём False.
    """
    if not _openai_key_present():
        return False
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":"ping"}],
            max_tokens=1,
            temperature=0.0,
        )
        return True
    except Exception as e:
        msg = str(e).lower()
        if "insufficient_quota" in msg or "too many requests" in msg or "429" in msg:
            return False
        return False

# -------------------------
# ОТКРЫТИЕ ПЛАНИРОВЩИКА (вызывается из twitter_bot)
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
    await _safe_edit_or_send(
        q,
        "Введите тему (или несколько) для поста и отправьте сообщением.\n"
        "Можно отменить или вернуться в основное меню кнопками ниже.",
        reply_markup=cancel_only()
    )

async def _ask_text(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.mode = "gen"
    st.step = "waiting_text"
    await _safe_edit_or_send(
        q,
        "Отправьте контент поста (текст) и/или фото (можно одним сообщением — фото с подписью).",
        reply_markup=cancel_only()
    )

async def _ask_time(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    st.step = "waiting_time"
    await _safe_edit_or_send(
        q, "Введите время публикации в формате <b>HH:MM</b> по Киеву (например, 14:30).",
        reply_markup=cancel_only()
    )

async def _show_ready_add_cancel(q: CallbackQuery):
    uid = q.from_user.id
    st = _ensure(uid)
    prefix = "PLAN_" if st.mode == "plan" else "GEN_"
    lines: List[str] = []
    if st.mode == "plan":
        lines.append(f"Тема: {st.topic or '—'}")
    else:
        text = (st.text or "—").strip()
        if len(text) > 400:
            text = text[:397] + "…"
        lines.append(f"Текст: {text}")
        lines.append(f"Картинка: {'есть' if st.image_url else 'нет'}")
    lines.append(f"Время: {st.time_str or '—'}")
    await _safe_edit_or_send(
        q, "Проверьте данные:\n" + "\n".join(lines),
        reply_markup=step_buttons_done_add_cancel(prefix)
    )

# -------------------------
# CALLBACKS
# -------------------------
async def cb_open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _openai_usable():
        q = update.callback_query
        await _safe_edit_or_send(
            q,
            "❗ <b>OpenAI недоступен или квота исчерпана</b>.\n"
            "Вы можете продолжить вручную (кнопка ниже) — всё сохранится и будет опубликовано по расписанию.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Ручной план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )
        return
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
    USER_STATE.setdefault(uid, {"items": [], "current": PlannedItem()})
    USER_STATE[uid]["current"] = PlannedItem()  # полный сброс текущего шага
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=main_planner_menu())

async def cb_back_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _safe_edit_or_send(
        q, "Открываю основное меню…",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Открыть основное меню", callback_data="cancel_to_main")]])
    )

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

# ---- AI build now (по кнопке) ----
async def cb_plan_ai_build_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await _openai_usable():
        return await _safe_edit_or_send(
            q,
            "❗ <b>OpenAI недоступен или квота исчерпана</b> — пополните баланс, чтобы строить ИИ-план автоматически.\n\n"
            "Пока можно перейти в ручной режим:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Ручной план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    # Пытаемся импортировать генератор из основного бота
    try:
        from twitter_bot import ai_generate_content_en
    except Exception:
        return await _safe_edit_or_send(
            q,
            "Не получилось вызвать ИИ-генератор из основного бота. Можно продолжить вручную.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Ручной план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    topics = [
        "Utility, community growth and joining early.",
        "Governance & on-chain voting with AI analysis.",
        "AI-powered proposals and speed of execution."
    ]
    uid = q.from_user.id
    _ensure(uid)

    created = 0
    for th in topics:
        try:
            text_en, tags, img = await ai_generate_content_en(th)
            USER_STATE[uid]["items"].append({
                "mode": "plan",
                "topic": th,
                "text": f"{text_en}\n\n{' '.join(tags)}".strip(),
                "time": None,
                "image_url": img,
                "added_at": datetime.utcnow().isoformat() + "Z"
            })
            created += 1
        except Exception:
            pass

    if created == 0:
        return await _safe_edit_or_send(
            q,
            "Не удалось сгенерировать план. Попробуйте позже или перейдите в ручной режим.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Ручной план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    return await _safe_edit_or_send(
        q,
        f"Сгенерировано позиций: <b>{created}</b>.\nТеперь добавьте время для нужных задач (через «Ручной план») или вернитесь в основное меню.",
        reply_markup=main_planner_menu()
    )

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
        fake_cb = await update.to_callback_query(context.bot)
        return await _ask_time(fake_cb)

    # Сбор контента (GEN) + картинка опционально
    if st.step == "waiting_text":
        # фото как фото
        if msg.photo:
            st.image_url = msg.photo[-1].file_id
        # фото как документ (скрепка)
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                st.image_url = msg.document.file_id
        if text:
            st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("Пришлите текст поста и/или фото.", reply_markup=cancel_only())
        fake_cb = await update.to_callback_query(context.bot)
        return await _ask_time(fake_cb)

    # Время для обоих режимов
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
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# -------------------------
def register_planner_handlers(app: Application):
    # Планировщик — высокий приоритет (group=0). В PTB v20 параметра `block` нет.
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode,    pattern="^OPEN_PLAN_MODE$"),    group=0)
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,     pattern="^OPEN_GEN_MODE$"),     group=0)
    app.add_handler(CallbackQueryHandler(cb_list_today,        pattern="^PLAN_LIST_TODAY$"),   group=0)
    app.add_handler(CallbackQueryHandler(cb_plan_ai_build_now, pattern="^PLAN_AI_BUILD_NOW$"), group=0)

    app.add_handler(CallbackQueryHandler(cb_step_back,         pattern="^STEP_BACK$"),         group=0)
    app.add_handler(CallbackQueryHandler(cb_back_main_menu,    pattern="^BACK_MAIN_MENU$"),    group=0)
    app.add_handler(CallbackQueryHandler(cb_plan_done,         pattern="^PLAN_DONE$"),         group=0)
    app.add_handler(CallbackQueryHandler(cb_gen_done,          pattern="^GEN_DONE$"),          group=0)
    app.add_handler(CallbackQueryHandler(cb_add_more,          pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$"), group=0)

    # Ввод пользователем — тоже в нулевой группе, но on_user_message сам “отпускает” события,
    # если режим планировщика не активен (см. ранний return в начале функции).
    app.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, on_user_message),
        group=0
    )

# -------------------------
# FAKE CallbackQuery (для унификации шагов)
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

# Патч метода Update — удобно дергать одинаково из шагов
setattr(Update, "to_callback_query", _update_to_callback_query)