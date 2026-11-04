"""Парсер публичных Telegram-каналов через веб-превью https://t.me/s/<channel>.

Без внешних зависимостей: только стандартная библиотека.
"""
import html
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; AIProducerBot/1.0)"

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class ChannelError(Exception):
    """Канал не найден, закрыт или у него нет веб-превью."""


def normalize_channel(raw: str) -> str | None:
    """'@name', 'name', 't.me/name', 'https://t.me/s/name/123' -> 'name'."""
    s = (raw or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"^(www\.)?(t\.me|telegram\.me)/", "", s, flags=re.I)
    s = re.sub(r"^s/", "", s)
    s = s.lstrip("@").split("/")[0].split("?")[0]
    return s if _USERNAME_RE.match(s) else None


def parse_count(s: str) -> int:
    """'1.2K' -> 1200, '3M' -> 3000000, '856' -> 856."""
    s = (s or "").strip().replace(" ", "").replace(",", ".")
    m = re.match(r"^([\d.]+)([KMB]?)$", s, flags=re.I)
    if not m:
        return 0
    num = float(m.group(1))
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(round(num * mult))


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).strip()


def _media_type(chunk: str) -> str:
    if "tgme_widget_message_roundvideo" in chunk:
        return "кружок"
    if "tgme_widget_message_video" in chunk:
        return "видео"
    if "tgme_widget_message_poll" in chunk:
        return "опрос"
    if "tgme_widget_message_voice" in chunk:
        return "голосовое"
    if "tgme_widget_message_document" in chunk:
        return "файл"
    if "tgme_widget_message_photo" in chunk or "tgme_widget_message_grouped" in chunk:
        return "фото"
    return "текст"


def parse_page(page: str) -> dict:
    """Разбирает одну страницу t.me/s/... -> {'title', 'subscribers', 'posts': [...]}."""
    info = {"title": None, "subscribers": None, "description": None, "posts": []}

    m = re.search(r'class="tgme_channel_info_header_title"[^>]*>(.*?)</div>', page, re.S)
    if m:
        info["title"] = _strip_tags(m.group(1))
    m = re.search(r'class="tgme_channel_info_description"[^>]*>(.*?)</div>', page, re.S)
    if m:
        info["description"] = _strip_tags(m.group(1))
    for val, typ in re.findall(
        r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>', page
    ):
        if "subscriber" in typ or "подписч" in typ:
            info["subscribers"] = parse_count(val)

    chunks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for chunk in chunks:
        pm = re.search(r'data-post="[^"/]+/(\d+)"', chunk)
        if not pm:
            continue
        post_id = int(pm.group(1))

        text = ""
        tm = re.search(
            r'<div class="tgme_widget_message_text[^"]*js-message_text[^"]*"[^>]*>(.*?)'
            r'(?=<div class="tgme_widget_message_(?:footer|reactions|link_preview|inline_keyboard)|'
            r'<div class="tgme_widget_message_bubble_tail|$)',
            chunk,
            re.S,
        )
        if tm:
            text = _strip_tags(tm.group(1))

        vm = re.search(r'<span class="tgme_widget_message_views">([^<]+)</span>', chunk)
        views = parse_count(vm.group(1)) if vm else 0

        dm = re.search(r'<time[^>]+datetime="([^"]+)"', chunk)
        dt = None
        if dm:
            try:
                dt = datetime.fromisoformat(dm.group(1))
            except ValueError:
                dt = None

        vid = re.search(r'<video[^>]+src="([^"]+)"', chunk)
        thumb = re.search(
            r'(?:video_thumb|roundvideo_thumb|photo_wrap)[^>]*background-image:url\(\'([^\']+)\'\)', chunk
        )
        is_forward = "tgme_widget_message_forwarded_from" in chunk
        info["posts"].append(
            {
                "id": post_id,
                "text": text,
                "views": views,
                "date": dt,
                "type": _media_type(chunk),
                "forward": is_forward,
                "has_link": bool(re.search(r"https?://|t\.me/", text)),
                "video_url": html.unescape(vid.group(1)) if vid else None,
                "thumb_url": html.unescape(thumb.group(1)) if thumb else None,
            }
        )
    return info


def _fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_channel(username: str, days: int = 180, max_posts: int = 1000, fetch=_fetch, pause: float = 0.35) -> dict:
    """Собирает посты канала за последние `days` дней (по ~20 на страницу), но не больше `max_posts`."""
    base = f"https://t.me/s/{username}"
    try:
        first = fetch(base)
    except urllib.error.HTTPError as e:
        raise ChannelError(f"t.me ответил {e.code}") from e
    except urllib.error.URLError as e:
        raise ChannelError("не удалось открыть t.me") from e

    info = parse_page(first)
    if not info["posts"]:
        raise ChannelError("канал не найден, закрыт или без публичного превью")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen = {p["id"] for p in info["posts"]}

    def oldest_date():
        ds = [p["date"] for p in info["posts"] if p.get("date")]
        return min(ds) if ds else None

    pages = 1
    while len(info["posts"]) < max_posts and pages < 80:
        od = oldest_date()
        if od and od < cutoff:
            break
        before = min(seen)
        if before <= 1:
            break
        if pause:
            time.sleep(pause)
        try:
            more = parse_page(fetch(f"{base}?before={before}"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3)
                pages += 1
                continue
            break
        except Exception:
            break
        new = [p for p in more["posts"] if p["id"] not in seen]
        if not new:
            break
        info["posts"].extend(new)
        seen.update(p["id"] for p in new)
        pages += 1

    info["posts"] = [p for p in info["posts"] if not p.get("date") or p["date"] >= cutoff]
    info["posts"].sort(key=lambda p: p["id"], reverse=True)
    info["posts"] = info["posts"][:max_posts]
    info["username"] = username
    return info


def download(url: str, limit: int = 60_000_000, timeout: int = 60, retries: int = 2) -> bytes:
    """Скачивает файл; CDN Telegram иногда отвечает 500 — повторяем."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(limit + 1)
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    if len(data) > limit:
        raise ValueError("file too big")
    return data
