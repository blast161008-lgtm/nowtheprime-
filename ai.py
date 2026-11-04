"""ИИ-слой. Работает с Claude (Anthropic) или любым OpenAI-совместимым API
(OpenAI, OpenRouter, DeepSeek и т.п.). Настройка через переменные окружения:

AI_PROVIDER = anthropic | openai
AI_API_KEY  = ключ
AI_MODEL    = модель (по умолчанию claude-sonnet-5-5 для anthropic)
AI_BASE_URL = для openai-совместимых, напр. https://openrouter.ai/api/v1
"""
import base64
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
    "Советы должны быть конкретными: не «делайте интересный контент», а «пост-разбор X в формате Y, начни с фразы Z». "
    "ВАЖНО: никогда не выдумывай события, которых автор не называл, — классы, мастер-классы, батлы, дропы, концерты, "
    "даты, цены, призы, ссылки на запись. Анонс или продажа — не контент, а то, что автор делает, только когда ему есть что "
    "анонсировать. Если видишь, что анонсы хорошо заходят, — так и скажи и советуй, КАК подавать анонс, когда событие "
    "будет, а в идеях и планах предлагай контент, который не требует событий: обучалки, разборы, мнения, истории, "
    "бэкстейдж, опросы, челленджи, юмор, ответы на вопросы."
)


def enabled() -> bool:
    return bool(os.getenv("AI_API_KEY"))


def _post(url, headers, payload, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ask(prompt, max_tokens: int = 4000, heavy: bool = False, tag: str = "") -> str:
    """AI_MODEL — модель для обычных задач, AI_MODEL_HEAVY — для тяжёлых (видео).
    Можно списком через запятую: если модель недоступна, пробуем следующую."""
    default = "claude-sonnet-5-5" if os.getenv("AI_PROVIDER", "anthropic").lower() == "anthropic" else "gpt-4o-mini"
    env = (os.getenv("AI_MODEL_HEAVY") if heavy else None) or os.getenv("AI_MODEL", default)
    models = [m.strip() for m in env.split(",") if m.strip()]
    global _tag
    _tag = tag
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


def _content(prompt, provider):
    """prompt — строка или список частей: str (текст) или bytes (jpeg)."""
    if isinstance(prompt, str):
        return prompt
    out = []
    for part in prompt:
        if isinstance(part, bytes):
            b64 = base64.b64encode(part).decode()
            if provider == "anthropic":
                out.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            else:
                out.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
        else:
            out.append({"type": "text", "text": part})
    return out


_tag = ""


def _log_usage(model, usage):
    if usage:
        inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        log.info("AI usage task=%s model=%s in=%s out=%s", _tag, model, inp, out)


def _ask_one(prompt, max_tokens: int, model: str) -> str:
    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    key = os.environ["AI_API_KEY"]
    prompt = _content(prompt, "anthropic" if provider == "anthropic" else "openai")
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
        _log_usage(model, data.get("usage"))
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
    _log_usage(model, data.get("usage"))
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

Сравни мой контент с контентом конкурентов. Цель — понять, что МНЕ снимать и постить, чтобы поднять охваты.
Охват сравнивай в процентах от подписчиков (а не в абсолютных просмотрах), т.к. каналы разного размера.

Структура ответа:

1. Счёт — 3–4 строки: у кого охват выше, кто чаще постит, у кого какой формат работает лучше.
2. Что у них залетает — разбери их лучшие посты: тема, формат, подача, хук. Почему это набрало больше обычного.
3. Чего нет у меня — темы и форматы, которые у них работают, а у меня их нет или они слабые.
4. Где я сильнее — что у меня работает лучше, и это надо усилить, а не бросать.
5. Что мне снять/выложить — 5 конкретных идей под МОЙ стиль и тематику (не копия конкурента). Для каждой: формат (видео/кружок/фото/текст), о чём, первая строка или первые слова в кадре дословно, и какой их пост это «перебивает».
6. Одно главное изменение на ближайшие 2 недели.

До 3500 знаков."""


def plan_prompt(stats: str, week: str | None = None) -> str:
    if week:
        week_txt = (f"ЧТО У АВТОРА НА ЭТОЙ НЕДЕЛЕ (со слов автора): {week}\n"
                    "Анонсы и продажи — только про это, максимум 2 поста в неделю, остальное — обычный контент. "
                    "Даты/цены/место бери только из слов автора, если их нет — пиши [дата], [цена].")
    else:
        week_txt = ("На этой неделе у автора НЕТ событий и продаж. Ни одного анонса, ни одного «записывайтесь», "
                    "никаких классов/батлов/дропов — только контент, который не требует событий.")
    return f"""Статистика канала:

{stats}

{week_txt}

Составь контент-план на 7 дней, начиная с понедельника. Для каждого дня: время публикации (МСК),
формат, тема и первая строка поста дословно. Если по цифрам лучше постить не каждый день — пропусти день и
напиши «отдых». Опирайся на то, что у канала уже заходит, и добавь 1–2 эксперимента, пометь их «тест».
Каждый пост должен быть реально выполним одним человеком за 15–60 минут.
В конце — одна строка, по какой цифре через неделю понять, что план сработал.

До 2500 знаков."""


def video_intro(stats: str) -> str:
    return f"""Статистика канала (для контекста):

{stats}

Дальше — кадры из роликов этого канала. Сначала ролики, которые набрали БОЛЬШЕ всего просмотров, потом — МЕНЬШЕ всего.
У каждого ролика: просмотры, во сколько раз больше/меньше обычного поста, подпись к посту и кадры
(первый кадр — первая секунда, то, что зритель видит до того, как решить, смотреть ли дальше)."""


def video_outro() -> str:
    return """Посмотри на кадры и сравни сильные ролики со слабыми. Ответ:

1. Что общего у роликов, которые залетели: что в кадре в первую секунду, крупность, свет, локация, движение, люди, текст на экране, одежда/стиль, эмоция.
2. Что не так со слабыми — конкретно по кадрам («в первой секунде пустой зал», «лицо далеко», «темно» и т.п.).
3. Хук: как должна выглядеть первая секунда следующего ролика — опиши кадр, как режиссёр оператору.
4. 3 идеи роликов, которые повторяют рецепт сильных: что снять, где, как поставить камеру, текст на экране дословно.
5. Чек-лист перед публикацией из 5 пунктов.

Честно: если по кадрам разница не видна, так и скажи. Видео целиком ты не видишь — только кадры, звук не слышишь; не выдумывай то, чего нет на кадрах.
До 3500 знаков."""


def ideas_prompt(stats: str, focus: str | None = None, previous: str | None = None) -> str:
    focus_txt = f"\nТЕМА: автор просит идеи именно про: «{focus}». Все идеи — в рамках этой темы.\n" if focus else ""
    prev_txt = (
        f"\nЭТИ ИДЕИ УЖЕ БЫЛИ — не повторяй их и не перефразируй, придумай новые направления:\n{previous}\n"
        if previous else ""
    )
    return f"""Статистика канала:

{stats}
{focus_txt}{prev_txt}
Придумай 10 идей, что СНЯТЬ (ролики и кружки, можно 1–2 фото-поста), чтобы поднять охваты этого канала.
Опирайся на то, что у канала уже залетает (лучшие посты, форматы, темы), и на стиль автора по его подписям.
Смешай: 4 идеи «повторить рецепт хита», 3 — новые форматы, которых у канала ещё не было, 3 — смелые эксперименты.

Для каждой идеи строго такой блок:
N. Название (3–6 слов)
Что снять: 1–2 предложения, конкретно — кто, где, что происходит.
Первый кадр: что видно в первую секунду.
Первые слова / текст на экране: дословно.
Формат и длина: видео/кружок/фото, сколько секунд.
Сложность: легко / средне / сложно (и что нужно — локация, люди, реквизит).
Почему зайдёт: одна строка со ссылкой на цифры или посты канала.

Без вступления, сразу идеи. Без markdown."""
