"""Видео со звуком (Gemini через нативный API) и расшифровка речи."""
import base64
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

from ai import SYSTEM, _post

log = logging.getLogger("ai")


def gemini_enabled() -> bool:
    return bool(os.getenv("AI_API_KEY")) and os.getenv("VIDEO_AI", "gemini").lower() == "gemini"


def ask_gemini_video(video: bytes, prompt: str, max_tokens: int = 6000) -> str:
    """Отдаёт ролик целиком (картинка + звук) модели Gemini."""
    base = os.getenv("GEMINI_BASE_URL") or os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    data = _post(
        f"{base.rstrip('/')}/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": os.environ["AI_API_KEY"]},
        {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(video).decode()}},
                {"text": prompt},
            ]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 1024}},
        },
        timeout=240,
    )
    u = data.get("usageMetadata") or {}
    log.info("AI usage task=reel model=%s in=%s out=%s", model, u.get("promptTokenCount"), u.get("candidatesTokenCount"))
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise RuntimeError("gemini: пустой ответ " + json.dumps(data)[:300])
    return text


def _multipart(fields: dict, file_bytes: bytes, filename: str, ctype: str):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe(mp3: bytes) -> str | None:
    """Расшифровка речи через OpenAI-совместимый /v1/audio/transcriptions. None — если не вышло."""
    base = os.getenv("STT_BASE_URL") or (os.getenv("AI_BASE_URL", "https://api.openai.com").rstrip("/") + "/v1")
    models = [m.strip() for m in os.getenv(
        "STT_MODEL", "openai/whisper-1,whisper-1,openai/gpt-4o-mini-transcribe").split(",") if m.strip()]
    for model in models:
        fields = {"model": model, "language": "ru",
                  "response_format": "verbose_json" if "whisper" in model else "json"}
        body, ctype = _multipart(fields, mp3, "a.mp3", "audio/mpeg")
        req = urllib.request.Request(base + "/audio/transcriptions", data=body, headers={
            "Authorization": f"Bearer {os.environ['AI_API_KEY']}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            log.warning("stt %s failed %s: %s", model, e.code, e.read()[:200])
            continue
        except Exception as e:
            log.warning("stt %s failed: %s", model, e)
            continue
        segs = d.get("segments")
        if segs:
            return "\n".join(f"[{s['start']:.1f}–{s['end']:.1f} с] {s['text'].strip()}" for s in segs if s.get("text"))
        return (d.get("text") or "").strip() or None
    return None


SOUND_BLOCK = """
ЗВУК: что звучит (музыка / голос / тишина), с какой секунды начинается, подходит ли трек к ролику и настроению,
попадает ли движение и монтаж в бит, понятна ли речь, что сказать вслух в первые 2 секунды (дословно).
"""
