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


def selftest_research():
    if os.getenv("RESEARCH_SELFTEST"):
        try:
            r = research_niche("бокс")
            log.info("research selftest: grounded=%s sources=%d model=%s text=%s", r["grounded"], len(r["sources"]),
                     r["model"], r["text"][:300].replace("\n", " "))
        except Exception as e:
            log.warning("research selftest FAILED: %s", e)


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


OBSERVE = """Ты — глаза и уши продюсера. Посмотри и прослушай ролик целиком и опиши его ФАКТАМИ, без советов.

1. ПЕРВАЯ СЕКУНДА (0–1.5 с): что именно в кадре, крупность, свет, есть ли движение, текст на экране, что звучит.
2. ПО СЕКУНДАМ: хронология ключевых моментов (склейки, смена плана, зумы, яркие движения/позы, эмоции, текст на экране) с таймкодами.
3. ЗВУК: музыка (жанр, темп, настроение, где акценты/дропы по секундам), голос — ДОСЛОВНО что говорят и когда, тишина/паузы, качество звука.
4. СИНХРОН: попадают ли движения и монтаж в акценты музыки — где да, где нет (таймкоды).
5. КАРТИНКА: формат и кадрирование, свет, цвет, фон, одежда/бренды/логотипы, люди (сколько, где в кадре).
6. ФИНАЛ: чем заканчивается ролик, есть ли точка/зацикливание.

Только то, что реально видно и слышно. Если что-то не разобрать — так и скажи. По-русски, плотно, до 2500 знаков."""


def describe_video(video: bytes) -> str:
    """Gemini описывает ролик (картинка + звук) по секундам — потом Claude пишет по этому описанию советы."""
    return ask_gemini_video(video, OBSERVE, max_tokens=3000)


# ---------- исследование ниши через поиск Google (Gemini grounding) ----------

RESEARCH_PROMPT = """Ты — аналитик коротких видео. С помощью поиска Google найди, какие ролики (Instagram Reels, TikTok, YouTube Shorts)
РЕАЛЬНО залетают у авторов в нише «{niche}» в 2025–2026 годах — в первую очередь русскоязычных, затем зарубежных.

Нужны ФАКТЫ из найденного, не общие советы:
1. Конкретные авторы/аккаунты ниши, у которых залетают ролики (ники), и чем они берут.
2. 8–12 повторяющихся форматов ниши: как устроен ролик, хук первой секунды, длина, приёмы монтажа и звука,
   пример конкретного ролика (у кого, о чём, сколько просмотров — если есть в источниках).
3. Что в этой нише НЕ работает или уже приелось.

Если по нише мало данных — скажи прямо и опирайся на самые близкие ниши (назови какие).
Пиши по-русски, плотно, без воды, до 4000 знаков. Не выдумывай авторов, цифры и ролики — только то, что нашёл."""


def research_niche(niche: str) -> dict:
    """{"text", "sources": [(title, uri)], "grounded": bool, "model"}. Бросает исключение, если не вышло."""
    base = os.getenv("GEMINI_BASE_URL") or os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com")
    last = None
    for model in _models():
        try:
            data = _post(
                f"{base.rstrip('/')}/v1beta/models/{model}:generateContent",
                {"x-goog-api-key": os.environ["AI_API_KEY"]},
                {"contents": [{"role": "user", "parts": [{"text": RESEARCH_PROMPT.format(niche=niche)}]}],
                 "tools": [{"google_search": {}}],
                 "generationConfig": {"maxOutputTokens": 5000}},
                timeout=240,
            )
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode(errors="ignore")
            log.warning("research model=%s HTTP %s: %s", model, e.code, body)
            last = RuntimeError(f"research HTTP {e.code}: {body[:200]}")
            continue
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought")).strip()
        gm = cand.get("groundingMetadata") or {}
        sources = []
        for ch in gm.get("groundingChunks") or []:
            web = ch.get("web") or {}
            if web.get("uri"):
                sources.append((web.get("title") or web["uri"], web["uri"]))
        u = data.get("usageMetadata") or {}
        log.info("AI usage task=research model=%s in=%s out=%s grounded=%s sources=%d", model,
                 u.get("promptTokenCount"), u.get("candidatesTokenCount"), bool(gm), len(sources))
        if text:
            return {"text": text, "sources": sources[:12], "grounded": bool(gm), "model": model}
        last = RuntimeError("research: empty answer")
    raise last or RuntimeError("research failed")
