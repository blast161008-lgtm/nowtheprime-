"""ffmpeg: звук из видео, субтитры (SRT) и «вшивание» субтитров в ролик.

Если ffmpeg нет в системе, один раз скачивается статическая сборка в ./bin (как в Improve Lab).
Шрифт с кириллицей лежит в network/fonts — не зависим от шрифтов сервера.
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

log = logging.getLogger("media")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(HERE, "bin")
FONTS_DIR = os.path.join(HERE, "fonts")
FFMPEG_URLS = [
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
]
_lock = threading.Lock()
_ff = None


def ffmpeg() -> str | None:
    global _ff
    with _lock:
        if _ff:
            return _ff
        local = os.path.join(BIN_DIR, "ffmpeg")
        _ff = shutil.which("ffmpeg") or (local if os.path.exists(local) else None) or _install()
        log.info("ffmpeg: %s", _ff)
        return _ff


def _install() -> str | None:
    os.makedirs(BIN_DIR, exist_ok=True)
    dest = os.path.join(BIN_DIR, "ffmpeg")
    for url in FFMPEG_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=180) as r, tarfile.open(fileobj=r, mode="r|xz") as tar:
                for m in tar:
                    if m.isfile() and os.path.basename(m.name) == "ffmpeg":
                        with open(dest, "wb") as out:
                            shutil.copyfileobj(tar.extractfile(m), out)
                        os.chmod(dest, 0o755)
                        return dest
        except Exception as e:
            log.warning("ffmpeg download failed: %s", e)
    return None


def warmup():
    threading.Thread(target=ffmpeg, daemon=True).start()


def _run(args: list[str], timeout: int = 240):
    r = subprocess.run(args, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-600:])


def audio_mp3(video: bytes) -> bytes:
    """Звуковая дорожка в компактный mp3 (моно, 16 кГц) — дёшево для распознавания."""
    ff = ffmpeg()
    if not ff:
        raise RuntimeError("no ffmpeg")
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in.mp4"), os.path.join(d, "a.mp3")
        open(src, "wb").write(video)
        _run([ff, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", dst])
        return open(dst, "rb").read()


# ---------- SRT ----------

def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_for_screen(segments: list[dict], max_words: int = 6) -> list[dict]:
    """Короткие строки для вертикального видео: длинный отрезок делится на куски по ~max_words слов,
    время делится пропорционально длине кусков."""
    out = []
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        chunks = [words[i:i + max_words] for i in range(0, len(words), max_words)]
        total = sum(len(" ".join(c)) for c in chunks) or 1
        t = seg["start"]
        dur = max(0.3, seg["end"] - seg["start"])
        for c in chunks:
            text = " ".join(c)
            d = dur * len(text) / total
            out.append({"start": t, "end": t + d, "text": text})
            t += d
    return out


def to_srt(segments: list[dict]) -> str:
    return "\n".join(f"{i}\n{_ts(s['start'])} --> {_ts(s['end'])}\n{s['text']}\n"
                     for i, s in enumerate(segments, 1))


def burn(video: bytes, srt: str, style: str = "classic") -> bytes:
    """Вшивает субтитры в видео. Крупный жирный шрифт с обводкой — читается на любом фоне."""
    ff = ffmpeg()
    if not ff:
        raise RuntimeError("no ffmpeg")
    styles = {
        "classic": "FontName=DejaVu Sans,Bold=1,FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                   "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=60",
        "yellow": "FontName=DejaVu Sans,Bold=1,FontSize=16,PrimaryColour=&H0000E6FF,OutlineColour=&H00000000,"
                  "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=60",
        "box": "FontName=DejaVu Sans,Bold=1,FontSize=15,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,"
               "BorderStyle=3,Outline=6,Shadow=0,Alignment=2,MarginV=60",
    }
    with tempfile.TemporaryDirectory() as d:
        src, sub, dst = os.path.join(d, "in.mp4"), os.path.join(d, "s.srt"), os.path.join(d, "out.mp4")
        open(src, "wb").write(video)
        open(sub, "w", encoding="utf-8").write(srt)
        # не больше 1280 по высоте и быстрый пресет: на слабом сервере кодирование должно уложиться в пару минут
        vf = (f"scale=-2:'min(1280,ih)',subtitles={sub}:fontsdir={FONTS_DIR}"
              f":force_style='{styles.get(style, styles['classic'])}'")
        _run([ff, "-y", "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
              "-c:a", "copy", "-movflags", "+faststart", dst], timeout=900)
        return open(dst, "rb").read()


def duration(video: bytes) -> float:
    ff = ffmpeg()
    if not ff:
        return 0.0
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(video)
        f.flush()
        r = subprocess.run([ff, "-i", f.name], capture_output=True, timeout=60)
        m = re.search(rb"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
        return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
