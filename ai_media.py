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


_VARIANTS = ["full", "plain", "camel"]
_preferred = None


def _payload(variant: str, video_b64: str, prompt: str, max_tokens: int) -> dict:
    if variant == "camel":
        media = {"inlineData": {"mimeType": "video/mp4", "data": video_b64}}
    else:
        media = {"inline_data": {"mime_type": "video/mp4", "data": video_b64}}
    if variant == "full":
        return {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [media, {"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 1024}},
        }
    return {
        "contents": [{"role": "user", "parts": [media, {"text": SYSTEM + "\n\n" + prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }


def _models():
    return [m.strip() for m in os.getenv(
        "GEMINI_MODEL", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-3-flash-preview").split(",") if m.strip()]


def _call(variant, video, prompt, max_tokens, model):
    base = os.getenv("GEMINI_BASE_URL") or os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com")
    data = _post(
        f"{base.rstrip('/')}/v1beta/models/{model}:generateContent",
        {"x-goog-api-key": os.environ["AI_API_KEY"]},
        _payload(variant, base64.b64encode(video).decode(), prompt, max_tokens),
        timeout=240,
    )
    u = data.get("usageMetadata") or {}
    log.info("AI usage task=reel model=%s variant=%s in=%s out=%s", model, variant,
             u.get("promptTokenCount"), u.get("candidatesTokenCount"))
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise RuntimeError("gemini: пустой ответ " + json.dumps(data)[:300])
    return text


def ask_gemini_video(video: bytes, prompt: str, max_tokens: int = 6000) -> str:
    """Отдаёт ролик целиком (картинка + звук) модели Gemini. Перебирает модели и форматы, запоминает рабочие."""
    global _preferred
    combos = [(m, v) for m in _models() for v in _VARIANTS]
    if _preferred in combos:
        combos.remove(_preferred)
        combos.insert(0, _preferred)
    last = None
    dead_models = set()
    for model, v in combos:
        if model in dead_models:
            continue
        try:
            text = _call(v, video, prompt, max_tokens, model)
            _preferred = (model, v)
            return text
        except urllib.error.HTTPError as e:
            body = e.read()[:600].decode(errors="ignore")
            log.warning("gemini model=%s variant=%s HTTP %s: %s", model, v, e.code, body)
            last = RuntimeError(f"gemini HTTP {e.code}: {body[:200]}")
            if "not supported" in body.lower() or "not found" in body.lower() or e.code == 404:
                dead_models.add(model)
                continue
            if e.code not in (400, 422):
                break
    raise last or RuntimeError("gemini failed")


def selftest(make_clip):
    """При старте: проверяем, принимает ли прокси видео для Gemini (1-секундный ролик, копейки)."""
    if not gemini_enabled():
        return
    try:
        clip = make_clip()
        text = ask_gemini_video(clip, "Что на видео и есть ли звук? Ответь одной короткой фразой.", max_tokens=200)
        log.info("gemini selftest OK (%s): %s", _preferred, text[:120])
    except Exception as e:
        log.warning("gemini selftest FAILED: %s", e)


def _multipart(fields: dict, file_bytes: bytes, filename: str, ctype: str):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


_JUNK = ("подпиш", "продолжение следует", "субтитр", "редактор субтитров", "спасибо за просмотр",
         "ставьте лайк", "dimatorzok", "amara.org", "всем пока")


def _hallucination(seg: dict) -> bool:
    """Whisper на музыке без голоса выдумывает фразы вроде «Подпишись» — отсеиваем."""
    t = seg.get("text", "").strip().lower()
    if not t or any(j in t for j in _JUNK):
        return True
    if seg.get("no_speech_prob", 0) > 0.5:
        return True
    if seg.get("avg_logprob", 0) < -1.0:
        return True
    return False


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
        if segs is not None:
            good = [g for g in segs if g.get("text") and not _hallucination(g)]
            return "\n".join(f"[{g['start']:.1f}–{g['end']:.1f} с] {g['text'].strip()}" for g in good) or None
        t = (d.get("text") or "").strip()
        return None if not t or _hallucination({"text": t}) else t
    return None


SOUND_BLOCK = """
ЗВУК: что звучит (музыка / голос / тишина), с какой секунды начинается, подходит ли трек к ролику и настроению,
попадает ли движение и монтаж в бит, понятна ли речь, что сказать вслух в первые 2 секунды (дословно).
"""
