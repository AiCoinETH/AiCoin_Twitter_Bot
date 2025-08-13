# -*- coding: utf-8 -*-
"""
Ежедневная аналитика (RU) для канала согласования — ОТ ЗАПРОСА К СТРАНАМ
Для КАЖДОГО поискового запроса:
  • Google Trends: топ-страны (now 7-d) по этому запросу
  • Для каждой страны: топ-3 related queries (по этому запросу), свежие хэштеги X
  • Внизу: кнопки «📋 Копировать — <запрос>/<страна>» (deeplink в ЛС бота /start copy_<ISO2>_<slug>)

Зависимости: pytrends, snscrape, python-telegram-bot==21.*, pycountry
"""

import os, re, textwrap, asyncio
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from pytrends.request import TrendReq
import pycountry

# snscrape (X/Twitter) — опционально
try:
    import snscrape.modules.twitter as sntwitter
    SNS_OK = True
except Exception:
    SNS_OK = False

# ------------------ ENV ------------------
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
CHAT_ID      = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")       # -100... или @username
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME") or ""     # без @, для deeplink (если есть)

if not TOKEN or not CHAT_ID:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN_APPROVAL and TELEGRAM_APPROVAL_CHAT_ID")

# ------------------ НАСТРОЙКИ ------------------
# ключи, по которым строим аналитику
SEARCH_TERMS = [
    "Ai Coin",
    "AI crypto",
    "blockchain AI",
    "$Ai",
]

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Сколько стран показывать на каждый запрос
TOP_COUNTRIES_PER_TERM = 3
# Сколько related queries на страну/запрос
TOP_RELATED_QUERIES = 3
# Ограничение по твитам на страну/запрос (для выборки хэштегов)
TW_SAMPLE = 150

# регэксп для доменов (если когда-то решим вернуть домены из текста)
DOMAIN_RE = re.compile(r"(?:https?://)?([a-z0-9\-]+\.[a-z\.]{2,})(?:/|$)", re.I)

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def _iso2_from_name(name: str) -> str | None:
    try:
        if name == "United States":  # pycountry нюанс
            name = "United States of America"
        return pycountry.countries.lookup(name).alpha_2
    except Exception:
        return None

def _flag(iso2: str) -> str:
    if not iso2 or len(iso2)!=2: return "🌍"
    base = 0x1F1E6
    return "".join(chr(base + ord(c.upper()) - ord('A')) for c in iso2)

def _local_time_label(iso2: str) -> tuple[str, str]:
    tzmap = {
        "UA":"Europe/Kyiv","DE":"Europe/Berlin","TR":"Europe/Istanbul","GB":"Europe/London",
        "US":"America/New_York","CA":"America/Toronto","BR":"America/Sao_Paulo","MX":"America/Mexico_City",
        "IN":"Asia/Kolkata","ID":"Asia/Jakarta","PH":"Asia/Manila","JP":"Asia/Tokyo",
        "AE":"Asia/Dubai","SA":"Asia/Riyadh","NG":"Africa/Lagos","ZA":"Africa/Johannesburg"
    }
    tz = ZoneInfo(tzmap.get(iso2, "UTC"))
    now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    return now_local, tz.key

# -------------- Google Trends: топ стран для КОНКРЕТНОГО запроса --------------
def trends_top_countries_for_term(term: str, top_n: int) -> list[tuple[str, str, int]]:
    """
    Возвращает [(country_name, iso2, score), ...] для одного поискового запроса.
    """
    py = TrendReq(hl='en-US', tz=0)
    # payload строим на ОДНОМ запросе, а не на списке
    py.build_payload([term], timeframe='now 7-d', geo='')
    df = py.interest_by_region(resolution='COUNTRY', inc_low_vol=True)
    if df.empty:
        return []
    # в df будет столбец с именем term
    series = df[term].fillna(0).astype(int).sort_values(ascending=False)
    out = []
    for country_name, score in series.head(10).items():
        iso2 = _iso2_from_name(country_name)
        if not iso2: 
            continue
        out.append((country_name, iso2, int(score)))
        if len(out) >= top_n:
            break
    return out

# -------------- Google Trends: related queries для (term, country) --------------
def related_queries_top_for(term: str, iso2: str, top_k: int) -> list[str]:
    """
    Возвращает ТОЛЬКО связанные запросы для конкретного (term, iso2)
    """
    py = TrendReq(hl='en-US', tz=0)
    py.build_payload([term], timeframe='now 7-d', geo=iso2)
    rq = py.related_queries() or {}
    # структура: {'<term>': {'top': df|None, 'rising': df|None}}
    pack = rq.get(term) or {}
    items = []
    for kind in ("top", "rising"):
        df = pack.get(kind)
        if df is None or df.empty:
            continue
        for _, row in df.head(10).iterrows():
            q = (row.get('query') or '').strip()
            v = int(row.get('value') or 0)
            if q:
                items.append((q, v))
    if not items:
        return []
    # агрегируем одинаковые query
    agg = Counter()
    for q, v in items:
        agg[q] += v
    return [q for q, _ in agg.most_common(top_k)]

# -------------- Twitter (snscrape): хэштеги для (term, country) --------------
def twitter_tags_for(term: str, country_label: str, limit=TW_SAMPLE) -> list[str]:
    if not SNS_OK:
        return []
    # свежий суточный срез по термину + названию страны
    since = (datetime.utcnow() - timedelta(days=1)).date()
    query = f'"{term}" {country_label} since:{since}'
    tags = Counter()
    try:
        for i, tw in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
            if i >= limit:
                break
            for tag in (getattr(tw, "hashtags", []) or []):
                t = str(tag).lower().lstrip("#")
                if t:
                    tags[t] += 1
    except Exception:
        # если у snscrape проблемы с SSL/доступом — возвращаем пусто
        return []
    return [f"#{t}" for t, _ in tags.most_common(6)]

# -------------- Текст и кнопки --------------
def build_message_and_buttons() -> tuple[str, InlineKeyboardMarkup | None]:
    now_kyiv = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")

    lines: list[str] = [
        "📊 <b>Ежедневная аналитика трендов</b>",
        f"🕒 Время анализа: {now_kyiv} (Киев)",
        "🔎 Источники: Google Trends + Twitter (snscrape)",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    for term in SEARCH_TERMS:
        lines.append(f"📌 <b>Запрос</b>: {term}")
        countries = trends_top_countries_for_term(term, TOP_COUNTRIES_PER_TERM)

        if not countries:
            lines.append("— нет данных по странам за 7 дней\n")
            continue

        for (country_name, iso2, score) in countries:
            flag = _flag(iso2)
            local_now, tzkey = _local_time_label(iso2)

            rel_q = related_queries_top_for(term, iso2, TOP_RELATED_QUERIES)
            tags  = twitter_tags_for(term, country_name)

            block = textwrap.dedent(f"""\
                {flag} <b>{country_name}</b> · score {score}
                🕒 Сейчас: {local_now} [{tzkey}]
                📈 Related (Google): {(' · '.join(rel_q) if rel_q else '—')}
                🏷️ Хэштеги X: {(' '.join(tags[:3]) if tags else '—')}
            """).rstrip()
            lines.append(block)

            if BOT_USERNAME:
                deeplink = f"https://t.me/{BOT_USERNAME}?start=copy_{iso2}_{_slug(term)}"
                buttons.append([InlineKeyboardButton(f"📋 Копировать — {country_name} / {term}", url=deeplink)])

        lines.append("")  # пустая строка между запросами

    text = "\n".join(lines).strip()
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    return text, kb

# -------------- Отправка (разбиение на части, if needed) --------------
async def _send_long(bot: Bot, chat_id: str, text: str, **kw):
    LIMIT = 3900  # запас под HTML-теги
    if len(text) <= LIMIT:
        await bot.send_message(chat_id=chat_id, text=text, **kw)
        return
    chunk = []
    total = 0
    parts: list[str] = []
    for line in text.splitlines(True):
        if total + len(line) > LIMIT:
            parts.append("".join(chunk))
            chunk, total = [line], len(line)
        else:
            chunk.append(line); total += len(line)
    if chunk:
        parts.append("".join(chunk))
    for i, p in enumerate(parts):
        await bot.send_message(
            chat_id=chat_id,
            text=p,
            **(kw if i == 0 else {k:v for k,v in kw.items() if k != "reply_markup"})
        )

async def amain():
    bot = Bot(token=TOKEN)
    text, kb = build_message_and_buttons()
    await _send_long(
        bot,
        CHAT_ID,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb
    )

if __name__ == "__main__":
    asyncio.run(amain())