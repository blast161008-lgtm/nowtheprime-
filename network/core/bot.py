"""Каркас бота сети. Каждый бот — подкласс Bot с методами on_text / on_media / on_button.

Общее для всех ботов сети:
- бесплатные попытки в каждом боте (FREE_TRIES), потом — ОДНА подписка на всю сеть (NET_PRICE ₽ / 30 дней);
- оплата по СБП как в Improve Lab: код в комментарии → скриншот → админу кнопки ✅/❌;
- админу: новые пользователи, оплаты, 👎 с комментарием, вечерняя сводка по всей сети;
- 👍/👎 под ответами, «Ещё», перекрёстные ссылки на другие боты сети и Improve Lab.
"""
import logging
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import store
from .tg import TG

log = logging.getLogger("bot")

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
PAYWALL = os.getenv("PAYWALL", "on").lower() not in ("off", "0", "false", "no")
FREE_TRIES = int(os.getenv("FREE_TRIES", "3"))           # бесплатных попыток в каждом боте
DAILY_FREE_TEST = int(os.getenv("DAILY_LIMIT", "10"))    # лимит в день при PAYWALL=off
SUB_DAILY = int(os.getenv("SUB_DAILY", "40"))            # потолок в день для подписчика (защита от злоупотреблений)
NET_PRICE = int(os.getenv("NET_PRICE", "199"))
YEAR_MONTHS = 10                                          # год = цена 10 месяцев
PAY_DETAILS = os.getenv("PAY_DETAILS", "")                # «+7 …, Альфа-Банк, Никита Л.»
MAIN_BOT = os.getenv("IMPROVE_LAB", "Improvelab_bot")     # большой платный бот, куда ведёт сеть
NET_NAME = os.getenv("NET_NAME", "Improve Lab")
PERIODS = {"m": (30, 1, "месяц"), "y": (365, YEAR_MONTHS, "год")}

jobs = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "6")))
_busy: set = set()
_lock = threading.Lock()


def msk():
    return time.gmtime(time.time() + 3 * 3600)


def today() -> str:
    return time.strftime("%Y-%m-%d", msk())


def who(user) -> str:
    return "@" + user["username"] if user.get("username") else (user.get("first_name") or str(user["id"]))


# ---------------- сеть: все боты, общая подписка ----------------

class Network:
    def __init__(self):
        self.bots: list["Bot"] = []

    def live(self):
        return [b for b in self.bots if b.tg]

    def hub(self):
        """Бот, через который админу приходит вечерняя сводка (первый запущенный)."""
        live = self.live()
        return live[0] if live else None

    def notify_admins(self, text, kb=None, via: "Bot" = None):
        bot = via or self.hub()
        if not bot:
            return
        for a in ADMIN_IDS:
            try:
                bot.tg.send(a, text, kb)
            except Exception:
                pass


network = Network()


def sub_until(uid) -> float:
    return float(store.get(f"net:sub:{uid}", 0) or 0)


def subscribed(uid) -> bool:
    return sub_until(uid) > time.time()


def extend_sub(uid, days) -> str:
    until = max(sub_until(uid), time.time()) + days * 86400
    store.put(f"net:sub:{uid}", until)
    return time.strftime("%d.%m.%Y", time.localtime(until))


def pay_code(uid, period="m") -> str:
    return f"NB{str(uid)[-5:]}" + ("Y" if period == "y" else "")


def price(period) -> int:
    return NET_PRICE * PERIODS[period][1]


class Ctx:
    """Один входящий запрос: бот, пользователь, чат."""

    def __init__(self, bot: "Bot", user: dict, chat_id: int):
        self.bot, self.user, self.chat_id, self.uid = bot, user, chat_id, user["id"]

    def send(self, text, kb=None):
        self.bot.tg.send(self.chat_id, text, kb)

    def typing(self, action="typing"):
        self.bot.tg.typing(self.chat_id, action)

    def get(self, name, default=None):
        return store.get(f"b:{self.bot.id}:{name}:{self.uid}", default)

    def put(self, name, value, ttl=None):
        store.put(f"b:{self.bot.id}:{name}:{self.uid}", value, ttl)


class Bot:
    id = "base"                # латиница; токен берётся из TOKEN_<ID>
    title = "Бот"
    emoji = "🤖"
    pitch = ""                 # одна строка для перекрёстных ссылок: «субтитры для рилс за минуту»
    description = ""           # описание в профиле бота (до 512 знаков)
    start_text = ""
    help_text = ""
    commands: list = []        # [(команда, описание)]
    use_name = "запрос"        # как называть одно действие в сообщениях о лимитах

    def __init__(self):
        self.tg: TG | None = None
        self.username = ""

    # ----- запуск -----
    def attach(self, token: str):
        self.tg = TG(token)
        me = self.tg.api("getMe") or {}
        self.username = me.get("username", "")
        cmds = [("start", "Начать"), *self.commands, ("more", "Другие полезные боты"), ("sub", "Подписка")]
        self.tg.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds])
        if self.description:
            self.tg.api("setMyDescription", description=self.description[:512])
        if self.pitch:
            self.tg.api("setMyShortDescription", short_description=self.pitch[:120])
        log.info("bot %s → @%s", self.id, self.username or "?")

    # ----- вход -----
    def handle_update(self, upd: dict):
        try:
            if "callback_query" in upd:
                cb = upd["callback_query"]
                self.tg.api("answerCallbackQuery", callback_query_id=cb["id"])
                msg = cb.get("message") or {}
                ctx = Ctx(self, cb["from"], msg.get("chat", {}).get("id", cb["from"]["id"]))
                self._button(ctx, cb.get("data", ""))
                return
            msg = upd.get("message")
            if not msg or "from" not in msg or msg.get("chat", {}).get("type") != "private":
                return
            ctx = Ctx(self, msg["from"], msg["chat"]["id"])
            self._seen(ctx)
            self._message(ctx, msg)
        except Exception:
            log.error("update failed (%s):\n%s", self.id, traceback.format_exc())

    def _seen(self, ctx):
        uid = ctx.uid
        new_here = store.add_to_set(f"b:{self.id}:users", uid)
        new_net = store.add_to_set("net:users", uid)
        if new_here:
            store.hincr(f"net:day:{today()}", f"{self.id}:new")
            if uid not in ADMIN_IDS:
                tag = " (новый в сети)" if new_net else ""
                network.notify_admins(f"🆕 {who(ctx.user)} → {self.emoji} {self.title}{tag}", via=self)

    def _message(self, ctx, msg):
        text = (msg.get("text") or "").strip()
        if store.get(f"net:paywait:{ctx.uid}") and (msg.get("photo") or msg.get("document")):
            return self._payment_proof(ctx, msg)
        if store.get(f"b:{self.id}:fbwait:{ctx.uid}") and text and not text.startswith("/"):
            store.put(f"b:{self.id}:fbwait:{ctx.uid}", "", 1)
            network.notify_admins(f"👎 {self.emoji} {self.title}, {who(ctx.user)}: {text}", via=self)
            return ctx.send("Спасибо, передал автору бота — поправим 🙌")
        if text.startswith("/start"):
            store.hincr(f"net:day:{today()}", f"{self.id}:start")
            return ctx.send(self.start_text + self.footer_hint(), self.start_keyboard(ctx))
        if text == "/help":
            return ctx.send(self.help_text or self.start_text)
        if text == "/more":
            return ctx.send(self.network_text())
        if text == "/sub":
            return self.show_sub(ctx)
        if msg.get("video") or msg.get("video_note") or msg.get("voice") or msg.get("audio") or msg.get("photo") \
                or (msg.get("document") or {}).get("mime_type", "").startswith(("video/", "audio/", "image/")):
            return self.on_media(ctx, msg)
        if text.startswith("/"):
            return self.on_command(ctx, text)
        if text:
            return self.on_text(ctx, text)

    def _button(self, ctx, data):
        if data == "pay":
            return self.show_pay(ctx)
        if data in ("pay_m", "pay_y"):
            return self.show_pay_details(ctx, data[-1])
        if data in ("paid_m", "paid_y"):
            store.put(f"net:paywait:{ctx.uid}", data[-1], 3600)
            return ctx.send("Пришли сюда скриншот перевода 📎")
        if data.startswith(("ok:", "no:")):
            return self._admin_decide(ctx, data)
        if data.startswith("fb:"):
            return self._feedback(ctx, data)
        if data == "more":
            return ctx.send(self.network_text())
        return self.on_button(ctx, data)

    # ----- переопределяют боты -----
    def start_keyboard(self, ctx):
        return None

    def on_text(self, ctx, text):
        ctx.send(self.help_text or self.start_text)

    def on_media(self, ctx, msg):
        ctx.send("Этот бот работает с текстом 🙂\n\n" + (self.help_text or self.start_text))

    def on_command(self, ctx, text):
        ctx.send(self.help_text or self.start_text)

    def on_button(self, ctx, data):
        pass

    # ----- доступ и лимиты -----
    def allow(self, ctx) -> bool | str:
        """True — можно, "trial" — бесплатная попытка (уже списана), False — нет (сообщение уже отправлено)."""
        uid = ctx.uid
        if uid in ADMIN_IDS:
            return True
        day_key = f"b:{self.id}:day:{today()}:{uid}"
        if not PAYWALL:
            if store.incr(day_key, ttl=2 * 86400) > DAILY_FREE_TEST:
                ctx.send(f"На сегодня лимит {DAILY_FREE_TEST} закончился — возвращайся завтра 🙌")
                return False
            return True
        if subscribed(uid):
            if store.incr(day_key, ttl=2 * 86400) > SUB_DAILY:
                ctx.send("На сегодня хватит — лимит обновится завтра 🙌")
                return False
            return True
        used = int(store.get(f"b:{self.id}:free:{uid}", 0) or 0)
        if used < FREE_TRIES:
            store.put(f"b:{self.id}:free:{uid}", used + 1)
            return "trial"
        self.paywall(ctx)
        return False

    def refund(self, ctx, trial):
        """Вернуть бесплатную попытку, если результат не получился не по вине пользователя."""
        if trial == "trial":
            used = int(store.get(f"b:{self.id}:free:{ctx.uid}", 0) or 0)
            store.put(f"b:{self.id}:free:{ctx.uid}", max(0, used - 1))

    def free_left(self, ctx) -> int:
        if not PAYWALL or ctx.uid in ADMIN_IDS or subscribed(ctx.uid):
            return -1
        return max(0, FREE_TRIES - int(store.get(f"b:{self.id}:free:{ctx.uid}", 0) or 0))

    def paywall(self, ctx):
        ctx.send(f"Бесплатные попытки закончились 🙌\n\n{self.sub_text()}",
                 [[{"text": f"💳 Подписка — {NET_PRICE} ₽ в месяц", "callback_data": "pay"}]])

    def sub_text(self) -> str:
        names = "\n".join(f"{b.emoji} {b.title}" + (f" — @{b.username}" if b.username else "") for b in network.live())
        return f"Одна подписка открывает ВСЕ боты сети {NET_NAME}:\n{names}"

    def run(self, ctx, fn, *args):
        """Долгая задача в фоне; один запрос пользователя за раз."""
        key = (self.id, ctx.uid)
        with _lock:
            if key in _busy:
                ctx.send("Ещё работаю над прошлым запросом — секунду ⏳")
                return
            _busy.add(key)

        def wrapped():
            try:
                fn(ctx, *args)
                store.hincr(f"net:day:{today()}", f"{self.id}:use")
            except Exception:
                log.error("job failed (%s):\n%s", self.id, traceback.format_exc())
                ctx.send("Что-то пошло не так 😕 Попробуй ещё раз через минуту.")
            finally:
                _busy.discard(key)

        jobs.submit(wrapped)

    # ----- ответы -----
    def result_keyboard(self, kind: str, extra=None):
        rows = list(extra or [])
        rows.append([{"text": "👍 Полезно", "callback_data": f"fb:1:{kind}"},
                     {"text": "👎 Не то", "callback_data": f"fb:0:{kind}"}])
        return rows

    def after_result(self, ctx, trial):
        """Хвост после ответа: сколько бесплатных осталось + иногда ссылка на соседний бот."""
        n = int(ctx.get("uses", 0) or 0) + 1
        ctx.put("uses", n)
        tail = []
        if trial == "trial":
            left = self.free_left(ctx)
            tail.append(f"Бесплатно осталось: {left}" if left else "Это была последняя бесплатная попытка.")
        if n in (2, 5) or n % 10 == 0:
            other = self.cross_promo()
            if other:
                tail.append(other)
        if tail:
            ctx.send("\n\n".join(tail))

    def cross_promo(self) -> str:
        others = [b for b in network.live() if b is not self and b.username]
        if not others:
            return f"💡 Полный разбор твоего контента и идеи для съёмок — @{MAIN_BOT}" if MAIN_BOT else ""
        b = others[int(time.time()) % len(others)]
        return f"💡 Ещё пригодится: {b.emoji} @{b.username} — {b.pitch}"

    def footer_hint(self) -> str:
        if not PAYWALL:
            return ""
        return f"\n\nПервые {FREE_TRIES} — бесплатно."

    def network_text(self) -> str:
        lines = [f"Боты сети {NET_NAME} — для тех, кто снимает и ведёт соцсети:\n"]
        for b in network.live():
            if b.username:
                lines.append(f"{b.emoji} @{b.username} — {b.pitch}")
        if MAIN_BOT:
            lines.append(f"\n🧠 @{MAIN_BOT} — главный: разбор твоего канала и роликов, идеи и план съёмок.")
        return "\n".join(lines)

    def _feedback(self, ctx, data):
        _, up, kind = (data.split(":") + ["", ""])[:3]
        store.hincr(f"net:day:{today()}", f"{self.id}:fb_{'up' if up == '1' else 'down'}")
        if up == "1":
            ctx.send("Спасибо! 🙌")
        else:
            store.put(f"b:{self.id}:fbwait:{ctx.uid}", "1", 3600)
            ctx.send("Что было не так? Напиши одним сообщением — передам автору бота.")

    # ----- оплата (СБП, как в Improve Lab) -----
    def show_sub(self, ctx):
        until = sub_until(ctx.uid)
        if until > time.time():
            return ctx.send(f"Подписка активна до {time.strftime('%d.%m.%Y', time.localtime(until))} ✅\n\n{self.sub_text()}")
        self.show_pay(ctx)

    def show_pay(self, ctx):
        if not PAY_DETAILS:
            return ctx.send("Оплата скоро появится 🙌")
        ctx.send(f"{self.sub_text()}\n\nВыбери срок:\n• Месяц — {NET_PRICE} ₽\n"
                 f"• Год — {price('y')} ₽ (12 месяцев по цене {YEAR_MONTHS}) 🎁",
                 [[{"text": f"Месяц — {NET_PRICE} ₽", "callback_data": "pay_m"}],
                  [{"text": f"Год — {price('y')} ₽ 🎁", "callback_data": "pay_y"}]])

    def show_pay_details(self, ctx, period):
        ctx.send(f"Подписка на {PERIODS[period][2]} — {price(period)} ₽.\n\n"
                 f"1. Переведи {price(period)} ₽ по СБП: {PAY_DETAILS}\n"
                 f"2. В комментарии к переводу напиши код: {pay_code(ctx.uid, period)}\n"
                 "3. Нажми «Я оплатил» и пришли скриншот перевода.\n\n"
                 "Подписку включаю вручную после проверки — обычно в течение пары часов.",
                 [[{"text": "✅ Я оплатил", "callback_data": f"paid_{period}"}]])

    def _payment_proof(self, ctx, msg):
        period = store.get(f"net:paywait:{ctx.uid}") or "m"
        store.put(f"net:paywait:{ctx.uid}", "", 1)
        info = (f"💳 Оплата подписки сети ({PERIODS[period][2]})\nБот: {self.emoji} {self.title}\n"
                f"От: {who(ctx.user)} (id {ctx.uid})\nКод: {pay_code(ctx.uid, period)}\nСумма: {price(period)} ₽")
        kb = [[{"text": "✅ Подтвердить", "callback_data": f"ok:{ctx.uid}:{period}"},
               {"text": "❌ Отклонить", "callback_data": f"no:{ctx.uid}:{period}"}]]
        for a in ADMIN_IDS:
            self.tg.api("forwardMessage", chat_id=a, from_chat_id=ctx.chat_id, message_id=msg["message_id"])
            self.tg.send(a, info, kb)
        ctx.send("Спасибо! Проверяю перевод и включу подписку — пришлю сообщение 🙌")

    def _admin_decide(self, ctx, data):
        if ctx.uid not in ADMIN_IDS:
            return
        parts = data.split(":")
        uid = int(parts[1])
        period = parts[2] if len(parts) > 2 and parts[2] in PERIODS else "m"
        if parts[0] == "ok":
            d = extend_sub(uid, PERIODS[period][0])
            store.hincr(f"net:day:{today()}", "paid")
            self.tg.send(uid, f"🔥 Подписка активна до {d}. Открыты все боты сети:\n\n{self.network_text()}")
            ctx.send(f"✅ Подписка {uid} ({PERIODS[period][2]}) до {d}. Не забудь чек в «Мой налог».")
        else:
            self.tg.send(uid, "Не нашёл перевод 😕 Проверь сумму и код в комментарии и пришли скриншот ещё раз.",
                         [[{"text": "💳 Оплатить", "callback_data": "pay"}]])
            ctx.send(f"❌ Оплата {uid} отклонена.")


# ---------------- вечерняя сводка по сети ----------------

def daily_summary_loop():
    while True:
        try:
            t = msk()
            day = time.strftime("%Y-%m-%d", t)
            if t.tm_hour >= 21 and store.get("net:summary_sent") != day:
                store.put("net:summary_sent", day)
                d = store.hall(f"net:day:{day}")
                lines = [f"📊 Сеть ботов за {time.strftime('%d.%m', t)}",
                         f"Всего людей в сети: {store.set_size('net:users')} · оплат сегодня: {d.get('paid', 0)}"]
                for b in network.live():
                    g = lambda k: int(d.get(f"{b.id}:{k}", 0))  # noqa: E731
                    lines.append(f"{b.emoji} {b.title}: новых {g('new')}, запросов {g('use')}, "
                                 f"👍 {g('fb_up')} / 👎 {g('fb_down')} · всего {store.set_size(f'b:{b.id}:users')}")
                network.notify_admins("\n".join(lines))
        except Exception as e:
            log.warning("summary failed: %s", e)
        time.sleep(300)
