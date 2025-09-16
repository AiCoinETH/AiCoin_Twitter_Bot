# ai_client.py
# -*- coding: utf-8 -*-

import os
import io
import re
import json
import time
import base64
import hashlib
import random
import logging
import sqlite3
import tempfile
import datetime as dt
from typing import Dict, List, Optional, Tuple, Callable

import requests

# Pillow cover generator (fallback & default)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# -----------------------------------------------------------------------------
# Optional deps (graceful fallbacks)
# -----------------------------------------------------------------------------

# Trends (both are optional at runtime)
try:
    from pytrends.request import TrendReq  # type: ignore
    _pytrends_ok = True
except Exception:
    _pytrends_ok = False

try:
    # lightweight scraping of public tweets; no API key needed (может ломаться)
    import snscrape.modules.twitter as sntwitter  # type: ignore
    _sns_ok = True
except Exception:
    _sns_ok = False

# Gemini text / media
_genai_ok = False
_genai_images_ok = False
try:
    import google.generativeai as genai  # type: ignore
    _genai_ok = True
    try:
        # Официальный модуль для генерации изображений Gemini Images API
        from google.generativeai import images as gen_images  # type: ignore
        _genai_images_ok = True
    except Exception:
        _genai_images_ok = False
except Exception:
    _genai_ok = False
    _genai_images_ok = False

# Vertex AI (Imagen via vertexai SDK)
_vertex_ok = False
try:
    import json as _json_for_vertex
    import vertexai  # type: ignore
    try:
        from vertexai.preview.vision_models import ImageGenerationModel  # type: ignore
        _vertex_ok = True
    except Exception:
        _vertex_ok = False
except Exception:
    _vertex_ok = False

# Опциональные зависимости для локального видео-фолбэка
try:
    import numpy as _np  # type: ignore
    import imageio.v3 as _iio  # type: ignore
    _vid_local_ok = True
except Exception:
    _vid_local_ok = False

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TEXT_MODEL  = os.getenv("GEMINI_TEXT_MODEL",  "gemini-1.5-pro")

# ВНИМАНИЕ:
# GEMINI_IMAGE_MODEL относится к Gemini Images API (а не Vertex). Для него обычно
# используются имена семейств "imagen-3.0" и т.п. Оставляем как есть — это запасной путь.
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "imagen-3.0")

# Видеогенерация через Gemini/Veo — как экспериментальный фолбэк
GEMINI_VIDEO_MODEL = os.getenv("GEMINI_VIDEO_MODEL", "veo-1.0")

# Vertex AI (доп. путь для картинок через Imagen 4)
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON", "").strip()
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT", "").strip()  # явное указание project (опционально)
# Модель по умолчанию — быстрая 4.0 Fast; можно переопределить переменной окружения:
#   VERTEX_IMAGEN_MODEL=imagen-4.0-generate-001 (полное качество)
VERTEX_IMAGEN_MODEL_DEFAULT = "imagen-4.0-fast-generate-001"

_vertex_inited = False
_vertex_err: Optional[str] = None

# Длина поста под Twitter/Telegram
TARGET_CHAR_LEN = int(os.getenv("TARGET_CHAR_LEN", "666"))
TARGET_CHAR_TOL = int(os.getenv("TARGET_CHAR_TOL", "20"))

# GitHub upload
ACTION_PAT_GITHUB = os.getenv("ACTION_PAT_GITHUB", "")
ACTION_REPO_GITHUB = os.getenv("ACTION_REPO_GITHUB", "")  # owner/repo
ACTION_BRANCH = os.getenv("ACTION_BRANCH", "main")

# Папки для медиа в репозитории
GH_IMAGES_DIR = os.getenv("GH_IMAGES_DIR", "images_for_posts")
GH_VIDEOS_DIR = os.getenv("GH_VIDEOS_DIR", "videos_for_posts")

# Локальные пути
LOCAL_MEDIA_DIR = os.getenv("LOCAL_MEDIA_DIR", "./images_for_posts")
os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)
LOCAL_VIDEO_DIR = os.getenv("LOCAL_VIDEO_DIR", "./videos_for_posts")
os.makedirs(LOCAL_VIDEO_DIR, exist_ok=True)

# Авто-аплоад
AUTO_UPLOAD_IMAGE_TO_GH = (os.getenv("AUTO_UPLOAD_IMAGE_TO_GH", "1") or "1").lower() not in ("0", "false", "no")
AUTO_UPLOAD_VIDEO_TO_GH = (os.getenv("AUTO_UPLOAD_VIDEO_TO_GH", "1") or "1").lower() not in ("0", "false", "no")

# База для дедупликации
DEDUP_DB_PATH = os.getenv("DEDUP_DB_PATH", "./history.db")
DEDUP_TTL_DAYS = int(os.getenv("DEDUP_TTL_DAYS", "15"))

# Логирование
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ai_client")
log_http = logging.getLogger("ai_client.http")
log_gh = logging.getLogger("ai_client.github")

_RAW_GH = "https://raw.githubusercontent.com"
_UA = {"User-Agent": "AiCoinBot/1.0 (+https://x.com/AiCoin_ETH)"}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _sha_short(data: bytes, n: int = 12) -> str:
    return hashlib.sha256(data).hexdigest()[:n]

def _sha_text(text: str, n: int = 12) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:n]

def _now_ts() -> int:
    return int(time.time())

def _clean_bracket_hints(text: str) -> str:
    # убираем [скобочные подсказки] и любые подсказки в круглых <> <...>
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\<[^\>]*\>", "", text)
    # убираем маркдаун-подсказки вида (**рекомендация**)
    text = re.sub(r"\(\*{1,2}[^)]*\*{1,2}\)", "", text)
    # никаких ссылок
    text = re.sub(r"https?://\S+", "", text)
    # двойные пробелы, хвостовые переводы строк
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    # Убираем возможные заголовки-шаблоны
    text = re.sub(r"(Website\s*\|\s*Twitter\s*X)\s*", "", text, flags=re.I)
    return text.strip()

def _clamp_to_len(text: str, target: int, tol: int) -> str:
    min_len, max_len = target - tol, target + tol
    s = (text or "").strip()
    if len(s) <= max_len and len(s) >= min_len:
        return s
    if len(s) > max_len:
        cut = s[:max_len]
        # завершить на последней точке/восклиц/вопросе в диапазоне
        m = re.search(r"(?s)[.!?…](?!.*[.!?…]).*", cut)
        if m:
            cut = cut[:m.end()].strip()
        return cut.strip()
    return s

def _detect_lang(s: str) -> str:
    """Грубый авто-детект + уважение явных подсказок."""
    txt = (s or "").strip()
    if re.search(r"\[(en|eng|english)\]|\b(en|english)\b|на\s+англ", txt, re.I):
        return "en"
    if re.search(r"\[(ru|rus|russian)\]|\b(ru|russian|по-русски|на\s+русском)\b", txt, re.I):
        return "ru"
    if re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", txt):
        return "ru"
    return "en"

# Pillow ≥10 — используем textbbox
def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    w = max(0, bbox[2] - bbox[0])
    h = max(0, bbox[3] - bbox[1])
    return w, h

# Подробный лог о локальном файле (размер, сигнатура)
def _log_file_info(path: str, logger=log):
    try:
        if not os.path.exists(path):
            logger.warning("FS|missing file: %s", path); return
        st = os.stat(path)
        with open(path, "rb") as f:
            head = f.read(16)
        logger.info("FS|file=%s size=%dB head=%s", path, st.st_size, head.hex())
    except Exception as e:
        logger.warning("FS|info error for %s: %s", path, e)

# -----------------------------------------------------------------------------
# Dedup storage (SQLite)
# -----------------------------------------------------------------------------
class Deduper:
    def __init__(self, path: str):
        self.path = path
        self._ensure()

    def _ensure(self):
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_hash TEXT,
                    img_hash  TEXT,
                    created_at INTEGER
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_created ON posts(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_text ON posts(text_hash)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_img ON posts(img_hash)")
            conn.commit()
        finally:
            conn.close()

    def purge_old(self, ttl_days: int = DEDUP_TTL_DAYS):
        cutoff = _now_ts() - ttl_days * 86400
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM posts WHERE created_at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    def is_duplicate(self, text: Optional[str], img_bytes: Optional[bytes]) -> bool:
        self.purge_old()
        th = _sha_text(text) if text else None
        ih = _sha_short(img_bytes) if img_bytes else None
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            if th and ih:
                cur.execute("SELECT 1 FROM posts WHERE text_hash=? OR img_hash=? LIMIT 1", (th, ih))
            elif th:
                cur.execute("SELECT 1 FROM posts WHERE text_hash=? LIMIT 1", (th,))
            elif ih:
                cur.execute("SELECT 1 FROM posts WHERE img_hash=? LIMIT 1", (ih,))
            else:
                return False
            row = cur.fetchone()
            return bool(row)
        finally:
            conn.close()

    def record(self, text: Optional[str], img_bytes: Optional[bytes]):
        th = _sha_text(text) if text else None
        ih = _sha_short(img_bytes) if img_bytes else None
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO posts(text_hash, img_hash, created_at) VALUES(?,?,?)",
                (th, ih, _now_ts()),
            )
            conn.commit()
        finally:
            conn.close()

DEDUP = Deduper(DEDUP_DB_PATH)

# -----------------------------------------------------------------------------
# Trends
# -----------------------------------------------------------------------------
_DEFAULT_TOPICS = ["AI", "Bitcoin", "Ethereum", "Solana", "DeFi", "OpenAI", "Gemini", "Web3", "L2", "NFT"]

def get_google_trends(limit: int = 7) -> List[str]:
    if not _pytrends_ok:
        log.info("Trends|pytrends not available")
        return _DEFAULT_TOPICS[:limit]
    try:
        t0 = time.time()
        pytrends = TrendReq(hl='ru-RU', tz=180)
        seeds = ["AI", "криптовалюта", "биткоин", "эфириум", "солана", "нейросеть", "web3"]
        seen: List[str] = []
        seenset = set()
        for kw in seeds:
            pytrends.build_payload([kw], timeframe="now 7-d", geo="")
            rel = pytrends.related_topics()
            for v in rel.values():
                try:
                    rising = v["rising"]
                    for _, row in rising.head(5).iterrows():
                        q = str(row.get("topic_title") or row.get("query") or "").strip()
                        if not q:
                            continue
                        ql = q.lower()
                        if ql not in seenset:
                            seenset.add(ql)
                            seen.append(q)
                            if len(seen) >= limit:
                                log.info("Trends|pytrends ok: %d items in %.2fs", len(seen), time.time()-t0)
                                return seen
                except Exception:
                    continue
        log.info("Trends|pytrends ok: %d items in %.2fs", len(seen), time.time()-t0)
        return seen[:limit] or _DEFAULT_TOPICS[:limit]
    except Exception as e:
        log.info("Trends|pytrends fallback: %s", e)
        return _DEFAULT_TOPICS[:limit]

def get_x_hashtags(limit: int = 7) -> List[str]:
    if not _sns_ok:
        log.info("Trends|snscrape not available")
        return []
    try:
        since = (dt.date.today() - dt.timedelta(days=2)).isoformat()
        query = f'(AI OR crypto OR bitcoin OR ethereum OR solana OR web3) lang:ru OR lang:en since:{since}'
        log.info("X|query: %s", query)
        tags: Dict[str, int] = {}
        cnt = 0
        for tw in sntwitter.TwitterSearchScraper(query).get_items():
            txt = getattr(tw, "rawContent", "") or getattr(tw, "content", "")
            for tag in re.findall(r"#\w{3,30}", txt):
                tags[tag.lower()] = tags.get(tag.lower(), 0) + 1
            cnt += 1
            if cnt >= 200:
                break
        top = sorted(tags.items(), key=lambda x: x[1], reverse=True)
        res = [k for k, _ in top[:limit]]
        log.info("X|scanned=%d, unique_tags=%d, top=%s", cnt, len(tags), " ".join(res))
        return res
    except Exception as e:
        log.info("Trends|snscrape fallback: %s", e)
        return []

# -----------------------------------------------------------------------------
# Prices (CoinGecko, no key)
# -----------------------------------------------------------------------------
def fetch_prices_coingecko_simple(vs: str = "usd") -> Dict[str, Dict[str, float]]:
    """
    Без ключа: /simple/price. Возвращает {'bitcoin': {'usd': 000}, 'ethereum': ...}
    """
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin,ethereum,solana", "vs_currencies": vs}
    try:
        r = requests.get(url, params=params, headers=_UA, timeout=12)
        if r.status_code == 429:
            log.info("CG|rate limited, backing off")
            time.sleep(1.2)
            r = requests.get(url, params=params, headers=_UA, timeout=12)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        log.info("CG|error: %s", e)
        return {}

def build_prices_context() -> str:
    data = fetch_prices_coingecko_simple("usd")
    if not data:
        return ""
    parts = []
    def fmt(sym: str, key: str):
        v = data.get(key, {}).get("usd")
        if v is None:
            return None
        if v >= 1000:
            s = f"${v:,.0f}"
        elif v >= 1:
            s = f"${v:,.2f}"
        else:
            s = f"${v:.4f}"
        return f"{sym} {s}"
    for sym, key in [("BTC", "bitcoin"), ("ETH", "ethereum"), ("SOL", "solana")]:
        t = fmt(sym, key)
        if t:
            parts.append(t)
    return "; ".join(parts)

# -----------------------------------------------------------------------------
# News (Google News + crypto RSS, no keys)
# -----------------------------------------------------------------------------
from xml.etree import ElementTree as ET
from urllib.parse import quote_plus

def _fetch_rss(url: str, timeout: int = 12) -> List[Dict[str, str]]:
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out: List[Dict[str, str]] = []
        # RSS 2.0
        for item in root.findall(".//item"):
            t = (item.findtext("title") or "").strip()
            l = (item.findtext("link") or "").strip()
            p = (item.findtext("pubDate") or "").strip()
            if t and l:
                out.append({"title": t, "link": l, "published": p})
        # Atom
        if not out:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for e in root.findall(".//a:entry", ns):
                t = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
                link_el = e.find("a:link", ns)
                l = (link_el.get("href") if link_el is not None else "").strip()
                p = (e.findtext("a:updated", default="", namespaces=ns) or "").strip()
                if t and l:
                    out.append({"title": t, "link": l, "published": p})
        return out
    except Exception as e:
        log.info("RSS|error: %s", e)
        return []

def fetch_google_news(query: str, lang="ru", country="UA", n: int = 5) -> List[Dict[str, str]]:
    base = "https://news.google.com/rss/search"
    q = quote_plus(query)
    url = f"{base}?q={q}&hl={lang}&gl={country}&ceid={country}:{lang}"
    items = _fetch_rss(url)
    return items[:n]

def fetch_crypto_feeds(n: int = 5) -> List[Dict[str, str]]:
    feeds = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://www.theblock.co/rss",
        "https://decrypt.co/feed",
        "https://www.reuters.com/markets/crypto/rss",
    ]
    items: List[Dict[str, str]] = []
    for u in feeds:
        items.extend(_fetch_rss(u))
        if len(items) >= n:
            break
    # дедуп по заголовку
    seen = set()
    out: List[Dict[str, str]] = []
    for it in items:
        k = it["title"].lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
        if len(out) >= n:
            break
    return out

def build_news_context(max_items: int = 3, user_query: Optional[str] = None) -> str:
    items: List[Dict[str, str]] = []
    if user_query:
        items = fetch_google_news(user_query, n=max_items)
    if not items:
        items = fetch_crypto_feeds(n=max_items)
    lines = []
    for it in items[:max_items]:
        title = it["title"].strip()
        link  = it["link"].strip()
        pub   = it.get("published", "").strip()
        lines.append(f"- {title} (источник: {link}; время: {pub})")
    return "\n".join(lines)

# -----------------------------------------------------------------------------
# Gemini init
# -----------------------------------------------------------------------------
if _genai_ok and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        _model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
        log.info("Gemini configured: %s", GEMINI_TEXT_MODEL)
    except Exception as e:
        log.warning("Gemini init failed: %s", e)
        _model = None
else:
    _model = None
    log.info("Gemini not available (package or key missing)")
if _genai_images_ok:
    log.info("Gemini Images API available")
else:
    log.info("Gemini Images API NOT available (fallback → Pillow)")

# -----------------------------------------------------------------------------
# Vertex init
# -----------------------------------------------------------------------------
def _init_vertex_ai_once() -> bool:
    """
    Инициализация vertexai.init(project, location) один раз.

    Источники кредов:
      1) GCP_KEY_JSON: raw JSON | base64(JSON) | путь к *.json
      2) GOOGLE_APPLICATION_CREDENTIALS: уже указывает на файл с ключом/ADC
    Если указан VERTEX_PROJECT — используем его; иначе берём project_id из ключа.
    """
    global _vertex_inited, _vertex_err
    if _vertex_inited:
        return True
    try:
        if not _vertex_ok:
            _vertex_err = "vertexai package not available"
            return False

        project_id: Optional[str] = (VERTEX_PROJECT or None)
        cred_path: Optional[str] = None
        key_bytes: Optional[bytes] = None
        svc_email: Optional[str] = None

        # 1) Если уже задан GOOGLE_APPLICATION_CREDENTIALS и он существует — используем его
        gac = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
        if gac and os.path.exists(gac):
            cred_path = gac
            try:
                with open(gac, "rb") as f:
                    kb = f.read()
                key_bytes = kb
                try:
                    jd = _json_for_vertex.loads(kb.decode("utf-8"))
                    project_id = project_id or jd.get("project_id") or jd.get("project") or jd.get("quota_project_id")
                    svc_email = jd.get("client_email")
                except Exception:
                    pass
            except Exception:
                pass

        # 2) Иначе разбираем GCP_KEY_JSON (raw/base64/путь)
        if not cred_path:
            if not GCP_KEY_JSON:
                _vertex_err = "no credentials: set GOOGLE_APPLICATION_CREDENTIALS or GCP_KEY_JSON"
                return False

            if GCP_KEY_JSON.startswith("{"):
                key_bytes = GCP_KEY_JSON.encode("utf-8")
            elif GCP_KEY_JSON.endswith(".json") or GCP_KEY_JSON.startswith("/"):
                if os.path.exists(GCP_KEY_JSON):
                    cred_path = GCP_KEY_JSON
                    try:
                        with open(GCP_KEY_JSON, "rb") as f:
                            key_bytes = f.read()
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
                    except Exception:
                        key_bytes = None
                else:
                    _vertex_err = f"Key file not found: {GCP_KEY_JSON}"
                    return False
            else:
                # вероятно base64
                try:
                    key_bytes = base64.b64decode(GCP_KEY_JSON)
                except Exception:
                    key_bytes = GCP_KEY_JSON.encode("utf-8")

            # если у нас байты — кладём во временный файл и экспортируем GAC
            if key_bytes and not cred_path:
                tmp_dir = tempfile.mkdtemp(prefix="gcp_cred_")
                cred_path = os.path.join(tmp_dir, "key.json")
                with open(cred_path, "wb") as f:
                    f.write(key_bytes)
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path

            # вытащим project_id/email из JSON, если ещё не известны
            if key_bytes:
                try:
                    jd = _json_for_vertex.loads(key_bytes.decode("utf-8"))
                    project_id = project_id or jd.get("project_id") or jd.get("project") or jd.get("quota_project_id")
                    svc_email = svc_email or jd.get("client_email")
                except Exception:
                    pass

        if not cred_path or not os.path.exists(cred_path):
            _vertex_err = "credentials file missing/unreadable"
            return False

        if not project_id:
            _vertex_err = "project_id not found (set VERTEX_PROJECT or use a service key with project_id)"
            return False

        # Экспортируем project vars — это иногда критично для биллинга/квот
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
        os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", project_id)

        vertexai.init(project=project_id, location=VERTEX_LOCATION)
        _vertex_inited = True
        log.info(
            "VertexAI initialized: project=%s location=%s creds=%s svc=%s",
            project_id, VERTEX_LOCATION, cred_path, (svc_email or "n/a")
        )
        return True
    except Exception as e:
        _vertex_err = f"vertex init error: {e}"
        log.warning("VertexAI init failed: %s", e)
        return False

# -----------------------------------------------------------------------------
# Crypto normalization (tickers + hashtags)
# -----------------------------------------------------------------------------
# Варианты написания → тикер
_CRYPTO_VARIANTS: Dict[str, List[str]] = {
    "BTC": [r"bitcoin", r"биткоин", r"биткойн", r"btc"],
    "ETH": [r"ethereum", r"эфириум", r"eth"],
    "SOL": [r"solana", r"солана", r"sol"],
    "XRP": [r"ripple", r"xrp"],
    "BNB": [r"binance\s*coin", r"\bbnb\b"],
    "DOGE": [r"dogecoin", r"додж(?:коин|койн)?", r"doge"],
    "TON": [r"\bton(?:coin)?\b", r"тон(?:коин|койн)?"],
    "ADA": [r"cardano", r"кардано", r"\bada\b"],
    "DOT": [r"polkadot", r"полкадот", r"\bdot\b"],
    "AVAX":[r"avalanche", r"\bavax\b"],
    "MATIC":[r"polygon", r"\bmatic\b"],
    "TRX":[r"tron", r"\btrx\b"],
}

# Скомпилируем паттерны (чувствительны к словам; игнорируем, если уже есть $TICKER/#TICKER)
_CR_PATTERNS: List[Tuple[str, re.Pattern]] = []
for ticker, variants in _CRYPTO_VARIANTS.items():
    pat = r"(?i)(?<![\$#])\b(?:" + "|".join(variants) + r")\b"
    _CR_PATTERNS.append((ticker, re.compile(pat, flags=re.IGNORECASE)))

def _ensure_crypto_tickers_and_hashtags(text: str) -> str:
    """
    1) Заменяет полные названия монет на $TICKER.
    2) Добавляет #TICKER в конец (если ещё нет).
    """
    if not text:
        return text
    found: List[str] = []
    s = text

    # Замена на $TICKER
    for ticker, pat in _CR_PATTERNS:
        if pat.search(s):
            s = pat.sub(f"${ticker}", s)
            found.append(ticker)

    # Добавить #TICKER (без дублей) — аккуратно, чтобы не ломать длину: максимум 4 тега
    if found:
        found_uniq = []
        seen = set()
        for t in found:
            if t not in seen:
                seen.add(t); found_uniq.append(t)
        # уже присутствующие #TICKER не дублируем
        existing_tags = set(m.group(1).upper() for m in re.finditer(r"#([A-Za-z]{2,10})\b", s))
        add_tags = [t for t in found_uniq if t not in existing_tags][:4]
        if add_tags:
            tail = " " + " ".join(f"#{t}" for t in add_tags)
            s = (s.rstrip() + tail).strip()

    return s

# -----------------------------------------------------------------------------
# Text generation
# -----------------------------------------------------------------------------
def generate_text(topic: str, locale_hint: Optional[str] = None) -> str:
    lang = (locale_hint or _detect_lang(topic)).lower()

    # контексты: тренды/тэги (могут пустить fallback), цены, новости
    trends = get_google_trends()
    tags = get_x_hashtags()
    trend_bits = ", ".join(trends[:5]) if trends else ""
    tag_bits = " ".join(tags[:5]) if tags else ""

    prices_ctx = build_prices_context()
    news_ctx = build_news_context(max_items=3, user_query=topic)

    prices_block = f"\nАктуальные цены (CoinGecko, USD): {prices_ctx}\n" if prices_ctx else "\nАктуальные цены недоступны — не указывай конкретные числа.\n"
    news_block = f"\nНовости для контекста (используй факты только отсюда, ссылки НЕ вставляй в итог):\n{news_ctx}\n" if news_ctx else "\nНовости недоступны — не упоминай конкретные события/даты.\n"

    # Разрешаем тикеры/хэштеги именно для монет
    prompt = f"""
Сгенерируй короткий пост для соцсетей на языке "{'Русский' if lang=='ru' else 'English'}".
Тема: {topic}

{prices_block}
{news_block}

Обязательные требования:
- Длина {TARGET_CHAR_LEN}±{TARGET_CHAR_TOL} символов.
- Никаких подсказок в квадратных скобках и без служебных пометок.
- Без ссылок.
- Разрешены тикеры и хэштеги ТОЛЬКО для монет (например: $BTC и #BTC), но не для всего подряд.
- Фактура и актуальность: опирайся на популярные темы и формулировки из трендов Google и X(Twitter).
- Пиши как инфо-пост/наблюдение, польза и конкретика.
- Тон: бодрый, уверенный, без кликбейта, без эмодзи.
- Факты, события и ЧИСЛА бери только из блоков «Актуальные цены» и «Новости». Не выдумывай новые цифры.

Подсказки по трендам (не вставляй дословно как список, используй смысл): {trend_bits} {tag_bits}
"""
    text = ""
    if _model:
        try:
            resp = _model.generate_content(prompt)
            text = (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            log.warning("Gemini text error: %s", e)

    if not text:
        base = f"{topic.strip().capitalize()}: актуальные наблюдения без воды. "
        text = base + "Фокус на практической пользе, рисках и возможностях, чтобы принимать быстрые решения."

    text = _clean_bracket_hints(text)

    # если модель сорвалась не на тот язык — мини-фикс
    if lang == "en" and re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", text):
        try:
            if _model:
                resp2 = _model.generate_content(
                    "Rewrite in concise English (no links):\n" + text
                )
                text2 = (getattr(resp2, "text", "") or "").strip()
                if text2:
                    text = text2
        except Exception:
            pass

    # >>> НОРМАЛИЗАЦИЯ КРИПТО-УПОМИНАНИЙ
    text = _ensure_crypto_tickers_and_hashtags(text)
    # <<<

    text = _clamp_to_len(text, TARGET_CHAR_LEN, TARGET_CHAR_TOL)
    log.info("Text|len=%d lang=%s", len(text), lang)
    return text

# -----------------------------------------------------------------------------
# Image generation (Gemini → Vertex → Pillow fallback)
# -----------------------------------------------------------------------------
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()

def _neon_bg(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), (10, 12, 20))
    draw = ImageDraw.Draw(img)
    for _ in range(6):
        cx, cy = random.randint(0, w), random.randint(0, h)
        r = random.randint(int(min(w, h)*0.15), int(min(w, h)*0.45))
        col = random.choice([(30,180,255), (80,255,200), (0,255,130), (130,160,255)])
        grad = Image.new("L", (r*2, r*2), 0)
        gd = ImageDraw.Draw(grad)
        for i in range(r, 0, -1):
            a = int(255 * (i/r)**2)
            gd.ellipse((r-i, r-i, r+i, r+i), fill=a)
        glow = Image.new("RGB", (r*2, r*2), col)
        img.paste(glow, (cx-r, cy-r), grad)
    return img.filter(ImageFilter.GaussianBlur(2))

def _draw_circuit(draw: ImageDraw.ImageDraw, w: int, h: int):
    for _ in range(55):
        x1 = random.randint(40, w-40)
        y1 = random.randint(40, h-40)
        x2 = x1 + random.randint(-220, 220)
        y2 = y1 + random.randint(-120, 120)
        col = (random.randint(60,120), random.randint(200,255), random.randint(200,255))
        draw.line((x1,y1,x2,y2), fill=col, width=random.randint(1,3))
        for (cx, cy) in [(x1,y1),(x2,y2)]:
            r = random.randint(2,4)
            draw.ellipse((cx-r, cy-r, cx+r, cy+r), fill=col)

def _draw_tokens(draw: ImageDraw.ImageDraw, w: int, h: int):
    tokens = ["AI", "BTC", "Ξ", "SOL", "L2", "DeFi"]
    for _ in range(10):
        t = random.choice(tokens)
        fs = random.randint(24, 56)
        f = _load_font(fs)
        x = random.randint(30, w-150)
        y = random.randint(30, h-100)
        fill = random.choice([(240,240,255), (180,255,230), (130,200,255)])
        draw.text((x, y), t, font=f, fill=fill)

def _cover_from_topic(topic: str, text: str, size=(1280, 960)) -> bytes:
    w, h = size
    img = _neon_bg(w, h)
    draw = ImageDraw.Draw(img)
    _draw_circuit(draw, w, h)
    _draw_tokens(draw, w, h)

    cx, cy = w//2, int(h*0.42)
    for r, col, wd in [
        (210, (255, 210, 60), 5),
        (170, (255, 240, 120), 3),
        (120, (255, 255, 190), 2),
    ]:
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), outline=col, width=wd)

    head = re.sub(r"\s+", " ", topic).strip()[:64]
    title_font = _load_font(72)
    tw, _ = _measure_text(draw, head, title_font)
    draw.text((cx - tw//2, cy + 220), head, font=title_font, fill=(240, 255, 255))

    img = img.filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    out = buf.getvalue()
    log.info("IMG|fallback cover built | size=%dx%d | bytes=%d", w, h, len(out))
    return out

def _gemini_image_bytes(topic: str) -> Optional[bytes]:
    """Пробуем получить картинку через **официальный Gemini Images API**; при неуспехе — None."""
    if not (_genai_images_ok and GEMINI_API_KEY and GEMINI_IMAGE_MODEL):
        log.info("IMG|gemini skip (images sdk/key/model missing)")
        return None
    try:
        prompt = (
            "High-quality social cover image (no text), dark/gradient tech background, "
            "subtle AI/crypto vibe, clean composition, 3D lighting. Topic: " + (topic or "").strip()
        )
        size = "1280x960"
        log.info("IMG|gemini start | model=%s | size=%s | topic='%s'",
                 GEMINI_IMAGE_MODEL, size, (topic or "")[:160])
        resp = gen_images.generate(model=GEMINI_IMAGE_MODEL, prompt=prompt, size=size)  # type: ignore
        # v>=0.7: bytes лежат внутри images[i].bytes
        if hasattr(resp, "images") and resp.images:
            img = resp.images[0]
            data = getattr(img, "bytes", None) or getattr(img, "data", None)
            if isinstance(data, (bytes, bytearray)):
                log.info("IMG|gemini ok | bytes=%d", len(data))
                return bytes(data)
        # Альтернативные поля на всякий случай
        for key in ("image", "bytes", "data"):
            data = getattr(resp, key, None)
            if isinstance(data, (bytes, bytearray)):
                log.info("IMG|gemini ok (alt %s) | bytes=%d", key, len(data))
                return bytes(data)
        log.warning("IMG|gemini returned no image bytes; fields: %s",
                    [k for k in dir(resp) if not k.startswith("_")][:20])
    except Exception as e:
        log.warning("IMG|gemini.generate error: %s", e)
    return None

def _vertex_image_bytes(topic: str) -> Optional[bytes]:
    """Генерация через Vertex **Imagen 4** (Fast по умолчанию)."""
    if not _init_vertex_ai_once():
        if _vertex_err:
            log.info("IMG|vertex skip: %s", _vertex_err)
        return None
    try:
        model_name = os.getenv("VERTEX_IMAGEN_MODEL", VERTEX_IMAGEN_MODEL_DEFAULT)
        prompt = (
            "High-quality social cover image (no text), dark/gradient tech background, "
            "subtle AI/crypto vibe, clean composition, 3D lighting. Topic: " + (topic or "").strip()
        )
        size = os.getenv("VERTEX_IMAGE_SIZE", "1280x960")
        safety = os.getenv("VERTEX_SAFETY_LEVEL", "block_few")

        log.info("IMG|vertex start | model=%s | size=%s | topic='%s'", model_name, size, (topic or "")[:160])
        model = ImageGenerationModel.from_pretrained(model_name)

        images = model.generate_images(
            prompt=prompt,
            number_of_images=1,
            size=size,
            safety_filter_level=safety
        )

        if not images:
            log.warning("IMG|vertex got empty response")
            return None

        first = images[0]
        data = getattr(first, "image_bytes", None) or getattr(first, "bytes", None)
        if isinstance(data, (bytes, bytearray)):
            log.info("IMG|vertex ok | bytes=%d", len(data))
            return bytes(data)

        # Некоторые версии SDK возвращают PIL-объект
        pil = getattr(first, "_pil_image", None)
        if pil is not None:
            buf = io.BytesIO(); pil.save(buf, format="PNG")
            out = buf.getvalue()
            log.info("IMG|vertex ok (PIL) | bytes=%d", len(out))
            return out

        log.warning("IMG|vertex returned unknown structure: %s", type(first))
    except Exception as e:
        msg = str(e)
        # Частые варианты: no permission / auth / model not enabled
        if any(x in msg.lower() for x in ("permission", "unauth", "forbidden", "quota", "authenticate")):
            log.warning("IMG|vertex auth/perm error: %s", msg)
        else:
            log.warning("IMG|vertex error: %s", msg)
    return None

def generate_image(topic: str, text: str) -> Dict[str, Optional[str]]:
    """
    Старый публичный интерфейс (не используется ботом напрямую).
    Сохраняет PNG локально + заливает в GH. Возвращает словарь с url/локальным путём/sha.
    """
    # Порядок: Gemini → Vertex → Pillow
    png_bytes = _gemini_image_bytes(topic) or _vertex_image_bytes(topic) or _cover_from_topic(topic, text)
    sha = _sha_short(png_bytes)
    log.info("Image|bytes=%d sha=%s", len(png_bytes), sha)

    ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{sha}.png"
    local_path = os.path.join(LOCAL_MEDIA_DIR, fname)
    with open(local_path, "wb") as f:
        f.write(png_bytes)
    log.info("Image|saved local: %s", local_path)
    _log_file_info(local_path, log)

    try:
        ensure_github_dir()
    except Exception as e:
        log_gh.warning("ensure_github_dir failed: %s", e)

    url = upload_file_to_github(png_bytes, fname)

    # HEAD-проверка на стороне RAW
    if url:
        try:
            clean = url.split("?", 1)[0]
            r = requests.head(clean, headers=_UA, timeout=12, allow_redirects=True)
            log_gh.info("GH|HEAD %s -> %s | CT=%s | len=%s",
                        clean, r.status_code, r.headers.get("Content-Type",""), r.headers.get("Content-Length"))
            with open(local_path + ".url.txt", "w", encoding="utf-8") as uf:
                uf.write(clean)
            with open(local_path + ".meta.json", "w", encoding="utf-8") as mf:
                json.dump({
                    "url": clean,
                    "status": r.status_code,
                    "content_type": r.headers.get("Content-Type"),
                    "content_length": r.headers.get("Content-Length"),
                }, mf, ensure_ascii=False, indent=2)
        except Exception as e:
            log_gh.warning("GH|HEAD check failed: %s", e)

    try:
        DEDUP.record(text, png_bytes)
    except Exception as e:
        log.warning("Dedup record failed: %s", e)

    return {"url": url, "local_path": local_path, "sha": sha}

# -----------------------------------------------------------------------------
# Video generation (Gemini/Veo → fallback: MP4 pan-zoom from image)
# -----------------------------------------------------------------------------
def _gemini_video_bytes(topic: str, seconds: int = 6) -> Optional[bytes]:
    """Пробуем получить MP4 через Gemini/Veo. Если нет доступа — None."""
    if not (_genai_ok and GEMINI_API_KEY and GEMINI_VIDEO_MODEL):
        return None
    try:
        model = genai.GenerativeModel(GEMINI_VIDEO_MODEL)
        prompt = (
            "Create a short 16:9 MP4 teaser (no text overlay) with abstract AI/crypto vibes. "
            f"Duration ~{seconds}s. Topic: " + (topic or "").strip()
        )
        if hasattr(model, "generate_video"):
            try:
                resp = model.generate_video(prompt=prompt)  # type: ignore[attr-defined]
                vid = getattr(resp, "video", None) or getattr(resp, "bytes", None)
                if isinstance(vid, (bytes, bytearray)):
                    return bytes(vid)
            except Exception:
                pass
    except Exception as e:
        log.warning("Gemini video gen failed: %s", e)
    return None

def _build_panzoom_from_image(png_bytes: bytes, seconds: int = 6, fps: int = 24) -> Optional[bytes]:
    """Локальный фолбэк: короткий MP4 с плавным зумом из одной картинки (Pillow -> numpy)."""
    if not _vid_local_ok:
        return None
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        W, H = im.size
        out_w, out_h = 1280, 720
        frames = seconds * fps
        out = _np.zeros((frames, out_h, out_w, 3), dtype=_np.uint8)
        for i in range(frames):
            t = i / max(1, frames - 1)
            scale = 1.0 + 0.08 * t
            sw, sh = int(W / scale), int(H / scale)
            cx, cy = W // 2, H // 2
            x1 = max(0, cx - sw // 2); x2 = min(W, x1 + sw)
            y1 = max(0, cy - sh // 2); y2 = min(H, y1 + sh)
            crop = im.crop((x1, y1, x2, y2))
            resized = crop.resize((out_w, out_h), Image.BICUBIC)
            out[i] = _np.array(resized, dtype=_np.uint8)
        tmp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
        _iio.imwrite(tmp_path, out, fps=fps, codec="libx264", bitrate="3M", pix_fmt="yuv420p")
        with open(tmp_path, "rb") as f:
            data = f.read()
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return data
    except Exception as e:
        log.warning("Local video build failed: %s", e)
        return None

def _upload_video_to_github(file_bytes: bytes, filename: str) -> Optional[str]:
    """Аплоад MP4 в GH (в папку GH_VIDEOS_DIR)."""
    if not (ACTION_PAT_GITHUB and ACTION_REPO_GITHUB):
        return None
    try:
        owner, repo = _split_repo(ACTION_REPO_GITHUB)
    except ValueError as e:
        log.error(str(e)); return None
    rel_path = f"{GH_VIDEOS_DIR}/{filename}"
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{rel_path}"
    headers = _gh_headers()
    data = {
        "message": f"Add video {filename}",
        "branch": ACTION_BRANCH,
        "content": base64.b64encode(file_bytes).decode("ascii"),
    }
    log_gh.info("GH|PUT video | repo=%s | branch=%s | rel=%s | bytes=%d",
                ACTION_REPO_GITHUB, ACTION_BRANCH, rel_path, len(file_bytes))
    try:
        r = requests.get(api, headers=headers, params={"ref": ACTION_BRANCH}, timeout=20)
        if r.status_code == 200:
            sha_existing = (r.json() or {}).get("sha")
            if sha_existing:
                data["sha"] = sha_existing
    except Exception:
        pass
    for attempt in range(1, 4):
        try:
            r = requests.put(api, headers=headers, data=json.dumps(data), timeout=60)
            if r.status_code in (200, 201):
                raw = f"{_RAW_GH}/{owner}/{repo}/{ACTION_BRANCH}/{rel_path}"
                log_gh.info("GH|uploaded video OK | %s", raw)
                return raw
            else:
                log_gh.error("Video PUT failed: %s %s", r.status_code, r.text[:300])
        except Exception as e:
            log_gh.error("Video PUT try %d: %s", attempt, e)
        time.sleep(1.2 * attempt)
    return None

def _tmp_write_and_maybe_upload_media(
    data: bytes,
    kind: str,              # "image" | "video"
    dir_local: str,
    auto_upload: bool
) -> Tuple[str, Optional[str]]:
    """Сохранить (png/mp4) во временный файл, опц. залить в GH, положить *.url.txt и *.meta.json."""
    ext = ".png" if kind == "image" else ".mp4"
    os.makedirs(dir_local, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=dir_local)
    with open(tmp.name, "wb") as f:
        f.write(data)

    # Локальная проверка файла
    _log_file_info(tmp.name, log)

    gh_url = None
    if auto_upload and (ACTION_PAT_GITHUB and ACTION_REPO_GITHUB):
        try:
            ensure_github_dir()  # создаёт каталоги при первом запуске
        except Exception as e:
            log_gh.warning("ensure_github_dir failed: %s", e)

        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        sha = _sha_short(data)
        filename = f"{ts}_{sha}{ext}"
        log_gh.info("GH|upload start | kind=%s | repo=%s | branch=%s | file=%s | bytes=%d",
                    kind, ACTION_REPO_GITHUB, ACTION_BRANCH, filename, len(data))
        if kind == "image":
            gh_url = upload_file_to_github(data, filename)
        else:
            gh_url = _upload_video_to_github(data, filename)

        if gh_url:
            clean = gh_url.split("?", 1)[0].strip()
            # Сохраняем URL рядом с файлом
            try:
                with open(tmp.name + ".url.txt", "w", encoding="utf-8") as uf:
                    uf.write(clean)
                log_gh.info("GH|url saved | %s", clean)
            except Exception as e:
                log_gh.warning("write url file failed: %s", e)

            # HEAD-проверка доступности и Content-Type
            try:
                r = requests.head(clean, headers=_UA, timeout=12, allow_redirects=True)
                ct = r.headers.get("Content-Type", "")
                log_gh.info("GH|HEAD %s -> %s | CT=%s | len=%s",
                            clean, r.status_code, ct, r.headers.get("Content-Length"))
                meta = {
                    "kind": kind, "branch": ACTION_BRANCH, "url": clean,
                    "status": r.status_code, "content_type": ct,
                    "content_length": r.headers.get("Content-Length")
                }
                with open(tmp.name + ".meta.json", "w", encoding="utf-8") as mf:
                    json.dump(meta, mf, ensure_ascii=False, indent=2)
            except Exception as e:
                log_gh.warning("GH|HEAD check failed for %s: %s", clean, e)
        else:
            log_gh.warning("GH|upload returned None (kind=%s)", kind)

    return tmp.name, gh_url

# -----------------------------------------------------------------------------
# Public API: text/image/video + progress hooks
# -----------------------------------------------------------------------------
ProgressFn = Optional[Callable[[str], None]]

def _report(progress: ProgressFn, msg: str) -> None:
    try:
        if progress:
            progress(msg)
    except Exception:
        pass

def make_post(topic: str, progress: ProgressFn = None) -> Dict[str, Optional[str]]:
    """
    Полный цикл:
    1) прогресс "typing": начало генерации
    2) генерим текст (666±20, факты — из цен/новостей) + нормализация $TICKER/#TICKER
    3) проверяем дубль текста
    4) прогресс "upload_photo": генерация изображения
    5) кладём локально и (опционально) в GitHub
    6) записываем в дедуп
    """
    _report(progress, "typing:start_text")
    text = generate_text(topic)
    _report(progress, "typing:text_ready")

    try:
        is_dup_text = DEDUP.is_duplicate(text, None)
    except Exception as e:
        log.warning("Dedup check(text) failed: %s", e)
        is_dup_text = False

    if is_dup_text:
        parts = re.split(r"(?<=[.!?…])\s+", text)
        random.shuffle(parts)
        text = " ".join(parts)
        text = _clamp_to_len(_clean_bracket_hints(text), TARGET_CHAR_LEN, TARGET_CHAR_TOL)
        text = _ensure_crypto_tickers_and_hashtags(text)

    _report(progress, "upload_photo:start_image")
    # Изображение (Gemini → Vertex → Pillow), сохранить/загрузить
    path_img, url_img_direct, _ = ai_generate_image(topic, progress)
    _report(progress, "upload_photo:image_ready")

    # URL берём приоритетно из возврата аплоада, .url.txt — резерв
    url_img = url_img_direct
    if not url_img and path_img and os.path.exists(path_img + ".url.txt"):
        try:
            url_img = (open(path_img + ".url.txt", "r", encoding="utf-8").read() or "").strip() or None
        except Exception:
            pass

    # посчитаем sha по локальному файлу для консистентности
    image_sha = None
    try:
        if path_img and os.path.exists(path_img):
            with open(path_img, "rb") as f:
                image_sha = _sha_short(f.read())
    except Exception:
        pass

    # запись в дедуп по тексту+картинке
    try:
        if path_img and os.path.exists(path_img):
            with open(path_img, "rb") as f:
                DEDUP.record(text, f.read())
    except Exception as e:
        log.warning("Dedup record in make_post failed: %s", e)

    return {
        "text": text,
        "image_url": url_img,
        "image_local_path": path_img,
        "image_sha": image_sha,
    }

# Совместимость с twitter_bot.py
def ai_generate_text(topic: str, progress: ProgressFn = None) -> Tuple[str, Optional[str]]:
    """
    Returns: (text, warn_message_or_None)
    progress(msg):
      'typing:start_text'  -> показать "печатает..."
      'typing:text_ready'  -> убрать индикацию/написать "черновик готов"
    """
    topic = (topic or "").strip()
    if not topic:
        return ("", "empty topic")

    warn_parts: List[str] = []
    if not _model:
        warn_parts.append("Gemini not available")
    if not _pytrends_ok:
        warn_parts.append("pytrends missing")
    if not _sns_ok:
        warn_parts.append("snscrape missing")

    _report(progress, "typing:start_text")
    text = generate_text(topic)
    _report(progress, "typing:text_ready")

    warn = " | ".join(warn_parts) if warn_parts else None
    return text, (warn or None)

def ai_suggest_hashtags(text: str) -> List[str]:
    defaults = ["#AiCoin", "#AI", "$Ai", "#crypto"]
    dynamic = get_x_hashtags(limit=6)

    # добавим хэштеги по найденным тикерам в тексте (если ещё нет)
    tickers_in_text = set(m.group(1).upper() for m in re.finditer(r"\$([A-Za-z]{2,10})\b", text or ""))
    for t in sorted(tickers_in_text):
        ht = f"#{t}"
        if ht not in dynamic and ht not in defaults:
            dynamic.append(ht)

    low = (text or "").lower()
    if "ethereum" in low or " eth " in f" {low} " or "$eth" in low:
        dynamic.append("#ETH")
    if "solana" in low or " sol " in f" {low} " or "$sol" in low:
        dynamic.append("#SOL")
    if "defi" in low:
        dynamic.append("#DeFi")
    if "trading" in low:
        dynamic.append("#trading")

    out: List[str] = []
    seen = set()
    for h in defaults + dynamic:
        if not h:
            continue
        k = h.lower()
        if k not in seen:
            seen.add(k); out.append(h)
        if len(out) >= 8:
            break
    return out

def ai_generate_image(topic: str, progress: ProgressFn = None) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns: (local_png_path, gh_raw_url_or_none, warn_or_none)
    progress(msg):
      'upload_photo:start_image' -> показать "отправляет фото..."
      'upload_photo:image_ready' -> убрать индикацию
    """
    try:
        _report(progress, "upload_photo:start_image")
        log.info("IMG|make start | topic='%s'", (topic or "")[:160])

        warn = None

        # 1) Gemini Images (приоритет)
        img_bytes = _gemini_image_bytes(topic)

        # 2) Vertex (Imagen 4) — если Gemini недоступен/вернул None
        if not img_bytes:
            vb = _vertex_image_bytes(topic)
            if vb:
                img_bytes = vb

        # 3) Фолбэк (Pillow)
        if not img_bytes:
            log.info("IMG|gemini/vertex none -> fallback")
            text_for_cover = _clamp_to_len(_clean_bracket_hints(topic), 72, 8)
            img_bytes = _cover_from_topic(topic, text_for_cover)
            warn = "fallback (Pillow)"

        log.info("IMG|bytes ready | %d", len(img_bytes) if img_bytes else -1)

        # дедуп-запись по картинке (и теме как текстовому ключу)
        try:
            DEDUP.record(topic or "", img_bytes)
        except Exception as e:
            log.warning("Dedup record (image) failed: %s", e)

        path, gh_url = _tmp_write_and_maybe_upload_media(
            img_bytes, "image", LOCAL_MEDIA_DIR, AUTO_UPLOAD_IMAGE_TO_GH
        )
        _log_file_info(path, log)
        if gh_url:
            log.info("IMG|gh_url=%s", gh_url)
        else:
            log.warning("IMG|gh_url is None (auto_upload=%s)", AUTO_UPLOAD_IMAGE_TO_GH)

        _report(progress, "upload_photo:image_ready")
        return path, gh_url, warn
    except Exception as e:
        log.warning("Image gen error: %s", e)
        _report(progress, "upload_photo:image_ready")
        return None, None, "image generation failed"

def ai_generate_video(topic: str, seconds: int = 6, progress: ProgressFn = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns: (local_mp4_path, warn_or_none)
    progress(msg):
      'upload_photo:start_image' -> показать "отправляет медиа..."
      'upload_photo:image_ready' -> убрать индикацию
    """
    try:
        _report(progress, "upload_photo:start_image")
        vid_bytes = _gemini_video_bytes(topic, seconds=seconds)
        warn = None
        if not vid_bytes:
            # фолбэк: берём обложку и строим MP4 пан-зум
            cover = _gemini_image_bytes(topic) or _vertex_image_bytes(topic) or _cover_from_topic(topic, _clamp_to_len(_clean_bracket_hints(topic), 72, 8))
            vid_bytes = _build_panzoom_from_image(cover, seconds=seconds) if cover else None
            warn = "fallback (pan-zoom MP4)"
        if not vid_bytes:
            _report(progress, "upload_photo:image_ready")
            return None, "video generation failed"
        path, _ = _tmp_write_and_maybe_upload_media(
            vid_bytes, "video", LOCAL_VIDEO_DIR, AUTO_UPLOAD_VIDEO_TO_GH
        )
        _report(progress, "upload_photo:image_ready")
        return path, warn
    except Exception as e:
        log.warning("Video gen error: %s", e)
        _report(progress, "upload_photo:image_ready")
        return None, "video generation failed"

# --- alias for twitter_bot backward-compat ---
def suggest_hashtags(text: str) -> List[str]:
    """Alias, т.к. twitter_bot ищет suggest_hashtags()."""
    return ai_suggest_hashtags(text)

# -----------------------------------------------------------------------------
# GitHub upload (images & ensure dirs)
# -----------------------------------------------------------------------------
def _split_repo(repo: str) -> Tuple[str, str]:
    try:
        owner, name = repo.split("/", 1)
        return owner, name
    except Exception:
        raise ValueError("ACTION_REPO_GITHUB must be 'owner/repo'")

def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"token {ACTION_PAT_GITHUB}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "aicoin-twitter-bot",
    }

def ensure_github_dir() -> None:
    if not (ACTION_PAT_GITHUB and ACTION_REPO_GITHUB):
        return
    owner, repo = _split_repo(ACTION_REPO_GITHUB)
    for sub in (GH_IMAGES_DIR, GH_VIDEOS_DIR):
        api_dir = f"https://api.github.com/repos/{owner}/{repo}/contents/{sub}"
        params = {"ref": ACTION_BRANCH}
        r = requests.get(api_dir, headers=_gh_headers(), params=params, timeout=20)
        log_gh.info("Check dir %s -> %s", sub, r.status_code)
        if r.status_code == 200:
            continue
        if r.status_code != 404:
            log_gh.warning("Dir check unexpected: %s %s", r.status_code, r.text[:200])
            continue
        api_put = f"https://api.github.com/repos/{owner}/{repo}/contents/{sub}/.gitkeep"
        data = {
            "message": f"Create {sub}/ (bootstrap .gitkeep)",
            "branch": ACTION_BRANCH,
            "content": base64.b64encode(b"").decode("ascii"),
        }
        r2 = requests.put(api_put, headers=_gh_headers(), data=json.dumps(data), timeout=30)
        log_gh.info("Create dir %s/.gitkeep -> %s", sub, r2.status_code)

def upload_file_to_github(file_bytes: bytes, filename: str) -> Optional[str]:
    """Заливает картинку в GH_IMAGES_DIR, возвращает RAW URL."""
    if not (ACTION_PAT_GITHUB and ACTION_REPO_GITHUB):
        log.info("GitHub upload skipped: no creds")
        return None
    try:
        owner, repo = _split_repo(ACTION_REPO_GITHUB)
    except ValueError as e:
        log.error(str(e))
        return None

    rel_path = f"{GH_IMAGES_DIR}/{filename}"
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{rel_path}"
    headers = _gh_headers()

    data = {
        "message": f"Add image {filename}",
        "branch": ACTION_BRANCH,
        "content": base64.b64encode(file_bytes).decode("ascii"),
    }

    log_gh.info("GH|PUT image | repo=%s | branch=%s | rel=%s | bytes=%d",
        ACTION_REPO_GITHUB, ACTION_BRANCH, rel_path, len(file_bytes))

    try:
        r_get = requests.get(api, headers=headers, params={"ref": ACTION_BRANCH}, timeout=20)
        log_gh.info("GET %s -> %s", rel_path, r_get.status_code)
        if r_get.status_code == 200:
            sha_existing = (r_get.json() or {}).get("sha")
            if sha_existing:
                data["sha"] = sha_existing
    except Exception as e:
        log_gh.warning("GET existing failed: %s", e)

    for attempt in range(1, 4):
        try:
            r = requests.put(api, headers=headers, data=json.dumps(data), timeout=30)
            log_gh.info("PUT %s (try %d) -> %s", rel_path, attempt, r.status_code)
            if r.status_code in (200, 201):
                raw = f"{_RAW_GH}/{owner}/{repo}/{ACTION_BRANCH}/{rel_path}"
                log_gh.info("GH|uploaded OK | %s", raw)
                return raw
            if r.status_code in (409, 422):
                try:
                    r_get2 = requests.get(api, headers=headers, params={"ref": ACTION_BRANCH}, timeout=20)
                    if r_get2.status_code == 200:
                        data["sha"] = (r_get2.json() or {}).get("sha")
                        log_gh.info("Resolved sha, retrying…")
                        continue
                except Exception:
                    pass
            log_gh.error("PUT failed: %s %s", r.status_code, r.text[:300])
        except Exception as e:
            log_gh.error("PUT exception (try %d): %s", attempt, e)
        time.sleep(1.5 * attempt)
    return None

# -----------------------------------------------------------------------------
# If run directly
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    t = "Горячие ИИ-тренды, Bitcoin и Ethereum сигналы недели"
    post = make_post(t)
    print("TEXT:\n", post["text"])
    print("IMG URL:", post["image_url"])
    print("IMG FILE:", post["image_local_path"])
    print("IMG SHA:", post["image_sha"])