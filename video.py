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


def _probe(ff: str, path: str) -> dict:
    r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True)
    info = {"has_audio": "Audio:" in r.stderr}
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    if m:
        info["w"], info["h"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?) fps", r.stderr)
    if m:
        info["fps"] = float(m.group(1))
    if info["has_audio"]:
        v = subprocess.run([ff, "-hide_banner", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        mm = re.search(r"mean_volume: (-?[\d.]+) dB", v.stderr)
        if mm:
            info["mean_db"] = float(mm.group(1))
    return info


def reel_frames(data: bytes) -> tuple[list[tuple[str, bytes]], dict]:
    """Кадры из присланного ролика: начало подробно (там решается, смотреть ли), дальше по ходу."""
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg недоступен")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        dur = _duration(ff, path)
        meta = _probe(ff, path)
        meta["duration"] = round(dur, 1)
        if dur <= 0:
            raise RuntimeError("не удалось прочитать видео")
        marks = [("самый первый кадр (0.0 с) — то, что видно в ленте до старта", 0.0)]
        for t in (0.7, 1.5, 3.0):
            if t < dur - 0.3:
                marks.append((f"{t} с", t))
        for frac in (0.35, 0.6, 0.85):
            t = dur * frac
            if t > 3.3:
                marks.append((f"{round(t, 1)} с из {round(dur, 1)}", t))
        marks.append(("последний кадр (финал)", max(dur - 0.3, 0)))
        out = []
        for label, t in marks:
            img = _frame(ff, path, t)
            if img:
                out.append((label, img))
        return out, meta
    finally:
        os.unlink(path)


def to_jpeg(data: bytes) -> bytes:
    """Картинку любого формата (png, webp, heic…) — в jpeg для ИИ."""
    if data[:3] == b"\xff\xd8\xff":
        return data
    ff = ffmpeg_path()
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vf", "scale='min(1280,iw)':-2",
                        "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                       input=data, capture_output=True, timeout=60)
    if r.returncode or not r.stdout:
        raise RuntimeError("не получилось прочитать картинку")
    return r.stdout


def compress_for_ai(data: bytes, max_sec: int = 90, height: int = 360, fps: int = 4, crf: int = 34,
                    abr: str = "48k") -> bytes:
    """Сжимает ролик для ИИ (Gemini всё равно смотрит ~1 кадр/с): 360p, 4 fps, моно-звук — обычно 200–600 КБ."""
    ff = ffmpeg_path()
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(max_sec),
             "-vf", f"scale=-2:'min({height},ih)',fps={fps}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
             "-c:a", "aac", "-b:a", abr, "-ac", "1", "-ar", "22050", "-movflags", "+faststart", dst],
            capture_output=True, timeout=240,
        )
        if r.returncode or not os.path.exists(dst):
            raise RuntimeError("не удалось сжать видео: " + r.stderr.decode(errors="ignore")[-300:])
        with open(dst, "rb") as f:
            return f.read()


def audio_facts(data: bytes) -> dict:
    """Факты о звуке без ИИ: когда начинается звук, паузы, громкость. + mp3 для расшифровки речи."""
    ff = ffmpeg_path()
    out = {}
    with tempfile.TemporaryDirectory() as d:
        src, mp3 = os.path.join(d, "in.mp4"), os.path.join(d, "a.mp3")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run([ff, "-hide_banner", "-i", src, "-vn", "-af", "silencedetect=n=-40dB:d=0.4", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=120)
        starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", r.stderr)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
        if starts and starts[0] <= 0.05 and ends:
            out["sound_starts_at"] = round(ends[0], 1)
        else:
            out["sound_starts_at"] = 0.0
        out["silences"] = [(round(a, 1), round(b, 1)) for a, b in zip(starts, ends) if b - a >= 0.4][:6]
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                            "-b:a", "32k", mp3], capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.exists(mp3):
            with open(mp3, "rb") as f:
                out["mp3"] = f.read()
    return out


def test_clip() -> bytes:
    """1-секундный ролик со звуком для самопроверки."""
    ff = ffmpeg_path()
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x568:d=1:r=12",
                        "-f", "lavfi", "-i", "sine=f=440:d=1", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart", "-f", "mp4", "pipe:1"],
                       capture_output=True, timeout=60)
    if r.returncode or not r.stdout:
        # mp4 в pipe требует frag — запасной вариант
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x568:d=1:r=12",
                            "-f", "lavfi", "-i", "sine=f=440:d=1", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                            "-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"],
                           capture_output=True, timeout=60)
    return r.stdout
