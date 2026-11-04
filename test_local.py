"""Локальный прогон без сети: синтетический t.me/s HTML + фейковые Telegram и ИИ."""
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TEST")
os.environ["DATA_DIR"] = "/tmp/aiproducer_test_data"

import tg_parser
from analyzer import analyze, compact_for_ai, stats_text

random.seed(1)
NOW = datetime.now(timezone.utc)


def make_post(ch, pid, i):
    kind = ["photo", "video", "text", "text", "roundvideo"][i % 5]
    views = {"photo": 2400, "video": 4100, "text": 1500, "roundvideo": 3000}[kind] + random.randint(-400, 400)
    dt = NOW - timedelta(hours=10 + i * 20)
    media = {
        "photo": '<a class="tgme_widget_message_photo_wrap" style="x"></a>',
        "video": '<div class="tgme_widget_message_video_player"></div>',
        "roundvideo": '<div class="tgme_widget_message_roundvideo_player"></div>',
        "text": "",
    }[kind]
    body = f"Пост номер {pid} &amp; немного текста<br/>вторая строка" + (" длинно" * (i * 15))
    v = f"{views / 1000:.1f}K" if views >= 1000 else str(views)
    return f"""<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{ch}/{pid}" data-view="x">
<div class="tgme_widget_message_bubble">{media}
<div class="tgme_widget_message_text js-message_text" dir="auto">{body}</div>
<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short js-message_info">
<span class="tgme_widget_message_views">{v}</span><span class="copyonclick">,</span>
<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/{ch}/{pid}"><time datetime="{dt.isoformat()}" class="time">12:00</time></a></span>
</div></div></div></div></div>"""


def make_page(ch, before=None, total=70):
    top = total if before is None else before - 1
    ids = [i for i in range(top, max(top - 20, 0), -1)]
    header = f"""<div class="tgme_channel_info_header_title"><span dir="auto">Канал {ch}</span></div>
<div class="tgme_channel_info_description">Про танцы и бренд</div>
<div class="tgme_channel_info_counters"><div class="tgme_channel_info_counter"><span class="counter_value">12.4K</span> <span class="counter_type">subscribers</span></div></div>"""
    posts = "".join(make_post(ch, pid, total - pid) for pid in reversed(ids))
    return header + posts


def fake_fetch(url):
    m = re.match(r"https://t.me/s/(\w+)(?:\?before=(\d+))?", url)
    ch, before = m.group(1), m.group(2)
    if ch == "nonexistent":
        return "<html>nothing</html>"
    return make_page(ch, int(before) if before else None)


# ---- 1. парсер ----
assert tg_parser.normalize_channel("https://t.me/s/nik_tik/123") == "nik_tik"
assert tg_parser.normalize_channel("@durov") == "durov"
assert tg_parser.normalize_channel("t.me/durov") == "durov"
assert tg_parser.normalize_channel("привет") is None
assert tg_parser.parse_count("1.2K") == 1200 and tg_parser.parse_count("3M") == 3_000_000

info = tg_parser.fetch_channel("testchan", fetch=fake_fetch, pause=0)
assert len(info["posts"]) == 70, len(info["posts"])
assert info["subscribers"] == 12400 and info["title"] == "Канал testchan"
p = info["posts"][0]
assert "&" in p["text"] and "\n" in p["text"], p["text"][:80]
assert {x["type"] for x in info["posts"]} == {"фото", "видео", "текст", "кружок"}
print("parser OK:", len(info["posts"]), "posts")

# ---- 2. аналитика ----
a = analyze(info)
assert a["ok"] and a["posts_analyzed"] == 69  # самый свежий (<24ч) исключён
assert a["by_type"][0]["key"] == "видео"
print("\n" + stats_text(a))
print("\n--- выжимка для ИИ (начало) ---\n" + compact_for_ai(a)[:700])

# ---- 3. бот целиком с фейковым Telegram и ИИ ----
import ai
import bot

sent = []
bot.api = lambda method, **kw: sent.append((method, kw)) or {"username": "test"}
bot.fetch_channel = lambda name: tg_parser.fetch_channel(name, fetch=fake_fetch, pause=0)
os.environ["AI_API_KEY"] = "fake"
ai.ask = lambda prompt, max_tokens=0: f"[ИИ-ответ на промпт длиной {len(prompt)} знаков]"


def wait():
    for _ in range(100):
        if not bot.busy:
            return
        time.sleep(0.05)


def msg(text, uid=1):
    bot.handle_message({"chat": {"id": uid}, "from": {"id": uid, "username": "nik"}, "text": text})
    time.sleep(0.1)
    wait()


def texts():
    return [kw.get("text") for m, kw in sent if m == "sendMessage"]


msg("/start")
assert "ИИ-продюсер" in texts()[-1]
msg("https://t.me/testchan")
assert any("Подписчиков: 12.4K" in t for t in texts()), texts()
assert "ИИ-ответ" in texts()[-1]
msg("/plan")
assert "ИИ-ответ" in texts()[-1]
msg("/compare @testchan @rival1 @nonexistent")
assert any("Не смог открыть: @nonexistent" in t for t in texts())
assert any("@rival1:" in t for t in texts())
msg("абракадабра 123")
assert "Не вижу канала" in texts()[-1]
msg("@nonexistent")
assert "Не получилось открыть канал" in texts()[-1]
for _ in range(6):
    msg("@testchan", uid=2)
assert "лимит" in texts()[-1], texts()[-1]
print("\nbot flow OK, messages sent:", len(texts()))
print("\nПример ответа на /compare:\n" + [t for t in texts() if "Коротко в цифрах" in t][0])
sys.exit(0)
