# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, Awaitable, Tuple

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
    step: str = "idle"   # idle | waiting_topic | waiting_text | waiting_time | confirm | editing_*
    mode: str = "none"   # plan | gen

# -------------------------
# РЕГИСТРАТОР ИИ-ГЕНЕРАТОРА (ЧТОБЫ НЕ ИМПОРТИРОВАТЬ twitter_bot)
# -------------------------
_AI_GEN_FN: Optional[
    Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]
] = None

def set_ai_generator(fn: Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]):
    """Регистрируется из основного бота: set_ai_generator(ai_generate_content_en)"""
    global _AI_GEN_FN
    _AI_GEN_FN = fn

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

def _item_actions_kb(pid: int, mode: str) -> InlineKeyboardMarkup:
    # Редактирование, изменение времени, удаление, и «ИИ заполнит текст, сохранив тему/время» (только для PLAN)
    rows = [
        [
            InlineKeyboardButton("✏️ Править", callback_data=f"EDIT_ITEM:{pid}"),
            InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{pid}"),
        ],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{pid}")],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")],
    ]
    if mode == "plan":
        rows.insert(1, [InlineKeyboardButton("🤖 ИИ: дополнить текст (сохр. тему/время)", callback_data=f"AI_FILL_TEXT:{pid}")])
    return InlineKeyboardMarkup(rows)

def _edit_fields_kb(pid: int, mode: str) -> InlineKeyboardMarkup:
    rows = []
    if mode == "plan":
        rows.append([InlineKeyboardButton("📝 Тема", callback_data=f"EDIT_FIELD:topic:{pid}")])
        rows.append([InlineKeyboardButton("✍️ Текст (ручн.)", callback_data=f"EDIT_FIELD:text:{pid}")])
    else:
        rows.append([InlineKeyboardButton("✍️ Текст", callback_data=f"EDIT_FIELD:text:{pid}")])
    rows.append([InlineKeyboardButton("🖼 Картинка", callback_data=f"EDIT_FIELD:image:{pid}")])
    rows.append([InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_FIELD:time:{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ITEM_MENU:{pid}")])
    return InlineKeyboardMarkup(rows)

# -------------------------
# ХЕЛПЕРЫ СОСТОЯНИЯ
# -------------------------
def _ensure(uid: int) -> PlannedItem:
    row = USER_STATE.get(uid) or {}
    if "current" not in row:
        row["current"] = PlannedItem()
        row.setdefault("items", [])
        row.setdefault("seq", 0)
        USER_STATE[uid] = row
    return row["current"]

def _new_pid(uid: int) -> int:
    USER_STATE[uid]["seq"] = USER_STATE[uid].get("seq", 0) + 1
    return USER_STATE[uid]["seq"]

def _find_item(uid: int, pid: int) -> Optional[Dict[str, Any]]:
    for it in USER_STATE.get(uid, {}).get("items", []):
        if it.get("id") == pid:
            return it
    return None

def _push(uid: int, item: PlannedItem):
    pid = _new_pid(uid)
    USER_STATE[uid]["items"].append({
        "id": pid,
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
# ОТКРЫТИЕ ПЛАНИРОВЩИКА
# -------------------------
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    USER_STATE.setdefault(uid, {"mode": "none", "items": [], "current": PlannedItem(), "seq": 0})
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
# CALLBACKS (режимы и список)
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

def _format_item_row(i: int, it: Dict[str, Any]) -> str:
    mode = it.get("mode")
    time_s = it.get("time") or "—"
    if mode == "plan":
        return f"{i}) [PLAN] {time_s} — {it.get('topic')}"
    txt = (it.get("text") or "").strip()
    if len(txt) > 60:
        txt = txt[:57] + "…"
    img = "🖼" if it.get("image_url") else "—"
    return f"{i}) [GEN] {time_s} — {txt} {img}"

def _list_kb(uid: int) -> InlineKeyboardMarkup:
    # Кнопки «управления» по каждому элементу
    items = USER_STATE.get(uid, {}).get("items", [])
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        pid = it["id"]
        title = f"#{pid}"
        rows.append([
            InlineKeyboardButton(f"⚙️ {title}", callback_data=f"ITEM_MENU:{pid}"),
            InlineKeyboardButton("🗑", callback_data=f"DEL_ITEM:{pid}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")])
    return InlineKeyboardMarkup(rows)

async def cb_list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    items = USER_STATE.get(uid, {}).get("items", [])
    if not items:
        return await _safe_edit_or_send(q, "На сегодня пока пусто.", reply_markup=main_planner_menu())
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(_format_item_row(i, it))
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=_list_kb(uid))

# -------------------------
# ITEM MENU / EDIT / DELETE / TIME / AI_FILL
# -------------------------
async def cb_item_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка идентификатора.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())

    # Показываем краткую сводку и действия
    lines = [
        f"ID: {pid}",
        f"Режим: {it['mode']}",
        f"Время: {it.get('time') or '—'}",
    ]
    if it["mode"] == "plan":
        lines.append(f"Тема: {it.get('topic') or '—'}")
        txt = (it.get("text") or "—").strip()
        if len(txt) > 300: txt = txt[:297] + "…"
        lines.append(f"Текст: {txt}")
    else:
        txt = (it.get("text") or "—").strip()
        if len(txt) > 300: txt = txt[:297] + "…"
        lines.append(f"Текст: {txt}")
        lines.append(f"Картинка: {'есть' if it.get('image_url') else 'нет'}")

    return await _safe_edit_or_send(q, "\n".join(lines), reply_markup=_item_actions_kb(pid, it["mode"]))

async def cb_delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID для удаления.", reply_markup=main_planner_menu())
    items = USER_STATE.get(uid, {}).get("items", [])
    USER_STATE[uid]["items"] = [x for x in items if x.get("id") != pid]
    return await _safe_edit_or_send(q, f"Удалено #{pid}.", reply_markup=main_planner_menu())

async def cb_edit_time_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Быстрая смена времени из меню элемента
    q = update.callback_query
    uid = q.from_user.id
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    st = _ensure(uid)
    st.step = "editing_time"
    st.mode = "edit"
    USER_STATE[uid]["edit_target"] = pid
    return await _safe_edit_or_send(q, "Введите новое время в формате <b>HH:MM</b> (Киев).", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад к элементу", callback_data=f"ITEM_MENU:{pid}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
    ]))

async def cb_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID для редактирования.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    return await _safe_edit_or_send(q, "Что меняем?", reply_markup=_edit_fields_kb(pid, it["mode"]))

async def cb_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    try:
        _, field, pid_s = q.data.split(":", 2)
        pid = int(pid_s)
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка выбора поля.", reply_markup=main_planner_menu())

    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())

    st = _ensure(uid)
    USER_STATE[uid]["edit_target"] = pid

    if field == "topic":
        st.step = "editing_topic"
        return await _safe_edit_or_send(q, "Введите новую тему:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "text":
        st.step = "editing_text"
        return await _safe_edit_or_send(q, "Пришлите новый текст поста:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "image":
        st.step = "editing_image"
        return await _safe_edit_or_send(q, "Пришлите новую картинку <i>(как фото или документ)</i> или отправьте «удалить», чтобы убрать картинку.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "time":
        st.step = "editing_time"
        return await _safe_edit_or_send(q, "Введите новое время в формате <b>HH:MM</b> (Киев).", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    return await _safe_edit_or_send(q, "Неизвестное поле.", reply_markup=main_planner_menu())

async def cb_ai_fill_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ИИ-дозаполнение текста (оставляя тему/время)
    q = update.callback_query
    uid = q.from_user.id
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    if it["mode"] != "plan":
        return await _safe_edit_or_send(q, "ИИ-дополнение доступно только для позиций с режимом PLAN.", reply_markup=main_planner_menu())
    if _AI_GEN_FN is None:
        return await _safe_edit_or_send(q, "Генератор ИИ не подключён.", reply_markup=main_planner_menu())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        # Сохраняем текст (+хэштеги в самом тексте — как в кнопке AI build now), картинку — тоже
        it["text"] = f"{text_en}\n\n{' '.join(tags)}".strip()
        if img:
            it["image_url"] = img
        return await _safe_edit_or_send(q, "Текст дополнён ИИ (тема/время сохранены).", reply_markup=_item_actions_kb(pid, it["mode"]))
    except Exception:
        return await _safe_edit_or_send(q, "Не удалось сгенерировать текст ИИ.", reply_markup=_item_actions_kb(pid, it["mode"]))

# -------------------------
# CALLBACKS (шаги завершения)
# -------------------------
async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    USER_STATE.setdefault(uid, {"items": [], "current": PlannedItem(), "seq": 0})
    USER_STATE[uid]["current"] = PlannedItem()  # полный сброс текущего шага
    USER_STATE[uid].pop("edit_target", None)
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

    if _AI_GEN_FN is None:
        return await _safe_edit_or_send(
            q,
            "Не подключён ИИ-генератор из основного бота. Можно продолжить вручную.",
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
            text_en, tags, img = await _AI_GEN_FN(th)
            USER_STATE[uid]["items"].append({
                "id": _new_pid(uid),
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
        f"Сгенерировано позиций: <b>{created}</b>.\nТеперь добавьте время для нужных задач или редактируйте через «Список на сегодня».",
        reply_markup=main_planner_menu()
    )

# -------------------------
# INPUT (текст/фото) ПО ШАГАМ + РЕДАКТИРОВАНИЕ
# -------------------------
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод только когда пользователь в режиме планировщика/редактирования."""
    uid = update.effective_user.id
    st = _ensure(uid)
    # Режимы, в которых мы «перехватываем» сообщение
    active_steps = {
        "waiting_topic", "waiting_text", "waiting_time",
        "editing_time", "editing_text", "editing_topic", "editing_image"
    }
    if (st.mode not in ("plan", "gen", "edit")) and (st.step not in active_steps):
        return  # не наш режим — пусть основное приложение обработает

    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()

    # --- Блок редактирования существующих элементов ---
    if st.step == "editing_topic":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text:
            return await msg.reply_text("Нужна новая тема текстом.")
        it["topic"] = text
        st.step = "idle"; st.mode = "none"
        USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Тема обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if st.step == "editing_text":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text:
            return await msg.reply_text("Нужен новый текст.")
        it["text"] = text
        st.step = "idle"; st.mode = "none"
        USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Текст обновлён для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if st.step == "editing_image":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())

        # удалить картинку ключевым словом
        if text.lower() in {"удалить", "delete", "none", "remove"}:
            it["image_url"] = None
            st.step = "idle"; st.mode = "none"
            USER_STATE[uid].pop("edit_target", None)
            return await msg.reply_text(f"Картинка удалена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

        # фото как фото
        if msg.photo:
            it["image_url"] = msg.photo[-1].file_id
        # фото как документ
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                it["image_url"] = msg.document.file_id

        if not it.get("image_url"):
            return await msg.reply_text("Пришлите фото или отправьте «удалить».")
        st.step = "idle"; st.mode = "none"
        USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Картинка обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if st.step == "editing_time":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("Неверный формат. Пример: 14:30")
        it["time"] = f"{int(hh):02d}:{int(mm):02d}"
        st.step = "idle"; st.mode = "none"
        USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Время обновлено для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    # --- Обычные шаги создания ---
    if st.step == "waiting_topic":
        if not text:
            return await msg.reply_text("Нужна тема текстом. Попробуй ещё раз.", reply_markup=cancel_only())
        st.topic = text
        fake_cb = await update.to_callback_query(context.bot)
        if fake_cb:
            return await _ask_time(fake_cb)
        st.step = "waiting_time"
        return await msg.reply_text(
            "Введите время публикации в формате <b>HH:MM</b> по Киеву (например, 14:30).",
            reply_markup=cancel_only(),
            parse_mode="HTML"
        )

    if st.step == "waiting_text":
        if msg.photo:
            st.image_url = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                st.image_url = msg.document.file_id
        if text:
            st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("Пришлите текст поста и/или фото.", reply_markup=cancel_only())
        fake_cb = await update.to_callback_query(context.bot)
        if fake_cb:
            return await _ask_time(fake_cb)
        st.step = "waiting_time"
        return await msg.reply_text(
            "Введите время публикации в формате <b>HH:MM</b> по Киеву (например, 14:30).",
            reply_markup=cancel_only(),
            parse_mode="HTML"
        )

    if st.step == "waiting_time":
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("Неверный формат. Пример: 14:30", reply_markup=cancel_only())
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"

        fake_cb = await update.to_callback_query(context.bot)
        if fake_cb:
            return await _show_ready_add_cancel(fake_cb)

        prefix = "PLAN_" if st.mode == "plan" else "GEN_"
        lines: List[str] = []
        if st.mode == "plan":
            lines.append(f"Тема: {st.topic or '—'}")
        else:
            t = (st.text or "—").strip()
            if len(t) > 400: t = t[:397] + "…"
            lines.append(f"Текст: {t}")
            lines.append(f"Картинка: {'есть' if st.image_url else 'нет'}")
        lines.append(f"Время: {st.time_str or '—'}")

        return await msg.reply_text(
            "Проверьте данные:\n" + "\n".join(lines),
            reply_markup=step_buttons_done_add_cancel(prefix),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

# -------------------------
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# -------------------------
def register_planner_handlers(app: Application):
    # Режимы
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode,    pattern="^OPEN_PLAN_MODE$"),    group=0)
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,     pattern="^OPEN_GEN_MODE$"),     group=0)
    app.add_handler(CallbackQueryHandler(cb_list_today,        pattern="^PLAN_LIST_TODAY$"),   group=0)
    app.add_handler(CallbackQueryHandler(cb_plan_ai_build_now, pattern="^PLAN_AI_BUILD_NOW$"), group=0)

    # Навигация
    app.add_handler(CallbackQueryHandler(cb_step_back,         pattern="^STEP_BACK$"),         group=0)
    app.add_handler(CallbackQueryHandler(cb_back_main_menu,    pattern="^BACK_MAIN_MENU$"),    group=0)

    # Завершение шагов
    app.add_handler(CallbackQueryHandler(cb_plan_done,         pattern="^PLAN_DONE$"),         group=0)
    app.add_handler(CallbackQueryHandler(cb_gen_done,          pattern="^GEN_DONE$"),          group=0)
    app.add_handler(CallbackQueryHandler(cb_add_more,          pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$"), group=0)

    # Управление элементами
    app.add_handler(CallbackQueryHandler(cb_item_menu,         pattern="^ITEM_MENU:\\d+$"),      group=0)
    app.add_handler(CallbackQueryHandler(cb_delete_item,       pattern="^DEL_ITEM:\\d+$"),       group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_time_shortcut,pattern="^EDIT_TIME:\\d+$"),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_item,         pattern="^EDIT_ITEM:\\d+$"),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_field,        pattern="^EDIT_FIELD:(topic|text|image|time):\\d+$"), group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_fill_text,      pattern="^AI_FILL_TEXT:\\d+$"),   group=0)

    # Ввод пользователем — перехватываем, когда мы реально в шагах/редактировании
    app.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, on_user_message),
        group=0
    )

# -------------------------
# FAKE CallbackQuery (для унификации шагов)
# -------------------------
from typing import Optional as _Optional

async def _build_fake_callback_from_message(message: Message, bot) -> CallbackQuery:
    cq = CallbackQuery(
        id="fake",
        from_user=message.from_user,
        chat_instance="",
        message=message,
        bot=bot
    )
    return cq

async def _update_to_callback_query(update: Update, bot) -> _Optional[CallbackQuery]:
    if update.callback_query:
        return update.callback_query
    if update.message:
        return await _build_fake_callback_from_message(update.message, bot)
    return None

setattr(Update, "to_callback_query", _update_to_callback_query)