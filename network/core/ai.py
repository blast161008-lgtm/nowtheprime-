"""ИИ для сети ботов: тексты — Claude через ProxyAPI (как в Improve Lab), речь — Whisper.

Переменные: AI_API_KEY, AI_BASE_URL (https://api.proxyapi.ru), AI_PROVIDER=anthropic,
AI_MODEL — дешёвая модель (список через запятую), AI_MODEL_HEAVY — лучшая, STT_MODEL — распознавание речи.
"""
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("ai")

STYLE = (
    "Пиши по-русски, живым разговорным языком, как опытный креатор, а не как ИИ. Без markdown (никаких **, #, таблиц), "
    "без канцелярита и штампов: «погрузимся», «магия», «секрет успеха», «ключ к», «уникальный», «откройте для себя», "
    "«друзья», «не забудьте подписаться». Не выдумывай факты, цены, даты и события, которых автор не называл."
)


def enabled() -> bool:
    return bool(os.getenv("AI_API_KEY"))


def _models(heavy: bool) -> list[str]:
    default = "claude-haiku-4-5,claude-sonnet-4-5"
    env = (os.getenv("AI_MODEL_HEAVY") if heavy else None) or os.getenv("AI_MODEL", default)
    return [m.strip() for m in env.split(",") if m.strip()]


def _post(url, headers, payload, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ask(prompt: str, system: str = "", max_tokens: int = 1500, heavy: bool = False, tag: str = "") -> str:
    """Перебирает модели из списка, пока одна не ответит. 401/402/403 — сразу ошибка (ключ/баланс)."""
    sys_text = (system + "\n\n" + STYLE).strip()
    last = None
    for model in _models(heavy):
        try:
            return _ask_one(prompt, sys_text, max_tokens, model, tag)
        except urllib.error.HTTPError as e:
            log.warning("model %s failed %s: %s", model, e.code, e.read()[:300])
            last = e
            if e.code in (401, 402, 403):
                break
    raise last or RuntimeError("no models")


def _ask_one(prompt, system, max_tokens, model, tag):
    key = os.environ["AI_API_KEY"]
    if os.getenv("AI_PROVIDER", "anthropic").lower() == "anthropic":
        data = _post(os.getenv("AI_BASE_URL", "https://api.anthropic.com") + "/v1/messages",
                     {"x-api-key": key, "anthropic-version": "2023-06-01"},
                     {"model": model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": prompt}]})
        u = data.get("usage") or {}
        log.info("AI usage task=%s model=%s in=%s out=%s", tag, model, u.get("input_tokens"), u.get("output_tokens"))
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()
    base = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = _post(base + "/chat/completions", {"Authorization": f"Bearer {key}"},
                 {"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    u = data.get("usage") or {}
    log.info("AI usage task=%s model=%s in=%s out=%s", tag, model, u.get("prompt_tokens"), u.get("completion_tokens"))
    return data["choices"][0]["message"]["content"].strip()


# ---------- распознавание речи (Whisper) ----------

_JUNK = ("подпиш", "продолжение следует", "редактор субтитров", "субтитры сделал", "спасибо за просмотр",
         "ставьте лайк", "dimatorzok", "amara.org")


def _junk(seg: dict) -> bool:
    """Whisper на музыке без голоса выдумывает «Подпишись» и т.п. — отсеиваем."""
    t = (seg.get("text") or "").strip().lower()
    return (not t or any(j in t for j in _JUNK) or seg.get("no_speech_prob", 0) > 0.6
            or seg.get("avg_logprob", 0) < -1.2)


def _multipart(fields: dict, data: bytes, filename: str, mime: str):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe_segments(mp3: bytes, language: str = "ru") -> list[dict] | None:
    """Речь → список отрезков [{start, end, text}] (секунды). None — если распознать не вышло."""
    base = os.getenv("STT_BASE_URL") or (os.getenv("AI_BASE_URL", "https://api.openai.com").rstrip("/") + "/v1")
    models = [m.strip() for m in os.getenv("STT_MODEL", "openai/whisper-1,whisper-1").split(",") if m.strip()]
    for model in models:
        fields = {"model": model, "response_format": "verbose_json"}
        if language:
            fields["language"] = language
        body, ctype = _multipart(fields, mp3, "a.mp3", "audio/mpeg")
        req = urllib.request.Request(base + "/audio/transcriptions", data=body, headers={
            "Authorization": f"Bearer {os.environ['AI_API_KEY']}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            log.warning("stt %s failed %s: %s", model, e.code, e.read()[:200])
            continue
        except Exception as e:
            log.warning("stt %s failed: %s", model, e)
            continue
        segs = d.get("segments")
        if segs:
            good = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
                    for s in segs if not _junk(s)]
            return good or None
        text = (d.get("text") or "").strip()
        if text and not _junk({"text": text}):
            return [{"start": 0.0, "end": float(d.get("duration") or 5), "text": text}]
        return None
    return None
