# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, Awaitable, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message, CallbackQuery
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest

# =========================
# ЛОГИ
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("planner")
def _lg(msg: str): log.info(f"PLNR> {msg}")

# =========================
# АКТОР/ЧАТ
# =========================
GROUP_ANON_UID = 1087968824
TG_SERVICE_UID = 777000
_admin_env = os.getenv("APPROVAL_ADMIN_UID") or os.getenv("PLANNER_ADMIN_UID")
try:    ADMIN_UID: Optional[int] = int(_admin_env) if _admin_env else None
except Exception: ADMIN_UID = None
_chat_env = os.getenv("TELEGRAM_APPROVAL_CHAT_ID") or os.getenv("PLANNER_APPROVAL_CHAT_ID")
try:    APPROVAL_CHAT_ID: Optional[int] = int(_chat_env) if _chat_env else None
except Exception: APPROVAL_CHAT_ID = None

def _norm_uid(raw_uid: int) -> int:
    if raw_uid in (GROUP_ANON_UID, TG_SERVICE_UID) and ADMIN_UID:
        return ADMIN_UID
    return raw_uid

def _allowed_chat(update: Update) -> bool:
    if APPROVAL_CHAT_ID is None: return True
    ch = update.effective_chat
    return bool(ch and ch.id == APPROVAL_CHAT_ID)

def _uid_from_update(update: Update) -> int:
    u = update.effective_user
    raw = u.id if u else (ADMIN_UID or 0)
    return _norm_uid(raw)

def _uid_from_q(q: CallbackQuery) -> int:
    return _norm_uid(q.from_user.id)

# =========================
# СЕССИИ ПЛАНИРОВЩИКА
# =========================
USER_STATE: Dict[int, Dict[str, Any]] = {}

@dataclass
class PlannedItem:
    topic: Optional[str] = None     # PLAN
    text: Optional[str] = None      # GEN/PLAN
    time_str: Optional[str] = None
    image_url: Optional[str] = None
    ai_tags: List[str] = None       # <— ХЭШТЕГИ ОТДЕЛЬНО
    step: str = "idle"              # idle | waiting_topic | waiting_text | waiting_time | editing_*
    mode: str = "none"              # plan | gen | edit

# =========================
# БАЗА ДАННЫХ
# =========================
DB_FILE = os.getenv("PLANNER_DB_FILE", "planner_posts.db")

def _db_init():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planned_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,         -- 'plan' | 'gen'
            topic TEXT,
            text  TEXT,
            time_str TEXT,              -- HH:MM (Kyiv)
            image_url TEXT,             -- TG file_id or URL
            ai_tags TEXT,               -- space-separated hashtags
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_planned_status ON planned_posts(status)")
    conn.commit(); conn.close()

def _tags_to_str(tags: List[str] | None) -> str:
    return " ".join(tags or [])

def _str_to_tags(s: str | None) -> List[str]:
    if not s: return []
    s = s.replace(",", " ").replace(";", " ")
    out = []
    for t in s.split():
        t = t.strip()
        if not t: continue
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        if len(t) <= 50:
            out.append(t)
    return out

def db_insert_item(user_id: int, it: Dict[str, Any]) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO planned_posts (user_id, mode, topic, text, time_str, image_url, ai_tags, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?)
    """, (
        user_id,
        it.get("mode"),
        it.get("topic"),
        it.get("text"),
        it.get("time"),
        it.get("image_url"),
        _tags_to_str(it.get("ai_tags")),
        datetime.utcnow().isoformat() + "Z"
    ))
    rowid = cur.lastrowid
    conn.commit(); conn.close()
    return int(rowid)

def db_update_item(pid: int, fields: Dict[str, Any]) -> None:
    if not fields: return
    sets = []
    vals = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        if k == "ai_tags":
            vals.append(_tags_to_str(v))
        else:
            vals.append(v)
    vals.append(pid)
    conn = sqlite3.connect(DB_FILE)
    conn.execute(f"UPDATE planned_posts SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit(); conn.close()

def db_delete_item(pid: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM planned_posts WHERE id = ?", (pid,))
    conn.commit(); conn.close()

# =========================
# РЕГИСТРАТОР ИИ-ГЕНЕРАТОРА
# =========================
_AI_GEN_FN: Optional[Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]] = None

def set_ai_generator(fn: Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]):
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
        [InlineKeyboardButton("🔖 Хэштеги", callback_data=f"{prefix}TAGS")],  # <—
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
        [InlineKeyboardButton("🔖 Хэштеги", callback_data=f"EDIT_FIELD:tags:{pid}")],  # <—
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
    rows.append([InlineKeyboardButton("🔖 Хэштеги", callback_data=f"EDIT_FIELD:tags:{pid}")])  # <—
    rows.append([InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_FIELD:time:{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ITEM_MENU:{pid}")])
    return InlineKeyboardMarkup(rows)

# =========================
# ХЕЛПЕРЫ СОСТОЯНИЯ
# =========================
def _ensure(uid: int) -> PlannedItem:
    row = USER_STATE.get(uid) or {}
    if "current" not in row:
        row["current"] = PlannedItem(ai_tags=[])
        row.setdefault("items", [])
        row.setdefault("seq", 0)
        USER_STATE[uid] = row
    if row["current"].ai_tags is None:
        row["current"].ai_tags = []
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
        "ai_tags": list(item.ai_tags or []),
        "added_at": datetime.utcnow().isoformat() + "Z"
    }
    USER_STATE[uid]["items"].append(row)
    try:
        db_insert_item(uid, {
            "mode": row["mode"],
            "topic": row["topic"],
            "text": row["text"],
            "time": row["time"],
            "image_url": row["image_url"],
            "ai_tags": row["ai_tags"],
        })
    except Exception as e:
        _lg(f"db_insert_item failed: {e}")
    USER_STATE[uid]["current"] = PlannedItem(ai_tags=[])

def _can_finalize(item: PlannedItem) -> bool:
    if not item.time_str:
        return False
    if item.mode == "plan":
        return bool(item.topic)
    if item.mode == "gen":
        return bool(item.text or item.image_url)
    return False

# =========================
# SAFE EDIT/SEND
# =========================
async def _safe_edit_or_send(q: CallbackQuery, text: str,
                             reply_markup: Optional[InlineKeyboardMarkup]=None,
                             parse_mode: Optional[str]="HTML"):
    m = q.message
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
# OPENAI availability
# =========================
def _openai_key_present() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))

async def _openai_usable() -> bool:
    if not _openai_key_present():
        _lg("openai not present"); return False
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":"ping"}],
            max_tokens=1, temperature=0.0,
        )
        _lg("openai usable OK"); return True
    except Exception as e:
        _lg(f"openai unusable: {e}"); return False

# =========================
# OPEN PLANNER
# =========================
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    _db_init()
    q = update.callback_query
    uid = _uid_from_update(update)
    USER_STATE.setdefault(uid, {"mode": "none", "items": [], "current": PlannedItem(ai_tags=[]), "seq": 0})
    if q:
        await _safe_edit_or_send(q, "[ПЛАНИРОВЩИК] Выбери режим.", reply_markup=main_planner_menu())
    else:
        await context.bot.send_message(update.effective_chat.id, "[ПЛАНИРОВЩИК] Выбери режим.",
                                       reply_markup=main_planner_menu())

# =========================
# ШАГИ
# =========================
async def _ask_topic(q: CallbackQuery, mode: str):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.mode = mode
    st.step = "waiting_topic"
    await _safe_edit_or_send(
        q,
        "[PLAN] Введи <b>тему</b> для поста.\n"
        "Если ИИ доступен — сгенерирую текст и предложу время.\n"
        "Можно в любой момент нажать «Хэштеги».",
        reply_markup=step_buttons_done_add_cancel("PLAN_")
    )

async def _ask_text(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.mode = "gen"
    st.step = "waiting_text"
    await _safe_edit_or_send(
        q,
        "[GEN] Пришли текст поста и/или фото (фото можно с подписью). Затем попрошу время публикации.\n"
        "Можно сразу настроить «Хэштеги».",
        reply_markup=step_buttons_done_add_cancel("GEN_")
    )

async def _ask_time(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.step = "waiting_time"
    await _safe_edit_or_send(
        q, "[*] Введи время публикации в формате <b>HH:MM</b> (Киев). Например, 14:30.",
        reply_markup=step_buttons_done_add_cancel("PLAN_" if st.mode=="plan" else "GEN_")
    )

async def _ask_time_via_msg(msg: Message):
    uid = _norm_uid(msg.from_user.id)
    st = _ensure(uid)
    st.step = "waiting_time"
    await msg.reply_text(
        "[*] Введи время публикации в формате <b>HH:MM</b> (Киев). Например, 14:30.",
        reply_markup=step_buttons_done_add_cancel("PLAN_" if st.mode=="plan" else "GEN_"),
        parse_mode="HTML"
    )

# =========================
# СПИСОК
# =========================
def _format_item_row(i: int, it: Dict[str, Any]) -> str:
    mode = it.get("mode")
    time_s = it.get("time") or "—"
    tags = _tags_to_str(it.get("ai_tags"))
    tags_info = f" 〔{tags}〕" if tags else ""
    if mode == "plan":
        txt = (it.get("topic") or "—")
        return f"{i}) [PLAN] {time_s} — {txt}{tags_info}"
    t = (it.get("text") or "").strip()
    if len(t) > 60: t = t[:57] + "…"
    img = "🖼" if it.get("image_url") else "—"
    return f"{i}) [GEN] {time_s} — {t} {img}{tags_info}"

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
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    items = USER_STATE.get(uid, {}).get("items", [])
    if not items:
        return await _safe_edit_or_send(q, "На сегодня пока пусто.", reply_markup=main_planner_menu())
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(_format_item_row(i, it))
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=_list_kb(uid))

# =========================
# ITEM MENU / EDIT / DELETE / etc.
# =========================
async def cb_item_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка идентификатора.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())

    lines = [
        f"ID: {pid}",
        f"Режим: {it['mode']}",
        f"Время: {it.get('time') or '—'}",
        f"Хэштеги: { _tags_to_str(it.get('ai_tags')) or '—' }",
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
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID для удаления.", reply_markup=main_planner_menu())
    items = USER_STATE.get(uid, {}).get("items", [])
    USER_STATE[uid]["items"] = [x for x in items if x.get("id") != pid]
    try: db_delete_item(pid)
    except Exception: pass
    return await _safe_edit_or_send(q, f"Удалено #{pid}.", reply_markup=main_planner_menu())

async def cb_edit_time_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    st = _ensure(uid)
    st.step = "editing_time"; st.mode = "edit"
    USER_STATE[uid]["edit_target"] = pid
    return await _safe_edit_or_send(q, "Введите новое время в формате <b>HH:MM</b> (Киев).", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад к элементу", callback_data=f"ITEM_MENU:{pid}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
    ]))

async def cb_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID для редактирования.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    return await _safe_edit_or_send(q, "Что меняем?", reply_markup=_edit_fields_kb(pid, it["mode"]))

async def cb_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try:
        _, field, pid_s = q.data.split(":", 2)
        pid = int(pid_s)
    except Exception:
        return await _safe_edit_or_send(q, "Ошибка выбора поля.", reply_markup=main_planner_menu())

    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    st = _ensure(uid); USER_STATE[uid]["edit_target"] = pid

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
    if field == "tags":  # <— НОВОЕ: правка хэштегов
        st.step = "editing_tags"; st.mode = "edit"
        current = _tags_to_str(it.get("ai_tags"))
        return await _safe_edit_or_send(q,
            "🔖 Введите хэштеги одной строкой (пример):\n"
            "<code>#AiCoin #AI $Ai #crypto</code>\n\n"
            f"Текущие: <i>{current or '—'}</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к редактированию", callback_data=f"EDIT_ITEM:{pid}")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
            ]),
            parse_mode="HTML"
        )
    return await _safe_edit_or_send(q, "Неизвестное поле.", reply_markup=main_planner_menu())

async def cb_ai_fill_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    if it["mode"] != "plan": return await _safe_edit_or_send(q, "ИИ-дополнение доступно только для PLAN.", reply_markup=main_planner_menu())
    if _AI_GEN_FN is None: return await _safe_edit_or_send(q, "Генератор ИИ не подключён.", reply_markup=main_planner_menu())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        it["text"] = text_en
        it["ai_tags"] = list(tags or [])
        if img: it["image_url"] = img
        try: db_update_item(pid, {"text": it["text"], "image_url": it.get("image_url"), "ai_tags": it.get("ai_tags")})
        except Exception: pass
        return await _safe_edit_or_send(q, "Текст и хэштеги дополнены ИИ.", reply_markup=_item_actions_kb(pid, it["mode"]))
    except Exception as e:
        return await _safe_edit_or_send(q, "Не удалось сгенерировать текст ИИ.", reply_markup=_item_actions_kb(pid, it["mode"]))

async def cb_clone_item(update: Update, ContextTypes=None):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID для клонирования.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден для клона.", reply_markup=main_planner_menu())

    nid = _new_pid(uid)
    clone = {
        "id": nid,
        "mode": it["mode"],
        "topic": it.get("topic"),
        "text": None,
        "time": it.get("time"),
        "image_url": None,
        "ai_tags": list(it.get("ai_tags") or []),
        "added_at": datetime.utcnow().isoformat() + "Z"
    }
    USER_STATE[uid]["items"].append(clone)
    try:
        db_insert_item(uid, clone)
    except Exception:
        pass
    return await _safe_edit_or_send(q, f"Создан клон #{nid} (сохр. тему/время/хэштеги).", reply_markup=_item_actions_kb(nid, it["mode"]))

async def cb_ai_new_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":", 1)[1])
    except Exception: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=main_planner_menu())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=main_planner_menu())
    if it["mode"] != "plan": return await _safe_edit_or_send(q, "Доступно только для PLAN.", reply_markup=main_planner_menu())
    if _AI_GEN_FN is None: return await _safe_edit_or_send(q, "Генератор ИИ не подключён.", reply_markup=main_planner_menu())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        nid = _new_pid(uid)
        newrow = {
            "id": nid,
            "mode": "plan",
            "topic": topic,
            "text": text_en,
            "time": it.get("time"),
            "image_url": img,
            "ai_tags": list(tags or []),
            "added_at": datetime.utcnow().isoformat() + "Z"
        }
        USER_STATE[uid]["items"].append(newrow)
        try: db_insert_item(uid, newrow)
        except Exception: pass
        return await _safe_edit_or_send(q, f"Создан новый пост #{nid} (ИИ-текст, тема/время/хэштеги сохранены).", reply_markup=_item_actions_kb(nid, "plan"))
    except Exception as e:
        return await _safe_edit_or_send(q, "Не удалось сгенерировать новый ИИ-текст.", reply_markup=_item_actions_kb(pid, "plan"))

# =========================
# DONE / ADD MORE / STEP BACK
# =========================
async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    USER_STATE.setdefault(uid, {"items": [], "current": PlannedItem(ai_tags=[]), "seq": 0})
    USER_STATE[uid]["current"] = PlannedItem(ai_tags=[])
    USER_STATE[uid].pop("edit_target", None)
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=main_planner_menu())

async def _finalize_current_and_back(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    if _can_finalize(st):
        _push(uid, st)
        return await _safe_edit_or_send(q, "Сохранено. Что дальше?", reply_markup=main_planner_menu())
    else:
        return await _safe_edit_or_send(q, "Нечего сохранять — заполни данные и время.", reply_markup=main_planner_menu())

async def cb_plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    await _finalize_current_and_back(update.callback_query)

async def cb_gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    await _finalize_current_and_back(update.callback_query)

async def cb_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    st = _ensure(uid)
    if _can_finalize(st): _push(uid, st)
    if st.mode == "plan": await _ask_topic(q, mode="plan")
    else: await _ask_text(q)

# =========================
# AI build now
# =========================
async def cb_plan_ai_build_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query
    uid = _uid_from_q(q)
    usable = await _openai_usable()

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
                "text": text_en,
                "time": None,
                "image_url": img,
                "ai_tags": list(tags or []),
                "added_at": datetime.utcnow().isoformat() + "Z"
            }
            USER_STATE[uid]["items"].append(row)
            try: db_insert_item(uid, row)
            except Exception: pass
            created += 1
        except Exception:
            pass

    if created == 0:
        return await _safe_edit_or_send(
            q,
            "Не удалось сгенерировать план. Попробуй позже или перейди в ручной режим.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Мой план (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
                [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
            ])
        )

    return await _safe_edit_or_send(
        q,
        f"Сгенерировано позиций: <b>{created}</b>.\nДобавь время / отредактируй через «Список на сегодня».",
        reply_markup=main_planner_menu()
    )

# =========================
# INPUT + EDIT
# =========================
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватываем ввод только на шагах планировщика/редактирования."""
    if not _allowed_chat(update): return
    uid = _uid_from_update(update)
    st = _ensure(uid)
    active_steps = {"waiting_topic","waiting_text","waiting_time","editing_time","editing_text","editing_topic","editing_image","editing_tags"}
    step = st.step; mode = st.mode
    if (mode not in ("plan","gen","edit")) and (step not in active_steps):
        return

    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()

    # --- EDITING ---
    if step == "editing_topic":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text: return await msg.reply_text("Нужна новая тема текстом.")
        it["topic"] = text
        try: db_update_item(pid, {"topic": text})
        except Exception: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Тема обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_text":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if not text: return await msg.reply_text("Нужен новый текст.")
        it["text"] = text
        try: db_update_item(pid, {"text": text})
        except Exception: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Текст обновлён для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_image":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        if text.lower() in {"удалить","delete","none","remove"}:
            it["image_url"] = None
            try: db_update_item(pid, {"image_url": None})
            except Exception: pass
            st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
            return await msg.reply_text(f"Картинка удалена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))
        if msg.photo: it["image_url"] = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"):
                it["image_url"] = msg.document.file_id
        if not it.get("image_url"):
            return await msg.reply_text("Пришлите фото или отправьте «удалить».")
        try: db_update_item(pid, {"image_url": it["image_url"]})
        except Exception: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Картинка обновлена для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_time":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok: return await msg.reply_text("Неверный формат. Пример: 14:30")
        it["time"] = f"{int(hh):02d}:{int(mm):02d}"
        try: db_update_item(pid, {"time_str": it["time"]})
        except Exception: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Время обновлено для #{pid}.", reply_markup=_item_actions_kb(pid, it["mode"]))

    if step == "editing_tags":  # <— приём новых тэгов
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=main_planner_menu())
        it["ai_tags"] = _str_to_tags(text)
        try: db_update_item(pid, {"ai_tags": it["ai_tags"]})
        except Exception: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Хэштеги обновлены: {_tags_to_str(it['ai_tags']) or '—'}", reply_markup=_item_actions_kb(pid, it["mode"]))

    # --- СОЗДАНИЕ ---
    if step == "waiting_topic":
        st.mode = "plan"
        if not text:
            return await msg.reply_text("[PLAN] Нужна тема текстом. Попробуй ещё раз.", reply_markup=cancel_only())
        st.topic = text
        usable = False
        try: usable = await _openai_usable()
        except Exception: pass
        if (_AI_GEN_FN is not None) and usable:
            try:
                text_en, tags, img = await _AI_GEN_FN(st.topic)
                st.text = text_en
                st.ai_tags = list(tags or [])
                if img: st.image_url = img
            except Exception: pass
        await _ask_time_via_msg(msg); return

    if step == "waiting_text":
        st.mode = "gen"
        if msg.photo: st.image_url = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type", ""):
            if msg.document.mime_type.startswith("image/"): st.image_url = msg.document.file_id
        if text: st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("[GEN] Пришли текст поста и/или фото.", reply_markup=cancel_only())
        await _ask_time_via_msg(msg); return

    if step == "waiting_time":
        ok = False
        if len(text) >= 4 and ":" in text:
            hh, mm = text.split(":", 1)
            ok = hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        if not ok:
            return await msg.reply_text("[*] Неверный формат. Пример: 14:30", reply_markup=cancel_only())
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"

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
        lines.append(f"Хэштеги: {_tags_to_str(st.ai_tags) or '—'}")
        lines.append(f"Время: {st.time_str or '—'}")

        return await msg.reply_text(
            "Проверь данные:\n" + "\n".join(lines),
            reply_markup=step_buttons_done_add_cancel(prefix),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

# =========================
# РЕГИСТРАЦИЯ ХЕНДЛЕРОВ
# =========================
def register_planner_handlers(app: Application):
    _db_init()
    # Режимы
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode,    pattern="^OPEN_PLAN_MODE$", block=True),    group=0)
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,     pattern="^OPEN_GEN_MODE$", block=True),     group=0)
    app.add_handler(CallbackQueryHandler(cb_list_today,        pattern="^PLAN_LIST_TODAY$", block=True),   group=0)
    app.add_handler(CallbackQueryHandler(cb_plan_ai_build_now, pattern="^PLAN_AI_BUILD_NOW$", block=True), group=0)
    # Навигация
    app.add_handler(CallbackQueryHandler(cb_step_back,         pattern="^STEP_BACK$", block=True),         group=0)
    # Завершение
    app.add_handler(CallbackQueryHandler(cb_plan_done,         pattern="^PLAN_DONE$", block=True),         group=0)
    app.add_handler(CallbackQueryHandler(cb_gen_done,          pattern="^GEN_DONE$", block=True),          group=0)
    app.add_handler(CallbackQueryHandler(cb_add_more,          pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$", block=True), group=0)
    # Управление элементами
    app.add_handler(CallbackQueryHandler(cb_item_menu,         pattern="^ITEM_MENU:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_delete_item,       pattern="^DEL_ITEM:\\d+$", block=True),       group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_time_shortcut,pattern="^EDIT_TIME:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_item,         pattern="^EDIT_ITEM:\\d+$", block=True),      group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_field,        pattern="^EDIT_FIELD:(topic|text|image|time|tags):\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_fill_text,      pattern="^AI_FILL_TEXT:\\d+$", block=True),   group=0)
    app.add_handler(CallbackQueryHandler(cb_clone_item,        pattern="^CLONE_ITEM:\\d+$", block=True),     group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_new_from,       pattern="^AI_NEW_FROM:\\d+$", block=True),    group=0)

    # Пользовательский ввод
    chat_filter = filters.ALL
    if APPROVAL_CHAT_ID is not None:
        try: chat_filter = filters.Chat(APPROVAL_CHAT_ID)
        except Exception: pass
    app.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & chat_filter, on_user_message, block=True),
        group=0
    )

# (опц.) унификация CallbackQuery из Message (если вдруг понадобится)
from typing import Optional as _Optional
async def _build_fake_callback_from_message(message: Message, bot) -> CallbackQuery:
    cq = CallbackQuery(id="fake", from_user=message.from_user, chat_instance="", message=message, bot=bot)
    return cq
async def _update_to_callback_query(update: Update, bot) -> _Optional[CallbackQuery]:
    if update.callback_query: return update.callback_query
    if update.message: return await _build_fake_callback_from_message(update.message, bot)
    return None
setattr(Update, "to_callback_query", _update_to_callback_query)