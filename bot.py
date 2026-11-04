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

pool = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "4")))
busy: set[int] = set()
usage: dict[tuple[int, str], int] = {}
last_channel: dict[int, str] = {}
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

def _users_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, "users.json")


def track(user: dict, event: str):
    with lock:
        path = _users_path()
        try:
            with open(path) as f:
                db = json.load(f)
        except Exception:
            db = {}
        u = db.setdefault(str(user["id"]), {"name": user.get("username") or user.get("first_name"), "first": time.time()})
        u[event] = u.get(event, 0) + 1
        u["last"] = time.time()
        with open(path, "w") as f:
            json.dump(db, f, ensure_ascii=False)


def take_quota(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    k = (uid, date.today().isoformat())
    with lock:
        if usage.get(k, 0) >= DAILY_LIMIT:
            return False
        usage[k] = usage.get(k, 0) + 1
        return True


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

Смотрю статистику Telegram-канала и говорю, что постить, чтобы росли просмотры.

Как пользоваться:
• Пришли @канал или ссылку t.me/... — сделаю разбор: что заходит, что нет, 5 идей постов и лучшее время.
• /compare @мой @конкурент1 @конкурент2 — сравню с конкурентами и скажу, что у них забрать.
• /plan — контент-план на неделю для последнего канала.

Работаю с публичными каналами. Попробуй — пришли свой канал 👇"""

HELP = START


def channel_keyboard(name: str):
    return [
        [{"text": "📅 План на неделю", "callback_data": f"plan:{name}"}],
        [{"text": "⚔️ Сравнить с конкурентами", "callback_data": f"cmp:{name}"}],
    ]


# ---------------- сценарии ----------------

def run_job(chat_id: int, user: dict, fn, *args):
    uid = user["id"]
    if uid in busy:
        send(chat_id, "Ещё разбираю прошлый запрос — секунду ⏳")
        return
    if not take_quota(uid):
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


def job_report(chat_id, user, name):
    send(chat_id, f"Смотрю @{name}… обычно это 20–40 секунд ⏳")
    typing(chat_id)
    a = get_channel(name)
    if not a["ok"]:
        send(chat_id, f"У @{name} не нашёл постов с просмотрами — нечего анализировать.")
        return
    last_channel[user["id"]] = name
    track(user, "report")
    if not ai.enabled():
        send(chat_id, stats_text(a) + "\n\n(ИИ-разбор пока выключен — нет ключа)", channel_keyboard(name))
        return
    send(chat_id, stats_text(a))
    typing(chat_id)
    text = ai.ask(ai.report_prompt(compact_for_ai(a)))
    send(chat_id, text, channel_keyboard(name))


def job_compare(chat_id, user, mine, rivals):
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
    last_channel[user["id"]] = mine
    track(user, "compare")
    table = ["Коротко в цифрах (медиана просмотров · постов в неделю):"]
    for x in [a_mine] + a_riv:
        reach = f" · охват {x['reach_pct']}%" if x["reach_pct"] is not None else ""
        table.append(f"@{x['username']}: {x['median_views']} · {x['posts_per_week']}/нед{reach}")
    send(chat_id, "\n".join(table))
    if not ai.enabled():
        return
    typing(chat_id)
    text = ai.ask(ai.compare_prompt(compact_for_ai(a_mine), [compact_for_ai(x) for x in a_riv]), max_tokens=2200)
    send(chat_id, text, [[{"text": "📅 План на неделю", "callback_data": f"plan:{mine}"}]])


def job_plan(chat_id, user, name):
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
    send(chat_id, ai.ask(ai.plan_prompt(compact_for_ai(a))))


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


def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {"id": chat_id}
    text = (msg.get("text") or "").strip()
    if not text:
        send(chat_id, "Пришли @канал или ссылку t.me/...")
        return
    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

    if cmd == "/start":
        track(user, "start")
        send(chat_id, START)
        return
    if cmd == "/help":
        send(chat_id, HELP)
        return
    if cmd == "/stats" and user["id"] in ADMIN_IDS:
        try:
            with open(_users_path()) as f:
                db = json.load(f)
        except Exception:
            db = {}
        reports = sum(u.get("report", 0) + u.get("compare", 0) + u.get("plan", 0) for u in db.values())
        send(chat_id, f"Пользователей: {len(db)}\nРазборов всего: {reports}")
        return

    chans = extract_channels(text[len(cmd):] if cmd else text)

    if cmd == "/plan":
        name = chans[0] if chans else last_channel.get(user["id"])
        if not name:
            send(chat_id, "Сначала пришли канал, или так: /plan @канал")
            return
        run_job(chat_id, user, job_plan, name)
        return

    if cmd == "/compare" or (not cmd and len(chans) >= 2):
        if len(chans) < 2:
            send(chat_id, "Формат: /compare @мой_канал @конкурент1 @конкурент2\n(первым — твой, до 3 конкурентов)")
            return
        run_job(chat_id, user, job_compare, chans[0], chans[1 : 1 + MAX_RIVALS])
        return

    if cmd:
        send(chat_id, "Не знаю такую команду. /help — что я умею.")
        return
    if not chans:
        send(chat_id, "Не вижу канала. Пришли @ник или ссылку t.me/...")
        return
    run_job(chat_id, user, job_report, chans[0])


pending_compare: dict[int, str] = {}


def handle_callback(cb: dict):
    api("answerCallbackQuery", callback_query_id=cb["id"])
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    user = cb["from"]
    if data.startswith("plan:"):
        run_job(chat_id, user, job_plan, data[5:])
    elif data.startswith("cmp:"):
        name = data[4:]
        send(chat_id, f"Пришли одним сообщением 1–3 канала конкурентов, например:\n@{name} @конкурент1 @конкурент2")


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
            {"command": "plan", "description": "Контент-план на неделю"},
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
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        run_webhook(base, int(os.getenv("PORT", "10000")))
    else:
        run_polling()


if __name__ == "__main__":
    main()
