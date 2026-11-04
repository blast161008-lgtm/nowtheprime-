"""Кадры из видео для ИИ: скачивает ролик и вырезает ключевые кадры через ffmpeg.

Если ffmpeg нет в системе, один раз скачивает статическую сборку в ./bin.
Если видео недоступно (большое — t.me не отдаёт файл), берёт обложку ролика.
"""
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request

from tg_parser import download

log = logging.getLogger("video")
BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
FFMPEG_URLS = [
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
]
_lock = threading.Lock()
_ffmpeg = None


def ffmpeg_path() -> str | None:
    global _ffmpeg
    if _ffmpeg:
        return _ffmpeg
    with _lock:
        if _ffmpeg:
            return _ffmpeg
        local = os.path.join(BIN_DIR, "ffmpeg")
        found = shutil.which("ffmpeg") or (local if os.path.exists(local) else None)
        if not found:
            found = _install()
        _ffmpeg = found
        log.info("ffmpeg: %s", found)
        return found


def _install() -> str | None:
    os.makedirs(BIN_DIR, exist_ok=True)
    dest = os.path.join(BIN_DIR, "ffmpeg")
    for url in FFMPEG_URLS:
        try:
            log.info("downloading ffmpeg from %s", url)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r:
                with tarfile.open(fileobj=r, mode="r|xz") as tar:
                    for m in tar:
                        if m.isfile() and os.path.basename(m.name) == "ffmpeg":
                            f = tar.extractfile(m)
                            with open(dest, "wb") as out:
                                shutil.copyfileobj(f, out)
                            os.chmod(dest, 0o755)
                            log.info("ffmpeg installed")
                            return dest
        except Exception as e:
            log.warning("ffmpeg download failed: %s", e)
    return None


def warmup():
    """Фоновая подготовка ffmpeg при старте бота."""
    threading.Thread(target=ffmpeg_path, daemon=True).start()


def _duration(ff: str, path: str) -> float:
    r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def _frame(ff: str, path: str, t: float) -> bytes | None:
    r = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path,
         "-frames:v", "1", "-vf", "scale='min(540,iw)':-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True, timeout=60,
    )
    return r.stdout if r.returncode == 0 and r.stdout else None


def frames_for(post: dict, n: int = 4) -> tuple[list[tuple[str, bytes]], float]:
    """Возвращает [(подпись, jpeg)], длительность. Первый кадр — «хук» (0.5 с)."""
    out: list[tuple[str, bytes]] = []
    dur = 0.0
    ff = ffmpeg_path()
    if post.get("video_url") and ff:
        try:
            data = download(post["video_url"])
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(data)
                path = tmp.name
            try:
                dur = _duration(ff, path)
                if dur > 0:
                    marks = [min(0.5, dur * 0.05)] + [dur * x for x in (0.3, 0.55, 0.85)][: n - 1]
                    for i, t in enumerate(marks):
                        img = _frame(ff, path, t)
                        if img:
                            label = "первая секунда (хук)" if i == 0 else f"{round(t)} с из {round(dur)}"
                            out.append((label, img))
            finally:
                os.unlink(path)
        except Exception as e:
            log.warning("video frames failed for %s: %s", post.get("id"), e)
    if not out and post.get("thumb_url"):
        try:
            out.append(("обложка (видео целиком недоступно)", download(post["thumb_url"], limit=5_000_000)))
        except Exception as e:
            log.warning("thumb failed for %s: %s", post.get("id"), e)
    return out, dur
