"""ИИ-слой. Работает с Claude (Anthropic) или любым OpenAI-совместимым API
(OpenAI, OpenRouter, DeepSeek и т.п.). Настройка через переменные окружения:

AI_PROVIDER = anthropic | openai
AI_API_KEY  = ключ
AI_MODEL    = модель (по умолчанию claude-sonnet-5-5 для anthropic)
AI_BASE_URL = для openai-совместимых, напр. https://openrouter.ai/api/v1
"""
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("ai")

SYSTEM = (
    "Ты — продюсер контента для авторов Telegram-каналов. Пишешь по-русски, просто и по делу, "
    "как опытный друг-продюсер, а не как отчёт консультанта. Без воды, без канцелярита, без markdown "
    "(никаких **, #, таблиц) — только обычный текст, короткие абзацы, можно эмодзи-маркеры и тире. "
    "Опирайся только на цифры и посты, которые тебе дали. Если данных мало — так и скажи. "
    "Советы должны быть конкретными: не «делайте интересный контент», а «пост-разбор X в формате Y, начни с фразы Z»."
)


def enabled() -> bool:
    return bool(os.getenv("AI_API_KEY"))


def _post(url, headers, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ask(prompt: str, max_tokens: int = 4000) -> str:
    """AI_MODEL может быть списком через запятую: если модель недоступна, пробуем следующую."""
    default = "claude-sonnet-5-5" if os.getenv("AI_PROVIDER", "anthropic").lower() == "anthropic" else "gpt-4o-mini"
    models = [m.strip() for m in os.getenv("AI_MODEL", default).split(",") if m.strip()]
    last = None
    for model in models:
        try:
            return _ask_one(prompt, max_tokens, model)
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            log.warning("model %s failed %s: %s", model, e.code, body)
            last = e
            if e.code in (401, 402, 403):
                break
    raise last or RuntimeError("no models")


def _ask_one(prompt: str, max_tokens: int, model: str) -> str:
    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    key = os.environ["AI_API_KEY"]
    if provider == "anthropic":
        data = _post(
            os.getenv("AI_BASE_URL", "https://api.anthropic.com") + "/v1/messages",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {
                "model": model,
                "max_tokens": max_tokens,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()

    base = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = _post(
        base + "/chat/completions",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        },
    )
    return data["choices"][0]["message"]["content"].strip()


# ---------- промпты ----------

def report_prompt(stats: str) -> str:
    return f"""Вот статистика Telegram-канала:

{stats}

Сделай разбор для автора. Структура:

1. Что заходит — 2–4 пункта, каждый со ссылкой на цифры (формат, темы, длина, время).
2. Что не работает — 2–3 пункта, честно.
3. 5 идей постов на ближайшие дни. Для каждой: тема, формат (текст/фото/видео/кружок/опрос), и первая строка-хук дословно.
4. Когда постить — дни и время по МСК, и сколько раз в неделю.
5. Одно главное действие на эту неделю.

До 2500 знаков."""


def compare_prompt(mine: str, rivals: list[str]) -> str:
    rivals_txt = "\n\n---\n\n".join(rivals)
    return f"""МОЙ КАНАЛ:
{mine}

КОНКУРЕНТЫ:
{rivals_txt}

Сравни мой канал с конкурентами. Структура:

1. Цифры рядом: охват, частота, лучший формат — у меня и у них, коротко.
2. Что у конкурентов набирает больше всего и почему (по их лучшим постам).
3. Что из этого я могу забрать себе — 3–5 конкретных приёмов, без копирования.
4. Где у меня преимущество, которое стоит усилить.
5. 3 идеи постов с хуками, которые закрывают разрыв.

До 3000 знаков."""


def plan_prompt(stats: str) -> str:
    return f"""Статистика канала:

{stats}

Составь контент-план на 7 дней, начиная с понедельника. Для каждого дня: время публикации (МСК),
формат, тема и первая строка поста дословно. Если по цифрам лучше постить не каждый день — пропусти день и
напиши «отдых». Опирайся на то, что у канала уже заходит, и добавь 1–2 эксперимента, пометь их «тест».
В конце — одна строка, по какой цифре через неделю понять, что план сработал.

До 2500 знаков."""
