# -*- coding: utf-8 -*-
"""
Ежедневная аналитика (RU) для канала согласования:
- Google Trends: топ-3 страны по ключам (now 7-d)
- Для каждой страны: топ-3 related queries, домены (из queries и твитов), топ хэштеги X
- Кнопки "📋 Копировать — <страна>" -> deeplink в ЛС бота /start copy_<ISO2>

Зависимости: pytrends, snscrape, python-telegram-bot==21.*, pycountry
"""

import os, re, textwrap, ssl, warnings, asyncio
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- SSL workaround для GitHub Actions / snscrape (небезопасно, но нужно для тестов) ---
try:
    ssl._create_default_https_context = ssl._create_unverified_context  # noqa: SLF001
except Exception:
    pass
warnings.filterwarnings("ignore", message=".*certificate verify failed.*", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from pytrends.request import TrendReq
import pycountry

try:
    import snscrape.modules.twitter as sntwitter
    SNS_OK = True
except Exception:
    SNS_OK = False

# ------------------ ENV (тестовые значения — по твоей просьбе) ------------------
TOKEN        = "8326777624:AAG_Owp9T4zsFryttparUnqjqtrVhpHR_LQ"
CHAT_ID      = "-1002892475684"    # канал согласования
BOT_USERNAME = "AiCoinBot"         # без @, для deeplink
APPROVAL_USER_ID = "6105016521"    # твой Telegram ID (на будущее)

# ------------------ НАСТРОЙКИ ------------------
SEARCH_TERMS = ["Ai Coin", "AI crypto", "blockchain AI", "$Ai"]
KYIV_TZ = ZoneInfo("Europe/Kyiv")
MAX_COUNTRIES = 3
TOP_N_QUERIES = 3
TW_SAMPLE = 180  # сколько твитов сэмплировать максимум

DOMAIN_RE = re.compile(r"(?:https?://)?([a-z0-9\-]+\.[a-z\.]{2,})(?:/|$)", re.I)

def _extract_domains(text: str) -> list[str]:
    return [m.group(1).lower() for m in DOMAIN_RE.finditer(text or "")]

def _iso2_from_name(name: str) -> str | None:
    try:
        if name == "United States": name = "United States of America"
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

# ---------------- Безопасные вызовы ----------------
def _safe(callable_, default):
    try:
        return callable_()
    except Exception:
        return default

# ---------------- Google Trends ----------------
def trends_top_countries() -> list[tuple[str, str, int]]:
    def _inner():
        py = TrendReq(hl='en-US', tz=0)
        py.build_payload(SEARCH_TERMS, timeframe='now 7-d', geo='')
        df = py.interest_by_region(resolution='COUNTRY', inc_low_vol=True)
        if df.empty:
            return []
        df['score'] = df.sum(axis=1)
        df = df[df['score'] > 0].sort_values('score', ascending=False)
        out = []
        for name, score in df['score'].head(10).items():
            iso2 = _iso2_from_name(name)
            if iso2:
                out.append((name, iso2, int(score)))
            if len(out) >= 6:
                break
        return out
    return _safe(_inner, [])

def related_queries_top(iso2: str, top_n: int = TOP_N_QUERIES) -> tuple[list[str], list[str]]:
    def _inner():
        py = TrendReq(hl='en-US', tz=0)
        py.build_payload(SEARCH_TERMS, timeframe='now 7-d', geo=iso2)
        rq = py.related_queries()
        q_counter = Counter()
        d_counter = Counter()
        for _, data in (rq or {}).items():
            if not data: continue
            for kind in ("rising", "top"):
                df = data.get(kind)
                if df is None: continue
                for _, row in df.head(12).iterrows():
                    q = str(row.get('query') or '').strip()
                    if not q: continue
                    q_counter[q] += int(row.get('value') or 0)
                    for d in _extract_domains(q):
                        d_counter[d] += 1
        top_queries = [q for q,_ in q_counter.most_common(top_n)]
        top_domains = [d for d,_ in d_counter.most_common(5)]
        return top_queries, top_domains
    return _safe(_inner, ([], []))

# ---------------- Twitter (snscrape) ----------------
def twitter_domains_and_tags(country_label: str, limit=TW_SAMPLE) -> tuple[list[str], list[str]]:
    if not SNS_OK:
        return [], []
    def _inner():
        query = f'("Ai Coin" OR "AI crypto" OR "blockchain AI" OR "$Ai") {country_label} since:{(datetime.utcnow()-timedelta(days=1)).date()}'
        domains = Counter()
        tags = Counter()
        for i, tw in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
            if i >= limit: break
            for d in _extract_domains(getattr(tw, "content", "")):
                domains[d] += 1
            for tag in (getattr(tw, "hashtags", []) or []):
                t = str(tag).lower().lstrip("#")
                if t: tags[t] += 1
        return [d for d,_ in domains.most_common(6)], [f"#{t}" for t,_ in tags.most_common(6)]
    return _safe(_inner, ([], []))

# ---------------- Формирование текста и кнопок ----------------
def build_message_and_buttons():
    now_kyiv = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    countries = trends_top_countries()

    # возьмём первые 3 уникальных по ISO2
    picked, seen = [], set()
    for name, iso2, score in countries:
        if iso2 in seen: continue
        picked.append((name, iso2, score))
        seen.add(iso2)
        if len(picked) >= MAX_COUNTRIES: break

    lines = [
        f"📊 <b>Ежедневная аналитика трендов</b>",
        f"🕒 Время анализа: {now_kyiv} (Киев)",
        f"🔎 Источники: Google Trends + Twitter (snscrape)",
        f"💡 Тема: {', '.join(SEARCH_TERMS)}",
        ""
    ]
    buttons = []

    if not picked:
        lines.append("Данных по странам нет сегодня.")
    else:
        for idx, (country, iso2, score) in enumerate(picked, 1):
            flag = _flag(iso2)
            local_now, tzkey = _local_time_label(iso2)

            top_queries, rq_domains = related_queries_top(iso2)
            tw_domains, tw_tags = twitter_domains_and_tags(country)

            # Объединяем домены (из Trends и Twitter), убираем повторы
            seen_d, domains = set(), []
            for d in (rq_domains + tw_domains):
                if d in seen_d: continue
                seen_d.add(d); domains.append(d)
            if not domains:
                domains = ["getaicoin.com"]

            block = textwrap.dedent(f"""\
                {idx}️⃣ {flag} <b>{country}</b>
                🕒 Локальное время сейчас: {local_now} [{tzkey}]
                📈 Топ‑3 темы (Google): {(' · '.join(top_queries) if top_queries else '—')}
                🌐 Часто встречающиеся сайты: {', '.join(domains[:3])}
                🏷️ Хэштеги X: {(' '.join(tw_tags[:3]) if tw_tags else '#AiCoin #AI #crypto')}
            """).rstrip()
            lines.append(block)

            if BOT_USERNAME:
                deeplink = f"https://t.me/{BOT_USERNAME}?start=copy_{iso2}"
                buttons.append([InlineKeyboardButton(f"📋 Копировать — {country}", url=deeplink)])

    text = "\n\n".join(lines)
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    return text, kb

# ---------------- Отправка (асинхронная) ----------------
async def _send_long(bot: Bot, chat_id: str, text: str, **kw):
    LIMIT = 3900  # запас под HTML-теги
    if len(text) <= LIMIT:
        await bot.send_message(chat_id=chat_id, text=text, **kw)
        return
    parts = []
    cur = ""
    for line in text.splitlines(True):
        if len(cur) + len(line) > LIMIT:
            parts.append(cur)
            cur = line
        else:
            cur += line
    if cur: parts.append(cur)
    for i, p in enumerate(parts):
        await bot.send_message(chat_id=chat_id, text=p, **kw if i == 0 else {k:v for k,v in kw.items() if k != "reply_markup"})

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