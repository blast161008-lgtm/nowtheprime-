"""Ведение канала Improve Lab (@improvelab) самим ботом.

Как работает:
- Claude пишет черновики постов на неделю (по кнопке /drafts или сам по воскресеньям, когда публикация включена),
  учитывая, какие прошлые посты канала набрали больше просмотров;
- каждый черновик приходит админу с кнопками ✅ В очередь / ✏️ Переписать / ❌ Удалить;
- одобренные посты бот сам публикует в канал в своё время (по умолчанию 19:00 МСК, пн–пт);
- публикация выключена, пока админ не напишет /posting on (сначала — запуск ролика).

Честность: никаких выдуманных отзывов, цифр, клиентов и результатов — только факты о продукте ниже.
Ролики без лица: кнопка 🎬 Видео под черновиком или /video <тема> — сценарий, ИИ-голос, вертикальное видео
с субтитрами; ролик приходит админу на ✅ и публикуется в канал как видео с подписью (его же можно скачать для рилс).
Команды админа: /drafts, /video <тема>, /queue, /posting on|off, /channel_check.
"""
import json
import logging
import os
import re
import threading
import time
import uuid

import faceless

log = logging.getLogger("channel")

CHANNEL = os.getenv("CONTENT_CHANNEL", "improvelab").lstrip("@")
POST_HOUR = int(os.getenv("POST_HOUR", "19"))             # время публикации, МСК
POST_DAYS = [int(d) for d in os.getenv("POST_DAYS", "0,1,2,3,4").split(",")]  # 0 = понедельник
DRAFTS_PER_WEEK = int(os.getenv("DRAFTS_PER_WEEK", "5"))

FACTS = """ПРОДУКТ: @Improvelab_bot — ИИ-продюсер в Telegram для авторов, которые сами ведут соцсети.
Что умеет (только это, ничего не добавляй):
- разбор Telegram-канала за 6 месяцев: какие посты и форматы заходят, отчёт и выводы;
- идеи постов и роликов под нишу автора на основе того, что реально залетает в нише (поиск по нише);
- план контента на неделю: сначала спрашивает, что у автора происходит на неделе, ничего не выдумывает;
- сравнение с конкурентами: у кого что заходит;
- разбор видео канала;
- проверка рилс перед публикацией — ИИ смотрит видео со звуком и говорит, что поправить в первых секундах;
- разбор Instagram/TikTok по своим рилс (что залетело, что нет, почему) и по скриншотам статистики;
- кнопки ❤️ под идеями — бот учится вкусу автора.
Цена: 279 ₽ в месяц. Бесплатно: попробовать каждую функцию по одному разу.
Создатель: Никита — танцор и хореограф, сам ведёт свои соцсети и бренд одежды; сделал бота сначала для себя.
Аудитория: эксперты и мастера, которые продают услуги через соцсети (хореографы, тренеры, бьюти, репетиторы,
психологи), небольшие бренды, начинающие блогеры; 500–20 000 подписчиков, ведут всё сами без SMM."""

RUBRICS = [
    "Совет автору: один конкретный приём (хук первых секунд, структура поста, частота, формат) с примером",
    "Разбор ошибки: частая ошибка авторов и как исправить, с примером «было → стало»",
    "Как пользоваться ботом: одна функция, по шагам, что получишь (с призывом попробовать бесплатно)",
    "Миф о соцсетях и правда о нём",
    "История создателя: почему Никита сделал бота, что понял, ведя свои соцсети (без выдуманных фактов)",
    "Пример: как выглядит разбор / идеи / проверка рилс — пример ПОМЕЧЕН как пример, без выдуманных результатов",
    "Вопрос аудитории: короткий пост-вопрос, чтобы люди написали в комментариях",
    "Продающий пост: что даёт подписка за 279 ₽ и чем отличается от «спросить ChatGPT»",
]

RULES = """ПРАВИЛА:
- Пиши по-русски, живо и просто, как человек, а не как ИИ. Без markdown (никаких **, #, таблиц). Эмодзи — 0–3 на пост.
- Длина 400–1200 знаков. Первая строка — цепляет.
- НЕ выдумывай отзывы, клиентов, цифры роста, кейсы «у Маши выросли охваты на 300%». Если нужен пример —
  прямо напиши «например» и сделай его иллюстрацией, а не фактом.
- Не обещай охваты и продажи. Не используй штампы: «погрузимся», «магия», «секрет успеха», «друзья», «не забудьте подписаться».
- Призыв к действию — один и простой (попробовать бесплатно в @Improvelab_bot, ответить в комментариях).
- Не повторяй темы прошлых постов канала."""


class Posts:
    def __init__(self, api, send, notify_admins, ask, kv_get, kv_set, fetch_channel, admin_ids,
                 token: str = "", ffmpeg=lambda: None):
        self.api, self.send, self.notify, self.ask = api, send, notify_admins, ask
        self.kv_get, self.kv_set, self.fetch_channel, self.admin_ids = kv_get, kv_set, fetch_channel, admin_ids
        self.token, self.ffmpeg = token, ffmpeg
        self.lock = threading.Lock()
        self.video_busy = threading.Lock()   # один ролик за раз — бесплатный сервер слабый
        self.wait_edit: dict[int, str] = {}   # админ → id черновика, который он правит

    # ---------- хранилище ----------
    def _load(self) -> list:
        try:
            return json.loads(self.kv_get("cp:queue") or "[]")
        except Exception:
            return []

    def _save(self, items: list):
        self.kv_set("cp:queue", json.dumps(items[-60:], ensure_ascii=False))

    def _get(self, pid):
        return next((p for p in self._load() if p["id"] == pid), None)

    def _update(self, pid, **fields):
        with self.lock:
            items = self._load()
            for p in items:
                if p["id"] == pid:
                    p.update(fields)
            self._save(items)

    def posting_on(self) -> bool:
        v = self.kv_get("cp:posting")
        return (v or os.getenv("CHANNEL_POSTING", "off")) == "on"

    # ---------- время публикации ----------
    def next_slots(self, n: int, after: float | None = None) -> list[float]:
        """Ближайшие свободные слоты: дни POST_DAYS в POST_HOUR:00 МСК, не раньше чем через час."""
        taken = {p["at"] for p in self._load() if p["status"] in ("approved", "draft")}
        t = max(after or 0, time.time() + 3600)
        out = []
        day = int((t + 3 * 3600) // 86400)
        while len(out) < n:
            ts = day * 86400 + POST_HOUR * 3600 - 3 * 3600
            wd = time.gmtime(ts + 3 * 3600).tm_wday
            if ts >= t and wd in POST_DAYS and ts not in taken:
                out.append(ts)
            day += 1
        return out

    @staticmethod
    def when(ts) -> str:
        days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        g = time.gmtime(ts + 3 * 3600)
        return f"{days[g.tm_wday]} {time.strftime('%d.%m %H:%M', g)} МСК"

    # ---------- черновики ----------
    def channel_context(self) -> str:
        """Прошлые посты канала с просмотрами — чтобы не повторяться и опираться на то, что зашло."""
        try:
            data = self.fetch_channel(CHANNEL, days=90, max_posts=40)
        except Exception as e:
            return f"(канал пока пустой или недоступен: {e})"
        posts = [p for p in data.get("posts", []) if (p.get("text") or "").strip()]
        if not posts:
            return "(в канале ещё нет постов)"
        posts.sort(key=lambda p: -(p.get("views") or 0))
        lines = [f"- {p.get('views') or 0} просмотров: {p['text'][:160].replace(chr(10), ' ')}" for p in posts[:20]]
        return f"Подписчиков: {data.get('subscribers') or '?'}\nПрошлые посты (по просмотрам):\n" + "\n".join(lines)

    def make_drafts(self, n: int = DRAFTS_PER_WEEK) -> int:
        used = self.kv_get("cp:last_rubrics") or ""
        rubrics = [r for r in RUBRICS if r[:20] not in used][:n]
        if len(rubrics) < n:
            rubrics += [r for r in RUBRICS if r not in rubrics][: n - len(rubrics)]
        self.kv_set("cp:last_rubrics", "|".join(r[:20] for r in rubrics))
        prompt = (f"Ты ведёшь Telegram-канал @{CHANNEL} продукта Improve Lab.\n\n{FACTS}\n\n{self.channel_context()}\n\n{RULES}\n\n"
                  f"Напиши {n} готовых постов, по одному на каждую рубрику:\n"
                  + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rubrics))
                  + "\n\nОтвет — только JSON-массив строк (тексты постов), без пояснений.")
        raw = self.ask(prompt, max_tokens=4000, heavy=True, tag="channel_drafts")
        texts = _parse_list(raw)[:n]
        slots = self.next_slots(len(texts))
        with self.lock:
            items = self._load()
            new = [{"id": uuid.uuid4().hex[:8], "text": t.strip(), "rubric": r, "at": at, "status": "draft",
                    "created": time.time()} for t, r, at in zip(texts, rubrics, slots)]
            self._save(items + new)
        for p in new:
            self.show(p)
        return len(new)

    def show(self, p, chat_ids=None):
        kb = [[{"text": "✅ В очередь", "callback_data": f"cp:ok:{p['id']}"},
               {"text": "✏️ Переписать", "callback_data": f"cp:ed:{p['id']}"},
               {"text": "❌ Удалить", "callback_data": f"cp:no:{p['id']}"}]]
        if p.get("file_id"):
            kb.append([{"text": "🔁 Другой ролик", "callback_data": f"cp:vr:{p['id']}"}])
            cap = f"🎬 Ролик для @{CHANNEL} · {self.when(p['at'])}\n\n{p['text']}"
            for a in (chat_ids or self.admin_ids):
                self.api("sendVideo", chat_id=a, video=p["file_id"], caption=cap[:1024],
                         reply_markup={"inline_keyboard": kb})
            return
        kb[0].insert(2, {"text": "🎬 Видео", "callback_data": f"cp:vd:{p['id']}"})
        kb = [kb[0][:2], kb[0][2:]]
        head = f"📝 Черновик для @{CHANNEL} · {self.when(p['at'])}\nРубрика: {p['rubric'][:60]}\n\n"
        for a in (chat_ids or self.admin_ids):
            self.send(a, head + p["text"], kb)

    # ---------- ролики ----------
    def make_video(self, chat_id, source: str, replace_id: str | None = None):
        """Сценарий → голос → видео. Видео грузится в чат админа, file_id сохраняется в очереди."""
        if not self.video_busy.acquire(blocking=False):
            self.send(chat_id, "Уже делаю другой ролик — подожди пару минут и нажми ещё раз.")
            return
        try:
            ff = self.ffmpeg()
            if not ff:
                raise RuntimeError("нет ffmpeg")
            self.send(chat_id, "🎬 Пишу сценарий, озвучиваю и монтирую… обычно 1–3 минуты.")
            script = faceless.parse_script(self.ask(faceless.script_prompt(source, FACTS, RULES),
                                                    max_tokens=1500, tag="video_script"))
            mp4 = faceless.render(ff, script["title"], faceless.tts(script["voice"]), script["voice"])
            at = self.next_slots(1)[0]
            pid = uuid.uuid4().hex[:8]
            kb = [[{"text": "✅ В очередь", "callback_data": f"cp:ok:{pid}"},
                   {"text": "✏️ Подпись", "callback_data": f"cp:ed:{pid}"},
                   {"text": "❌ Удалить", "callback_data": f"cp:no:{pid}"}],
                  [{"text": "🔁 Другой ролик", "callback_data": f"cp:vr:{pid}"}]]
            cap = f"🎬 Ролик для @{CHANNEL} · {self.when(at)}\n\n{script['caption']}"
            msg = faceless.upload_video(self.token, chat_id, mp4, cap, kb)
            if not msg or not msg.get("video"):
                raise RuntimeError("Telegram не принял видео")
            item = {"id": pid, "text": script["caption"], "rubric": "Ролик", "at": at, "status": "draft",
                    "created": time.time(), "file_id": msg["video"]["file_id"], "source": source[:1500],
                    "voice": script["voice"]}
            with self.lock:
                items = self._load()
                if replace_id:
                    for p in items:
                        if p["id"] == replace_id and p["status"] == "draft":
                            p["status"] = "deleted"
                self._save(items + [item])
            for a in self.admin_ids:
                if a != chat_id:
                    self.show(item, [a])
            log.info("video ok %d KB", len(mp4) // 1024)
        except Exception as e:
            log.error("video failed: %s", e)
            self.send(chat_id, "Не получилось сделать ролик 😕 Попробуй ещё раз через минуту.")
        finally:
            self.video_busy.release()

    def selftest(self):
        """Один раз (до первого успеха): проверить ИИ-голос и монтаж на сервере, результат — в журнал."""
        if self.kv_get("cp:tts_ok"):
            return
        try:
            ff = self.ffmpeg()
            mp3 = faceless.tts("Проверка голоса Improve Lab.")
            mp4 = faceless.render(ff, "Проверка", mp3, "Проверка голоса Improve Lab.")
            self.kv_set("cp:tts_ok", "1")
            log.info("video selftest OK: voice %d KB, video %d KB", len(mp3) // 1024, len(mp4) // 1024)
        except Exception as e:
            log.warning("video selftest failed: %s", e)

    # ---------- кнопки и правки ----------
    def on_callback(self, chat_id, user, data) -> bool:
        if not data.startswith("cp:"):
            return False
        if user["id"] not in self.admin_ids:
            return True
        _, action, pid = (data.split(":") + ["", ""])[:3]
        p = self._get(pid)
        if not p:
            self.send(chat_id, "Этого черновика уже нет.")
            return True
        if action == "ok":
            self._update(pid, status="approved")
            on = "" if self.posting_on() else "\n(Публикация пока выключена — включится по /posting on.)"
            self.send(chat_id, f"✅ В очереди: {self.when(p['at'])}{on}")
        elif action == "no":
            self._update(pid, status="deleted")
            self.send(chat_id, "❌ Удалил.")
        elif action in ("vd", "vr"):
            src = p.get("source") or p["text"]
            threading.Thread(target=self.make_video, args=(chat_id, src, pid if action == "vr" else None),
                             daemon=True).start()
        elif action == "ed":
            self.wait_edit[user["id"]] = pid
            self.send(chat_id, "Что поправить? Напиши одним сообщением — перепишу.\n"
                               "Или пришли готовый текст, начав со слова «Текст:» — поставлю его как есть.")
        return True

    def on_text(self, chat_id, user, text) -> bool:
        pid = self.wait_edit.pop(user["id"], None)
        if not pid or text.startswith("/"):
            return False
        p = self._get(pid)
        if not p:
            return False
        if text.lower().startswith("текст:"):
            new_text = text[6:].strip()
        else:
            self.send(chat_id, "Переписываю… ✍️")
            new_text = self.ask(f"{FACTS}\n\n{RULES}\n\nВот пост для канала:\n\n{p['text']}\n\n"
                                f"Перепиши его с учётом правки автора: {text}\n"
                                + ("Это подпись к видео — не длиннее 900 знаков.\n" if p.get("file_id") else "")
                                + "Ответ — только текст поста.",
                                max_tokens=1500, tag="channel_edit").strip()
        self._update(pid, text=new_text, status="draft")
        self.show(dict(p, text=new_text), [chat_id])
        return True

    # ---------- команды ----------
    def on_command(self, chat_id, user, cmd, arg, arg_full: str = "") -> bool:
        if user["id"] not in self.admin_ids or cmd not in ("/drafts", "/video", "/queue", "/posting", "/channel_check"):
            return False
        if cmd == "/drafts":
            self.send(chat_id, f"Пишу {DRAFTS_PER_WEEK} постов для @{CHANNEL}… это пара минут ⏳")
            threading.Thread(target=self._drafts_job, args=(chat_id,), daemon=True).start()
        elif cmd == "/video":
            topic = arg_full or ""
            if not topic:
                self.send(chat_id, "Напиши тему: /video почему рилс не набирают просмотры\n"
                                   "Или нажми 🎬 Видео под любым черновиком поста.")
            else:
                threading.Thread(target=self.make_video, args=(chat_id, topic), daemon=True).start()
        elif cmd == "/queue":
            items = [p for p in self._load() if p["status"] in ("draft", "approved")]
            items.sort(key=lambda p: p["at"])
            if not items:
                self.send(chat_id, "Очередь пустая. /drafts — написать посты на неделю.")
            else:
                mark = {"draft": "📝 ждёт ✅", "approved": "✅ в очереди"}
                self.send(chat_id, f"Публикация: {'включена' if self.posting_on() else 'выключена'}\n\n" + "\n\n".join(
                    f"{self.when(p['at'])} — {'🎬 ' if p.get('file_id') else ''}{mark[p['status']]}\n{p['text'][:90]}…"
                    for p in items))
        elif cmd == "/posting":
            if arg in ("on", "off"):
                self.kv_set("cp:posting", arg)
                if arg == "on":
                    moved = self._reschedule_overdue()
                    self.send(chat_id, f"Публикация включена ✅ Посты выходят в {POST_HOUR}:00 МСК."
                                       + (f" Просроченные ({moved}) переставил на ближайшие дни." if moved else ""))
                else:
                    self.send(chat_id, "Публикация выключена ⏸")
            else:
                self.send(chat_id, f"Сейчас: {'включена' if self.posting_on() else 'выключена'}. /posting on или /posting off")
        elif cmd == "/channel_check":
            self.send(chat_id, self.check())
        return True

    def _drafts_job(self, chat_id):
        try:
            n = self.make_drafts()
            self.send(chat_id, f"Готово: {n} черновиков выше. Жми ✅ на тех, что нравятся.")
        except Exception as e:
            log.error("drafts failed: %s", e)
            self.send(chat_id, "Не получилось написать посты 😕 Попробуй /drafts ещё раз через минуту.")

    def check(self) -> str:
        me = self.api("getMe") or {}
        m = self.api("getChatMember", chat_id=f"@{CHANNEL}", user_id=me.get("id", 0)) or {}
        if m.get("status") == "administrator" and m.get("can_post_messages", True):
            return f"✅ Бот — админ @{CHANNEL} и может публиковать."
        return (f"❌ Бот не может публиковать в @{CHANNEL}. Канал → Управление → Администраторы → добавить "
                f"@{me.get('username', 'Improvelab_bot')} с правом «Публикация сообщений».")

    def _reschedule_overdue(self) -> int:
        with self.lock:
            items = self._load()
            late = [p for p in items if p["status"] == "approved" and p["at"] < time.time()]
            for p, at in zip(sorted(late, key=lambda p: p["at"]), self.next_slots(len(late))):
                p["at"] = at
            self._save(items)
        return len(late)

    # ---------- публикация и еженедельные черновики ----------
    def loop(self):
        while True:
            try:
                if self.posting_on():
                    self._publish_due()
                    self._weekly_drafts()
            except Exception as e:
                log.warning("channel loop: %s", e)
            time.sleep(120)

    def _publish_due(self):
        for p in self._load():
            if p["status"] == "approved" and p["at"] <= time.time():
                if p.get("file_id"):
                    r = self.api("sendVideo", chat_id=f"@{CHANNEL}", video=p["file_id"], caption=p["text"][:1024],
                                 supports_streaming=True)
                else:
                    r = self.api("sendMessage", chat_id=f"@{CHANNEL}", text=p["text"], disable_web_page_preview=True)
                if r:
                    self._update(p["id"], status="posted", posted=time.time())
                    self.notify(f"📣 Опубликовал в @{CHANNEL}:\n{p['text'][:120]}…")
                else:
                    self._update(p["id"], status="failed")
                    self.notify(f"⚠️ Не смог опубликовать в @{CHANNEL}. Проверь: /channel_check")

    def _weekly_drafts(self):
        g = time.gmtime(time.time() + 3 * 3600)
        week = time.strftime("%G-%V", g)
        if g.tm_wday == 6 and g.tm_hour >= 12 and self.kv_get("cp:week_done") != week:
            self.kv_set("cp:week_done", week)
            n = self.make_drafts()
            self.notify(f"🗓 Посты на следующую неделю готовы ({n}) — посмотри и нажми ✅.")


def _parse_list(raw: str) -> list[str]:
    raw = raw.strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return [str(x) for x in data if str(x).strip()]
        except Exception:
            pass
    return [part.strip() for part in re.split(r"\n\s*(?:---+|\d+[.)])\s*\n", raw) if len(part.strip()) > 80]
