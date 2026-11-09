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
import playbook
import storage
import video
from analyzer import analyze, compact_for_ai, samples_for_ai, stats_text
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
PRICE = int(os.getenv("PRICE", "279"))
SUB_DAYS = 30
YEAR_MONTHS_PAID = 10  # год = цена 10 месяцев
PERIODS = {"m": (SUB_DAYS, 1, "месяц"), "y": (365, YEAR_MONTHS_PAID, "год")}
BOT_USERNAME = os.getenv("BOT_USERNAME", "Improvelab_bot")
PAY_DETAILS = os.getenv("PAY_DETAILS", "")  # напр. «+7 900 000-00-00, Т-Банк, Никита Л.»
# месячные лимиты подписки: тяжёлые функции отдельно, остальное — общий пул "text"
QUOTAS = json.loads(os.getenv("QUOTAS", '{"plan": 5, "text": 15, "reel": 10, "video": 1, "reels": 1, "insta": 3}'))
QUOTA_BUCKET = {"report": "text", "ideas": "text", "compare": "text"}  # план — отдельный лимит "plan"
QUOTA_NAMES = {"plan": "планов на неделю", "reels": "разборов Inst/TikTok по роликам", "video": "разборов видео канала",
               "insta": "разборов по скриншотам", "reel": "проверок рилсов", "text": "разборов, идей и сравнений"}
PAYWALL = os.getenv("PAYWALL", "on").lower() not in ("off", "0", "false", "no")
# бесплатный пробник: каждая функция по 1 разу (решение 2026-11-09 — новый продукт, пусть пробуют всё)
FREE = {"report": 1, "ideas": 1, "reel": 1, "video": 1, "plan": 1, "compare": 1, "insta": 1, "reels": 1}
PAYWALLS = {
    "report": "Бесплатный ИИ-разбор ты уже использовал.",
    "ideas": "Бесплатную порцию идей ты уже использовал.",
    "plan": "Бесплатный план на неделю ты уже использовал.",
    "video": "Бесплатный разбор видео канала ты уже использовал.",
    "compare": "Бесплатное сравнение с конкурентом ты уже использовал.",
    "reel": "Бесплатную проверку рилса ты уже использовал.",
    "insta": "Бесплатный разбор по скриншотам ты уже использовал.",
    "reels": "Бесплатный разбор Inst / TikTok по роликам ты уже использовал.",
}

pool = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "6")))       # долгие задачи (ИИ, видео)
updates_pool = ThreadPoolExecutor(max_workers=8)                              # входящие сообщения — всегда быстро
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
    except Exception as e:
        log.warning("TG %s failed: %s", method, e)
        return None


def send(chat_id: int, text: str, keyboard=None):
    parts, buf = [], ""
    lines = []
    for line in text.split("\n"):
        while len(line) > 3800:
            lines.append(line[:3800])
            line = line[3800:]
        lines.append(line)
    for line in lines:
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

def msk_now():
    return time.gmtime(time.time() + 3 * 3600)


def today() -> str:
    return time.strftime("%Y-%m-%d", msk_now())


def notify_admins(text, kb=None):
    for admin in ADMIN_IDS:
        try:
            send(admin, text, kb)
        except Exception:
            pass


def who(user) -> str:
    return "@" + user["username"] if user.get("username") else (user.get("first_name") or str(user["id"]))


def track(user: dict, event: str) -> bool:
    is_new = storage.track(user, event, time.time())
    storage.day_incr(today(), event)
    if is_new:
        storage.day_incr(today(), "new_users")
        if user["id"] not in ADMIN_IDS:
            notify_admins(f"🆕 Новый пользователь: {who(user)} (id {user['id']})")
    return is_new


# ---------- обратная связь 👍/👎 ----------
FB_NAMES = {"research": "анализ ниши", "report": "разбор канала", "compare": "сравнение", "video": "видео канала", "ideas": "идеи", "plan": "план",
            "reel": "проверка рилса", "insta": "скриншоты", "reels": "разбор по роликам"}
pending_fb: dict[int, str] = {}
voted: set = set()


def fb(kind: str, kb=None):
    """Клавиатура ответа ИИ + строка оценки."""
    return (kb or []) + [[{"text": "👍 Полезно", "callback_data": f"fb:1:{kind}"},
                          {"text": "👎 Не то", "callback_data": f"fb:0:{kind}"}]]


def on_feedback(cb, user, data):
    _, up, kind = data.split(":", 2)
    mid = (cb.get("message") or {}).get("message_id")
    if (user["id"], mid) in voted:
        return
    voted.add((user["id"], mid))
    chat_id = cb["message"]["chat"]["id"]
    storage.fb_add(kind, up == "1")
    storage.day_incr(today(), "fb_up" if up == "1" else "fb_down")
    if up == "1":
        send(chat_id, "Спасибо! 🙌")
        notify_admins(f"👍 {who(user)} оценил: {FB_NAMES.get(kind, kind)}")
    else:
        pending_fb[user["id"]] = kind
        send(chat_id, "Что было не так? Напиши одним сообщением — это сильно поможет сделать бота лучше 🙏")


# ---------- ниша автора ----------
NICHES = [("dance", "💃 Танцы"), ("sport", "🏋️ Спорт / фитнес"), ("beauty", "💅 Бьюти"), ("brand", "🛍 Бренд / магазин"),
          ("expert", "🎓 Эксперт / обучение"), ("blog", "📸 Блог / лайфстайл"), ("music", "🎵 Музыка / творчество"),
          ("other", "✍️ Другое — напишу")]
pending_niche: set = set()


def ask_niche(chat_id):
    rows = [[{"text": t, "callback_data": f"niche:{k}"}] for k, t in NICHES]
    send(chat_id, "О чём ты снимаешь? Буду учитывать это во всех советах 👇", rows)


def niche_line(user) -> str:
    n = storage.get_niche(user["id"])
    out = f"Ниша автора (со слов автора): {n}. Учитывай её во всех советах.\n\n" if n else ""
    liked = storage.get_liked(user["id"])[:4]
    if liked:
        out += ("ИДЕИ, КОТОРЫЕ АВТОРУ ПОНРАВИЛИСЬ (предлагай больше такого типа — по уровню конкретики, теме и подаче, "
                "но не повторяй их):\n" + "\n---\n".join(liked) + "\n\n")
    return out


def idea_blocks(text: str) -> dict:
    """{номер: текст идеи} из ответа с идеями."""
    blocks, cur = {}, None
    for ln in text.splitlines():
        m = re.match(r"^\s*(\d{1,2})[.)]\s", ln)
        if m:
            cur = int(m.group(1))
            blocks[cur] = ln.strip()
        elif cur is not None and ln.strip():
            blocks[cur] += "\n" + ln.strip()
    return blocks


def like_row(user, text: str):
    """Сохраняет идеи и даёт кнопки ❤️ 1…N, чтобы отметить понравившиеся."""
    nums = sorted(idea_blocks(text))[:10]
    if not nums:
        return []
    storage.set_last_ideas(user["id"], text)
    btns = [{"text": f"❤️ {n}", "callback_data": f"like:{n}"} for n in nums]
    return [btns[:5], btns[5:10]] if len(btns) > 5 else [btns]


def on_like(chat_id, user, n: int):
    idea = idea_blocks(storage.get_last_ideas(user["id"])).get(n)
    if not idea:
        send(chat_id, "Не нашёл эту идею — пришли ещё раз «Идеи», и отметь заново 🙏")
        return
    storage.add_liked(user["id"], idea)
    storage.day_incr(today(), "likes")
    send(chat_id, f"Запомнил идею №{n} ❤️ Буду предлагать больше такого — по теме и подаче.")
    notify_admins(f"❤️ {who(user)} отметил идею: {idea.splitlines()[0][:150]}")


INSIGHT_NAMES = {"compare": "Сравнение с конкурентами", "video": "Разбор видео канала", "reels": "Разбор роликов Inst/TikTok"}


def insights_block(user) -> tuple[str, list]:
    """Выводы прошлых разборов (до 2 недель) для плана и идей."""
    items = storage.get_insights(user["id"], time.time())
    if not items:
        return "", []
    lines = ["\nЧТО УЖЕ ИЗВЕСТНО ИЗ ПРОШЛЫХ РАЗБОРОВ ЭТОГО АВТОРА — опирайся на эти выводы (рецепты, что забрать у конкурентов, "
             "чего не делать), используй их в плане/идеях и не противоречь им:"]
    used = []
    for kind, ts, text in items:
        d = time.strftime("%d.%m", time.localtime(ts))
        lines.append(f"[{INSIGHT_NAMES[kind]}, {d}]:\n{text[:1800]}")
        used.append(f"{INSIGHT_NAMES[kind].lower()} от {d}")
    return "\n\n".join(lines) + "\n", used


def remember(user, kind, text):
    try:
        storage.save_insight(user["id"], kind, text, time.time())
    except Exception as e:
        log.warning("save insight failed: %s", e)


def daily_summary_loop():
    """Каждый вечер в 21:00 МСК — сводка админу."""
    while True:
        try:
            t = msk_now()
            day = time.strftime("%Y-%m-%d", t)
            if t.tm_hour >= 21 and storage.kv_get("summary_sent") != day:
                storage.kv_set("summary_sent", day)
                d = storage.day_stats(day)
                total = len(storage.all_stats())
                acts = [(k, v) for k, v in d.items() if k in FB_NAMES]
                lines = [f"📊 Сводка за {time.strftime('%d.%m', t)}",
                         f"Новых пользователей: {d.get('new_users', 0)} (всего {total})",
                         f"Запусков /start: {d.get('start', 0)}"]
                if acts:
                    lines.append("Запросы: " + ", ".join(f"{FB_NAMES[k]} — {v}" for k, v in sorted(acts, key=lambda x: -x[1])))
                else:
                    lines.append("Запросов к ИИ сегодня не было.")
                lines.append(f"Оценки: 👍 {d.get('fb_up', 0)} / 👎 {d.get('fb_down', 0)} · ❤️ идей: {d.get('likes', 0)}")
                notify_admins("\n".join(lines))
        except Exception as e:
            log.warning("summary failed: %s", e)
        time.sleep(300)


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


def first(user, task, key) -> bool:
    """Первый раз за неделю — лучшая модель (Sonnet), повторы — дешёвая."""
    return storage.first_time(user["id"], task, key, time.time())


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
• /niche — о чём ты снимаешь (учитываю во всех советах).
• После разбора жми «Сравнить с конкурентами» и пришли ник конкурента — скажу, что тебе снимать, чтобы обогнать его по охватам.
• «💡 Идеи для съёмок» — 5 сильных идей под твой канал, с первым кадром и первыми словами. Жми «Ещё» сколько угодно, или /ideas тема — идеи на конкретную тему.
• «🎬 Разобрать видео» — посмотрю со звуком твои лучшие и худшие ролики и скажу, чем они отличаются.
• /plan — контент-план на неделю для последнего канала.

📸 Инстаграм / TikTok:
• «📲 Проверить рилс» — пришли ролик ДО публикации, скажу, залетит ли и что поправить.
• «🎞 Разбор Inst / TikTok по роликам» — пришли хиты и слабые ролики, скажу, в чём разница, и дам рецепт + 10 идей.
• «📊 По скриншотам» — быстрый разбор статистики по скринам.

Первый канал, который пришлёшь, станет твоим: разбор, идеи, план и видео — для него. Сравниваться можно с любыми каналами.
🎁 /invite — приведи друга и получи месяц бесплатно.
Работаю с публичными каналами. Попробуй — пришли свой канал 👇"""

HELP = START

START_SHORT = """Привет! Я ИИ-продюсер 🎬
Подскажу, что снимать и постить, чтобы росли просмотры — в Telegram, Instagram и TikTok.

С чего начнём? 👇"""

MENU_START = [
    [{"text": "📢 Разобрать мой Telegram-канал", "callback_data": "tg_help"}],
    [{"text": "🔍 Что залетает в моей нише", "callback_data": "nr"}],
    [{"text": "📲 Проверить рилс до публикации", "callback_data": "help_reel"}],
    [{"text": "🎞 Разбор Inst / TikTok по роликам", "callback_data": "vs_start"}],
    [{"text": "📊 По скриншотам статистики", "callback_data": "help_insta"}],
    [{"text": "ℹ️ Все возможности", "callback_data": "help_all"}],
]


MENU_INSTA = [
    [{"text": "🔍 Что залетает в моей нише", "callback_data": "nr"}],
    [{"text": "📲 Проверить рилс", "callback_data": "help_reel"}],
    [{"text": "🎞 Разбор Inst / TikTok по роликам", "callback_data": "vs_start"}],
    [{"text": "📊 По скриншотам статистики", "callback_data": "help_insta"}],
]

HELP_VS = """🎞 Разбор Inst / TikTok по роликам

Пойму, чем твои хиты отличаются от слабых роликов, и дам рецепт + 10 идей.

1. Пришли сюда 2–6 своих роликов (лучшие и слабые) — как обычное видео, можно по одному или пачкой.
   Свои рилсы/тиктоки можно сохранить на телефон прямо из приложения.
2. В подписи к каждому напиши просмотры: «12к», «800», «1.2м». Можно добавить «хит» или «слабый».
   Можно прислать и ЧУЖИЕ залетевшие ролики из твоей ниши (запись экрана / скачанный тикток) — подпиши «чужой 2м».
3. По желанию — скрин статистики.
4. Нажми «✅ Готово — разобрать».

Смотрю каждый ролик со звуком: хук, первая секунда, трек, бит, монтаж."""

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
    with lock:
        is_busy = uid in busy
        if not is_busy:
            busy.add(uid)
    if is_busy:
        send(chat_id, "Ещё разбираю прошлый запрос — секунду ⏳")
        return
    if quota and not take_quota(uid):
        busy.discard(uid)
        send(chat_id, f"На сегодня лимит {DAILY_LIMIT} разборов закончился. Возвращайся завтра 🙌")
        return

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


def allow(chat_id, user, kind):
    """Можно ли сделать ИИ-запрос `kind`. True — полный доступ, "trial" — бесплатная проба (списывается), False — нет."""
    uid = user["id"]
    if not PAYWALL or uid in ADMIN_IDS:
        return True
    if subscribed(uid):
        bucket = QUOTA_BUCKET.get(kind, kind)
        limit = QUOTAS.get(bucket)
        month = time.strftime("%Y-%m")
        if limit is not None and storage.month_used(uid, bucket, month) >= limit:
            nxt = time.strftime("01.%m", time.localtime(time.mktime(time.strptime(month, "%Y-%m")) + 32 * 86400))
            send(chat_id, f"В этом месяце лимит {QUOTA_NAMES.get(bucket, bucket)} исчерпан ({limit}). Обновится {nxt} 🙌\n"
                          "Нужно больше — напиши, подберём тариф побольше.")
            return False
        storage.month_use(uid, bucket, month)
        return True
    if storage.free_used(uid, kind) < FREE.get(kind, 0):
        storage.use_free(uid, kind)
        return "trial"
    send(
        chat_id,
        f"{PAYWALLS.get(kind, 'Это — в подписке.')}\n\n"
        f"Подписка {PRICE} ₽ на {SUB_DAYS} дней:\n{sub_includes()}",
        [[{"text": f"💳 Оформить подписку — {PRICE} ₽", "callback_data": "pay"}]],
    )
    return False


pending_pay: dict[int, str] = {}  # uid -> период ("m" / "y")
proof_groups: set = set()


def pay_code(uid, period="m") -> str:
    return f"IL{str(uid)[-5:]}" + ("Y" if period == "y" else "")


def period_price(period) -> int:
    return PRICE * PERIODS[period][1]


def sub_includes() -> str:
    q = QUOTAS
    rows = [(q.get("plan"), "планов на неделю"), (q.get("text"), "запросов идей, разборов и сравнений (1 запрос = 5 идей)"),
            (q.get("reel"), "проверок рилса до публикации"), (q.get("video"), "разбор видео канала"),
            (q.get("reels"), "разбор Inst/TikTok по роликам"), (q.get("insta"), "разбора по скриншотам статистики")]
    return "\n".join(f"• {n} {t}" for n, t in rows if n)


def show_pay(chat_id, user):
    if not PAY_DETAILS:
        send(chat_id, "Оплата скоро появится 🙌")
        return
    until = storage.sub_until(user["id"])
    extra = ""
    if until > time.time():
        extra = f"Подписка уже активна до {time.strftime('%d.%m', time.localtime(until))} — оплата продлит её.\n\n"
    send(
        chat_id,
        extra + f"В подписке на месяц:\n{sub_includes()}\n\nВыбери срок:\n\n"
        f"• Месяц — {PRICE} ₽\n"
        f"• Год — {period_price('y')} ₽ (12 месяцев по цене {YEAR_MONTHS_PAID}, 2 в подарок 🎁)",
        [[{"text": f"Месяц — {PRICE} ₽", "callback_data": "pay_m"}],
         [{"text": f"Год — {period_price('y')} ₽ 🎁", "callback_data": "pay_y"}]],
    )


def show_pay_details(chat_id, user, period):
    days, _, label = PERIODS[period]
    amount = period_price(period)
    send(
        chat_id,
        f"Подписка на {label} — {amount} ₽.\n\n"
        f"1. Переведи {amount} ₽ по СБП: {PAY_DETAILS}\n"
        f"2. В комментарии к переводу напиши код: {pay_code(user['id'], period)}\n"
        "3. Нажми «Я оплатил» и пришли скриншот перевода.\n\n"
        "Подписку включаю вручную после проверки — обычно в течение пары часов.",
        [[{"text": "✅ Я оплатил", "callback_data": f"paid_{period}"}]],
    )


def got_payment_proof(msg, user):
    """Пользователь прислал скриншот — пересылаем админам с кнопками."""
    uid = user["id"]
    period = pending_pay.pop(uid, "m")
    ref = storage.ref_of(uid)
    info = (f"💳 Оплата подписки ({PERIODS[period][2]})\nОт: {who(user)} (id {uid})\nКод: {pay_code(uid, period)}\n"
            f"Сумма: {period_price(period)} ₽\nКанал: @{home_of(user) or '—'}" + (f"\nПригласил: {ref}" if ref else ""))
    kb = [[{"text": "✅ Подтвердить", "callback_data": f"ok:{uid}:{period}"},
           {"text": "❌ Отклонить", "callback_data": f"no:{uid}:{period}"}]]
    for admin in ADMIN_IDS:
        api("forwardMessage", chat_id=admin, from_chat_id=msg["chat"]["id"], message_id=msg["message_id"])
        send(admin, info, kb)
    send(msg["chat"]["id"], "Спасибо! Проверяю перевод и включу подписку — пришлю сообщение 🙌")


def extend_sub(uid, days) -> str:
    until = max(storage.sub_until(uid), time.time()) + days * 86400
    storage.set_sub(uid, until)
    return time.strftime("%d.%m.%Y", time.localtime(until))


def admin_decide(chat_id, admin, data):
    if admin["id"] not in ADMIN_IDS:
        return
    parts = data.split(":")
    ok, uid = parts[0] == "ok", int(parts[1])
    period = parts[2] if len(parts) > 2 and parts[2] in PERIODS else "m"
    if ok:
        d = extend_sub(uid, PERIODS[period][0])
        home = home_of({"id": uid})
        send(uid, f"🔥 Подписка активна до {d}. Всё открыто — жми кнопки или пришли свой канал.",
             channel_keyboard(home) if home else None)
        locked = storage.pop_locked_ideas(uid)
        if locked:
            send(uid, "🔓 Обещанные идеи:\n\n" + locked)
        send(chat_id, f"✅ Подписка для {uid} ({PERIODS[period][2]}) включена до {d}. Не забудь чек в «Мой налог».")
        # рефералка: пригласившему +30 дней за первую оплату друга
        ref = storage.ref_of(uid)
        if ref and storage.ref_rewarded(uid):
            rd = extend_sub(int(ref), SUB_DAYS)
            send(int(ref), f"🎁 Твой друг оформил подписку — дарю тебе +{SUB_DAYS} дней! Подписка активна до {rd}.")
            send(chat_id, f"🎁 Пригласившему ({ref}) начислено +{SUB_DAYS} дней.")
    else:
        send(uid, "Не нашёл твой перевод 😕 Проверь сумму и код в комментарии и пришли скриншот ещё раз — или напиши, разберёмся.",
             [[{"text": "💳 Оплатить", "callback_data": "pay"}]])
        send(chat_id, f"❌ Оплата {uid} отклонена.")


def show_invite(chat_id, user):
    uid = user["id"]
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    n = len(storage.invited(uid))
    send(chat_id,
         f"🎁 Приведи друга — получи месяц бесплатно.\n\nТвоя ссылка:\n{link}\n\n"
         f"Когда друг по ней придёт и оформит подписку, тебе добавится +{SUB_DAYS} дней.\n"
         f"Ты уже пригласил: {n}")


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
    text = cached_ai(("report", name.lower()), lambda: ai.report_prompt(niche_line(user) + compact_for_ai(a) + "\n\nТЕКСТЫ АВТОРА (чтобы понять тему и голос):\n" + samples_for_ai(a)[:4500]),
                     heavy=first(user, "report", name))
    send(chat_id, text, fb("report", channel_keyboard(name)))


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
    text = cached_ai(key, lambda: ai.compare_prompt(niche_line(user) + compact_for_ai(a_mine), [compact_for_ai(x) for x in a_riv]),
                     max_tokens=4000, heavy=False)
    remember(user, "compare", text)
    send(chat_id, text, fb("compare", [[{"text": "📅 План с учётом сравнения", "callback_data": f"plan:{mine}"}],
                                       [{"text": "💡 Идеи с учётом сравнения", "callback_data": f"idea:{mine}"}]]))


def job_videos(chat_id, user, name):
    if not guard_own(chat_id, user, name):
        return
    if not ai.enabled():
        send(chat_id, "Разбор видео делает ИИ, а он пока не подключён.")
        return
    a = get_channel(name)
    if not a["ok"] or not a.get("video_top"):
        send(chat_id, "Не нашёл у канала достаточно видео или кружков для сравнения (нужно хотя бы 2).")
        return
    if not allow(chat_id, user, "video"):
        return
    kb = fb("video", [[{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}]])
    with lock:
        hit = ai_cache.get(("video", name.lower()))
    if hit and time.time() - hit[0] < AI_CACHE_TTL:  # уже смотрели недавно — не скачиваем и не платим заново
        remember(user, "video", hit[1])
        send(chat_id, hit[1], kb)
        return
    send(chat_id, f"Смотрю ролики @{name} со звуком: лучшие и худшие… это 2–3 минуты 🎬")
    typing(chat_id)
    parts = [ai.video_intro(niche_line(user) + compact_for_ai(a))]
    got = heard = 0
    for title, group in (("СИЛЬНЫЕ РОЛИКИ", a["video_top"]), ("СЛАБЫЕ РОЛИКИ", a["video_worst"])):
        parts.append(f"\n===== {title} =====")
        for p in group:
            typing(chat_id)
            data = video.post_video_bytes(p)
            obs = None
            if data and ai_media.gemini_enabled():
                try:
                    obs = ai_media.describe_video(video.compress_for_ai(data))
                except Exception as e:
                    log.warning("describe channel video failed: %s", e)
            if data:
                try:
                    frames, meta = video.reel_frames(data)
                    dur = meta.get("duration", 0)
                    frames = (frames[:1] + frames[4:5]) if obs else frames[:5]
                except Exception:
                    frames, dur = video.frames_for(p)
            else:
                frames, dur = video.frames_for(p)
            if not frames and not obs:
                continue
            got += 1
            heard += bool(obs)
            length = f", длина {round(dur)} с" if dur else ""
            parts.append(
                f"\nРолик t.me/{name}/{p['id']} ({p['type']}{length}): {p['views']} просмотров, ×{p['x_median']} к обычному посту. "
                f"Подпись: {p['text'][:200] or '[без подписи]'}"
            )
            if obs:
                parts.append("Описание ролика (картинка + звук, по секундам):\n" + obs)
            for label, img in frames:
                parts.append(f"Кадр: {label}")
                parts.append(img)
    if got < 2:
        send(chat_id, "Не получилось скачать ролики — Telegram не отдаёт их в открытый доступ. Попробуй другой канал.")
        return
    parts.append(ai.video_outro(heard=heard > 0))
    track(user, "video")
    typing(chat_id)
    text = cached_ai(("video", name.lower()), lambda: parts, heavy=False)
    remember(user, "video", text)
    send(chat_id, text, kb)


past_ideas: dict[tuple[int, str], list[str]] = {}


TEASER_IDEAS = 2


research_locks: dict[str, threading.Lock] = {}


def niche_research(user, wait: bool = True) -> dict | None:
    """Исследование ниши через поиск (кэш 14 дней). wait=False — запустить в фоне."""
    niche = (storage.get_niche(user["id"]) or "").strip()
    if not niche or not ai_media.gemini_enabled():
        return None
    hit = storage.get_research(niche, time.time())
    if hit:
        return hit
    if not wait:
        threading.Thread(target=niche_research, args=(user, True), daemon=True).start()
        return None
    with lock:
        lk = research_locks.setdefault(niche.lower(), threading.Lock())
    with lk:
        hit = storage.get_research(niche, time.time())
        if hit:
            return hit
        try:
            res = ai_media.research_niche(niche)
        except Exception as e:
            log.warning("niche research failed: %s", e)
            return None
        storage.set_research(niche, res, time.time())
        return res


def niche_block(user) -> str:
    r = niche_research(user)
    if not r:
        return ""
    src = "найдено поиском Google" if r.get("grounded") else "общий анализ модели (поиск не сработал)"
    return (f"\nАНАЛИЗ НИШИ — что реально залетает у ДРУГИХ авторов этой ниши ({src}). Опирай идеи на эти форматы "
            f"и примеры, адаптируя под автора:\n{r['text'][:3500]}\n")


def job_niche_report(chat_id, user):
    if not storage.get_niche(user["id"]):
        send(chat_id, "Сначала скажи, о чём ты снимаешь 👇")
        ask_niche(chat_id)
        return
    niche = storage.get_niche(user["id"])
    send(chat_id, f"Изучаю, что сейчас залетает в нише «{niche}»: ищу авторов, форматы и примеры… до 1–2 минут 🔍")
    typing(chat_id)
    r = niche_research(user)
    if not r:
        send(chat_id, "Не получилось исследовать нишу 😕 Попробуй позже.")
        return
    track(user, "research")
    srcs = "\n".join(f"• {t[:70]} — {u}" for t, u in r.get("sources", [])[:8])
    note = "" if r.get("grounded") else "\n\n(Поиск в этот раз не сработал — это общий анализ без свежих ссылок.)"
    send(chat_id, f"🔍 Что залетает в нише «{niche}»\n\n{r['text']}" + (f"\n\nИсточники:\n{srcs}" if srcs else "") + note,
         fb("research", [[{"text": "💡 Идеи на основе этого", "callback_data": f"idea:{home_of(user)}"}]] if home_of(user) else None))


def playbook_for(user, extra: str = "") -> str:
    """Библиотека проверенных форматов под нишу автора."""
    return playbook.block(storage.get_niche(user["id"]) or extra)


def channel_dna(name: str, a: dict) -> str:
    """«ДНК канала» — профиль автора для идей. Считается раз в неделю на канал."""
    dna = storage.get_dna(name, time.time())
    if dna:
        return dna
    try:
        dna = ai.ask(ai.dna_prompt(compact_for_ai(a), samples_for_ai(a)), tag="dna", heavy=True, max_tokens=1200)
        storage.set_dna(name, dna, time.time())
        return dna
    except Exception as e:
        log.warning("dna failed: %s", e)
        return ""


def split_ideas(text: str, keep: int = TEASER_IDEAS) -> tuple[str, str]:
    """Режет ответ с идеями: первые `keep` идей и остальное."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(rf"^{keep + 1}[.)]\s", ln.strip()):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    cut = max(len(text) // 3, 1)
    return text[:cut].rstrip() + "…", text[cut:].strip()


def job_ideas(chat_id, user, name, focus=None):
    if not guard_own(chat_id, user, name):
        return
    access = allow(chat_id, user, "ideas")
    if not access:
        return
    if not ai.enabled():
        send(chat_id, "Идеи придумывает ИИ, а он пока не подключён.")
        return
    key = (user["id"], name.lower(), (focus or "").lower())
    prev = past_ideas.get(key, [])
    send(chat_id, ("Придумываю ещё 5 идей" if prev else "Изучаю канал и придумываю 5 сильных идей") + f" для @{name}" + (f" на тему «{focus}»" if focus else "") + "… около минуты ⏳")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, "Не нашёл постов с просмотрами.")
        return
    previous = "\n".join(prev)[-6000:] or None
    ins, used = insights_block(user)
    ins = niche_block(user) + ins
    dna = channel_dna(name, a)
    text = ai.ask(ai.ideas_prompt(niche_line(user) + compact_for_ai(a), focus, previous, ins, dna=dna,
                                  samples=samples_for_ai(a), playbook=playbook_for(user, dna[:300]),
                                  spec=playbook.IDEA_SPEC), tag="ideas", heavy=True, max_tokens=2600)
    if used and not prev:
        text += "\n\n🧠 Учёл: " + ", ".join(used)
    titles = [ln.strip() for ln in text.splitlines() if re.match(r"^\d{1,2}[.)]\s", ln.strip())]
    past_ideas[key] = prev + (titles or [text[:1500]])
    track(user, "ideas")
    if access == "trial":
        # бесплатная проба: показываем 3 идеи, остальные — после подписки
        shown, locked = split_ideas(text)
        storage.set_locked_ideas(user["id"], locked)
        rest = len([ln for ln in locked.splitlines() if re.match(r"^\d{1,2}[.)]\s", ln.strip())]) or 3
        send(
            chat_id,
            shown + f"\n\n🔒 Ещё {rest} идей уже готовы — открываются в подписке.",
            like_row(user, shown) + [[{"text": f"🔓 Открыть остальные {rest} — подписка {PRICE} ₽", "callback_data": "pay"}],
                                     [{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}]],
        )
        return
    cb = f"idea:{name}" + (f"|{focus}" if focus else "")
    if len(cb.encode()) > 64:
        cb = f"idea:{name}"
    send(
        chat_id,
        text + "\n\nХочешь идеи на конкретную тему — напиши, например: /ideas бренд одежды",
        like_row(user, text) + fb("ideas", [[{"text": "🔁 Ещё идеи", "callback_data": cb}],
                                            [{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}]]),
    )


pending_plan = storage.Pending("plan")


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


def previous_plan_block(prev, a) -> str:
    """Прошлый план + посты, вышедшие после него."""
    if not prev or time.time() - prev["ts"] < 20 * 3600:
        return ""
    after = [p for p in a.get("fresh", []) if p["ts"] >= prev["ts"] - 3600]
    days = max(1, round((time.time() - prev["ts"]) / 86400))
    lines = [f"ПРОШЛЫЙ ПЛАН (выдан {days} дн. назад):", prev["text"][:3500], "",
             f"ПОСТЫ, КОТОРЫЕ ВЫШЛИ ПОСЛЕ НЕГО ({len(after)} шт.):"]
    if not after:
        lines.append("— ни одного поста после прошлого плана")
    for p in reversed(after):
        lines.append(f"- {p['date']} · {p['type']} · {p['views']} просм. (×{p['x_median']}): {p['text'] or '[без текста]'}")
    return "\n".join(lines)


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
    ins, used = insights_block(user)
    prev = storage.get_last_plan(user["id"], name)
    prev_block = previous_plan_block(prev, a)
    text = cached_ai(("plan", name.lower(), (week or "").lower(), user["id"], ins[:200], len(ins), ai.plan_days(),
                      ai._msk_now().hour, prev["ts"] if prev else 0),
                     lambda: ai.plan_prompt(niche_line(user) + compact_for_ai(a) + "\n\nДНК КАНАЛА:\n" + channel_dna(name, a),
                                            week, ins, previous=prev_block),
                     heavy=False)
    # сохраняем новый план, только если старый уже «отработал» хотя бы сутки — иначе повторный запрос
    # в тот же день сравнивал бы план сам с собой
    if not prev or time.time() - prev["ts"] > 20 * 3600:
        storage.set_last_plan(user["id"], name, text, time.time())
    if prev_block:
        used = list(used) + ["прошлый план"]
    if used:
        text += "\n\n🧠 Учёл: " + ", ".join(used)
    send(chat_id, text, fb("plan"))


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
    if niche_line(user):
        context = niche_line(user) + (context or "")
    track(user, "reel")
    kb = fb("reel", [[{"text": "📲 Проверить ещё рилс", "callback_data": "help_reel"}]])

    # 1) Gemini смотрит ролик целиком — картинка + звук
    if ai_media.gemini_enabled():
        try:
            how = ("Ролик прикреплён целиком — ты ВИДИШЬ картинку и СЛЫШИШЬ звук (музыку, голос). "
                   "Указывай моменты по секундам.")
            prompt = ai.reel_intro(meta, caption, context, how=how) + "\n\n" + ai.reel_outro(ai_media.SOUND_BLOCK)
            # Gemini смотрит и слушает ролик и сам пишет разбор — одна дешёвая модель (вариант «смешанный»)
            for opts in ({}, {"height": 240, "fps": 2, "crf": 38, "abr": "32k", "max_sec": 60}):
                small = video.compress_for_ai(data, **opts)
                log.info("gemini reel payload %d KB (%s)", len(small) // 1024, opts or "default")
                try:
                    typing(chat_id)
                    send(chat_id, ai_media.ask_gemini_video(small, prompt + "\n\n" + playbook.ANTI_AI, max_tokens=4000), kb)
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
    parts = [ai.insta_intro((niche_line(user) + niche_block(user) + (note or "")).strip() or None)]
    for i, fid in enumerate(items[:10], 1):
        parts.append(f"Скриншот {i}:")
        parts.append(video.to_jpeg(tg_download(fid)))
    parts.append(ai.insta_outro(playbook_for(user, note or ""), playbook.IDEA_SPEC))
    track(user, "insta")
    typing(chat_id)
    text = ai.ask(parts, heavy=False, tag="insta", max_tokens=4500)
    send(chat_id, text, like_row(user, text.split("5 ИДЕЙ", 1)[-1]) +
         fb("insta", [[{"text": "📲 Проверить рилс", "callback_data": "help_reel"}]]))


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


VS_MAX = 6
VS_TTL = 30 * 60
vs_sessions: dict[int, dict] = {}  # uid -> {"items": [{"file_id","views","label","caption"}], "shots": [...], "ts", "note"}


def parse_views(text: str | None):
    """«12к» → 12000, «1.2м» → 1200000, «800» → 800. Плюс метка хит/слабый."""
    t = (text or "").lower().replace(",", ".")
    label = None
    if re.search(r"хит|залет|топ|лучш|вирус", t):
        label = "хит"
    elif re.search(r"слаб|не зашел|не зашёл|флоп|плох|провал", t):
        label = "слабый"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(млн|м|m|тыс|к|k)?\b", t)
    views = None
    if m:
        num, suf = float(m.group(1)), (m.group(2) or "")
        mult = 1_000_000 if suf in ("млн", "м", "m") else 1000 if suf in ("тыс", "к", "k") else 1
        views = int(num * mult)
    return views, label


def vs_session(uid):
    sess = vs_sessions.get(uid)
    if sess and time.time() - sess["ts"] > VS_TTL:
        vs_sessions.pop(uid, None)
        return None
    return sess


VS_DONE_KB = [[{"text": "✅ Готово — разобрать", "callback_data": "vs_go"}], [{"text": "Отмена", "callback_data": "vs_cancel"}]]


def vs_add(msg, user, vid=None, shot=None):
    chat_id, uid = msg["chat"]["id"], user["id"]
    sess = vs_session(uid)
    sess["ts"] = time.time()
    if shot:
        sess["shots"].append(shot)
        send(chat_id, "Скрин статистики принял 📊 Ещё ролики или «Готово».", VS_DONE_KB)
        return
    if len(sess["items"]) >= VS_MAX:
        send(chat_id, f"Максимум {VS_MAX} роликов — жми «Готово».", VS_DONE_KB)
        return
    caption = (msg.get("caption") or "").strip()
    views, label = parse_views(caption)
    foreign = bool(re.search(r"чуж|конкурент|не мой|не моё|не мое|референс", caption.lower()))
    sess["items"].append({"file_id": vid["file_id"], "views": views, "label": label, "caption": caption, "foreign": foreign})
    n = len(sess["items"])
    info = (f"{views:,} просмотров".replace(",", " ") if views else "просмотры не указаны") + (f", {label}" if label else "") + (", чужой хит" if foreign else "")
    send(chat_id, f"Ролик {n} принял ({info}). " + ("Пришли ещё или жми «Готово»." if n >= 2 else "Пришли ещё хотя бы один."),
         VS_DONE_KB if n >= 2 else None)


def vs_go(chat_id, user):
    sess = vs_session(user["id"])
    if not sess or len(sess["items"]) < 2:
        send(chat_id, "Нужно хотя бы 2 ролика — пришли ещё.")
        return
    if not any(it["views"] or it["label"] or it.get("foreign") for it in sess["items"]):
        sess["await_views"] = True
        send(chat_id, f"Чтобы сравнить, напиши просмотры роликов одним сообщением по порядку ({len(sess['items'])} шт.), например: 12к, 3к, 800")
        return
    vs_sessions.pop(user["id"], None)
    run_job(chat_id, user, job_reels_compare, sess)


def job_reels_compare(chat_id, user, sess):
    if not ai.enabled():
        send(chat_id, "Разбор делает ИИ, а он пока не подключён.")
        return
    if not allow(chat_id, user, "reels"):
        return
    items = sess["items"]
    send(chat_id, f"Смотрю {len(items)} ролик(а/ов) со звуком и сравниваю хиты со слабыми… это 2–3 минуты 🎞")
    # порядок: по просмотрам (метки «хит/слабый» — если просмотров нет)
    def score(it):
        if it["views"] is not None:
            return it["views"]
        return {"хит": 10**12, "слабый": -1}.get(it["label"], 0)
    own = sorted([it for it in items if not it.get("foreign")], key=score, reverse=True)
    items = own + [it for it in items if it.get("foreign")]
    n_strong = max(1, (len(own) + 1) // 2)
    parts = [ai.reels_compare_intro((niche_line(user) + niche_block(user) + (sess.get("note") or "")).strip() or None)]
    got = 0
    for idx, it in enumerate(items, 1):
        typing(chat_id)
        group = "СИЛЬНЫЙ" if (it["label"] == "хит" or (it["label"] != "слабый" and idx <= n_strong)) else "СЛАБЫЙ"
        if it.get("foreign"):
            group = "ЧУЖОЙ ХИТ ИЗ НИШИ (не автора)"
        try:
            data = tg_download(it["file_id"])
        except Exception as e:
            log.warning("vs download failed: %s", e)
            continue
        obs = None
        if ai_media.gemini_enabled():
            try:
                obs = ai_media.describe_video(video.compress_for_ai(data))
            except Exception as e:
                log.warning("vs describe failed: %s", e)
        try:
            frames, meta = video.reel_frames(data)
        except Exception:
            frames, meta = [], {}
        if not obs and not frames:
            continue
        got += 1
        views = f"{it['views']:,}".replace(",", " ") + " просмотров" if it["views"] else "просмотры не указаны"
        parts.append(f"\n===== Ролик {idx} — {group} ({views}{', метка автора: ' + it['label'] if it['label'] else ''}, "
                     f"длина {meta.get('duration', '?')} с) =====")
        if it["caption"]:
            parts.append("Подпись автора: " + it["caption"][:200])
        if obs:
            parts.append("Описание (картинка + звук):\n" + obs)
        for label, img in (frames[:1] + frames[4:5] if obs else frames[:4]):
            parts.append(f"Кадр: {label}")
            parts.append(img)
    if got < 2:
        send(chat_id, "Не получилось прочитать ролики 😕 Попробуй прислать их ещё раз как обычное видео.")
        return
    for i, fid in enumerate(sess.get("shots", [])[:3], 1):
        try:
            parts.append(f"Скриншот статистики {i}:")
            parts.append(video.to_jpeg(tg_download(fid)))
        except Exception:
            pass
    parts.append(ai.reels_compare_outro(playbook_for(user, sess.get("note") or ""), playbook.IDEA_SPEC))
    track(user, "reels")
    typing(chat_id)
    text = ai.ask(parts, heavy=True, tag="reels", max_tokens=3800)
    remember(user, "reels", text)
    send(chat_id, text, like_row(user, text.split("5 ИДЕЙ", 1)[-1]) +
         fb("reels", [[{"text": "📲 Проверить новый рилс", "callback_data": "help_reel"}]]))


def handle_media(msg, user) -> bool:
    """Видео → проверка рилса (или в сессию разбора роликов), картинки → разбор по скринам. True, если обработано."""
    chat_id = msg["chat"]["id"]
    vid = msg.get("video") or msg.get("video_note") or msg.get("animation")
    doc = msg.get("document") or {}
    mime = doc.get("mime_type", "")
    if not vid and mime.startswith("video/"):
        vid = doc
    if vs_session(user["id"]):
        if vid:
            if (vid.get("file_size") or 0) > TG_FILE_LIMIT:
                send(chat_id, "Этот ролик больше 20 МБ — пришли его как обычное видео (не файлом), Telegram сожмёт.")
                return True
            vs_add(msg, user, vid=vid)
            return True
        img = msg["photo"][-1]["file_id"] if msg.get("photo") else (doc["file_id"] if mime.startswith("image/") else None)
        if img:
            vs_add(msg, user, shot=img)
            return True
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


pending_compare = storage.Pending("cmp")


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {"id": chat_id}
    text = (msg.get("text") or "").strip()
    gid = msg.get("media_group_id")
    if gid and gid in proof_groups:
        return  # остальные фото из альбома со скриншотом оплаты
    if user["id"] in pending_pay and (msg.get("photo") or msg.get("document")
                                      or (text and not text.startswith("/") and not extract_channels(text))):
        if gid:
            proof_groups.add(gid)
        got_payment_proof(msg, user)
        return
    if not text and handle_media(msg, user):
        return
    if not text:
        send(chat_id, "Пришли @канал, ссылку t.me/..., рилс (видео) или скриншоты статистики Inst / TikTok.")
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        payload = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
        is_new = track(user, "start")
        if is_new and payload.startswith("ref_") and payload[4:].isdigit():
            if storage.set_ref(user["id"], payload[4:]):
                log.info("referral: %s invited by %s", user["id"], payload[4:])
        mine = home_of(user)
        if mine:
            send(chat_id, f"С возвращением! Твой канал: @{mine} — жми кнопки 👇", channel_keyboard(mine) + MENU_INSTA)
        else:
            send(chat_id, START_SHORT, MENU_START)
        if is_new or not storage.get_niche(user["id"]):
            ask_niche(chat_id)
        return
    if cmd == "/help":
        send(chat_id, HELP)
        return
    if cmd == "/niche":
        ask_niche(chat_id)
        return
    if cmd == "/research":
        arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        if arg and user["id"] in ADMIN_IDS:  # админ: исследовать любую нишу
            storage.set_niche(user["id"], arg)
        run_job(chat_id, user, job_niche_report)
        return
    if cmd == "/invite":
        show_invite(chat_id, user)
        return
    if cmd == "/reel":
        send(chat_id, HELP_REEL)
        return
    if cmd == "/insta":
        vs_sessions[user["id"]] = {"items": [], "shots": [], "ts": time.time(), "note": None}
        send(chat_id, HELP_VS)
        return
    if cmd == "/shots":
        send(chat_id, HELP_INSTA)
        return
    if cmd in ("/sub", "/pay"):
        until = storage.sub_until(user["id"])
        if until > time.time():
            month = time.strftime("%Y-%m")
            left = "\n".join(f"• {QUOTA_NAMES.get(b, b)}: {max(lim - storage.month_used(user['id'], b, month), 0)} из {lim}"
                             for b, lim in QUOTAS.items())
            send(chat_id, f"Подписка активна до {time.strftime('%d.%m.%Y', time.localtime(until))} ✅\n\nОсталось в этом месяце:\n{left}",
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
        acts = ("report", "compare", "plan", "video", "ideas", "reel", "insta", "reels", "research")
        total = sum(int(u.get(k, 0)) for u in st.values() for k in acts)
        day_ago = time.time() - 86400
        active = sum(1 for u in st.values() if float(u.get("last", 0)) > day_ago)
        new = sum(1 for u in st.values() if float(u.get("first", 0)) > day_ago)
        top = sorted(st.items(), key=lambda kv: -sum(int(kv[1].get(k, 0)) for k in acts))[:10]
        lines = [f"Пользователей всего: {len(st)}", f"Новых за сутки: {new}", f"Активных за сутки: {active}", f"Запросов всего: {total}", ""]
        for uid, u in top:
            n = sum(int(u.get(k, 0)) for k in acts)
            lines.append(f"{'@' + u['name'] if u.get('name') else uid}: {n}")
        fbd = storage.fb_all()
        if fbd:
            lines.append("")
            lines.append("Оценки 👍/👎:")
            for k, name_ in FB_NAMES.items():
                u_, d_ = fbd.get(f"{k}:up", 0), fbd.get(f"{k}:down", 0)
                if u_ or d_:
                    lines.append(f"{name_}: {u_} / {d_}")
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

    if not cmd and user["id"] in pending_niche and not chans:
        pending_niche.discard(user["id"])
        storage.set_niche(user["id"], text)
        niche_research(user, wait=False)
        send(chat_id, f"Записал: {text[:80]} ✍️ Теперь пришли свой канал (@ник) или ролик 👇",
             [[{"text": "🔍 Что залетает в моей нише", "callback_data": "nr"}]])
        return
    if not cmd and user["id"] in pending_fb and not chans:
        kind = pending_fb.pop(user["id"])
        notify_admins(f"👎 Отзыв ({FB_NAMES.get(kind, kind)}) от {who(user)}:\n{text[:1000]}")
        send(chat_id, "Спасибо, передал! Исправим 🙏")
        return

    sess = vs_session(user["id"])
    if not cmd and sess and sess.get("await_views"):
        nums = [parse_views(x)[0] for x in re.split(r"[,;\n]+", text) if x.strip()]
        nums = [v for v in nums if v is not None]
        if len(nums) < len(sess["items"]):
            send(chat_id, f"Нужно {len(sess['items'])} чисел через запятую, например: 12к, 3к, 800")
            return
        for it, v in zip(sess["items"], nums):
            it["views"] = v
        sess["await_views"] = False
        vs_go(chat_id, user)
        return
    if not cmd and sess and not chans and text:
        sess["note"] = text[:300]
        send(chat_id, "Записал комментарий. Пришли ролики или жми «Готово».", VS_DONE_KB if len(sess["items"]) >= 2 else None)
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
    elif data == "nr":
        run_job(chat_id, user, job_niche_report)
    elif data.startswith("like:") and data[5:].isdigit():
        on_like(chat_id, user, int(data[5:]))
    elif data.startswith("fb:"):
        on_feedback(cb, user, data)
    elif data.startswith("niche:"):
        key = data[6:]
        if key == "other":
            pending_niche.add(user["id"])
            send(chat_id, "Напиши в двух словах, о чём ты снимаешь:")
        else:
            label = dict(NICHES).get(key, key)
            storage.set_niche(user["id"], label.split(" ", 1)[1] if " " in label else label)
            niche_research(user, wait=False)
            send(chat_id, f"Записал: {label} ✅ Теперь пришли свой канал (@ник) или ролик 👇",
                 [[{"text": "🔍 Что залетает в моей нише", "callback_data": "nr"}]])
    elif data == "tg_help":
        send(chat_id, "Пришли ник своего канала (@канал) или ссылку t.me/… — канал должен быть публичным. Сделаю разбор за полгода 📊")
    elif data == "help_all":
        send(chat_id, HELP, MENU_INSTA)
    elif data == "vs_start":
        vs_sessions[user["id"]] = {"items": [], "shots": [], "ts": time.time(), "note": None}
        send(chat_id, HELP_VS)
    elif data == "vs_go":
        vs_go(chat_id, user)
    elif data == "vs_cancel":
        vs_sessions.pop(user["id"], None)
        send(chat_id, "Ок, отменил. Ролики, которые пришлёшь дальше, буду проверять по одному 📲")
    elif data == "help_insta":
        send(chat_id, HELP_INSTA)
    elif data == "pay":
        show_pay(chat_id, user)
    elif data in ("pay_m", "pay_y"):
        show_pay_details(chat_id, user, data[-1])
    elif data.startswith("paid"):
        pending_pay[user["id"]] = "y" if data == "paid_y" else "m"
        send(chat_id, "Пришли скриншот перевода одним сообщением 📸")
    elif data == "invite":
        show_invite(chat_id, user)
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
            {"command": "insta", "description": "Разбор Inst / TikTok по роликам"},
            {"command": "shots", "description": "Разбор по скриншотам статистики"},
            {"command": "mychannel", "description": "Мой канал"},
            {"command": "niche", "description": "О чём я снимаю (ниша)"},
            {"command": "research", "description": "Что залетает в моей нише"},
            {"command": "sub", "description": "Подписка"},
            {"command": "invite", "description": "Пригласить друга — месяц в подарок"},
        ],
    )


BOT_DESCRIPTION = """ИИ-продюсер для авторов 🎬

• Разбираю Telegram-канал за полгода: что заходит, что нет и что снимать дальше
• Проверяю рилс ДО публикации — смотрю и слушаю: хук, трек, бит, монтаж
• Сравниваю твои хиты и слабые ролики в Instagram и TikTok и даю рецепт
• Идеи для съёмок, контент-план, сравнение с конкурентами

Жми «Старт» и пришли свой канал или ролик 👇"""
BOT_SHORT = "ИИ-продюсер: разбор Telegram-канала, проверка рилсов со звуком, идеи и план — что снимать, чтобы росли просмотры"


def set_profile():
    api("setMyDescription", description=BOT_DESCRIPTION)
    api("setMyShortDescription", short_description=BOT_SHORT[:120])


def run_polling():
    api("deleteWebhook", drop_pending_updates=False)
    offset = 0
    log.info("polling started")
    while True:
        updates = api("getUpdates", offset=offset, timeout=50) or []
        for u in updates:
            offset = u["update_id"] + 1
            updates_pool.submit(handle_update, u)
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
                updates_pool.submit(handle_update, json.loads(body))
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
    global BOT_USERNAME
    BOT_USERNAME = (me or {}).get("username") or BOT_USERNAME
    log.info("bot @%s, AI %s", (me or {}).get("username"), "on" if ai.enabled() else "off")
    set_commands()
    set_profile()
    storage.db()
    threading.Thread(target=daily_summary_loop, daemon=True).start()
    video.warmup()
    threading.Thread(target=ai_media.selftest, args=(video.test_clip,), daemon=True).start()
    threading.Thread(target=ai_media.selftest_research, daemon=True).start()
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        run_webhook(base, int(os.getenv("PORT", "10000")))
    else:
        run_polling()


if __name__ == "__main__":
    main()
