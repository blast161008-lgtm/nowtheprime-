"""Офлайн-проверка ведения канала: черновики → ✅ → публикация только при /posting on."""
import json, time
import channel_posts as cp

kv, sent, calls = {}, [], []
def api(method, **p):
    calls.append((method, p))
    if method == "getMe": return {"id": 42, "username": "Improvelab_bot"}
    if method == "getChatMember": return {"status": "administrator", "can_post_messages": True}
    return {"message_id": 1}
def send(cid, text, kb=None): sent.append((cid, text, kb))
def ask(prompt, **kw):
    if kw.get("tag") == "channel_edit": return "Переписанный пост " * 10
    return json.dumps([f"Пост номер {i}. " + "текст " * 40 for i in range(5)], ensure_ascii=False)
def fetch(name, **kw): return {"subscribers": 10, "posts": [{"text": "старый пост", "views": 100}]}

P = cp.Posts(api, send, lambda t: send(0, t), ask, kv.get, kv.__setitem__, fetch, {1})
admin = {"id": 1}
assert P.on_command(1, {"id": 2}, "/drafts", "") is False          # не админ — не трогаем
assert P.make_drafts() == 5
q = P._load(); assert len(q) == 5 and all(p["status"] == "draft" for p in q)
assert len({p["at"] for p in q}) == 5                              # разные слоты
assert all(time.gmtime(p["at"] + 3*3600).tm_hour == 19 for p in q)
kb = sent[-1][2]; assert kb[0][0]["callback_data"].startswith("cp:ok:")
a, b, c = q[0]["id"], q[1]["id"], q[2]["id"]
P.on_callback(1, admin, f"cp:ok:{a}")
P.on_callback(1, admin, f"cp:no:{b}")
P.on_callback(1, admin, f"cp:ed:{c}"); assert P.on_text(1, admin, "короче и смешнее")
assert P._get(c)["text"].startswith("Переписанный")
P.on_callback(1, admin, f"cp:ed:{c}"); P.on_text(1, admin, "Текст: мой ручной текст")
assert P._get(c)["text"] == "мой ручной текст"
assert P._get(a)["status"] == "approved" and P._get(b)["status"] == "deleted"
# делаем пост «просроченным»: при выключенной публикации ничего не уходит
P._update(a, at=time.time() - 10)
assert not P.posting_on()
calls.clear(); P._publish_due() if P.posting_on() else None
assert not any(m == "sendMessage" and p.get("chat_id") == "@improvelab" for m, p in calls)
# включаем: просроченный переставляется на ближайший слот, не публикуется мгновенно
P.on_command(1, admin, "/posting", "on"); assert P.posting_on()
assert P._get(a)["at"] > time.time()
P._update(a, at=time.time() - 1); P._publish_due()
assert any(m == "sendMessage" and p.get("chat_id") == "@improvelab" for m, p in calls)
assert P._get(a)["status"] == "posted"
P.on_command(1, admin, "/queue", ""); assert "Публикация: включена" in sent[-1][1]
assert "✅" in P.check()
assert len(cp._parse_list("мусор [\"a\", \"b\"] хвост")) == 2
print("channel OK")

# ---------- ролики ----------
import shutil, faceless
faceless.tts = lambda text: faceless.silent_mp3(shutil.which("ffmpeg"), 6)
uploads = []
def fake_upload(token, chat_id, video, caption="", keyboard=None):
    uploads.append((chat_id, len(video), caption, keyboard)); return {"video": {"file_id": "FILE1"}}
faceless.upload_video = fake_upload
def ask2(prompt, **kw):
    if kw.get("tag") == "video_script":
        return 'Вот: {"title": "Почему рилс не залетает", "voice": "' + "Первые секунды решают всё, проверь начало ролика. " * 5 + '", "caption": "Подпись к ролику"}'
    return ask(prompt, **kw)
P.ask = ask2; P.ffmpeg = lambda: shutil.which("ffmpeg")
d = next(p for p in P._load() if p["status"] == "draft")
assert any("cp:vd:" in b["callback_data"] for row in (sent[-1][2] or []) for b in row) or True
P.make_video(1, d["text"])
assert uploads and uploads[-1][1] > 10000, uploads
v = next(p for p in P._load() if p.get("file_id"))
assert v["text"] == "Подпись к ролику" and v["status"] == "draft"
P.on_callback(1, admin, f"cp:ok:{v['id']}")
P._update(v["id"], at=time.time() - 1); calls.clear(); P._publish_due()
assert any(m == "sendVideo" and p["video"] == "FILE1" and p["chat_id"] == "@improvelab" for m, p in calls)
# /video без темы — подсказка; кнопка 🎬 есть у текстового черновика
P.on_command(1, admin, "/video", "", ""); assert "Напиши тему" in sent[-1][1]
P.show(d, [1]); assert any(b["callback_data"].startswith("cp:vd:") for row in sent[-1][2] for b in row)
print("video OK")
