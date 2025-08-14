# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, Awaitable, Tuple

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, Message, CallbackQuery
)
# NOTE: Update is extended at the end of file with .to_callback_query helper
from telegram.ext import (
    Application, CallbackQueryHandler, MessageHandler, ContextTypes, filters
)
from telegram.error import BadRequest

# =========================
# ЛОГИРОВАНИЕ
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s",
)
log = logging.getLogger("planner")

def _lg(msg: str):
    log.info(f"PLNR> {msg}")

# =========================
# АКТОР/ЧАТ: нормализация uid и фильтр чата
# =========================
GROUP_ANON_UID = 1087968824   # Telegram GroupAnonymousBot (анонимный админ)
TG_SERVICE_UID = 777000       # Telegram service

_admin_env = os.getenv("APPROVAL_ADMIN_UID") or os.getenv("PLANNER_ADMIN_UID")
try:
    ADMIN_UID: Optional[int] = int(_admin_env) if _admin_env else None
except Exception:
    ADMIN_UID = None

_chat_env = os.getenv("TELEGRAM_APPROVAL_CHAT_ID") or os.getenv("PLANNER_APPROVAL_CHAT_ID")
try:
    APPROVAL_CHAT_ID: Optional[int] = int(_chat_env) if _chat_env else None
except Exception:
    APPROVAL_CHAT_ID = None

def _norm_uid(raw_uid: int) -> int:
    """Склеиваем анонимного админа/служебные uid с реальным админом (если задан)."""
    if raw_uid in (GROUP_ANON_UID, TG_SERVICE_UID) and ADMIN_UID:
        return ADMIN_UID
    return raw_uid

def _allowed_chat(update: Update) -> bool:
    """Если задан APPROVAL_CHAT_ID — работаем только в этом чате."""
    if APPROVAL_CHAT_ID is None:
        return True
    ch = update.effective_chat
    return bool(ch and ch.id == APPROVAL_CHAT_ID)

def _uid_from_update(update: Update) -> int:
    u = update.effective_user
    raw = u.id if u else (ADMIN_UID or 0)
    return _norm_uid(raw)

def _uid_from_q(q: CallbackQuery) -> int:
    return _norm_uid(q.from_user.id)

# =========================
# ПАМЯТЬ СЕССИЙ ПЛАНИРОВЩИКА
# =========================
USER_STATE: Dict[int, Dict[str, Any]] = {}

@dataclass
class PlannedItem:
    topic: Optional[str] = None
    text: Optional[str] = None
    time_str: Optional[str] = None
    image_url: Optional[str] = None
    step: str = "idle"   # idle | waiting_topic | waiting_text | waiting_time | editing_*
    mode: str = "none"   # plan | gen | edit

# =========================
# БАЗА ДАННЫХ (только план)
# =========================
DB_FILE = os.getenv("PLANNER_DB_FILE", "planner_posts.db")

def _db_init():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planned_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,         -- 'plan' | 'gen'
            topic TEXT,                 -- для plan
            text  TEXT,                 -- итоговый текст
            time_str TEXT,              -- HH:MM (Киев)
            image_url TEXT,             -- file_id или URL
            status TEXT NOT NULL DEFAULT 'planned', -- planned | posted | canceled
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_planned_status ON planned_posts(status)")
    conn.commit()
    conn.close()

def db_insert_item(user_id: int, it: Dict[str, Any]) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO planned_posts (user_id, mode, topic, text, time_str, image_url, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)
    """, (
        user_id,
        it.get("mode"),
        it.get("topic"),
        it.get("text"),
        it.get("time"),
        it.get("image_url"),
        datetime.utcnow().isoformat() + "Z"
    ))
    rowid = cur.lastrowid
    conn.commit()
    conn.close()
    return int(rowid)

def db_update_item(pid: int, fields: Dict[str, Any]) -> None:
    if not fields: return
    sets = ", ".join(f"{k} = ?" for k in fields.keys())
    vals = list(fields.values()) + [pid]
    conn = sqlite3.connect(DB_FILE)
    conn.execute(f"UPDATE planned_posts SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()

def db_delete_item(pid: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM planned_posts WHERE id = ?", (pid,))
    conn.commit()
    conn.close()

# =========================
# РЕГИСТРАТОР ИИ-ГЕНЕРАТОРА
# =========================
_AI_GEN_FN: Optional[
    Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]
] = None

def set_ai_generator(fn: Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]):
    """Регистрируется из основного бота: set_ai_generator(ai_generate_content_en)"""
    global _AI_GEN_FN
    _AI_GEN_FN = fn
    _lg("ai_generator registered")

# =========================
# КНОПКИ
# =========================
def main_planner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 ИИ план (тема → текст → время)", callback_data="OPEN_PLAN_MODE")],
        [InlineKeyboardButton("✨ Мой план (текст/фото → время)", callback_data="OPEN_GEN_MODE")],
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
        rows.insert(1, [InlineKeyboardButton("🤖 ИИ: дополнить текст", callback_data=f"AI_FILL_TEXT:{pid}")])
        rows.insert(2, [InlineKeyboardButton("🤖 ИИ: новый пост (та же тема/время)", callback_data=f"AI_NEW_FROM:{pid}")])
        rows.insert(3, [InlineKeyboardButton("➕ Клон (та же тема/время)", callback_data=f"CLONE_ITEM:{pid}")])
    else:
        rows.insert(1, [InlineKeyboardButton("➕ Клон (то же время)", callback_data=f"CLONE_ITEM:{pid}")])
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

# =========================
# ХЕЛПЕРЫ СОСТОЯНИЯ
# =========================
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
    row = {
        "id": pid,
        "mode": item.mode,
        "topic": item.topic,
        "text": item.text,
        "time": item.time_str,
        "image_url": item.image_url,
        "added_at": datetime.utcnow().isoformat() + "Z"
    }
    _lg(f"push -> items[{pid}] mode={row['mode']} time={row['time']} topic_len={len(row.get('topic') or '')} text_len={len(row.get('text') or '')} img={bool(row.get('image_url'))}")
    USER_STATE[uid]["items"].append(row)
    try:
        db_insert_item(uid, {
            "mode": row["mode"],
            "topic": row["topic"],
            "text": row["text"],
            "time": row["time"],
            "image_url": row["image_url"],
        })
    except Exception as e:
        _lg(f"db_insert_item failed: {e}")
    USER_STATE[uid]["current"] = PlannedItem()

def _can_finalize(item: PlannedItem) -> bool:
    if not item.time_str:
        return False
    if item.mode == "plan":
        return bool(item.topic)  # текст может быть автосгенерирован заранее
    if item.mode == "gen":
        return bool(item.text or item.image_url)
    return False

# =========================
# БЕЗОПАСНОЕ РЕДАКТИРОВАНИЕ
# =========================
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

# =========================
# OPENAI: ПРОВЕРКА ДОСТУПНОСТИ/КВОТЫ
# =========================
def _openai_key_present() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))

async def _openai_usable() -> bool:
    if not _openai_key_present():
        _lg("openai not present")
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
        _lg("openai usable OK")
        return True
    except Exception as e:
        _lg(f"openai unusable: {e}")
        return False

# =========================
# ОТКРЫТИЕ ПЛАНИРОВЩИКА
# =========================
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    _db_init()
    q = update.callback_query
    uid = _uid_from_update(update)
    USER_STATE.setdefault(uid, {"mode": "none", "items": [], "current": PlannedItem(), "seq": 0})
    _lg(f"open_planner uid={uid}")
    if q:
        await _safe_edit_or_send(q, "[ПЛАНИРОВЩИК] Выбери режим.", reply_markup=main_planner_menu())
    else:
        await context.bot.send_message(update.effective_chat.id, "[ПЛАНИРОВЩИК] Выбери режим.",
                                       reply_markup=main_planner_menu())

# =========================
# ПРОСЬБЫ/ШАГИ
# =========================
async def _ask_topic(q: CallbackQuery, mode: str):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.mode = mode
    st.step = "waiting_topic"
    _lg(f"ask_topic uid={uid} -> mode={mode} step={st.step}")
    await _safe_edit_or_send(
        q,
        "[PLAN] Введи <b>тему</b> для поста.\n"
        "Если ИИ доступен — я сгенерирую текст и сразу попрошу время публикации.\n"
        "Если ИИ недоступен — сразу перейдём к выбору времени.",
        reply_markup=cancel_only()
    )

async def _ask_text(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.mode = "gen"
    st.step = "waiting_text"
    _lg(f"ask_text uid={uid} -> mode=gen step={st.step}")
    await _safe_edit_or_send(
        q,
        "[GEN] Пришли текст поста и/или фото (можно одним сообщением — фото с подписью). Затем попрошу время публикации.",
        reply_markup=cancel_only()
    )

async def _ask_time(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.step = "waiting_time"
    _lg(f"ask_time via callback uid={uid} step={st.step} mode={st.mode}")
    await _safe_edit_or_send(
        q, "[*] Введи время публикации в формате <b>HH:MM</b> (Киев). Например, 14:30.",
        reply_markup=cancel_only()
    )

async def _ask_time_via_msg(msg: Message):
    uid = _norm_uid(msg.from_user.id)
    st = _ensure(uid)
    st.step = "waiting_time"
    _lg(f"ask_time via message uid={uid} step={st.step} mode={st.mode}")
    await msg.reply_text(
        "[*] Введи время публикации в формате <b>HH:MM</b> (Киев). Например, 14:30.",
        reply_markup=cancel_only(),
        parse_mode="HTML"
    )

async def _show_ready_add_cancel(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    prefix = "PLAN_" if st.mode == "plan" else "GEN_"
    lines: List[str] = []
    if st.mode == "plan":
        lines.append(f"Тема: {st.topic or '—'}")
        txt = (st.text or "—").strip()
        if len(txt) > 400: txt = txt[:397] + "…"
        lines.append(f"Текст: {txt}")
    else:
        text = (st.text or "—").strip()
        if len(text) > 400: text = text[:397] + "…"
        lines.append(f"Текст: {text}")
        lines.append(f"Картинка: {'есть' if st.image_url else 'нет'}")
    lines.append(f"Время: {st.time_str or '—'}")
    _lg(f"ready uid={uid} mode={st.mode} time={st.time_str} topic_len={len(st.topic or '')} text_len={len(st.text or '')} img={bool(st.image_url)}")
    await _safe_edit_or_send(
        q, "Проверь данные:\n" + "\n".join(lines),
        reply_markup=step_buttons_done_add_cancel(prefix)
    )

# =========================
# CALLBACKS (режимы и список)
# =========================
async def cb_open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    _lg(f"cb_open_plan_mode uid={_uid_from_q(q)}")
    usable = await _openai_usable()
    if not usable:
        try:
            await q.message.chat.send_message(
                "⚠️ OpenAI сейчас недоступен — продолжим в ветке [PLAN] без автогенерации.",
                reply_markup=cancel_only(), parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception:
            pass
    await _ask_topic(q, mode="plan")

async def cb_open_gen_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    _lg(f"cb_open_gen_mode uid={_uid_from_q(update.callback_query)}")
    await _ask_text(update.callback_query)

def _format_item_row(i: int, it: Dict[str, Any]) -> str:
    mode = it.get("mode")
    time_s = it.get("time") or "—"
    if mode == "plan":
        txt = (it.get("topic") or "—")
        return f"{i}) [PLAN] {time_s} — {txt}"
    t = (it.get("text") or "").strip()
    if len(t) > 60: t = t[:57] + "…"
    img = "🖼" if it.get("image_url") else "—"
    return f"{i}) [GEN] {time_s} — {t} {img}"

def _list_kb(uid: int) -> InlineKeyboardMarkup:
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
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    _lg(f"cb_list_today uid={uid}")
    items = USER_STATE.get(uid, {}).get("items", [])
    if not items:
        return await _safe_edit_or_send(q, "На сегодня пока пусто.", reply_markup=main_planner_menu())
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(_format_item_row(i, it))
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=_list_kb(uid))

# =========================
# ITEM MENU / EDIT / DELETE / TIME / AI_FILL / CLONE / AI_NEW_FROM
# =========================
async def cb_item_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка идентификатора.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    _lg(f"cb_item_menu uid={uid} pid={pid} found={bool(it)}")
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())

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
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID для удаления.", reply_markup=main_planner_menu())
    items = USER_STATE.get(uid, {}).get("items", [])
    USER_STATE[uid]["items"] = [x for x in items if x.get("id") != pid]
    try:
        db_delete_item(pid)
    except Exception:
        pass
    _lg(f"cb_delete_item uid={uid} pid={pid} -> ok")
    return await _safe_edit_or_send(q, f"Удалено #{pid}.", reply_markup=main_planner_menu())

async def cb_edit_time_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    st = _ensure(uid)
    st.step = "editing_time"
    st.mode = "edit"
    USER_STATE[uid]["edit_target"] = pid
    _lg(f"cb_edit_time_shortcut uid={uid} pid={pid} -> step=editing_time")
    return await _safe_edit_or_send(q, "Введите новое время в формате <b>HH:MM</b> (Киев).", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад к элементу", callback_data=f"ITEM_MENU:{pid}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
    ]))

async def cb_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID для редактирования.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    _lg(f"cb_edit_item uid={uid} pid={pid} found={bool(it)}")
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    return await _safe_edit_or_send(q, "Что меняем?", reply_markup=_edit_fields_kb(pid, it["mode"]))

async def cb_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        _, field, pid_s = q.data.split(":", 2)
        pid = int(pid_s)
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка выбора поля.", reply_markup=main_planner_menu())

    it = _find_item(uid, pid)
    _lg(f"cb_edit_field uid={uid} pid={pid} field={field} found={bool(it)}")
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())

    st = _ensure(uid)
    USER_STATE[uid]["edit_target"] = pid

    if field == "topic":
        st.step = "editing_topic"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Введите новую тему:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "text":
        st.step = "editing_text"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Пришлите новый текст поста:", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "image":
        st.step = "editing_image"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Пришлите новую картинку <i>(как фото или документ)</i> или отправьте «удалить».", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    if field == "time":
        st.step = "editing_time"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Введите новое время в формате <b>HH:MM</b> (Киев).", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
        ]))
    return await _safe_edit_or_send(q, "Неизвестное поле.", reply_markup=main_planner_menu())

async def cb_ai_fill_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    if it["mode"] != "plan":
        return await _safe_edit_or_send(q, "ИИ-дополнение доступно только для PLAN.", reply_markup=main_planner_menu())
    if _AI_GEN_FN is None:
        return await _safe_edit_or_send(q, "Генератор ИИ не подключён.", reply_markup=main_planner_menu())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        it["text"] = f"{text_en}\n\n{' '.join(tags)}".strip()
        if img:
            it["image_url"] = img
        try:
            db_update_item(pid, {"text": it["text"], "image_url": it.get("image_url")})
        except Exception:
            pass
        _lg(f"ai_fill_text uid={uid} pid={pid} ok")
        return await _safe_edit_or_send(q, "Текст дополнён ИИ (тема/время сохранены).", reply_markup=_item_actions_kb(pid, it["mode"]))
    except Exception as e:
        _lg(f"ai_fill_text uid={uid} pid={pid} failed: {e}")
        return await _safe_edit_or_send(q, "Не удалось сгенерировать текст ИИ.", reply_markup=_item_actions_kb(pid, it["mode"]))

async def cb_clone_item(update: Update, ContextTypes=None):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID для клонирования.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден для клона.", reply_markup=main_planner_menu())

    nid = _new_pid(uid)
    clone = {
        "id": nid,
        "mode": it["mode"],
        "topic": it.get("topic"),
        "text": None,
        "time": it.get("time"),
        "image_url": None,
        "added_at": datetime.utcnow().isoformat() + "Z"
    }
    USER_STATE[uid]["items"].append(clone)
    try:
        db_insert_item(uid, {
            "mode": clone["mode"],
            "topic": clone["topic"],
            "text": clone["text"],
            "time": clone["time"],
            "image_url": clone["image_url"],
        })
    except Exception:
        pass
    _lg(f"clone uid={uid} src={pid} -> new={nid}")
    return await _safe_edit_or_send(q, f"Создан клон #{nid} (сохр. тему/время).", reply_markup=_item_actions_kb(nid, it["mode"]))

async def cb_ai_new_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        pid = int(q.data.split(":", 1)[1])
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it:
        return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    if it["mode"] != "plan":
        return await _safe_edit_or_send(q, "Доступно только для PLAN.", reply_markup=main_planner_menu())
    if _AI_GEN_FN is None:
        return await _safe_edit_or_send(q, "Генератор ИИ не подключён.", reply_markup=main_planner_menu())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        nid = _new_pid(uid)
        newrow = {
            "id": nid,
            "mode": "plan",
            "topic": topic,
            "text": f"{text_en}\n\n{' '.join(tags)}".strip(),
            "time": it.get("time"),
            "image_url": img,
            "added_at": datetime.utcnow().isoformat() + "Z"
        }
        USER_STATE[uid]["items"].append(newrow)
        try:
            db_insert_item(uid, {
                "mode": newrow["mode"],
                "topic": newrow["topic"],
                "text": newrow["text"],
                "time": newrow["time"],
                "image_url": newrow["image_url"],
            })
        except Exception:
            pass
        _lg(f"ai_new_from uid={uid} src={pid} -> new={nid}")
        return await _safe_edit_or_send(q, f"Создан новый пост #{nid} (ИИ-текст, тема/время сохранены).", reply_markup=_item_actions_kb(nid, "plan"))
    except Exception as e:
        _lg(f"ai_new_from uid={uid} pid={pid} failed: {e}")
        return await _safe_edit_or_send(q, "Не удалось сгенерировать новый ИИ-текст.", reply_markup=_item_actions_kb(pid, "plan"))

# =========================
# CALLBACKS (шаги завершения)
# =========================
async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    USER_STATE.setdefault(uid, {"items": [], "current": PlannedItem(), "seq": 0})
    USER_STATE[uid]["current"] = PlannedItem()
    USER_STATE[uid].pop("edit_target", None)
    _lg(f"step_back uid={uid}")
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=main_planner_menu())

async def _finalize_current_and_back(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    if _can_finalize(st):
        _lg(f"finalize uid={uid} mode={st.mode} time={st.time_str}")
        _push(uid, st)
        return await _safe_edit_or_send(q, "Сохранено. Что дальше?", reply_markup=main_planner_menu())
    else:
        _lg(f"finalize uid={uid} failed: incomplete step={st.step} mode={st.mode}")
        return await _safe_edit_or_send(q, "Нечего сохранять — заполни данные и время.", reply_markup=main_planner_menu())

async def cb_plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    await _finalize_current_and_back(update.callback_query)

async def cb_gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    await _finalize_current_and_back(update.callback_query)

async def cb_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    st = _ensure(uid)
    if _can_finalize(st):
        _push(uid, st)
    _lg(f"add_more uid={uid} mode={st.mode}")
    if st.mode == "plan":
        await _ask_topic(q, mode="plan")
    else:
        await _ask_text(q)

# =========================
# AI build now (по кнопке)
# =========================
async def cb_plan_ai_build_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    q = update.callback_query
    uid = _uid_from_q(q)
    usable = await _openai_usable()
    _lg(f"ai_build_now uid={uid} openai_usable={usable}")

    if not usable:
        return await _safe_edit_or_send(
            q,
            "❗ <b>OpenAI недоступен или квота исчерпана</b>.\n"
            "Можно продолжить вручную (ветка [GEN]) или вернуться позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Мой план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    if _AI_GEN_FN is None:
        return await _safe_edit_or_send(
            q,
            "Не подключён ИИ-генератор из основного бота. Можно продолжить вручную.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Мой план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    topics = [
        "Utility, community growth and joining early.",
        "Governance & on-chain voting with AI analysis.",
        "AI-powered proposals and speed of execution."
    ]
    _ensure(uid)

    created = 0
    for th in topics:
        try:
            text_en, tags, img = await _AI_GEN_FN(th)
            row = {
                "id": _new_pid(uid),
                "mode": "plan",
                "topic": th,
                "text": f"{text_en}\n\n{' '.join(tags)}".strip(),
                "time": None,
                "image_url": img,
                "added_at": datetime.utcnow().isoformat() + "Z"
            }
            USER_STATE[uid]["items"].append(row)
            try:
                db_insert_item(uid, {
                    "mode": "plan",
                    "topic": th,
                    "text": row["text"],
                    "time": row["time"],
                    "image_url": img,
                })
            except Exception:
                pass
            created += 1
        except Exception:
            pass

    if created == 0:
        _lg(f"ai_build_now uid={uid} created=0")
        return await _safe_edit_or_send(
            q,
            "Не удалось сгенерировать план. Попробуй позже или перейди в ручной режим.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Мой план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    _lg(f"ai_build_now uid={uid} created={created}")
    return await _safe_edit_or_send(
        q,
        f"Сгенерировано позиций: <b>{created}</b>.\nТеперь добавь время для нужных задач или отредактируй через «Список на сегодня».",
        reply_markup=main_planner_menu()
    )

# =========================
# INPUT (текст/фото) + РЕДАКТ
# =========================
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватываем ввод только когда реально на шаге планировщика."""
    if not _allowed_chat(update):
        return
    uid = _uid_from_update(update)
    st = _ensure(uid)
    active_steps = {
        "waiting_topic", "waiting_text", "waiting_time",
        "editing_time", "editing_text", "editing_topic", "editing_image"
    }
    step = st.step
    mode = st.mode
    _lg(f"on_user_message uid={uid} step={step} mode={mode}")
    if (mode not in ("plan", "gen", "edit")) and (step not in active_steps):
        _lg(f"on_user_message uid={uid} ignored (not in planner flow)")
        return

    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()

    # --- РЕДАКТИРОВАНИЕ ---
    if step == "editing_topic":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"; st.mode = "none"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text:
            return await msg.reply_text("Нужна новая тема текстом.")
        it["topic"] = text
        try: db_update_item(pid, {"topic": text})
        except Exception: pass
        st.step = "idle"; st.mode = "none"; USER_STATE[uid].pop("edit_target", None)
        _lg(f"edit topic uid={uid} pid={pid} -> ok")
        return await msg.reply_text(f"Тема обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_text":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"; st.mode = "none"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text:
            return await msg.reply_text("Нужен новый текст.")
        it["text"] = text
        try: db_update_item(pid, {"text": text})
        except Exception: pass
        st.step = "idle"; st.mode = "none"; USER_STATE[uid].pop("edit_target", None)
        _lg(f"edit text uid={uid} pid={pid} -> ok")
        return await msg.reply_text(f"Текст обновлён для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_image":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"; st.mode = "none"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())

        if text.lower() in {"удалить", "delete", "none", "remove"}:
            it["image_url"] = None
            try: db_update_item(pid, {"image_url": None})
            except Exception: pass
            st.step = "idle"; st.mode = "none"; USER_STATE[uid].pop("edit_target", None)
            _lg(f"edit image delete uid={uid} pid={pid} -> ok")
            return await msg.reply_text(f"Картинка удалена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

        if msg.photo:
    st.image_url = msg.photo[-1].file_id
if getattr(msg, 'video', None):
    st.image_url = msg.video.file_id
if getattr(msg, 'document', None) and getattr(msg.document, 'mime_type', ''):
    if msg.document.mime_type.startswith('image/') or msg.document.mime_type.startswith('video/'):
        st.image_url = msg.document.file_id
            it["image_url"] = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                it["image_url"] = msg.document.file_id

        if not it.get("image_url"):
            return await msg.reply_text("Пришлите фото или отправьте «удалить».")
        try: db_update_item(pid, {"image_url": it["image_url"]})
        except Exception: pass
        st.step = "idle"; st.mode = "none"; USER_STATE[uid].pop("edit_target", None)
        _lg(f"edit image uid={uid} pid={pid} -> ok")
        return await msg.reply_text(f"Картинка обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_time":
        pid = USER_STATE[uid].get("edit_target")
        it = _find_item(uid, pid) if pid else None
        if not it:
            st.step = "idle"; st.mode = "none"
            return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("Неверный формат. Пример: 14:30")
        it["time"] = f"{int(hh):02d}:{int(mm):02d}"
        try: db_update_item(pid, {"time_str": it["time"]})
        except Exception: pass
        st.step = "idle"; st.mode = "none"; USER_STATE[uid].pop("edit_target", None)
        _lg(f"edit time uid={uid} pid={pid} -> {it['time']}")
        return await msg.reply_text(f"Время обновлено для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    # --- СОЗДАНИЕ ---
    if step == "waiting_topic":
        st.mode = "plan"
        if not text:
            return await msg.reply_text("[PLAN] Нужна тема текстом. Попробуй ещё раз.", reply_markup=cancel_only())
        st.topic = text
        _lg(f"topic set uid={uid} len={len(text)}")

        try:
            usable = await _openai_usable()
        except Exception:
            usable = False

        if (_AI_GEN_FN is not None) and usable:
            try:
                text_en, tags, img = await _AI_GEN_FN(st.topic)
                st.text = f"{text_en}\n\n{' '.join(tags)}".strip()
                if img:
                    st.image_url = img
                _lg(f"ai prefill uid={uid} ok text_len={len(st.text)} img={bool(st.image_url)}")
            except Exception as e:
                _lg(f"ai prefill uid={uid} failed: {e}")
                st.text = st.text or ""

        await _ask_time_via_msg(msg)
        return

    if step == "waiting_text":
        st.mode = "gen"
        if msg.photo:
    st.image_url = msg.photo[-1].file_id
if getattr(msg, 'video', None):
    st.image_url = msg.video.file_id
if getattr(msg, 'document', None) and getattr(msg.document, 'mime_type', ''):
    if msg.document.mime_type.startswith('image/') or msg.document.mime_type.startswith('video/'):
        st.image_url = msg.document.file_id
            st.image_url = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                st.image_url = msg.document.file_id
        if text:
            st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("[GEN] Пришли текст поста и/или фото.", reply_markup=cancel_only())
        _lg(f"text/image captured uid={uid} text_len={len(st.text or '')} img={bool(st.image_url)}")
        await _ask_time_via_msg(msg)
        return

    if step == "waiting_time":
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("[*] Неверный формат. Пример: 14:30", reply_markup=cancel_only())
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"
        _lg(f"time set uid={uid} -> {st.time_str} mode={st.mode}")

        prefix = "PLAN_" if st.mode == "plan" else "GEN_"
        lines: List[str] = []
        if st.mode == "plan":
            lines.append(f"Тема: {st.topic or '—'}")
            t = (st.text or "—").strip()
            if len(t) > 400: t = t[:397] + "…"
            lines.append(f"Текст: {t}")
        else:
            t = (st.text or "—").strip()
            if len(t) > 400: t = t[:397] + "…"
            lines.append(f"Текст: {t}")
            lines.append(f"Картинка: {'есть' if st.image_url else 'нет'}")
        lines.append(f"Время: {st.time_str or '—'}")

        return await msg.reply_text(
            "Проверь данные:\n" + "\n".join(lines),
            reply_markup=step_buttons_done_add_cancel(prefix),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

# =========================
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ (block=True!)
# =========================
def register_planner_handlers(app: Application):
    _db_init()
    _lg("register handlers")

    # Режимы
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode,    pattern="^OPEN_PLAN_MODE$", block=True),    group=0)
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,     pattern="^OPEN_GEN_MODE$", block=True),     group=0)
    app.add_handler(CallbackQueryHandler(cb_list_today,        pattern="^PLAN_LIST_TODAY$", block=True),   group=0)
    app.add_handler(CallbackQueryHandler(cb_plan_ai_build_now, pattern="^PLAN_AI_BUILD_NOW$", block=True), group=0)

    # Навигация внутри планировщика
    app.add_handler(CallbackQueryHandler(cb_step_back,         pattern="^STEP_BACK$", block=True),         group=0)
    # ВАЖНО: не перехватываем BACK_MAIN_MENU — пусть его обрабатывает основной бот для мгновенного выхода.

    # Завершение шагов
    app.add_handler(CallbackQueryHandler(cb_plan_done,         pattern="^PLAN_DONE$", block=True),         group=0)
    app.add_handler(CallbackQueryHandler(cb_gen_done,          pattern="^GEN_DONE$", block=True),          group=0)
    app.add_handler(CallbackQueryHandler(cb_add_more,          pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$", block=True), group=0)

    # Управление элементами
    app.add_handler(CallbackQueryHandler(cb_item_menu,         pattern="^ITEM_MENU:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_delete_item,       pattern="^DEL_ITEM:\\d+$", block=True),       group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_time_shortcut,pattern="^EDIT_TIME:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_item,         pattern="^EDIT_ITEM:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_field,        pattern="^EDIT_FIELD:(topic|text|image|time):\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_fill_text,      pattern="^AI_FILL_TEXT:\\d+$", block=True),   group=0)
    app.add_handler(CallbackQueryHandler(cb_clone_item,        pattern="^CLONE_ITEM:\\d+$", block=True),     group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_new_from,       pattern="^AI_NEW_FROM:\\d+$", block=True),    group=0)

    # Пользовательский ввод на шагах/редактировании
    chat_filter = filters.ALL
    if APPROVAL_CHAT_ID is not None:
        try:
            chat_filter = filters.Chat(APPROVAL_CHAT_ID)
        except Exception:
            pass

    app.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.IMAGE | filters.Document.VIDEO) & chat_filter, on_user_message, block=True),
        group=0
    )

# =========================
# (опц.) унификация CallbackQuery из Message
# =========================
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
