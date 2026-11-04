"""ИИ-продюсер — Telegram-бот, который разбирает каналы и говорит, что постить.

Без внешних зависимостей. Два режима запуска:
- webhook (если задан RENDER_EXTERNAL_URL или WEBHOOK_URL) — для Render / любого хостинга с HTTPS;
- long polling (иначе) — для запуска на своём компьютере.
"""
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai
import ai_media
import storage
import video
from analyzer import analyze, compact_for_ai, stats_text
from tg_parser import ChannelError, fetch_channel, normalize_channel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}/"
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "5"))
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
DATA_DIR = os.getenv("DATA_DIR", "data")
MAX_RIVALS = 3
HOME_CHANGE_DAYS = int(os.getenv("HOME_CHANGE_DAYS", "30"))
PRICE = int(os.getenv("PRICE", "299"))
SUB_DAYS = 30
PAY_DETAILS = os.getenv("PAY_DETAILS", "")  # напр. «+7 900 000-00-00, Т-Банк, Никита Л.»
PAYWALL = os.getenv("PAYWALL", "on").lower() not in ("off", "0", "false", "no")
FREE = {"report": 1, "ideas": 1, "reel": 1, "insta": 1}  # бесплатные ИИ-запросы на всё время
PAYWALLS = {
    "report": "Бесплатный ИИ-разбор ты уже использовал.",
    "ideas": "Бесплатную порцию идей ты уже использовал.",
    "plan": "План на неделю — в подписке.",
    "video": "Разбор видео — в подписке.",
    "compare": "Выводы ИИ по конкурентам — в подписке.",
    "reel": "Бесплатную проверку рилса ты уже использовал.",
    "insta": "Бесплатный разбор Inst / TikTok ты уже использовал.",
}

pool = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "4")))
busy: set[int] = set()
usage: dict[tuple[int, str], int] = {}
class _LastChannel:
    """Последний канал пользователя — хранится в базе и переживает перезапуски."""

    def get(self, uid, default=None):
        return storage.get_channel(uid) or default

    def __getitem__(self, uid):
        return storage.get_channel(uid)

    def __setitem__(self, uid, name):
        storage.set_channel(uid, name)


last_channel = _LastChannel()
cache: dict[str, tuple[float, dict]] = {}
lock = threading.Lock()


# ---------------- Telegram API ----------------

def api(method: str, **params):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode()).get("result")
    except urllib.error.HTTPError as e:
        log.warning("TG %s %s: %s", method, e.code, e.read()[:300])
        return None


def send(chat_id: int, text: str, keyboard=None):
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3900:
            parts.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        parts.append(buf)
    for i, part in enumerate(parts):
        extra = {}
        if keyboard and i == len(parts) - 1:
            extra["reply_markup"] = {"inline_keyboard": keyboard}
        api("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True, **extra)


def typing(chat_id: int):
    api("sendChatAction", chat_id=chat_id, action="typing")


# ---------------- данные пользователей ----------------

def track(user: dict, event: str):
    storage.track(user, event, time.time())


def take_quota(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    k = (uid, date.today().isoformat())
    with lock:
        if usage.get(k, 0) >= DAILY_LIMIT:
            return False
        usage[k] = usage.get(k, 0) + 1
        return True


ai_cache: dict[tuple, tuple[float, str]] = {}
AI_CACHE_TTL = 6 * 3600


def cached_ai(key: tuple, make_prompt, **kw) -> str:
    """Один и тот же разбор в течение 6 часов не оплачиваем повторно."""
    with lock:
        hit = ai_cache.get(key)
    if hit and time.time() - hit[0] < AI_CACHE_TTL:
        log.info("AI cache hit %s", key)
        return hit[1]
    text = ai.ask(make_prompt(), tag=key[0], **kw)
    with lock:
        ai_cache[key] = (time.time(), text)
    return text


def get_channel(name: str) -> dict:
    with lock:
        hit = cache.get(name)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    info = fetch_channel(name)
    a = analyze(info)
    with lock:
        cache[name] = (time.time(), a)
    return a


# ---------------- тексты ----------------

START = """Привет! Я ИИ-продюсер 🎬

Разбираю Telegram-канал за последние полгода и говорю, что постить, чтобы росли просмотры.

Как пользоваться:
• Пришли @канал или ссылку t.me/... — сделаю разбор: что заходит, что нет, 5 идей постов и лучшее время.
• После разбора жми «Сравнить с конкурентами» и пришли ник конкурента — скажу, что тебе снимать, чтобы обогнать его по охватам.
• «💡 Идеи для съёмок» — 10 идей, что снять, с первым кадром и первыми словами. Жми «Ещё» сколько угодно, или /ideas тема — идеи на конкретную тему.
• «🎬 Разобрать видео» — посмотрю кадры твоих лучших и худших роликов и скажу, чем они отличаются.
• /plan — контент-план на неделю для последнего канала.

📸 Инстаграм / TikTok:
• «📲 Проверить рилс» — пришли ролик ДО публикации, скажу, залетит ли и что поправить.
• «📊 Разбор Inst / TikTok» — пришли скриншоты статистики, сделаю разбор и 10 идей роликов.

Первый канал, который пришлёшь, станет твоим: разбор, идеи, план и видео — для него. Сравниваться можно с любыми каналами.
Работаю с публичными каналами. Попробуй — пришли свой канал 👇"""

HELP = START


MENU_INSTA = [
    [{"text": "📲 Проверить рилс", "callback_data": "help_reel"}],
    [{"text": "📊 Разбор Inst / TikTok по скриншотам", "callback_data": "help_insta"}],
]

HELP_REEL = """📲 Проверка рилса до публикации (смотрю со звуком)

Пришли сюда ролик как обычное видео (до 20 МБ — Telegram обычно сам сжимает).
Можно добавить подпись, которую планируешь поставить под рилс.

Через минуту скажу: шансы залететь, цепляет ли первая секунда, как работает звук (трек, бит, речь), что поправить, 3 варианта текста-хука, подпись и что взять на обложку."""

HELP_INSTA = """📊 Разбор Inst / TikTok по скриншотам

Instagram: Профессиональная панель → Статистика.
TikTok: Профиль → ☰ → Инструменты автора → Аналитика.

1. Сделай 3–5 скриншотов: обзор за 30 дней (охват/просмотры), список роликов с просмотрами (лучшие сверху), аудитория.
2. Пришли их сюда одним сообщением (выдели все сразу).

Можно добавить подпись к скринам — например, «это TikTok», «это конкурент» или «хочу больше продаж мерча».

Сделаю разбор: цифры, что заходит, что нет, 10 идей роликов с хуками и когда постить."""


def channel_keyboard(name: str):
    return [
        [{"text": "💡 Идеи для съёмок", "callback_data": f"idea:{name}"}],
        [{"text": "🎬 Разобрать видео", "callback_data": f"vid:{name}"}],
        [{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}],
        [{"text": "⚔️ Сравнить с конкурентами", "callback_data": f"cmp:{name}"}],
    ]


# ---------------- сценарии ----------------

def run_job(chat_id: int, user: dict, fn, *args, quota: bool = True):
    uid = user["id"]
    if uid in busy:
        send(chat_id, "Ещё разбираю прошлый запрос — секунду ⏳")
        return
    if quota and not take_quota(uid):
        send(chat_id, f"На сегодня лимит {DAILY_LIMIT} разборов закончился. Возвращайся завтра 🙌")
        return
    busy.add(uid)

    def wrapped():
        try:
            fn(chat_id, user, *args)
        except ChannelError as e:
            send(chat_id, f"Не получилось открыть канал: {e}.\nПроверь ник — канал должен быть публичным.")
        except Exception:
            log.error("job failed:\n%s", traceback.format_exc())
            send(chat_id, "Что-то пошло не так 😕 Попробуй ещё раз через минуту.")
        finally:
            busy.discard(uid)

    pool.submit(wrapped)


# ---------------- подписка ----------------

def subscribed(uid) -> bool:
    return storage.sub_until(uid) > time.time()


def allow(chat_id, user, kind) -> bool:
    """Можно ли сделать ИИ-запрос `kind`. Бесплатные — списываются."""
    uid = user["id"]
    if not PAYWALL or uid in ADMIN_IDS or subscribed(uid):
        return True
    if storage.free_used(uid, kind) < FREE.get(kind, 0):
        storage.use_free(uid, kind)
        return True
    send(
        chat_id,
        f"{PAYWALLS.get(kind, 'Это — в подписке.')}\n\n"
        f"Подписка {PRICE} ₽ на {SUB_DAYS} дней: разборы, идеи без ограничений, план на неделю, разбор видео и сравнение с конкурентами для твоего канала.",
        [[{"text": f"💳 Оформить подписку — {PRICE} ₽", "callback_data": "pay"}]],
    )
    return False


pending_pay: set[int] = set()


def pay_code(uid) -> str:
    return f"IL{str(uid)[-5:]}"


def show_pay(chat_id, user):
    if not PAY_DETAILS:
        send(chat_id, "Оплата скоро появится 🙌")
        return
    until = storage.sub_until(user["id"])
    extra = ""
    if until > time.time():
        extra = f"Подписка уже активна до {time.strftime('%d.%m', time.localtime(until))} — оплата продлит её ещё на {SUB_DAYS} дней.\n\n"
    send(
        chat_id,
        extra + f"Подписка — {PRICE} ₽ на {SUB_DAYS} дней.\n\n"
        f"1. Переведи {PRICE} ₽ по СБП: {PAY_DETAILS}\n"
        f"2. В комментарии к переводу напиши код: {pay_code(user['id'])}\n"
        "3. Нажми «Я оплатил» и пришли скриншот перевода.\n\n"
        "Подписку включаю вручную после проверки — обычно в течение пары часов.",
        [[{"text": "✅ Я оплатил", "callback_data": "paid"}]],
    )


def got_payment_proof(msg, user):
    """Пользователь прислал скриншот — пересылаем админам с кнопками."""
    pending_pay.discard(user["id"])
    uid = user["id"]
    who = "@" + user["username"] if user.get("username") else user.get("first_name", "")
    info = f"💳 Оплата подписки\nОт: {who} (id {uid})\nКод: {pay_code(uid)}\nСумма: {PRICE} ₽\nКанал: @{home_of(user) or '—'}"
    kb = [[{"text": "✅ Подтвердить", "callback_data": f"ok:{uid}"}, {"text": "❌ Отклонить", "callback_data": f"no:{uid}"}]]
    for admin in ADMIN_IDS:
        api("forwardMessage", chat_id=admin, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        send(admin, info, kb)
    send(msg["chat"]["id"], "Спасибо! Проверяю перевод и включу подписку — пришлю сообщение 🙌")


def admin_decide(chat_id, admin, data):
    if admin["id"] not in ADMIN_IDS:
        return
    ok = data.startswith("ok:")
    uid = int(data.split(":", 1)[1])
    if ok:
        until = max(storage.sub_until(uid), time.time()) + SUB_DAYS * 86400
        storage.set_sub(uid, until)
        d = time.strftime("%d.%m.%Y", time.localtime(until))
        send(uid, f"🔥 Подписка активна до {d}. Всё открыто — жми кнопки или пришли свой канал.", channel_keyboard(home_of({"id": uid}) or "") if home_of({"id": uid}) else None)
        send(chat_id, f"✅ Подписка для {uid} включена до {d}. Не забудь чек в «Мой налог».")
    else:
        send(uid, "Не нашёл твой перевод 😕 Проверь сумму и код в комментарии и пришли скриншот ещё раз — или напиши, разберёмся.",
             [[{"text": "💳 Оплатить", "callback_data": "pay"}]])
        send(chat_id, f"❌ Оплата {uid} отклонена.")


# ---------------- свой канал ----------------

def is_admin(user) -> bool:
    return user["id"] in ADMIN_IDS


def home_of(user) -> str | None:
    if is_admin(user):
        return last_channel.get(user["id"])
    return storage.get_home(user["id"])[0]


def claim_home(user, name) -> str:
    """Возвращает свой канал пользователя; если его ещё нет — назначает `name`."""
    if is_admin(user):
        last_channel[user["id"]] = name
        return name
    home, _ = storage.get_home(user["id"])
    if not home:
        storage.set_home(user["id"], name, time.time())
        return name
    return home


def same(a, b) -> bool:
    return bool(a and b) and a.lower() == b.lower()


def guard_own(chat_id, user, name) -> bool:
    """Идеи, планы и разбор видео — только для своего канала."""
    if is_admin(user):
        return True
    home = home_of(user)
    if not home:
        claim_home(user, name)
        return True
    if same(home, name):
        return True
    send(
        chat_id,
        f"Идеи, планы и разбор видео — только для твоего канала @{home}.\n"
        f"С @{name} можно сравниться: /compare @{name}\nСменить свой канал: /mychannel",
    )
    return False


def change_home(chat_id, user, name):
    home, ts = storage.get_home(user["id"])
    if same(home, name):
        send(chat_id, f"@{name} и так твой канал 👍", channel_keyboard(name))
        return
    wait = ts + HOME_CHANGE_DAYS * 86400 - time.time()
    if home and wait > 0 and not is_admin(user):
        when = time.strftime("%d.%m", time.localtime(ts + HOME_CHANGE_DAYS * 86400))
        send(chat_id, f"Свой канал можно менять раз в {HOME_CHANGE_DAYS} дней. Сейчас это @{home}, сменить можно с {when}.")
        return
    storage.set_home(user["id"], name, time.time())
    send(chat_id, f"Готово: теперь твой канал — @{name}.")
    run_job(chat_id, user, job_report, name)


def job_foreign(chat_id, user, name, home):
    """Чужой канал: только цифры (без ИИ) и предложение сравниться."""
    send(chat_id, f"Смотрю @{name}… ⏳")
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, f"У @{name} не нашёл постов с просмотрами.")
        return
    track(user, "foreign")
    send(
        chat_id,
        stats_text(a) + f"\n\nЭто не твой канал, поэтому только цифры. Разбор, идеи и план — для @{home}.\n"
        "Хочешь понять, что у них забрать себе, — жми «Сравнить».",
        [
            [{"text": f"⚔️ Сравнить с @{home}", "callback_data": f"cmpw:{name}"}],
            [{"text": "🔄 Сделать это моим каналом", "callback_data": f"home:{name}"}],
        ],
    )


def job_report(chat_id, user, name):
    send(chat_id, f"Собираю посты @{name} за последние полгода и считаю… до минуты ⏳")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, f"У @{name} не нашёл постов с просмотрами — нечего анализировать.")
        return
    track(user, "report")
    if not ai.enabled():
        send(chat_id, stats_text(a) + "\n\n(ИИ-разбор пока выключен — нет ключа)", channel_keyboard(name))
        return
    send(chat_id, stats_text(a))
    if not allow(chat_id, user, "report"):
        return
    typing(chat_id)
    text = cached_ai(("report", name.lower()), lambda: ai.report_prompt(compact_for_ai(a)))
    send(chat_id, text, channel_keyboard(name))


def job_compare(chat_id, user, mine, rivals):
    if not is_admin(user):
        home = claim_home(user, mine)
        if not same(home, mine):
            rivals = [r for r in [mine] + list(rivals) if not same(r, home)][:MAX_RIVALS]
            mine = home
        if not rivals:
            send(chat_id, "Пришли канал конкурента, с которым сравнить.")
            return
    send(chat_id, f"Сравниваю @{mine} с {', '.join('@' + r for r in rivals)}… это до минуты ⏳")
    typing(chat_id)
    a_mine = get_channel(mine)
    a_riv = []
    failed = []
    for r in rivals:
        try:
            ar = get_channel(r)
            if ar["ok"]:
                a_riv.append(ar)
            else:
                failed.append(r)
        except ChannelError:
            failed.append(r)
    if failed:
        send(chat_id, "Не смог открыть: " + ", ".join("@" + f for f in failed))
    if not a_mine["ok"] or not a_riv:
        send(chat_id, "Не хватает данных для сравнения.")
        return
    track(user, "compare")
    table = ["Коротко в цифрах (медиана просмотров · постов в неделю):"]
    for x in [a_mine] + a_riv:
        reach = f" · охват {x['reach_pct']}%" if x["reach_pct"] is not None else ""
        table.append(f"@{x['username']}: {x['median_views']} · {x['posts_per_week']}/нед{reach}")
    send(chat_id, "\n".join(table))
    if not ai.enabled() or not allow(chat_id, user, "compare"):
        return
    typing(chat_id)
    key = ("compare", mine.lower()) + tuple(sorted(x["username"].lower() for x in a_riv))
    text = cached_ai(key, lambda: ai.compare_prompt(compact_for_ai(a_mine), [compact_for_ai(x) for x in a_riv]), max_tokens=5000)
    send(chat_id, text, [[{"text": "📅 План на неделю", "callback_data": f"plan:{mine}"}]])


def job_videos(chat_id, user, name):
    if not guard_own(chat_id, user, name) or not allow(chat_id, user, "video"):
        return
    if not ai.enabled():
        send(chat_id, "Разбор видео делает ИИ, а он пока не подключён.")
        return
    send(chat_id, f"Смотрю ролики @{name}: скачиваю лучшие и худшие, вырезаю кадры… это 1–2 минуты 🎬")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"] or not a.get("video_top"):
        send(chat_id, "Не нашёл у канала достаточно видео или кружков для сравнения (нужно хотя бы 2).")
        return
    parts = [ai.video_intro(compact_for_ai(a))]
    got = 0
    for title, group in (("СИЛЬНЫЕ РОЛИКИ", a["video_top"]), ("СЛАБЫЕ РОЛИКИ", a["video_worst"])):
        parts.append(f"\n===== {title} =====")
        for p in group:
            typing(chat_id)
            frames, dur = video.frames_for(p)
            if not frames:
                continue
            got += 1
            length = f", длина {round(dur)} с" if dur else ""
            parts.append(
                f"\nРолик t.me/{name}/{p['id']} ({p['type']}{length}): {p['views']} просмотров, ×{p['x_median']} к обычному посту. "
                f"Подпись: {p['text'][:200] or '[без подписи]'}"
            )
            for label, img in frames:
                parts.append(f"Кадр: {label}")
                parts.append(img)
    if got < 2:
        send(chat_id, "Не получилось скачать ролики — Telegram не отдаёт их в открытый доступ. Попробуй другой канал.")
        return
    parts.append(ai.video_outro())
    track(user, "video")
    typing(chat_id)
    send(chat_id, cached_ai(("video", name.lower()), lambda: parts, heavy=True), [[{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}]])


past_ideas: dict[tuple[int, str], list[str]] = {}


def job_ideas(chat_id, user, name, focus=None):
    if not guard_own(chat_id, user, name) or not allow(chat_id, user, "ideas"):
        return
    if not ai.enabled():
        send(chat_id, "Идеи придумывает ИИ, а он пока не подключён.")
        return
    key = (user["id"], name.lower(), (focus or "").lower())
    prev = past_ideas.get(key, [])
    send(chat_id, ("Придумываю ещё 10 идей" if prev else "Придумываю 10 идей для съёмок") + f" для @{name}" + (f" на тему «{focus}»" if focus else "") + "… ⏳")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, "Не нашёл постов с просмотрами.")
        return
    previous = "\n".join(prev)[-6000:] or None
    text = ai.ask(ai.ideas_prompt(compact_for_ai(a), focus, previous), tag="ideas")
    titles = [ln.strip() for ln in text.splitlines() if re.match(r"^\d{1,2}[.)]\s", ln.strip())]
    past_ideas[key] = prev + (titles or [text[:1500]])
    track(user, "ideas")
    cb = f"idea:{name}" + (f"|{focus}" if focus else "")
    if len(cb.encode()) > 64:
        cb = f"idea:{name}"
    send(
        chat_id,
        text + "\n\nХочешь идеи на конкретную тему — напиши, например: /ideas бренд одежды",
        [[{"text": "🔁 Ещё идеи", "callback_data": cb}], [{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}]],
    )


pending_plan: dict[int, str] = {}


def ask_week(chat_id, user, name):
    if not guard_own(chat_id, user, name):
        return
    pending_plan[user["id"]] = name
    send(
        chat_id,
        "Что у тебя происходит на этой неделе? Напиши коротко — например: «класс в субботу 18:00, 1500₽» или «дроп футболок в пятницу».\n\n"
        "Если ничего продавать и анонсировать не нужно — жми кнопку, сделаю план из обычного контента.",
        [[{"text": "Ничего особенного", "callback_data": f"plan0:{name}"}]],
    )


def job_plan(chat_id, user, name, week=None):
    if not guard_own(chat_id, user, name) or not allow(chat_id, user, "plan"):
        return
    if not ai.enabled():
        send(chat_id, "План на неделю делает ИИ, а он пока не подключён.")
        return
    send(chat_id, f"Составляю план для @{name}… ⏳")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, "Не нашёл постов с просмотрами.")
        return
    track(user, "plan")
    send(chat_id, cached_ai(("plan", name.lower(), (week or "").lower()), lambda: ai.plan_prompt(compact_for_ai(a), week)))


# ---------------- Инстаграм / рилсы ----------------

TG_FILE_LIMIT = 20 * 1024 * 1024


def tg_download(file_id: str) -> bytes:
    f = api("getFile", file_id=file_id)
    if not f or not f.get("file_path"):
        raise RuntimeError("telegram не отдал файл")
    url = f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}"
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def job_reel(chat_id, user, file_id, caption):
    if not ai.enabled():
        send(chat_id, "Проверку рилса делает ИИ, а он пока не подключён.")
        return
    if not allow(chat_id, user, "reel"):
        return
    send(chat_id, "Смотрю рилс со звуком: первая секунда, музыка, речь, монтаж… около минуты 🎬")
    typing(chat_id)
    data = tg_download(file_id)
    frames, meta = video.reel_frames(data)
    if not frames:
        send(chat_id, "Не получилось прочитать видео 😕 Попробуй прислать его ещё раз как обычное видео.")
        return
    home = home_of(user)
    context = None
    if home:
        try:
            context = compact_for_ai(get_channel(home))[:1500]
        except Exception:
            context = None
    track(user, "reel")
    kb = [[{"text": "📲 Проверить ещё рилс", "callback_data": "help_reel"}]]

    # 1) Gemini смотрит ролик целиком — картинка + звук
    if ai_media.gemini_enabled():
        try:
            how = ("Ролик прикреплён целиком — ты ВИДИШЬ картинку и СЛЫШИШЬ звук (музыку, голос). "
                   "Указывай моменты по секундам.")
            prompt = ai.reel_intro(meta, caption, context, how=how) + "\n\n" + ai.reel_outro(ai_media.SOUND_BLOCK)
            # если прокси откажет из-за оценки стоимости — пробуем ещё легче
            for opts, mt in (({}, 4000), ({"height": 240, "fps": 2, "crf": 38, "abr": "32k", "max_sec": 60}, 3000)):
                small = video.compress_for_ai(data, **opts)
                log.info("gemini reel payload %d KB (%s)", len(small) // 1024, opts or "default")
                try:
                    typing(chat_id)
                    send(chat_id, ai_media.ask_gemini_video(small, prompt, max_tokens=mt), kb)
                    return
                except RuntimeError as e:
                    if "402" not in str(e):
                        raise
                    log.warning("gemini 402 at %d KB, trying lighter", len(small) // 1024)
        except Exception as e:
            log.warning("gemini video failed, fallback to frames: %s", e)

    # 2) Запасной путь: кадры + данные о звуке + расшифровка речи
    sound_lines = []
    if meta.get("has_audio"):
        try:
            facts = video.audio_facts(data)
            sound_lines.append(f"Звук начинается с {facts.get('sound_starts_at', 0)} с.")
            if facts.get("silences"):
                sound_lines.append("Паузы (тишина): " + ", ".join(f"{a}–{b} с" for a, b in facts["silences"]))
            if facts.get("mp3"):
                text = ai_media.transcribe(facts["mp3"])
                sound_lines.append("Речь в ролике: " + (text if text else "речи не распознано (вероятно, только музыка)"))
        except Exception as e:
            log.warning("audio facts failed: %s", e)
    else:
        sound_lines.append("В ролике НЕТ звуковой дорожки.")
    how = ("Дальше — кадры ролика с отметками времени. Музыку ты не слышишь, но вот данные о звуке:\n"
           + "\n".join(sound_lines))
    parts = [ai.reel_intro(meta, caption, context, how=how)]
    for i, (label, img) in enumerate(frames, 1):
        parts.append(f"Кадр {i}: {label}")
        parts.append(img)
    parts.append(ai.reel_outro(ai_media.SOUND_BLOCK.replace("что звучит (музыка / голос / тишина)", "по данным о звуке и речи")))
    typing(chat_id)
    send(chat_id, ai.ask(parts, heavy=True, tag="reel"), kb)


def job_insta(chat_id, user, items, note):
    if not ai.enabled():
        send(chat_id, "Разбор делает ИИ, а он пока не подключён.")
        return
    if not allow(chat_id, user, "insta"):
        return
    send(chat_id, f"Читаю скриншоты ({len(items)}) и разбираю статистику… около минуты 📊")
    typing(chat_id)
    parts = [ai.insta_intro(note)]
    for i, fid in enumerate(items[:10], 1):
        parts.append(f"Скриншот {i}:")
        parts.append(video.to_jpeg(tg_download(fid)))
    parts.append(ai.insta_outro())
    track(user, "insta")
    typing(chat_id)
    send(chat_id, ai.ask(parts, heavy=True, tag="insta"),
         [[{"text": "📲 Проверить рилс", "callback_data": "help_reel"}]])


albums: dict[str, dict] = {}


def collect_photo(msg, user, file_id):
    """Скриншоты могут прийти альбомом — собираем все части и разбираем вместе."""
    chat_id = msg["chat"]["id"]
    note = (msg.get("caption") or "").strip() or None
    gid = msg.get("media_group_id")
    if not gid:
        run_job(chat_id, user, job_insta, [file_id], note)
        return
    with lock:
        al = albums.setdefault(gid, {"items": [], "note": None, "timer": None})
        al["items"].append(file_id)
        al["note"] = al["note"] or note
        if al["timer"]:
            al["timer"].cancel()

        def fire():
            with lock:
                done = albums.pop(gid, None)
            if done:
                run_job(chat_id, user, job_insta, done["items"], done["note"])

        al["timer"] = threading.Timer(2.5, fire)
        al["timer"].start()


def handle_media(msg, user) -> bool:
    """Видео → проверка рилса, картинки → разбор Инсты. True, если сообщение обработано."""
    chat_id = msg["chat"]["id"]
    vid = msg.get("video") or msg.get("video_note") or msg.get("animation")
    doc = msg.get("document") or {}
    mime = doc.get("mime_type", "")
    if not vid and mime.startswith("video/"):
        vid = doc
    if vid:
        if (vid.get("file_size") or 0) > TG_FILE_LIMIT:
            send(chat_id, "Видео больше 20 МБ — Telegram не даёт ботам скачивать такие. Пришли его как обычное видео (не файлом) — Telegram сожмёт, или укороти/сожми ролик.")
            return True
        run_job(chat_id, user, job_reel, vid["file_id"], (msg.get("caption") or "").strip() or None)
        return True
    if msg.get("photo"):
        collect_photo(msg, user, msg["photo"][-1]["file_id"])
        return True
    if mime.startswith("image/"):
        if (doc.get("file_size") or 0) > TG_FILE_LIMIT:
            send(chat_id, "Картинка слишком большая — пришли её как фото.")
            return True
        collect_photo(msg, user, doc["file_id"])
        return True
    return False


# ---------------- роутинг ----------------

def extract_channels(text: str) -> list[str]:
    found = []
    for tok in re.findall(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?[A-Za-z0-9_]+|@[A-Za-z0-9_]+", text):
        n = normalize_channel(tok)
        if n and n.lower() not in [f.lower() for f in found]:
            found.append(n)
    if not found:
        n = normalize_channel(text)
        if n:
            found.append(n)
    return found


pending_compare: dict[int, str] = {}


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {"id": chat_id}
    text = (msg.get("text") or "").strip()
    if user["id"] in pending_pay and (msg.get("photo") or msg.get("document") or (text and not text.startswith("/"))):
        got_payment_proof(msg, user)
        return
    if not text and handle_media(msg, user):
        return
    if not text:
        send(chat_id, "Пришли @канал, ссылку t.me/..., рилс (видео) или скриншоты статистики Inst / TikTok.")
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        track(user, "start")
        mine = home_of(user)
        if mine:
            send(chat_id, START + f"\n\nТвой канал: @{mine} — можно сразу жать кнопки 👇", channel_keyboard(mine) + MENU_INSTA)
        else:
            send(chat_id, START, MENU_INSTA)
        return
    if cmd == "/help":
        send(chat_id, HELP)
        return
    if cmd == "/reel":
        send(chat_id, HELP_REEL)
        return
    if cmd == "/insta":
        send(chat_id, HELP_INSTA)
        return
    if cmd in ("/sub", "/pay"):
        until = storage.sub_until(user["id"])
        if until > time.time():
            send(chat_id, f"Подписка активна до {time.strftime('%d.%m.%Y', time.localtime(until))} ✅",
                 [[{"text": "Продлить", "callback_data": "pay"}]])
        else:
            show_pay(chat_id, user)
        return
    if cmd == "/mychannel":
        home, ts = storage.get_home(user["id"])
        if not home:
            send(chat_id, "Своего канала пока нет. Пришли @ник канала — первый присланный станет твоим.")
            return
        nxt = ts + HOME_CHANGE_DAYS * 86400
        when = "уже сейчас" if nxt <= time.time() else "с " + time.strftime("%d.%m", time.localtime(nxt))
        send(
            chat_id,
            f"Твой канал: @{home}\nРазбор, идеи, план и видео работают для него. Сравниваться можно с кем угодно.\n\n"
            f"Сменить свой канал можно раз в {HOME_CHANGE_DAYS} дней ({when}): пришли новый канал и нажми «Сделать это моим каналом».",
            channel_keyboard(home),
        )
        return
    if cmd == "/stats" and user["id"] in ADMIN_IDS:
        st = storage.all_stats()
        acts = ("report", "compare", "plan", "video", "ideas", "reel", "insta")
        total = sum(int(u.get(k, 0)) for u in st.values() for k in acts)
        day_ago = time.time() - 86400
        active = sum(1 for u in st.values() if float(u.get("last", 0)) > day_ago)
        new = sum(1 for u in st.values() if float(u.get("first", 0)) > day_ago)
        top = sorted(st.items(), key=lambda kv: -sum(int(kv[1].get(k, 0)) for k in acts))[:10]
        lines = [f"Пользователей всего: {len(st)}", f"Новых за сутки: {new}", f"Активных за сутки: {active}", f"Запросов всего: {total}", ""]
        for uid, u in top:
            n = sum(int(u.get(k, 0)) for k in acts)
            lines.append(f"{'@' + u['name'] if u.get('name') else uid}: {n}")
        send(chat_id, "\n".join(lines))
        return

    chans = extract_channels(text[len(cmd):] if cmd else text)

    if cmd == "/ideas":
        rest = text[len(text.split()[0]):]
        focus = re.sub(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?[A-Za-z0-9_]+|@[A-Za-z0-9_]+", "", rest).strip() or None
        name = next((c for c in chans if "@" + c in rest or "t.me/" + c in rest), None) or home_of(user)
        if not name:
            send(chat_id, "Сначала пришли свой канал, или так: /ideas @канал тема")
            return
        if guard_own(chat_id, user, name):
            run_job(chat_id, user, job_ideas, name, focus[:60] if focus else None)
        return

    if cmd == "/plan":
        name = chans[0] if chans else home_of(user)
        if not name:
            send(chat_id, "Сначала пришли канал, или так: /plan @канал")
            return
        ask_week(chat_id, user, name)
        return

    if not cmd and user["id"] in pending_plan and not chans:
        run_job(chat_id, user, job_plan, pending_plan.pop(user["id"]), text[:400])
        return

    if not cmd and chans and user["id"] in pending_compare:
        mine = pending_compare.pop(user["id"])
        rivals = [c for c in chans if c.lower() != mine.lower()][:MAX_RIVALS]
        if rivals:
            run_job(chat_id, user, job_compare, mine, rivals)
            return

    if cmd == "/compare" and len(chans) == 1 and home_of(user) and not same(home_of(user), chans[0]):
        chans = [home_of(user)] + chans

    if cmd == "/compare" or (not cmd and len(chans) >= 2):
        if len(chans) < 2:
            send(chat_id, "Формат: /compare @мой_канал @конкурент1 @конкурент2\n(первым — твой, до 3 конкурентов). Если уже разбирал свой канал — можно просто /compare @конкурент")
            return
        run_job(chat_id, user, job_compare, chans[0], chans[1 : 1 + MAX_RIVALS])
        return

    if cmd:
        send(chat_id, "Не знаю такую команду. /help — что я умею.")
        return
    if not chans:
        send(chat_id, "Не вижу канала. Пришли @ник или ссылку t.me/...")
        return
    name = chans[0]
    home = claim_home(user, name)
    if same(home, name):
        run_job(chat_id, user, job_report, name)
    else:
        run_job(chat_id, user, job_foreign, name, home, quota=False)


def handle_callback(cb: dict):
    api("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    user = cb["from"]
    if data.startswith("idea:"):
        rest = data[5:]
        name, _, focus = rest.partition("|")
        if guard_own(chat_id, user, name):
            run_job(chat_id, user, job_ideas, name, focus or None)
    elif data.startswith("vid:"):
        if guard_own(chat_id, user, data[4:]):
            run_job(chat_id, user, job_videos, data[4:])
    elif data.startswith("plan0:"):
        pending_plan.pop(user["id"], None)
        run_job(chat_id, user, job_plan, data[6:], None)
    elif data.startswith("plan:"):
        ask_week(chat_id, user, data[5:])
    elif data == "help_reel":
        send(chat_id, HELP_REEL)
    elif data == "help_insta":
        send(chat_id, HELP_INSTA)
    elif data == "pay":
        show_pay(chat_id, user)
    elif data == "paid":
        pending_pay.add(user["id"])
        send(chat_id, "Пришли скриншот перевода одним сообщением 📸")
    elif data.startswith(("ok:", "no:")):
        admin_decide(chat_id, user, data)
    elif data.startswith("cmpw:"):
        home = home_of(user)
        if home:
            run_job(chat_id, user, job_compare, home, [data[5:]])
    elif data.startswith("home:"):
        change_home(chat_id, user, data[5:])
    elif data.startswith("cmp:"):
        name = data[4:]
        pending_compare[user["id"]] = name
        send(chat_id, f"Пришли ник конкурента (можно до трёх через пробел), например:\n@konkurent1 @konkurent2\n\nСравню с @{name} и скажу, что тебе снимать, чтобы поднять охваты.")


def handle_update(upd: dict):
    try:
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])
    except Exception:
        log.error("update failed:\n%s", traceback.format_exc())


# ---------------- запуск ----------------

def set_commands():
    api(
        "setMyCommands",
        commands=[
            {"command": "start", "description": "Как пользоваться"},
            {"command": "compare", "description": "Сравнить с конкурентами"},
            {"command": "ideas", "description": "Идеи, что снять (можно с темой)"},
            {"command": "plan", "description": "Контент-план на неделю"},
            {"command": "reel", "description": "Проверить рилс до публикации"},
            {"command": "insta", "description": "Разбор Inst / TikTok по скриншотам"},
            {"command": "mychannel", "description": "Мой канал"},
            {"command": "sub", "description": "Подписка"},
        ],
    )


def run_polling():
    api("deleteWebhook", drop_pending_updates=False)
    offset = 0
    log.info("polling started")
    while True:
        updates = api("getUpdates", offset=offset, timeout=50) or []
        for u in updates:
            offset = u["update_id"] + 1
            pool.submit(handle_update, u)
        if not updates:
            time.sleep(0.5)


def run_webhook(base_url: str, port: int):
    # стабильные путь и секрет: после «сна» бесплатного сервера старые запросы Telegram не теряются
    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    secret = os.getenv("WEBHOOK_SECRET") or digest[:32]
    path = "/tg/" + digest[32:48]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            if self.path != path or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
                self.send_response(403)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()
            try:
                pool.submit(handle_update, json.loads(body))
            except Exception:
                pass

        def log_message(self, *a):
            pass

    def keepalive():
        # бесплатный Render усыпляет сервис после 15 мин без входящих запросов — пингуем себя каждые 10 мин
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=30).read()
            except Exception as e:
                log.warning("keepalive failed: %s", e)
            api("setWebhook", url=base_url.rstrip("/") + path, secret_token=secret, drop_pending_updates=False)

    threading.Thread(target=keepalive, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), H)
    api("setWebhook", url=base_url.rstrip("/") + path, secret_token=secret, drop_pending_updates=False)
    log.info("webhook started on :%s", port)
    server.serve_forever()


def main():
    if not TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN")
    me = api("getMe")
    log.info("bot @%s, AI %s", (me or {}).get("username"), "on" if ai.enabled() else "off")
    set_commands()
    storage.db()
    video.warmup()
    threading.Thread(target=ai_media.selftest, args=(video.test_clip,), daemon=True).start()
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        run_webhook(base, int(os.getenv("PORT", "10000")))
    else:
        run_polling()


if __name__ == "__main__":
    main()
