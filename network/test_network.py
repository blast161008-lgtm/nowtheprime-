"""Оффлайн-проверка сети ботов: Telegram и ИИ подменены, ffmpeg — настоящий.
Запуск: cd network && python test_network.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

os.environ.update({"ADMIN_IDS": "1", "PAYWALL": "on", "FREE_TRIES": "3", "PAY_DETAILS": "+7 900 000-00-00, Банк, Тест",
                   "AI_API_KEY": "test", "TOKEN_HOOKS": "111:aaa", "TOKEN_CAPTIONS": "222:bbb", "TOKEN_SUBS": "333:ccc"})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import ai, media, store  # noqa: E402
from core import tg as tgmod  # noqa: E402
from core.bot import network, subscribed  # noqa: E402

SENT = []          # (bot_token, method, params)
FILES = []         # (bot_token, method, filename, size)
VIDEO = None


def fake_api(self, method, **p):
    SENT.append((self.token, method, p))
    if method == "getMe":
        return {"username": {"111:aaa": "test_hooks_bot", "222:bbb": "test_captions_bot", "333:ccc": "test_subs_bot"}[self.token]}
    if method == "getFile":
        return {"file_path": "x"}
    return {"ok": True}


def fake_send_file(self, chat_id, method, field, data, filename, mime, caption="", keyboard=None):
    FILES.append((self.token, method, filename, len(data), caption, keyboard))
    return {"ok": True}


tgmod.TG.api = fake_api
tgmod.TG.send_file = fake_send_file
tgmod.TG.download = lambda self, fid: VIDEO
PROMPTS = []
ai.ask = lambda prompt, system="", **kw: (PROMPTS.append((prompt, kw)) or f"ОТВЕТ ИИ ({kw.get('tag')}) heavy={kw.get('heavy')}")
ai.transcribe_segments = lambda mp3, language="ru": [
    {"start": 0.2, "end": 2.6, "text": "Привет, сегодня покажу три ошибки новичков в хип-хопе"},
    {"start": 2.8, "end": 4.6, "text": "и как их исправить за неделю"}]

import main  # noqa: E402

main.load_bots()
hooks, captions, subs = network.bots
OK = []


def check(cond, name):
    print(("✅ " if cond else "❌ ") + name)
    OK.append(bool(cond))


def texts(token, chat=None):
    return [p.get("text", "") for t, m, p in SENT if t == token and m == "sendMessage" and (chat is None or p["chat_id"] == chat)]


def last_kb(token, chat):
    for t, m, p in reversed(SENT):
        if t == token and m == "sendMessage" and p["chat_id"] == chat and p.get("reply_markup"):
            return p["reply_markup"]["inline_keyboard"]


def msg(bot, uid, **m):
    bot.handle_update({"message": {"message_id": int(time.time() * 1000) % 10**9, "from": {"id": uid, "first_name": f"U{uid}"},
                                   "chat": {"id": uid, "type": "private"}, **m}})


def btn(bot, uid, data):
    bot.handle_update({"callback_query": {"id": "c", "from": {"id": uid, "first_name": f"U{uid}"}, "data": data,
                                          "message": {"chat": {"id": uid}}}})


def wait():
    for _ in range(300):
        from core.bot import _busy
        if not _busy:
            time.sleep(0.05)
            if not _busy:
                return
        time.sleep(0.05)


# ---------- запуск ----------
check(all(b.username for b in network.live()), "все 3 бота запущены, имена получены")
cmds = [p for t, m, p in SENT if m == "setMyCommands"]
check(len(cmds) == 3 and any(c["command"] == "more" for c in cmds[0]["commands"]), "команды /more и /sub у всех ботов")

# ---------- хуки: 3 бесплатно, потом оплата ----------
U = 100
msg(hooks, U, text="/start")
check("хуки" in texts("111:aaa", U)[-1].lower() and "бесплатно" in texts("111:aaa", U)[-1], "хуки: приветствие + бесплатные попытки")
check(any("🆕" in t for t in texts("111:aaa", 1)), "админу пришло «новый пользователь»")
msg(hooks, U, text="3 ошибки новичков в хип-хопе")
wait()
check(any("ОТВЕТ ИИ (hooks) heavy=True" in t for t in texts("111:aaa", U)), "хуки: первый ответ — лучшая модель")
kb = last_kb("111:aaa", U) or []
check(any(b["callback_data"] == "hooks_more" for row in kb for b in row), "хуки: кнопка «Ещё варианты»")
btn(hooks, U, "hooks_more")
wait()
check(PROMPTS[-1][1].get("heavy") is False and "НЕ повторяй" in PROMPTS[-1][0], "«Ещё» — дешёвая модель и без повторов")
msg(hooks, U, text="как я похудела на 5 кг танцами")
wait()
check(any("последняя бесплатная" in t for t in texts("111:aaa", U)), "предупреждение о последней бесплатной")
msg(hooks, U, text="четвёртая тема")
wait()
check("закончились" in texts("111:aaa", U)[-1] and "Субтитры" in texts("111:aaa", U)[-1], "4-я попытка → платёжный экран со всей сетью")

# оплата по СБП → админ подтверждает → открыто
btn(hooks, U, "pay")
btn(hooks, U, "pay_m")
check("NB" in texts("111:aaa", U)[-1] and "199" in texts("111:aaa", U)[-1], "реквизиты и код NB…")
btn(hooks, U, "paid_m")
msg(hooks, U, photo=[{"file_id": "p1"}])
check(any(m == "forwardMessage" and p["chat_id"] == 1 for t, m, p in SENT), "скриншот переслан админу")
check(any("Оплата подписки сети" in t for t in texts("111:aaa", 1)), "админу — кнопки подтверждения")
btn(hooks, 1, f"ok:{U}:m")
check(subscribed(U), "после ✅ подписка активна")
msg(captions, U, text="Пост о том, как я провела первый мастер-класс по вогу")
wait()
check(any("ОТВЕТ ИИ (captions)" in t for t in texts("222:bbb", U)), "подписка действует и в другом боте сети")

# ---------- подписи: стиль, голосовое ----------
V = 200
msg(captions, V, text="/style")
msg(captions, V, text="Девочки, это было мощно 🔥 Короче, пришла я на первую тренировку...")
check(store.get(f"b:captions:style:{V}") is not None, "подписи: стиль сохранён")
msg(captions, V, text="Рилс про то, как выбрать первые кроссовки для танцев")
wait()
check("ПРИМЕРЫ ПОДПИСЕЙ АВТОРА" in PROMPTS[-1][0], "подписи: стиль автора в промпте")
kb = last_kb("222:bbb", V) or []
check(any(b["callback_data"] == "cap:tg" for row in kb for b in row), "подписи: кнопки других площадок")

# ---------- субтитры: настоящий ffmpeg ----------
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "v.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x334455:s=360x640:d=5", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=5", "-shortest", "-c:v", "libx264", "-c:a", "aac", path],
                   capture_output=True, check=True)
    VIDEO = open(path, "rb").read()
W = 300
msg(subs, W, video={"file_id": "v1", "file_size": len(VIDEO), "duration": 5})
wait()
vids = [f for f in FILES if f[0] == "333:ccc" and f[1] == "sendVideo"]
check(vids and vids[-1][3] > 10_000, f"субтитры: видео с вшитыми субтитрами отправлено ({vids[-1][3] if vids else 0} байт)")
btn(subs, W, "subs:srt")
srt = [f for f in FILES if f[2] == "subtitles.srt"]
check(srt, "субтитры: SRT-файл по кнопке")
srt_text = store.get(f"b:subs:srt:{W}")
check(srt_text and "-->" in srt_text and srt_text.count("-->") >= 3, "SRT разбит на короткие строки для вертикального видео")
free_before = store.get(f"b:subs:free:{W}")
btn(subs, W, "subs:yellow")
wait()
check(len([f for f in FILES if f[1] == "sendVideo"]) == 2 and store.get(f"b:subs:free:{W}") == free_before,
      "смена стиля — новое видео без списания попытки")
msg(subs, W, video={"file_id": "big", "file_size": 30 * 1024 * 1024, "duration": 30})
check("20 МБ" in texts("333:ccc", W)[-1], "видео больше 20 МБ — понятный отказ")

# ---------- 👎 с комментарием → админу ----------
btn(subs, W, "fb:0:subs")
msg(subs, W, text="Слово «хип-хоп» распозналось неправильно")
check(any("👎" in t and "хип-хоп" in t for t in texts("333:ccc", 1)), "👎 с комментарием дошёл до админа")

# ---------- перекрёстные ссылки ----------
net = hooks.network_text()
check("@test_captions_bot" in net and "@test_subs_bot" in net and "@Improvelab_bot" in net, "/more: все боты сети + Improve Lab")

# ---------- webhook: один сервер, у каждого бота свой путь ----------
os.environ["PORT"] = "18765"
th = threading.Thread(target=main.run_webhook, args=("https://example.test", 18765), daemon=True)
th.start()
time.sleep(0.5)
path, secret = main.secrets(hooks)
before = len(texts("111:aaa", 400))
req = urllib.request.Request("http://127.0.0.1:18765" + path, method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": secret},
                             data=json.dumps({"message": {"message_id": 1, "from": {"id": 400}, "chat": {"id": 400, "type": "private"},
                                                          "text": "/start"}}).encode())
urllib.request.urlopen(req).read()
time.sleep(0.5)
check(len(texts("111:aaa", 400)) > before, "webhook доставил /start нужному боту")
try:
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18765" + path, method="POST", data=b"{}"))
    bad = False
except urllib.error.HTTPError as e:
    bad = e.code == 403
check(bad, "чужой запрос без секрета отклонён (403)")
hooks_paths = [p["url"] for t, m, p in SENT if m == "setWebhook"]
check(len(set(hooks_paths)) == 3, "у каждого бота свой адрес webhook")

print(f"\n{sum(OK)} из {len(OK)} проверок прошли")
sys.exit(0 if all(OK) else 1)
