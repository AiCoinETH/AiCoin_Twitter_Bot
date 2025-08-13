# ops/daily_analytics.py
# -*- coding: utf-8 -*-
"""
Ежедневная аналитика (RU) для канала согласования:
- Google Trends: топ-3 страны по ключам (за сутки, now 1-d)
- Для каждой страны: топ-3 related queries, топ домены (из queries и твитов), топ хэштеги X
- Все домены кликабельные (https://<домен>)
- Кнопки "📋 Копировать — <страна>" -> deeplink в ЛС бота /start copy_<ISO2>

ENV:
  TELEGRAM_BOT_TOKEN_APPROVAL  — токен бота (который пишет в канал согласования)
  TELEGRAM_APPROVAL_CHAT_ID    — id канала/чата согласования (-100...)
  TELEGRAM_BOT_USERNAME        — username бота без @ (для deeplink)
"""

import os
import re
import asyncio
import textwrap
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from pytrends.request import TrendReq
import pycountry

# --- опционально (snscrape может падать в CI из-за SSL) ---
try:
    import snscrape.modules.twitter as sntwitter
    SNS_OK = True
except Exception:
    SNS_OK = False

# ------------------ ENV ------------------
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN_APPROVAL")
CHAT_ID      = os.getenv("TELEGRAM_APPROVAL_CHAT_ID")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")  # без @

if not TOKEN or not CHAT_ID or not BOT_USERNAME:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN_APPROVAL, TELEGRAM_APPROVAL_CHAT_ID, TELEGRAM_BOT_USERNAME")

# ------------------ НАСТРОЙКИ ------------------
SEARCH_TERMS      = ["Ai Coin", "AI crypto", "blockchain AI", "$Ai"]
KYIV_TZ           = ZoneInfo("Europe/Kyiv")
MAX_COUNTRIES     = 3
TOP_N_QUERIES     = 3
TW_SAMPLE_LIMIT   = 250  # сколько твитов просматриваем на страну
TW_DAYS_WINDOW    = 1    # сутки

DOMAIN_RE = re.compile(r"(?:https?://)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)", re.I)

def _extract_domains(text: str) -> list[str]:
    if not text:
        return []
    out = []
    for m in DOMAIN_RE.finditer(text):
        dom = m.group(1).lower().strip(".")
        # отсечём мусор типа t.co, x.com можно оставить, но t.co — переадресация
        if dom and dom not in {"t.co"}:
            out.append(dom)
    return out

def _iso2_from_name(name: str) -> str | None:
    try:
        # pycountry ждёт формальные названия
        if name == "United States":
            name = "United States of America"
        return pycountry.countries.lookup(name).alpha_2
    except Exception:
        return None

def _flag(iso2: str) -> str:
    if not iso2 or len(iso2) != 2:
        return "🌍"
    base = 0x1F1E6
    return "".join(chr(base + ord(c.upper()) - ord('A')) for c in iso2)

def _local_time_label(iso2: str) -> tuple[str, str]:
    # простая карта часовых поясов (на самые частые страны)
    tzmap = {
        "UA":"Europe/Kyiv","DE":"Europe/Berlin","TR":"Europe/Istanbul","GB":"Europe/London",
        "US":"America/New_York","CA":"America/Toronto","BR":"America/Sao_Paulo","MX":"America/Mexico_City",
        "AR":"America/Argentina/Buenos_Aires","CL":"America/Santiago",
        "IN":"Asia/Kolkata","ID":"Asia/Jakarta","PH":"Asia/Manila","JP":"Asia/Tokyo",
        "AE":"Asia/Dubai","SA":"Asia/Riyadh","NG":"Africa/Lagos","ZA":"Africa/Johannesburg",
        "AU":"Australia/Sydney"
    }
    tz = ZoneInfo(tzmap.get(iso2, "UTC"))
    now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    return now_local, tz.key

# ---------------- Google Trends ----------------
def trends_top_countries() -> list[tuple[str, str, int, dict]]:
    """
    Возвращает: [(country_name, iso2, sum_score, per_term_scores_dict), ...]
    sum_score — сумма по всем ключам, per_term_scores: {'Ai Coin': 100, ...}
    """
    py = TrendReq(hl='en-US', tz=0)
    py.build_payload(SEARCH_TERMS, timeframe='now 1-d', geo='', gprop='')
    df = py.interest_by_region(resolution='COUNTRY', inc_low_vol=True)
    if df.empty:
        return []

    # нормализуем названия колонок и пустые значения
    df = df.fillna(0)
    # суммарный интерес по всем ключам
    df['__sum__'] = df.sum(axis=1)
    # оставим только страны, где есть хоть какой-то интерес
    df = df[df['__sum__'] > 0].sort_values('__sum__', ascending=False)

    out = []
    for country_name, row in df.head(12).iterrows():
        iso2 = _iso2_from_name(country_name)
        if not iso2:
            continue
        per_term = {term: int(row.get(term, 0)) for term in SEARCH_TERMS}
        out.append((country_name, iso2, int(row['__sum__']), per_term))
        if len(out) >= 6:
            break
    return out

def related_queries_top(iso2: str, top_n: int = TOP_N_QUERIES) -> list[str]:
    """
    Берём related queries по всем ключам внутри страны iso2 и возвращаем топ‑N
    по частоте появления/весу (суммируем 'value' у top/rising).
    """
    py = TrendReq(hl='en-US', tz=0)
    py.build_payload(SEARCH_TERMS, timeframe='now 1-d', geo=iso2, gprop='')
    rq = py.related_queries()
    q_counter = Counter()

    for _, data in (rq or {}).items():
        if not data:
            continue
        for kind in ("rising", "top"):
            df = data.get(kind)
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                q = str(r.get('query') or '').strip()
                if not q:
                    continue
                # Google отдаёт value ~ относительный вес
                q_counter[q] += int(r.get('value') or 0)

    top = [q for q, _ in q_counter.most_common(top_n)]
    return top

# ---------------- Twitter (snscrape) ----------------
def twitter_domains_and_tags(country_label: str, limit=TW_SAMPLE_LIMIT) -> tuple[list[str], list[str]]:
    """
    Ищем твиты за сутки по ключам + упоминание страны в тексте твита.
    Возвращаем (топ_домены, топ_хэштеги)
    """
    if not SNS_OK:
        return [], []

    since_date = (datetime.utcnow() - timedelta(days=TW_DAYS_WINDOW)).date()
    query = f'("Ai Coin" OR "AI crypto" OR "blockchain AI" OR "$Ai") {country_label} since:{since_date}'
    domains = Counter()
    tags = Counter()
    try:
        for i, tw in enumerate(sntwitter.TwitterSearchScraper(query).get_items()):
            if i >= limit:
                break
            content = getattr(tw, "content", "") or ""
            # домены из текста и URL
            for d in _extract_domains(content):
                domains[d] += 1
            # иногда ссылки лежат в tw.outlinks
            for u in getattr(tw, "outlinks", []) or []:
                for d in _extract_domains(u):
                    domains[d] += 1
            for tag in (getattr(tw, "hashtags", []) or []):
                t = str(tag).lower().lstrip("#")
                if t:
                    tags[t] += 1
    except Exception:
        # если вдруг SSL/блоки — молча отдадим пустые
        return [], []

    top_domains = [d for d, _ in domains.most_common(6)]
    top_tags    = [f"#{t}" for t, _ in tags.most_common(6)]
    return top_domains, top_tags

def _linkify(domains: list[str]) -> list[str]:
    """
    Делает домены кликабельными: https://<domain>
    """
    out = []
    for d in domains:
        if not d:
            continue
        if not d.startswith("http://") and not d.startswith("https://"):
            out.append(f"https://{d}")
        else:
            out.append(d)
    return out

# ---------------- Формирование отчёта ----------------
def build_message_and_buttons() -> tuple[str, InlineKeyboardMarkup | None]:
    now_kyiv = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    countries = trends_top_countries()

    # выберем уникальные по ISO2, максимум MAX_COUNTRIES
    picked = []
    seen = set()
    for country_name, iso2, sum_score, per_term in countries:
        if iso2 in seen:
            continue
        picked.append((country_name, iso2, sum_score, per_term))
        seen.add(iso2)
        if len(picked) >= MAX_COUNTRIES:
            break

    lines = [
        f"📊 <b>Ежедневная аналитика трендов</b>",
        f"🕒 Время анализа: {now_kyiv} (Киев)",
        f"🔎 Источники: Google Trends (now 1‑d) + Twitter (snscrape, {TW_DAYS_WINDOW}d)",
        f"💡 Ключи: {', '.join(SEARCH_TERMS)}",
        ""
    ]
    buttons = []

    if not picked:
        lines.append("Данных по странам сегодня нет.")
        return "\n".join(lines), None

    for idx, (country, iso2, sum_score, per_term) in enumerate(picked, 1):
        flag = _flag(iso2)
        local_now, tzkey = _local_time_label(iso2)

        # Google: related queries + домены из них
        rq = related_queries_top(iso2, top_n=TOP_N_QUERIES)
        rq_domains = []
        for q in rq:
            rq_domains.extend(_extract_domains(q))
        # уникализировать домены
        rq_domains = list(dict.fromkeys(rq_domains))

        # Twitter: домены + теги
        tw_domains, tw_tags = twitter_domains_and_tags(country)

        # Свести домены в один список (GoogleRQ + Twitter), обрезать до 5
        combined_domains = list(dict.fromkeys(rq_domains + tw_domains))[:5]
        combined_links = _linkify(combined_domains)
        links_str = ", ".join(f'<a href="{u}">{u.replace("https://","").replace("http://","")}</a>' for u in combined_links) if combined_links else "—"

        per_terms_str = " · ".join(f'{k} ({v})' for k, v in per_term.items() if v > 0) or "—"
        tags_str = " ".join(tw_tags[:5]) if tw_tags else "#AiCoin #AI #crypto"

        block = textwrap.dedent(f"""\
            {idx}️⃣ {flag} <b>{country}</b>
            🕒 Локальное время: {local_now} [{tzkey}]
            📈 Интерес (по ключам): {per_terms_str}
            📝 Топ запросы (Google): {( " · ".join(rq) if rq else "—" )}
            🌐 Сайты: {links_str}
            🏷️ Теги X: {tags_str}
        """).rstrip()
        lines.append(block)
        lines.append("")  # пустая строка-разделитель

        deeplink = f"https://t.me/{BOT_USERNAME}?start=copy_{iso2}"
        buttons.append([InlineKeyboardButton(f"📋 Копировать — {country}", url=deeplink)])

    text = "\n".join(lines).strip()
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    return text, kb

# ---------------- Отправка ----------------
async def send_report():
    bot = Bot(token=TOKEN)
    text, kb = build_message_and_buttons()
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=kb
    )

def main():
    asyncio.run(send_report())

if __name__ == "__main__":
    main()