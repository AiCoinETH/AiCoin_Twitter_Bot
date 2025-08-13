# -*- coding: utf-8 -*-
"""
Ежедневная аналитика (RU): Топ-3 страны из Google Trends по темам AiCoin/AI/Crypto,
топ-3 подзапроса (related queries), топовые сайты (из related queries и ссылок в твитах),
и кнопки "📋 Копировать — <страна>" (deeplink в ЛС бота).
"""

import os, re, sqlite3, textwrap
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# pip install: pytrends snscrape pycountry babel python-telegram-bot==21.*
from pytrends.request import TrendReq
import pycountry
from babel import Locale
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ------------------ ENV ------------------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
TG_CHAT  = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")      # -100... или @username
BOT_USER = os.getenv("TELEGRAM_BOT_USERNAME")          # без @, для deeplink
if not (TG_TOKEN and TG_CHAT and BOT_USER):
    raise SystemExit("Set TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_BOT_USERNAME")

# Темы мониторинга
TOPICS = ["ai coin", "ai cryptocurrency", "crypto ai", "$ai", "ai+crypto", "ai token"]

# Настройки
MAX_COUNTRIES = 3        # ТОП-3 страны
TOP_N_QUERIES = 3        # ТОП-3 подзапроса/темы внутри страны
KYIV_TZ = ZoneInfo("Europe/Kyiv")
PRIME_HOUR = 19          # локальный прайм-тайм (час)
PRIME_MIN  = 30          # локальный прайм-тайм (мин)

# ---------------- SQLite для «копировать» ----------------
DB = "copy_payloads.db"
def _db_init():
    con = sqlite3.connect(DB)
    con.execute("""
    CREATE TABLE IF NOT EXISTS copy_payloads (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        country TEXT,
        iso2 TEXT,
        payload TEXT NOT NULL
    )""")
    con.commit(); con.close()

def save_copy_payload(copy_id: str, country: str, iso2: str, text: str):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT OR REPLACE INTO copy_payloads (id, created_at, country, iso2, payload) VALUES (?,?,?,?,?)",
        (copy_id, datetime.utcnow().isoformat()+"Z", country, iso2, text)
    )
    con.commit(); con.close()

# ---------------- Утилиты ----------------
def iso2_from_name(name: str) -> str | None:
    try:
        if name == "United States": name = "United States of America"
        return pycountry.countries.lookup(name).alpha_2
    except Exception:
        return None

def flag(iso2: str) -> str:
    if not iso2 or len(iso2)!=2: return "🌍"
    base = 0x1F1E6
    return "".join(chr(base + ord(c.upper()) - ord('A')) for c in iso2)

def local_prime_time(iso2: str):
    tzmap = {
        "UA":"Europe/Kyiv","DE":"Europe/Berlin","TR":"Europe/Istanbul","GB":"Europe/London",
        "US":"America/New_York","CA":"America/Toronto","BR":"America/Sao_Paulo","MX":"America/Mexico_City",
        "IN":"Asia/Kolkata","ID":"Asia/Jakarta","PH":"Asia/Manila","JP":"Asia/Tokyo",
        "AE":"Asia/Dubai","SA":"Asia/Riyadh","NG":"Africa/Lagos","ZA":"Africa/Johannesburg"
    }
    tz = ZoneInfo(tzmap.get(iso2, "UTC"))
    now = datetime.now(tz)
    target = now.replace(hour=PRIME_HOUR, minute=PRIME_MIN, second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return target, tz

DOMAIN_RE = re.compile(r"(?:https?://)?([a-z0-9\-]+\.[a-z\.]{2,})(?:/|$)", re.I)

def extract_domains(text: str) -> list[str]:
    out = []
    for m in DOMAIN_RE.finditer(text or ""):
        out.append(m.group(1).lower())
    return out

# ---------------- Google Trends ----------------
def trends_top_countries() -> list[tuple[str,int]]:
    py = TrendReq(hl='en-US', tz=0)
    py.build_payload(TOPICS, timeframe='now 7-d', geo='')
    df = py.interest_by_region(resolution='COUNTRY', inc_low_vol=True)
    df['score'] = df.sum(axis=1)
    df = df[df['score']>0].sort_values('score', ascending=False)
    rows = [(name, int(score)) for name, score in df['score'].head(20).items()]
    return rows

def related_queries_top(iso2: str, top_n: int = TOP_N_QUERIES) -> tuple[list[str], list[str]]:
    """Возвращает (топ-3 запросов, топ-домены из запросов) для страны."""
    py = TrendReq(hl='en-US', tz=0)
    py.build_payload(TOPICS, timeframe='now 7-d', geo=iso2)
    rq = py.related_queries()

    counter = Counter()
    domain_counter = Counter()

    for _, data in (rq or {}).items():
        if not data: continue
        for kind in ("rising", "top"):
            df = data.get(kind)
            if df is None: continue
            for _, row in df.head(10).iterrows():
                q = str(row.get('query') or '').strip()
                if not q: continue
                counter[q] += int(row.get('value') or 0)
                for d in extract_domains(q):
                    domain_counter[d] += 1

    top_queries = [q for q,_ in counter.most_common(top_n)]
    top_domains = [d for d,_ in domain_counter.most_common(3)]
    return top_queries, top_domains

# ---------------- Twitter (snscrape) ----------------
def twitter_domains_and_tags(country_label: str, limit=200) -> tuple[list[str], list[str]]:
    """
    Быстрый сэмпл твитов: собираем домены ссылок и хэштеги.
    Не требует API. Поиск – по основным темам + названию страны.
    """
    try:
        import snscrape.modules.twitter as sntwitter
    except Exception:
        return [], []

    query = f'("ai coin" OR "ai cryptocurrency" OR "crypto ai" OR "$ai") {country_label} since:{(datetime.utcnow()-timedelta(days=1)).date()}'
    domains = Counter()
    tags = Counter()

    try:
        for i, tw in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
            if i >= limit: break
            # домены из твита
            for d in extract_domains(tw.content):
                domains[d] += 1
            # хэштеги
            for tag in getattr(tw, "hashtags", []) or []:
                tags[str(tag).lower()] += 1
    except Exception:
        pass

    return [d for d,_ in domains.most_common(5)], [f"#{t}" for t,_ in tags.most_common(5)]

# ---------------- Основной процесс ----------------
def main():
    _db_init()
    bot = Bot(token=TG_TOKEN)

    # 1) топ стран по трендам
    countries = trends_top_countries()

    # 2) берём ТОП-3 уникальные страны
    picked = []
    seen = set()
    for name, score in countries:
        iso2 = iso2_from_name(name)
        if not iso2 or iso2 in seen: continue
        picked.append((name, iso2, score))
        seen.add(iso2)
        if len(picked) >= MAX_COUNTRIES: break

    # 3) соберём блоки + кнопки
    header = f"📊 Ежедневная аналитика трендов — {datetime.now(KYIV_TZ):%Y-%m-%d %H:%M} (Киев)\nТемы: {', '.join(TOPICS[:4])}"
    lines = [header, ""]
    keyboard_rows = []

    for idx, (country_name, iso2, score) in enumerate(picked, 1):
        f = flag(iso2)
        local_time, tz = local_prime_time(iso2)

        # Google related queries + домены
        top_queries, rq_domains = related_queries_top(iso2)

        # Twitter: домены и теги (мини-сэмпл последних 24h)
        tw_domains, tw_tags = twitter_domains_and_tags(country_name)

        # Объединим домены (Trends + Twitter) и отсечём шум
        domains = []
        seen_d = set()
        for d in (rq_domains + tw_domains):
            if d in seen_d: continue
            seen_d.add(d); domains.append(d)
        if not domains:
            domains = ["getaicoin.com"]

        # Блок для сообщения в канал
        lines.append(
            textwrap.dedent(f"""\
            {idx}️⃣ {f} <b>{country_name}</b>
            🕒 Прайм-тайм (локально): {local_time:%Y-%m-%d %H:%M} [{tz.key}]
            📈 Топ‑3 темы (Google): {(' · '.join(top_queries) if top_queries else '—')}
            🌐 Часто встречающиеся сайты: {', '.join(domains[:3])}
            🏷️ Хэштеги X: {(' '.join(tw_tags[:3]) if tw_tags else '#AiCoin #AI #crypto')}
            """).rstrip()
        )

        # Текст для «копирования» в ЛС (ровно то, что просил: страна, время, тема(топ3), сайт)
        copy_text = textwrap.dedent(f"""\
        {f} {country_name}
        Время (локально): {local_time:%Y-%m-%d %H:%M} [{tz.key}]
        Топ‑3 темы: {(' · '.join(top_queries) if top_queries else '—')}
        Топ сайты: {', '.join(domains[:3])}
        """).strip()

        copy_id = f"{iso2}_{int(local_time.timestamp())}"
        save_copy_payload(copy_id, country_name, iso2, copy_text)

        deeplink = f"https://t.me/{BOT_USER}?start=copy_{copy_id}"
        keyboard_rows.append([InlineKeyboardButton(f"📋 Копировать — {country_name}", url=deeplink)])

    # 4) отправляем сообщение в канал/группу согласования
    bot.send_message(
        chat_id=TG_CHAT,
        text="\n\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    )

if __name__ == "__main__":
    main()