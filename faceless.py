"""Ролики без лица для канала и рилс: сценарий (Claude) → ИИ-голос (OpenAI TTS через ProxyAPI) → вертикальное видео (ffmpeg).

Кадр 720×1280: сверху — крупный хук, по центру — фраза, которая сейчас звучит (субтитры), снизу — @Improvelab_bot
и полоска прогресса. Только ffmpeg и стандартная библиотека Python, без дополнительных пакетов.
"""
import json
import logging
import os
import re
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.request

log = logging.getLogger("faceless")
HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "DejaVuSans-Bold.ttf")
W, H, FPS = 720, 1280, 24
VOICE = os.getenv("TTS_VOICE", "onyx")            # onyx — мужской, nova / shimmer — женские
HANDLE = os.getenv("VIDEO_HANDLE", "@Improvelab_bot")
TTS_TONE = ("Говори по-русски, живо и уверенно, как автор короткого ролика в соцсетях: энергично, но без крика, "
            "с паузами между мыслями.")


# ---------- сценарий ----------

def script_prompt(source: str, facts: str, rules: str) -> str:
    return (f"{facts}\n\n{rules}\n\nСделай из этого короткий вертикальный ролик без лица (только голос + текст на экране) "
            f"для Telegram-канала и рилс.\n\nИСХОДНИК:\n{source}\n\n"
            "Ответ — только JSON-объект:\n"
            '{"title": "хук на экране, до 38 знаков, цепляет с первой секунды",\n'
            ' "voice": "текст для озвучки: 55–85 слов (20–35 секунд), разговорно, короткие фразы, первая фраза — хук, '
            'в конце один призыв: попробовать бесплатно в боте Improve Lab. Без эмодзи, без ссылок, цифры словами не нужно",\n'
            ' "caption": "подпись к ролику в канале, 200–600 знаков, по правилам выше"}')


def parse_script(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    s = {k: str(data.get(k, "")).strip() for k in ("title", "voice", "caption")}
    if len(s["voice"].split()) < 15:
        raise ValueError("слишком короткий сценарий")
    s["title"] = s["title"][:60] or s["voice"].split(".")[0][:60]
    return s


# ---------- голос ----------

def _tts_bases() -> list[str]:
    out = [os.getenv("TTS_BASE_URL"), os.getenv("STT_BASE_URL")]
    base = os.getenv("AI_BASE_URL", "")
    if "/anthropic" in base:
        out.append(base.replace("/anthropic", "/openai").rstrip("/") + "/v1")
    elif base:
        out.append(base.rstrip("/") + ("" if base.rstrip("/").endswith("/v1") else "/v1"))
    out.append("https://api.proxyapi.ru/openai/v1")
    seen, res = set(), []
    for b in out:
        if b and b.rstrip("/") not in seen:
            seen.add(b.rstrip("/"))
            res.append(b.rstrip("/"))
    return res


_good = {}  # base/model, которые сработали — чтобы не перебирать каждый раз


def tts(text: str) -> bytes:
    """MP3 с озвучкой. Перебирает адреса и модели, пока одна не ответит."""
    key = os.getenv("TTS_API_KEY") or os.environ["AI_API_KEY"]
    models = [m.strip() for m in os.getenv("TTS_MODEL", "gpt-4o-mini-tts,tts-1,openai/gpt-4o-mini-tts,openai/tts-1").split(",") if m.strip()]
    combos = [(b, m) for b in _tts_bases() for m in models]
    if _good:
        combos.insert(0, (_good["base"], _good["model"]))
    last = ""
    for base, model in combos:
        body = {"model": model, "voice": VOICE, "input": text[:3500], "response_format": "mp3"}
        if "gpt-4o" in model:
            body["instructions"] = TTS_TONE
        req = urllib.request.Request(base + "/audio/speech", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                audio = r.read()
            if len(audio) > 1000:
                if _good.get("model") != model:
                    log.info("tts ok model=%s base=%s", model, base.split("//")[-1].split("/")[0])
                _good.update(base=base, model=model)
                return audio
        except urllib.error.HTTPError as e:
            last = f"{model}: {e.code} {e.read()[:150]!r}"
        except Exception as e:
            last = f"{model}: {e}"
        log.warning("tts failed %s", last)
    raise RuntimeError(f"tts failed: {last}")


# ---------- субтитры и монтаж ----------

def phrases(text: str, max_words: int = 5) -> list[str]:
    """Режет текст на короткие фразы для экрана, стараясь резать по знакам препинания."""
    out = []
    text = re.sub(r"Improve Lab", "Improve\u00a0Lab", re.sub(r"\s+", " ", text))
    for sent in re.split(r"(?<=[.!?…]) +", text.strip()):
        parts = re.split(r"(?<=[,;:—–])\s+", sent)
        buf = []
        for part in parts:
            for w in part.split(" "):
                if w in ("—", "–") and not buf and out:
                    out[-1] += " " + w          # тире остаётся в конце предыдущей фразы
                    continue
                buf.append(w)
                if len(buf) >= max_words:
                    carry = [buf.pop()] if len(buf[-1]) <= 2 else []   # «со», «в», «и» не висят в конце
                    out.append(" ".join(buf))
                    buf = carry
            if len(buf) >= 3:
                out.append(" ".join(buf))
                buf = []
        if buf:
            if out and len(buf) <= 2 and len(out[-1].split()) + len(buf) <= max_words + 2:
                out[-1] += " " + " ".join(buf)
            else:
                out.append(" ".join(buf))
    return [p.replace("\u00a0", " ") for p in out if p]


def timings(chunks: list[str], duration: float, lead: float = 0.15) -> list[tuple[float, float, str]]:
    """Время каждой фразы пропорционально её длине (голос TTS ровный, этого хватает)."""
    weights = [len(c) + 6 for c in chunks]   # +6 — пауза между фразами
    total = sum(weights) or 1
    span = max(0.5, duration - lead - 0.2)
    t, out = lead, []
    for c, w in zip(chunks, weights):
        d = span * w / total
        out.append((t, t + d, c))
        t += d
    return out


def _duration(ff: str, path: str) -> float:
    r = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def render(ff: str, title: str, voice_mp3: bytes, voice_text: str, timeout: int = 900) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        audio = os.path.join(d, "voice.mp3")
        open(audio, "wb").write(voice_mp3)
        dur = _duration(ff, audio) or max(8.0, len(voice_text.split()) / 2.6)
        total = dur + 0.6

        def tfile(name, text):
            p = os.path.join(d, name)
            open(p, "w", encoding="utf-8").write(text)
            return p

        title_p = tfile("title.txt", "\n".join(textwrap.wrap(title.upper(), 14)[:3]))
        handle_p = tfile("handle.txt", HANDLE)
        f = [f"drawbox=x=60:y=120:w=90:h=10:color=0xFFD60A:t=fill",
             f"drawtext=fontfile={FONT}:textfile={title_p}:fontsize=54:fontcolor=0xFFD60A:line_spacing=12:"
             f"x=60:y=160",
             f"drawtext=fontfile={FONT}:textfile={handle_p}:fontsize=34:fontcolor=0x9A9AA6:x=(w-text_w)/2:y=h-150"]
        for i, (a, b, text) in enumerate(timings(phrases(voice_text), dur)):
            lines = textwrap.wrap(text, 15)[:4]
            top = H // 2 + 40 - len(lines) * 88 // 2
            for j, line in enumerate(lines):   # каждая строка отдельно — так она по центру
                p = tfile(f"s{i}_{j}.txt", line)
                f.append(f"drawtext=fontfile={FONT}:textfile={p}:fontsize=66:fontcolor=white:"
                         f"borderw=4:bordercolor=black:x=(w-text_w)/2:y={top + j * 88}:"
                         f"enable='between(t,{a:.2f},{b:.2f})'")
        graph = (f"[0:v]{','.join(f)}[base];"
                 f"[base][1:v]overlay=x='-W+W*t/{total:.2f}':y=H-10:shortest=1[v]")
        out = os.path.join(d, "out.mp4")
        args = [ff, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=0x0D0D12:s={W}x{H}:r={FPS}:d={total:.2f}",
                "-f", "lavfi", "-i", f"color=c=0xFFD60A:s={W}x10:r={FPS}:d={total:.2f}",
                "-i", audio, "-filter_complex", graph, "-map", "[v]", "-map", "2:a",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "23", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-af", "apad", "-t", f"{total:.2f}", "-movflags", "+faststart", out]
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           preexec_fn=lambda: os.nice(10))  # не мешаем боту отвечать людям
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="ignore")[-500:])
        return open(out, "rb").read()


def silent_mp3(ff: str, seconds: float = 3.0) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.mp3")
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono",
                        "-t", str(seconds), p], check=True, capture_output=True)
        return open(p, "rb").read()


# ---------- загрузка видео в Telegram ----------

def upload_video(token: str, chat_id, video: bytes, caption: str = "", keyboard=None) -> dict | None:
    import uuid
    boundary = uuid.uuid4().hex
    fields = {"chat_id": str(chat_id), "caption": caption[:1024], "supports_streaming": "true",
              "width": str(W), "height": str(H)}
    if keyboard:
        fields["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="improvelab.mp4"\r\n'
             "Content-Type: video/mp4\r\n\r\n").encode() + video + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendVideo", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode()).get("result")
    except urllib.error.HTTPError as e:
        log.warning("sendVideo %s: %s", e.code, e.read()[:200])
    except Exception as e:
        log.warning("sendVideo failed: %s", e)
    return None
