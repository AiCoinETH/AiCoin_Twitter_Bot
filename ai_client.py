# ai_client.py
# -*- coding: utf-8 -*-

import os
import io
import re
import json
import time
import math
import base64
import queue
import hashlib
import random
import logging
import sqlite3
import tempfile
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# Pillow cover generator (fallback & default)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Trends (both are optional at runtime)
try:
    from pytrends.request import TrendReq  # type: ignore
    _pytrends_ok = True
except Exception:
    _pytrends_ok = False

try:
    # lightweight scraping of public tweets; no API key needed
    import snscrape.modules.twitter as sntwitter  # type: ignore
    _sns_ok = True
except Exception:
    _sns_ok = False

# Gemini text
_genai_ok = False
try:
    import google.generativeai as genai  # type: ignore
    _genai_ok = True
except Exception:
    _genai_ok = False

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-1.5-pro")

# Длина поста под Twitter/Telegram
TARGET_CHAR_LEN = int(os.getenv("TARGET_CHAR_LEN", "666"))
TARGET_CHAR_TOL = int(os.getenv("TARGET_CHAR_TOL", "20"))

# GitHub upload
ACTION_PAT_GITHUB = os.getenv("ACTION_PAT_GITHUB", "")
ACTION_REPO_GITHUB = os.getenv("ACTION_REPO_GITHUB", "")  # owner/repo
ACTION_BRANCH = os.getenv("ACTION_BRANCH", "main")

# Папка для медиа в репозитории
GH_IMAGES_DIR = os.getenv("GH_IMAGES_DIR", "images_for_posts")

# Локальные пути
LOCAL_MEDIA_DIR = os.getenv("LOCAL_MEDIA_DIR", "./images_for_posts")
os.makedirs(LOCAL_MEDIA_DIR, exist_ok=True)

# База для дедупликации
DEDUP_DB_PATH = os.getenv("DEDUP_DB_PATH", "./history.db")
DEDUP_TTL_DAYS = int(os.getenv("DEDUP_TTL_DAYS", "15"))

# Логирование
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ai_client")

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _sha_short(data: bytes, n: int = 12) -> str:
    return hashlib.sha256(data).hexdigest()[:n]

def _sha_text(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]

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
    s = text.strip()
    if len(s) <= max_len and len(s) >= min_len:
        return s
    if len(s) > max_len:
        cut = s[:max_len]
        # завершить на границе предложения
        m = re.search(r"(?s)[.!?…](?!.*[.!?…]).*", cut)
        if m:
            cut = cut[:m.end()].strip()
        return cut.strip()
    # короче — возвращаем как есть (лучше недобор, чем вода)
    return s

def _detect_lang(s: str) -> str:
    if re.search(r"[А-Яа-яЁёІіЇїЄєҐґ]", s):
        return "ru"
    return "en"

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
                                return seen
                except Exception:
                    continue
        return seen[:limit] or _DEFAULT_TOPICS[:limit]
    except Exception as e:
        log.info("Trends|pytrends fallback: %s", e)
        return _DEFAULT_TOPICS[:limit]

def get_x_hashtags(limit: int = 7) -> List[str]:
    if not _sns_ok:
        log.info("Trends|snscrape not available")
        return []
    try:
        query = '(AI OR crypto OR bitcoin OR ethereum OR solana OR web3) lang:ru OR lang:en since:%s' % (
            (dt.date.today() - dt.timedelta(days=2)).isoformat()
        )
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
        return [k for k, _ in top[:limit]]
    except Exception as e:
        log.info("Trends|snscrape fallback: %s", e)
        return []

# -----------------------------------------------------------------------------
# Gemini text
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

def generate_text(topic: str, locale_hint: Optional[str] = None) -> str:
    lang = locale_hint or _detect_lang(topic)
    trends = get_google_trends()
    tags = get_x_hashtags()
    trend_bits = ", ".join(trends[:5])
    tag_bits = " ".join(tags[:5])

    prompt = f"""
Сгенерируй короткий пост для соцсетей на языке "{'Русский' if lang=='ru' else 'English'}".
Тема: {topic}

Обязательные требования:
- Длина {TARGET_CHAR_LEN}±{TARGET_CHAR_TOL} символов.
- Никаких подсказок в квадратных скобках и без служебных пометок.
- Без хештегов, без вопросов к читателю, без "подпишись", без ссылок.
- Фактура и актуальность: опирайся на популярные темы и формулировки из трендов Google и X(Twitter).
- Пиши как инфо-пост/наблюдение, польза и конкретика.
- Тон: бодрый, уверенный, без кликбейта, без эмодзи.

Подсказки по трендам (не вставляй дословно как список, используй смысл): {trend_bits} {tag_bits}
"""
    text = ""
    if _model:
        try:
            resp = _model.generate_content(prompt)
            text = (resp.text or "").strip()
        except Exception as e:
            log.warning("Gemini text error: %s", e)

    if not text:
        # минимальный резерв: нейтральный текст на основе темы
        base = f"{topic.strip().capitalize()}: актуальные наблюдения без воды. "
        text = base + "Фокус на практической пользе, рисках и возможностях, чтобы принимать быстрые решения."

    text = _clean_bracket_hints(text)
    text = _clamp_to_len(text, TARGET_CHAR_LEN, TARGET_CHAR_TOL)
    return text

# -----------------------------------------------------------------------------
# Image generation (default: Pillow neon cover). Optional hook for real T2I later.
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
    # радиальные вспышки
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

    # центральные кольца
    cx, cy = w//2, int(h*0.42)
    for r, col, wd in [
        (210, (255, 210, 60), 5),
        (170, (255, 240, 120), 3),
        (120, (255, 255, 190), 2),
    ]:
        draw.ellipse((cx-r, cy-r, cx+r, cy+r), outline=col, width=wd)

    # заголовок (укороченная тема)
    head = re.sub(r"\s+", " ", topic).strip()[:64]
    title_font = _load_font(72)
    tw, th = draw.textsize(head, font=title_font)
    draw.text((cx - tw//2, cy + 220), head, font=title_font, fill=(240, 255, 255))

    img = img.filter(ImageFilter.GaussianBlur(0.6))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def generate_image(topic: str, text: str) -> Dict[str, Optional[str]]:
    """
    Возвращает: {
      'url': <raw github url or None>,
      'local_path': <path to local png>,
      'sha': <short sha>,
    }
    """
    png_bytes = _cover_from_topic(topic, text)
    sha = _sha_short(png_bytes)

    ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"{ts}_{sha}.png"
    local_path = os.path.join(LOCAL_MEDIA_DIR, fname)
    with open(local_path, "wb") as f:
        f.write(png_bytes)

    url = upload_file_to_github(png_bytes, fname)

    try:
        DEDUP.record(text, png_bytes)
    except Exception as e:
        log.warning("Dedup record failed: %s", e)

    return {"url": url, "local_path": local_path, "sha": sha}

# -----------------------------------------------------------------------------
# GitHub upload
# -----------------------------------------------------------------------------
def upload_file_to_github(file_bytes: bytes, filename: str) -> Optional[str]:
    if not (ACTION_PAT_GITHUB and ACTION_REPO_GITHUB):
        log.info("GitHub upload skipped: no creds")
        return None
    try:
        owner, repo = ACTION_REPO_GITHUB.split("/", 1)
    except ValueError:
        log.error("ACTION_REPO_GITHUB must be 'owner/repo'")
        return None

    rel_path = f"{GH_IMAGES_DIR}/{filename}"
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{rel_path}"
    headers = {
        "Authorization": f"token {ACTION_PAT_GITHUB}",
        "Accept": "application/vnd.github+json",
    }

    data = {
        "message": f"Add image {filename}",
        "branch": ACTION_BRANCH,
        "content": base64.b64encode(file_bytes).decode("ascii"),
    }

    r_get = requests.get(api, headers=headers, params={"ref": ACTION_BRANCH})
    if r_get.status_code == 200:
        try:
            sha_existing = r_get.json().get("sha")
            if sha_existing:
                data["sha"] = sha_existing
        except Exception:
            pass

    r = requests.put(api, headers=headers, data=json.dumps(data))
    if r.status_code not in (200, 201):
        log.error("GitHub upload failed: %s %s", r.status_code, r.text[:200])
        return None

    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ACTION_BRANCH}/{rel_path}?r={_now_ts()}"
    log.info("GitHub uploaded: %s", raw)
    return raw

# -----------------------------------------------------------------------------
# Public API (high-level)
# -----------------------------------------------------------------------------
def make_post(topic: str) -> Dict[str, Optional[str]]:
    """
    Полный цикл для автономного использования (не обязательно боту):
    1) генерим текст (666±20, без скобочных подсказок, полезно и по трендам)
    2) проверяем дубль текста
    3) делаем картинку (неон-крипто), сохраняем и при желании грузим в GitHub
    4) укладываем в дедуп-базу
    """
    text = generate_text(topic)
    is_dup_text = False
    try:
        is_dup_text = DEDUP.is_duplicate(text, None)
    except Exception as e:
        log.warning("Dedup check(text) failed: %s", e)

    if is_dup_text:
        parts = re.split(r"(?<=[.!?…])\s+", text)
        random.shuffle(parts)
        text = " ".join(parts)
        text = _clamp_to_len(_clean_bracket_hints(text), TARGET_CHAR_LEN, TARGET_CHAR_TOL)

    img = generate_image(topic, text)
    return {
        "text": text,
        "image_url": img.get("url"),
        "image_local_path": img.get("local_path"),
        "image_sha": img.get("sha"),
    }

# -----------------------------------------------------------------------------
# Compatibility wrappers for twitter_bot.py
# -----------------------------------------------------------------------------
def ai_generate_text(topic: str) -> Tuple[str, Optional[str]]:
    """
    Expected by twitter_bot.py
    Returns: (text, warn_message_or_None)
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

    text = generate_text(topic)
    warn = " | ".join(warn_parts) if warn_parts else None
    return text, (warn or None)

def ai_suggest_hashtags(text: str) -> List[str]:
    """
    Простая эвристика: популярные теги из X (если доступно) + дефолты проекта.
    """
    defaults = ["#AiCoin", "#AI", "$Ai", "#crypto"]
    dynamic = get_x_hashtags(limit=6)
    # умные добавки
    low = (text or "").lower()
    if "ethereum" in low or " eth " in f" {low} ":
        dynamic.append("#ETH")
    if "solana" in low or " sol " in f" {low} ":
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

def ai_generate_image(topic: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Expected by twitter_bot.py
    Returns: (local_png_path, warn_or_none)
    Бот сам загрузит на GitHub и удалит временный файл.
    """
    try:
        # Генерация только локального файла (без аплоада), чтобы избежать двойной загрузки
        text_for_cover = _clamp_to_len(_clean_bracket_hints(topic), 72, 8)
        png_bytes = _cover_from_topic(topic, text_for_cover)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        with open(tmp.name, "wb") as f:
            f.write(png_bytes)
        return tmp.name, None
    except Exception as e:
        log.warning("Image gen error: %s", e)
        return None, "image generation failed"

# -----------------------------------------------------------------------------
# If run directly
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    t = "Горячие ИИ-тренды и крипто-сигналы недели"
    post = make_post(t)
    print("TEXT:\n", post["text"])
    print("IMG URL:", post["image_url"])
    print("IMG FILE:", post["image_local_path"])