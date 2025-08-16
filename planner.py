# -*- coding: utf-8 -*-
"""
planner.py — модуль планировщика для Twitter/TG бота.

Возможности:
- Режимы PLAN (тема→(ИИ-текст)→время) и GEN (текст/фото→время).
- Хэштеги можно ввести/переуказать на ЛЮБОМ шаге (отдельная кнопка + поле редактирования).
- Мини-БД SQLite: planned_posts (mode, topic, text, time_str, image_url, hashtags, status).
- Экспортируемые API: register_planner_handlers(app), open_planner(update, ctx),
  set_ai_generator(async fn(topic)->(text_en, tags:list[str], image_url|None)), USER_STATE.

Совместимость:
- Импорт из основного бота:
    from planner import register_planner_handlers, open_planner, set_ai_generator, USER_STATE as PLANNER_STATE
"""

from __future__ import annotations
import os, sqlite3, logging
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

# =========================
# ЛОГИРОВАНИЕ
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(funcName)s | %(message)s")
log = logging.getLogger("planner")
def _lg(msg: str): log.info(f"PLNR> {msg}")

# =========================
# Админ/чат ограничения
# =========================
GROUP_ANON_UID = 1087968824
TG_SERVICE_UID = 777000
_admin_env = os.getenv("APPROVAL_ADMIN_UID") or os.getenv("PLANNER_ADMIN_UID")
try:    ADMIN_UID: Optional[int] = int(_admin_env) if _admin_env else None
except: ADMIN_UID = None

_chat_env = os.getenv("TELEGRAM_APPROVAL_CHAT_ID") or os.getenv("PLANNER_APPROVAL_CHAT_ID")
try:    APPROVAL_CHAT_ID: Optional[int] = int(_chat_env) if _chat_env else None
except: APPROVAL_CHAT_ID = None

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
# Состояние пользователя
# =========================
USER_STATE: Dict[int, Dict[str, Any]] = {}

@dataclass
class PlannedItem:
    topic: Optional[str] = None
    text: Optional[str] = None
    time_str: Optional[str] = None
    image_url: Optional[str] = None
    hashtags: Optional[str] = None   # space-separated, как в X
    step: str = "idle"               # idle | waiting_* | editing_*
    mode: str = "none"               # plan | gen | edit

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
        "hashtags": item.hashtags,
        "added_at": datetime.utcnow().isoformat() + "Z"
    }
    _lg(f"push -> items[{pid}] mode={row['mode']} time={row['time']} tags={row['hashtags']}")
    USER_STATE[uid]["items"].append(row)
    try:
        db_insert_item(uid, row)
    except Exception as e:
        _lg(f"db_insert_item failed: {e}")
    USER_STATE[uid]["current"] = PlannedItem()

def _can_finalize(item: PlannedItem) -> bool:
    if not item.time_str: return False
    if item.mode == "plan": return bool(item.topic)
    if item.mode == "gen":  return bool(item.text or item.image_url)
    return False

# =========================
# БД
# =========================
DB_FILE = os.getenv("PLANNER_DB_FILE", "planner_posts.db")

def _db_init():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planned_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            topic TEXT,
            text  TEXT,
            time_str TEXT,
            image_url TEXT,
            hashtags TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_planned_status ON planned_posts(status)")
    conn.commit(); conn.close()

def db_insert_item(user_id: int, row: Dict[str, Any]) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO planned_posts (user_id, mode, topic, text, time_str, image_url, hashtags, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?)
    """, (
        user_id, row.get("mode"), row.get("topic"), row.get("text"),
        row.get("time"), row.get("image_url"), row.get("hashtags"),
        datetime.utcnow().isoformat() + "Z"
    ))
    rid = cur.lastrowid
    conn.commit(); conn.close()
    return int(rid)

def db_update_item(pid: int, fields: Dict[str, Any]) -> None:
    if not fields: return
    sets = ", ".join(f"{k} = ?" for k in fields.keys())
    vals = list(fields.values()) + [pid]
    conn = sqlite3.connect(DB_FILE)
    conn.execute(f"UPDATE planned_posts SET {sets} WHERE id = ?", vals)
    conn.commit(); conn.close()

def db_delete_item(pid: int) -> None:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM planned_posts WHERE id = ?", (pid,))
    conn.commit(); conn.close()

# =========================
# ИИ-генератор (из основного бота)
# =========================
_AI_GEN_FN: Optional[Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]] = None

def set_ai_generator(fn: Callable[[str], Awaitable[Tuple[str, List[str], Optional[str]]]]):
    global _AI_GEN_FN
    _AI_GEN_FN = fn
    _lg("ai_generator registered")

# =========================
# UI
# =========================
def _btns_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 ИИ план (тема→текст→время)", callback_data="OPEN_PLAN_MODE")],
        [InlineKeyboardButton("✨ Мой пост (текст/фото→время)", callback_data="OPEN_GEN_MODE")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="OPEN_HASHTAGS")],
        [InlineKeyboardButton("📋 Список на сегодня", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")]
    ])

def _btns_ready(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово", callback_data=f"{prefix}DONE"),
         InlineKeyboardButton("➕ Ещё", callback_data=f"{prefix}ADD_MORE")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data="OPEN_HASHTAGS")],
        [InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU"),
         InlineKeyboardButton("↩️ Отмена", callback_data="STEP_BACK")]
    ])

def _kb_item_actions(pid: int, mode: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Править", callback_data=f"EDIT_ITEM:{pid}"),
         InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_TIME:{pid}")],
        [InlineKeyboardButton("🔖 Хэштеги", callback_data=f"EDIT_FIELD:hashtags:{pid}")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"DEL_ITEM:{pid}")],
        [InlineKeyboardButton("⬅️ Назад к списку", callback_data="PLAN_LIST_TODAY")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")],
    ]
    if mode == "plan":
        rows.insert(1, [InlineKeyboardButton("🤖 ИИ: дополнить текст", callback_data=f"AI_FILL_TEXT:{pid}")])
    return InlineKeyboardMarkup(rows)

def _kb_edit_fields(pid: int, mode: str) -> InlineKeyboardMarkup:
    rows = []
    if mode == "plan":
        rows.append([InlineKeyboardButton("📝 Тема", callback_data=f"EDIT_FIELD:topic:{pid}")])
        rows.append([InlineKeyboardButton("✍️ Текст (ручн.)", callback_data=f"EDIT_FIELD:text:{pid}")])
    else:
        rows.append([InlineKeyboardButton("✍️ Текст", callback_data=f"EDIT_FIELD:text:{pid}")])
    rows.append([InlineKeyboardButton("🖼 Картинка", callback_data=f"EDIT_FIELD:image:{pid}")])
    rows.append([InlineKeyboardButton("🔖 Хэштеги", callback_data=f"EDIT_FIELD:hashtags:{pid}")])
    rows.append([InlineKeyboardButton("⏰ Время", callback_data=f"EDIT_FIELD:time:{pid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ITEM_MENU:{pid}")])
    return InlineKeyboardMarkup(rows)

# =========================
# Хелперы сообщений
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

def _norm_tags_line(s: str) -> str:
    """Приводим к ' #tag #Tag2 $Ai ' и убираем дубликаты (регистронезависимо)."""
    if not s: return ""
    raw = s.replace(",", " ").replace("\n", " ")
    seen, out = set(), []
    for tok in raw.split():
        t = tok.strip()
        if not t: continue
        if not (t.startswith("#") or t.startswith("$")):
            t = "#" + t
        key = t.lower()
        if key in seen: continue
        seen.add(key); out.append(t)
    return " ".join(out)

# =========================
# Открытие планировщика
# =========================
async def open_planner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    _db_init()
    uid = _uid_from_update(update)
    USER_STATE.setdefault(uid, {"mode":"none","items":[],"current":PlannedItem(),"seq":0})
    q = update.callback_query
    _lg(f"open_planner uid={uid}")
    if q:
        await _safe_edit_or_send(q, "[ПЛАНИРОВЩИК] Выбери режим.", reply_markup=_btns_main())
    else:
        await context.bot.send_message(update.effective_chat.id, "[ПЛАНИРОВЩИК] Выбери режим.",
                                       reply_markup=_btns_main())

# =========================
# Шаги
# =========================
async def _ask_topic(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid); st.mode = "plan"; st.step = "waiting_topic"
    await _safe_edit_or_send(
        q, "[PLAN] Введи <b>тему</b> для поста. Можешь в любой момент нажать «🔖 Хэштеги».",
        reply_markup=_btns_ready("PLAN_")
    )

async def _ask_text(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid); st.mode = "gen"; st.step = "waiting_text"
    await _safe_edit_or_send(
        q, "[GEN] Пришли текст поста и/или фото (одним сообщением). Затем укажем время.",
        reply_markup=_btns_ready("GEN_")
    )

async def _ask_time(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid); st.step = "waiting_time"
    await _safe_edit_or_send(q, "[*] Введи время <b>HH:MM</b> (Киев).", reply_markup=_btns_ready("PLAN_" if st.mode=="plan" else "GEN_"))

async def _ask_hashtags(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    st.step = "editing_hashtags" if st.mode in ("plan","gen","edit") else "waiting_hashtags"
    await _safe_edit_or_send(
        q, "🔖 Введи хэштеги одной строкой (пробелами). Пример: <code>#AiCoin #AI $Ai #crypto</code>",
        reply_markup=_btns_ready("PLAN_" if st.mode=="plan" else "GEN_"),
        parse_mode="HTML"
    )

async def _show_ready(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    prefix = "PLAN_" if st.mode=="plan" else "GEN_"
    lines = []
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
    lines.append(f"Хэштеги: {(st.hashtags or '—')}")
    await _safe_edit_or_send(q, "Проверь данные:\n" + "\n".join(lines), reply_markup=_btns_ready(prefix))

# =========================
# CALLBACKS — режимы/меню
# =========================
async def cb_open_plan_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    # пробуем пинговать ИИ опционально, но не блокируем
    await _ask_topic(q)

async def cb_open_gen_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    await _ask_text(q)

async def cb_open_hashtags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    await _ask_hashtags(q)

async def cb_list_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    items = USER_STATE.get(uid, {}).get("items", [])
    if not items:
        return await _safe_edit_or_send(q, "На сегодня пусто.", reply_markup=_btns_main())
    def _row(i, it):
        time_s = it.get("time") or "—"
        mode = it.get("mode")
        if mode == "plan":
            title = (it.get("topic") or "—")
        else:
            t = (it.get("text") or "—").strip()
            if len(t) > 60: t = t[:57]+"…"
            title = t + (" 🖼" if it.get("image_url") else "")
        return f"{i}) [{mode.upper()}] {time_s} — {title}"
    lines = [_row(i+1, it) for i,it in enumerate(items)]
    # список + кнопки к каждому элементу
    rows: List[List[InlineKeyboardButton]] = []
    for it in items:
        pid = it["id"]
        rows.append([InlineKeyboardButton(f"⚙️ #{pid}", callback_data=f"ITEM_MENU:{pid}"),
                     InlineKeyboardButton("🗑", callback_data=f"DEL_ITEM:{pid}")])
    rows.append([InlineKeyboardButton("⬅️ В основное меню", callback_data="BACK_MAIN_MENU")])
    await _safe_edit_or_send(q, "Список на сегодня:\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

# =========================
# ITEM actions / edit
# =========================
async def cb_item_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":",1)[1])
    except: return await _safe_edit_or_send(q, "Ошибка идентификатора.", reply_markup=_btns_main())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=_btns_main())
    lines = [f"ID: {pid}", f"Режим: {it['mode']}", f"Время: {it.get('time') or '—'}"]
    if it["mode"] == "plan":
        lines.append(f"Тема: {it.get('topic') or '—'}")
    lines.append(f"Текст: {(it.get('text') or '—')[:280]}{'…' if (it.get('text') and len(it['text'])>280) else ''}")
    lines.append(f"Картинка: {'есть' if it.get('image_url') else 'нет'}")
    lines.append(f"Хэштеги: {it.get('hashtags') or '—'}")
    return await _safe_edit_or_send(q, "\n".join(lines), reply_markup=_kb_item_actions(pid, it["mode"]))

async def cb_delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":",1)[1])
    except: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=_btns_main())
    items = USER_STATE.get(uid, {}).get("items", [])
    USER_STATE[uid]["items"] = [x for x in items if x.get("id") != pid]
    try: db_delete_item(pid)
    except: pass
    return await _safe_edit_or_send(q, f"Удалено #{pid}.", reply_markup=_btns_main())

async def cb_edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":",1)[1])
    except: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=_btns_main())
    st = _ensure(uid); st.step = "editing_time"; st.mode = "edit"
    USER_STATE[uid]["edit_target"] = pid
    return await _safe_edit_or_send(q, "Введите новое время <b>HH:MM</b> (Киев).",
                                    reply_markup=InlineKeyboardMarkup([
                                        [InlineKeyboardButton("⬅️ Назад к элементу", callback_data=f"ITEM_MENU:{pid}")],
                                        [InlineKeyboardButton("🏠 Главное меню", callback_data="BACK_MAIN_MENU")]
                                    ]),
                                    parse_mode="HTML")

async def cb_edit_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":",1)[1])
    except: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=_btns_main())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=_btns_main())
    return await _safe_edit_or_send(q, "Что меняем?", reply_markup=_kb_edit_fields(pid, it["mode"]))

async def cb_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try:
        _, field, pid_s = q.data.split(":", 2)
        pid = int(pid_s)
    except:
        return await _safe_edit_or_send(q, "Ошибка выбора поля.", reply_markup=_btns_main())

    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=_btns_main())
    st = _ensure(uid); USER_STATE[uid]["edit_target"] = pid

    if field == "topic":
        st.step = "editing_topic"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Введите новую тему:",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"EDIT_ITEM:{pid}")]]))
    if field == "text":
        st.step = "editing_text"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Пришлите новый текст поста:",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"EDIT_ITEM:{pid}")]]))
    if field == "image":
        st.step = "editing_image"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Пришлите новую картинку (как фото/документ) или отправьте «удалить».",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"EDIT_ITEM:{pid}")]]))
    if field == "time":
        st.step = "editing_time"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Введите новое время <b>HH:MM</b> (Киев).",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"EDIT_ITEM:{pid}")]]),
                                        parse_mode="HTML")
    if field == "hashtags":
        st.step = "editing_hashtags"; st.mode = "edit"
        return await _safe_edit_or_send(q, "Введите хэштеги одной строкой:",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"EDIT_ITEM:{pid}")]]))
    return await _safe_edit_or_send(q, "Неизвестное поле.", reply_markup=_btns_main())

async def cb_ai_fill_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    try: pid = int(q.data.split(":",1)[1])
    except: return await _safe_edit_or_send(q, "Ошибка ID.", reply_markup=_btns_main())
    it = _find_item(uid, pid)
    if not it: return await _safe_edit_or_send(q, "Элемент не найден.", reply_markup=_btns_main())
    if it["mode"] != "plan": return await _safe_edit_or_send(q, "Доступно только для PLAN.", reply_markup=_btns_main())
    if _AI_GEN_FN is None: return await _safe_edit_or_send(q, "ИИ-генератор не подключён.", reply_markup=_btns_main())

    topic = it.get("topic") or ""
    try:
        text_en, tags, img = await _AI_GEN_FN(topic)
        it["text"] = f"{text_en}".strip()
        # хэштеги: не затираем пользовательские; если пусто — подставим из ИИ
        if not (it.get("hashtags") or "").strip():
            it["hashtags"] = _norm_tags_line(" ".join(tags or []))
        if img: it["image_url"] = img
        try: db_update_item(pid, {"text": it["text"], "image_url": it.get("image_url"), "hashtags": it.get("hashtags")})
        except: pass
        return await _safe_edit_or_send(q, "Текст дополнён ИИ.", reply_markup=_kb_item_actions(pid, it["mode"]))
    except Exception as e:
        _lg(f"ai_fill_text fail: {e}")
        return await _safe_edit_or_send(q, "Не удалось сгенерировать текст.", reply_markup=_kb_item_actions(pid, it["mode"]))

# =========================
# Финал/отмена
# =========================
async def cb_step_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q)
    USER_STATE.setdefault(uid, {"items": [], "current": PlannedItem(), "seq": 0})
    USER_STATE[uid]["current"] = PlannedItem()
    USER_STATE[uid].pop("edit_target", None)
    await _safe_edit_or_send(q, "Отменено. Что дальше?", reply_markup=_btns_main())

async def _finalize_and_back(q: CallbackQuery):
    uid = _uid_from_q(q)
    st = _ensure(uid)
    if _can_finalize(st):
        _push(uid, st)
        return await _safe_edit_or_send(q, "Сохранено. Что дальше?", reply_markup=_btns_main())
    return await _safe_edit_or_send(q, "Нечего сохранять — заполни данные и время.", reply_markup=_btns_main())

async def cb_plan_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    await _finalize_and_back(update.callback_query)

async def cb_gen_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    await _finalize_and_back(update.callback_query)

async def cb_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update): return
    q = update.callback_query; await q.answer()
    uid = _uid_from_q(q); st = _ensure(uid)
    if _can_finalize(st): _push(uid, st)
    if st.mode == "plan": await _ask_topic(q)
    else:                  await _ask_text(q)

# =========================
# Ввод пользователя
# =========================
async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватываем ввод только когда реально на шаге планировщика."""
    if not _allowed_chat(update): return
    uid = _uid_from_update(update)
    st = _ensure(uid)
    active = {"waiting_topic","waiting_text","waiting_time","waiting_hashtags",
              "editing_time","editing_text","editing_topic","editing_image","editing_hashtags"}
    if (st.mode not in ("plan","gen","edit")) and (st.step not in active):
        return
    msg: Message = update.message
    text = (msg.text or msg.caption or "").strip()

    # ---- EDITING ----
    if st.step == "editing_topic":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=_btns_main())
        if not text: return await msg.reply_text("Нужна новая тема.")
        it["topic"] = text
        try: db_update_item(pid, {"topic": text})
        except: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Тема обновлена для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))

    if st.step == "editing_text":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=_btns_main())
        if not text: return await msg.reply_text("Нужен новый текст.")
        it["text"] = text
        try: db_update_item(pid, {"text": text})
        except: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Текст обновлён для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))

    if st.step == "editing_image":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=_btns_main())
        if text.lower() in {"удалить","delete","none","remove"}:
            it["image_url"] = None
            try: db_update_item(pid, {"image_url": None})
            except: pass
            st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
            return await msg.reply_text(f"Картинка удалена для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))
        if msg.photo: it["image_url"] = msg.photo[-1].file_id
        if getattr(msg, "document", None) and getattr(msg.document, "mime_type","").startswith("image/"):
            it["image_url"] = msg.document.file_id
        if not it.get("image_url"): return await msg.reply_text("Пришлите фото или отправьте «удалить».")
        try: db_update_item(pid, {"image_url": it["image_url"]})
        except: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Картинка обновлена для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))

    if st.step == "editing_time":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=_btns_main())
        ok=False
        if len(text)>=4 and ":" in text:
            hh,mm=text.split(":",1); ok=hh.isdigit() and mm.isdigit() and 0<=int(hh)<24 and 0<=int(mm)<60
        if not ok: return await msg.reply_text("Неверный формат. Пример: 14:30")
        it["time"] = f"{int(hh):02d}:{int(mm):02d}"
        try: db_update_item(pid, {"time_str": it["time"]})
        except: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Время обновлено для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))

    if st.step == "editing_hashtags":
        pid = USER_STATE[uid].get("edit_target"); it = _find_item(uid, pid) if pid else None
        if not it: st.step="idle"; st.mode="none"; return await msg.reply_text("Элемент не найден.", reply_markup=_btns_main())
        it["hashtags"] = _norm_tags_line(text)
        try: db_update_item(pid, {"hashtags": it["hashtags"]})
        except: pass
        st.step="idle"; st.mode="none"; USER_STATE[uid].pop("edit_target", None)
        return await msg.reply_text(f"Хэштеги обновлены для #{pid}.", reply_markup=_kb_item_actions(pid, it["mode"]))

    # ---- СОЗДАНИЕ ----
    if st.step == "waiting_topic":
        if not text: return await msg.reply_text("[PLAN] Нужна тема.")
        st.topic = text
        # Пытаемся автосгенерить текст (не обязательно)
        try:
            if _AI_GEN_FN:
                text_en, tags, img = await _AI_GEN_FN(st.topic)
                st.text = text_en.strip()
                if not st.hashtags: st.hashtags = _norm_tags_line(" ".join(tags or []))
                if img: st.image_url = img
        except Exception as e:
            _lg(f"ai prefill failed: {e}")
        await _ask_time(await update.to_callback_query(context.bot))
        return

    if st.step == "waiting_text":
        if msg.photo: st.image_url = msg.photo[-1].file_id
        if getattr(msg,"document",None) and getattr(msg.document,"mime_type","").startswith("image/"):
            st.image_url = msg.document.file_id
        if text: st.text = text
        if not (st.text or st.image_url):
            return await msg.reply_text("[GEN] Пришли текст и/или фото.")
        await _ask_time(await update.to_callback_query(context.bot))
        return

    if st.step == "waiting_time":
        ok=False
        if len(text)>=4 and ":" in text:
            hh,mm=text.split(":",1); ok=hh.isdigit() and mm.isdigit() and 0<=int(hh)<24 and 0<=int(mm)<60
        if not ok: return await msg.reply_text("[*] Неверный формат. Пример: 14:30")
        st.time_str = f"{int(hh):02d}:{int(mm):02d}"
        await _show_ready(await update.to_callback_query(context.bot))
        return

    if st.step == "waiting_hashtags":
        st.hashtags = _norm_tags_line(text)
        await _show_ready(await update.to_callback_query(context.bot))
        return

# =========================
# Регистрация хендлеров
# =========================
def register_planner_handlers(app: Application):
    _db_init(); _lg("register handlers")
    # Режимы
    app.add_handler(CallbackQueryHandler(cb_open_plan_mode,    pattern="^OPEN_PLAN_MODE$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_open_gen_mode,     pattern="^OPEN_GEN_MODE$",  block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_open_hashtags,     pattern="^OPEN_HASHTAGS$",  block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_list_today,        pattern="^PLAN_LIST_TODAY$",block=True), group=0)

    # Навигация
    app.add_handler(CallbackQueryHandler(cb_step_back,         pattern="^STEP_BACK$",      block=True), group=0)
    # (BACK_MAIN_MENU обрабатывает основной бот)

    # Завершение/добавление
    app.add_handler(CallbackQueryHandler(cb_plan_done,         pattern="^PLAN_DONE$",      block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_gen_done,          pattern="^GEN_DONE$",       block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_add_more,          pattern="^(PLAN_ADD_MORE|GEN_ADD_MORE)$", block=True), group=0)

    # Управление элементами
    app.add_handler(CallbackQueryHandler(cb_item_menu,         pattern="^ITEM_MENU:\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_delete_item,       pattern="^DEL_ITEM:\\d+$",  block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_time,         pattern="^EDIT_TIME:\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_item,         pattern="^EDIT_ITEM:\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_edit_field,        pattern="^EDIT_FIELD:(topic|text|image|time|hashtags):\\d+$", block=True), group=0)
    app.add_handler(CallbackQueryHandler(cb_ai_fill_text,      pattern="^AI_FILL_TEXT:\\d+$", block=True), group=0)

    # Ввод пользователя на шагах/редактировании
    chat_filter = filters.ALL
    if APPROVAL_CHAT_ID is not None:
        try: chat_filter = filters.Chat(APPROVAL_CHAT_ID)
        except Exception: pass
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE) & chat_filter, on_user_message, block=True), group=0)

# =========================
# (опц.) унификация CallbackQuery из Message
# =========================
from typing import Optional as _Optional
async def _build_fake_callback_from_message(message: Message, bot) -> CallbackQuery:
    return CallbackQuery(id="fake", from_user=message.from_user, chat_instance="", message=message, bot=bot)

async def _update_to_callback_query(update: Update, bot) -> _Optional[CallbackQuery]:
    if update.callback_query: return update.callback_query
    if update.message:        return await _build_fake_callback_from_message(update.message, bot)
    return None

setattr(Update, "to_callback_query", _update_to_callback_query)