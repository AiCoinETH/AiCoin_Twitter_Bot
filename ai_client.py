# ai_client.py
import os, io, re, json, base64, time, hashlib, logging
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("ai_client")
log.setLevel(logging.INFO)

# === Настройки длины ===
TARGET_CHAR_LEN = int(os.getenv("TARGET_CHAR_LEN", "666"))
TARGET_CHAR_TOL = int(os.getenv("TARGET_CHAR_TOL", "20"))
MIN_LEN = max(140, TARGET_CHAR_LEN - TARGET_CHAR_TOL)
MAX_LEN = TARGET_CHAR_LEN + TARGET_CHAR_TOL

# === Gemini (google-generativeai) ===
USE_GEMINI = True
try:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-1.5-flash")
    # (для рисования у многих аккаунтов пока нет стабильной имедж-модели, поэтому ниже будет фолбэк на Pillow)
    log.info("Gemini (google-generativeai) configured.")
except Exception as e:
    USE_GEMINI = False
    log.info("google-genai not available: %s", e)

# === Доп. либы для трендов и изображений ===
try:
    from pytrends.request import TrendReq
except Exception:
    TrendReq = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = ImageDraw = ImageFont = None

# === GitHub upload ===
from github import Github

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def upload_image_to_github(png_bytes: bytes,
                           *, repo_full: Optional[str] = None,
                           branch: str = "main",
                           topic: Optional[str] = None,
                           text_hash: Optional[str] = None) -> str:
    """Загружаем PNG в images_for_posts/... + дописываем manifest.jsonl."""
    repo_full = repo_full or os.getenv("ACTION_REPO_GITHUB")
    token = os.getenv("ACTION_PAT_GITHUB")
    if not repo_full or not token:
        raise RuntimeError("Missing ACTION_REPO_GITHUB/ACTION_PAT_GITHUB")

    g = Github(token)
    repo = g.get_repo(repo_full)

    ts = time.strftime("%Y%m%d_%H%M%S")
    sh8 = _sha256_hex(png_bytes)[:8]
    path = f"images_for_posts/{ts}_{sh8}.png"

    # создаём файл
    try:
        repo.create_file(path, f"bot: add {path}", png_bytes, branch=branch)
    except Exception as e:
        # Если вдруг файл уже есть (крайне маловероятно) — обновим
        try:
            cur = repo.get_contents(path, ref=branch)
            repo.update_file(path, f"bot: update {path}", png_bytes, cur.sha, branch=branch)
        except Exception as e2:
            raise RuntimeError(f"GitHub upload failed: {e}; fallback update failed: {e2}")

    raw_url = f"https://raw.githubusercontent.com/{repo_full}/{branch}/{path}"

    # обновим/создадим манифест для NFT-пайплайна
    manifest_path = "images_for_posts/manifest.jsonl"
    meta = {
        "path": path,
        "raw_url": raw_url,
        "sha256": _sha256_hex(png_bytes),
        "text_hash": text_hash,
        "topic": topic,
        "created_at": _now_iso(),
    }
    meta_line = (json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        try:
            mf = repo.get_contents(manifest_path, ref=branch)
            # декодим base64 -> bytes -> str и аппендим
            current = base64.b64decode(mf.content)
            updated = current + meta_line
            repo.update_file(manifest_path, f"bot: append manifest {path}", updated, mf.sha, branch=branch)
        except Exception:
            repo.create_file(manifest_path, "bot: create manifest", meta_line, branch=branch)
    except Exception as e:
        log.debug("manifest update failed: %s", e)

    return raw_url

# === Тренды (по возможности) ===
def _get_google_trends_top(n: int = 8, pn: str = "united_states") -> list:
    if not TrendReq:
        return []
    try:
        tr = TrendReq(hl="en-US", tz=0)
        df = tr.trending_searches(pn=pn)
        if df is not None and not df.empty:
            return [str(x) for x in df[0].head(n).tolist()]
    except Exception as e:
        log.debug("pytrends failed: %s", e)
    return []

# === Утилиты текста ===
def _strip_bracket_tips(s: str) -> str:
    # Убираем любые подсказки в квадратных скобках
    return re.sub(r"\[[^\]]{0,200}\]", "", s)

def _clean_text(s: str) -> str:
    s = s.replace("\u200b", "").strip()
    s = _strip_bracket_tips(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s

def _trim_to_bounds(s: str, min_len: int = MIN_LEN, max_len: int = MAX_LEN) -> str:
    s = s.strip()
    if len(s) <= max_len and len(s) >= min_len:
        return s
    if len(s) > max_len:
        # режем по предложению/слову
        cut = s[:max_len]
        # попробуем обрубить по последней точке/восклиц/вопрос
        m = re.search(r"[\.!\?](?!.*[\.!\?])", cut)
        if m and m.end() >= min_len:
            return cut[:m.end()].strip()
        # иначе — до последнего пробела
        sp = cut.rfind(" ")
        if sp >= min_len:
            return cut[:sp].strip()
        return cut.strip()
    # слишком коротко — просто вернём как есть (лучше недобрать, чем добавлять «воды»)
    return s

# === Генерация текста ===
def generate_post_text(topic: str, lang: str = "ru") -> str:
    trends = []
    # Попробуем подобрать Google Trends (регионально)
    pn = {"ru": "russia", "uk": "ukraine", "en": "united_states"}.get(lang.lower(), "united_states")
    trends = _get_google_trends_top(8, pn=pn)

    prompt = f"""
Сгенерируй краткий пост для X/Twitter на {lang}.
Требования:
- Никаких комментариев, ремарок или подсказок в квадратных скобках.
- Содержание должно быть полезным и актуальным.
- Опирайся на популярные темы из Twitter и Google (если тренды ниже есть — учитывай их).
- Без эмодзи-спама, без хэштегов внутри текста.
- Длина {TARGET_CHAR_LEN}±{TARGET_CHAR_TOL} символов.
- Ясный, разговорный тон, без клише «восторга ради восторга».

Тема от пользователя: {topic}

Тренды для ориентира (можно выборочно, не перечисляй списком):
{", ".join(trends) if trends else "—"}
""".strip()

    text = ""
    if USE_GEMINI:
        try:
            model = genai.GenerativeModel(GEMINI_TEXT_MODEL)
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
        except Exception as e:
            log.debug("gemini text failed: %s", e)

    if not text:
        # Фолбэк, если Gemini недоступен
        text = f"{topic.strip().capitalize()} — краткий пост о том, что сейчас обсуждают чаще всего: влияние ИИ на ежедневные сервисы, ускорение выхода новых моделей и прикладные кейсы. Компактная версия без воды и лишних деталей."

    text = _clean_text(text)
    text = _trim_to_bounds(text, MIN_LEN, MAX_LEN)
    return text

# === Генерация изображения ===
def _pillow_share_card(title: str, width: int = 1024, height: int = 576) -> bytes:
    if not Image:
        raise RuntimeError("Pillow not installed")
    img = Image.new("RGB", (width, height), (10, 12, 20))
    d = ImageDraw.Draw(img)
    # простая «подложка»
    for y in range(height):
        shade = int(20 + (y / height) * 80)
        d.line([(0, y), (width, y)], fill=(shade, shade, shade))
    # текст
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    margin = 60
    max_w = width - margin * 2
    # перенос строк
    words = title.strip().split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur: lines.append(cur); cur = w
            else: lines.append(w)
    if cur: lines.append(cur)
    y = height//2 - (len(lines)*56)//2
    for line in lines:
        d.text((margin, y), line, font=font, fill=(240, 240, 240))
        y += 56
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()

def generate_post_image(topic: str, text: str) -> Dict[str, Any]:
    """
    Возвращает dict: {"bytes": png_bytes, "url": url_or_None, "sha256": hex}
    - Всегда стараемся вернуть bytes (их шлём в ТГ/X).
    - Если есть GitHub креды — заливаем файл и отдаём url.
    """
    png_bytes = None

    # Попытка Gemini-имиджа (у некоторых аккаунтов может быть недоступна — будет фолбэк на Pillow)
    if USE_GEMINI:
        try:
            # У разных аккаунтов имидж-эндпоинт может называться по-разному; для надёжности сразу фолбэк ниже
            # Здесь можно попробовать применить text->image модель, если доступна.
            pass
        except Exception as e:
            log.debug("gemini image failed: %s", e)

    if png_bytes is None:
        title = re.sub(r"\s+", " ", text).strip()
        title = title[:160]
        try:
            png_bytes = _pillow_share_card(title or topic)
        except Exception as e:
            raise RuntimeError(f"image fallback failed: {e}")

    sha = _sha256_hex(png_bytes)
    # Заливаем в GitHub (для NFT-пайплайна) — но публиковать в ТГ/X будем БАЙТАМИ
    url = None
    try:
        url = upload_image_to_github(
            png_bytes,
            topic=topic,
            text_hash=hashlib.sha256((" ".join((text or "").lower().split())).encode("utf-8")).hexdigest()
        )
    except Exception as e:
        log.debug("upload_image_to_github failed: %s", e)

    return {"bytes": png_bytes, "url": url, "sha256": sha}