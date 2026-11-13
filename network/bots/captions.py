"""✍️ Подписи — текст под пост/рилс в стиле автора + хэштеги. Понимает текст, голосовые и видео (по речи)."""
from core import ai, media
from core.bot import Bot

SYSTEM = """Ты — автор подписей для соцсетей. Пишешь так, будто это написал сам автор: его словами, его тоном.
Подпись должна: зацепить первой строкой, дать ценность или историю, закончиться простым призывом к действию
(вопрос в комментарии, сохранить, отправить другу) — без «не забудьте подписаться». Хэштеги — по делу:
2–3 широких по нише, 4–6 узких по теме ролика, 1–2 локальных, если автор назвал город. Не обещай, что хэштеги
«взорвут охваты»."""

PLATFORMS = {"insta": "Instagram (подпись под рилс, до ~1000 знаков, абзацы короткие)",
             "tg": "Telegram-канал (пост, можно чуть длиннее, без хэштегов в середине текста)",
             "tiktok": "TikTok (коротко, 1–3 строки, хэштегов меньше)"}

PROMPT = """О чём публикация: {about}
Площадка: {platform}
{niche}{style}
Сделай:
1) ПЕРВАЯ СТРОКА — 3 варианта хука для начала подписи.
2) ПОДПИСЬ — 2 варианта: «коротко» и «история» (вторая — через личный опыт, если он есть в описании; не выдумывай факты).
3) ХЭШТЕГИ — одной строкой, 8–12 штук (для Telegram — 3–5).
Пиши готовым текстом, который можно сразу скопировать."""


class Captions(Bot):
    id = "captions"
    title = "Подписи"
    emoji = "✍️"
    pitch = "подпись к посту или рилс в твоём стиле + хэштеги"
    description = ("Опиши публикацию, пришли голосовое или само видео — получишь готовую подпись в твоём стиле, "
                   "варианты первой строки и хэштеги. Можно научить бота своему стилю: /style")
    start_text = ("✍️ Привет! Я пишу подписи к постам и рилс — твоими словами.\n\n"
                  "Пришли одно из:\n• текст: о чём публикация;\n• голосовое — расскажи как другу;\n"
                  "• само видео (до 20 МБ) — подпись будет по тому, что ты в нём говоришь.\n\n"
                  "Чтобы писал как ты: /style и пришли 2–3 свои прошлые подписи.")
    commands = [("style", "Научить моему стилю"), ("niche", "Моя ниша")]
    use_name = "подпись"

    def on_command(self, ctx, text):
        if text.startswith("/style"):
            ctx.put("wait_style", "1", 3600)
            return ctx.send("Пришли одним сообщением 2–3 свои прошлые подписи (можно подряд через пустую строку). "
                            "Я запомню тон, длину и любимые словечки.")
        if text.startswith("/niche"):
            niche = text[len("/niche"):].strip()
            if niche:
                ctx.put("niche", niche[:80])
                return ctx.send(f"Запомнил нишу: {niche[:80]} ✅")
            return ctx.send("Напиши нишу после команды, например: /niche бьюти, брови")
        super().on_command(ctx, text)

    def on_text(self, ctx, text):
        if ctx.get("wait_style"):
            ctx.put("wait_style", "", 1)
            ctx.put("style", text[:2500])
            return ctx.send("Запомнил твой стиль ✅ Теперь пришли, о чём публикация.")
        if len(text) < 6:
            return ctx.send("Опиши публикацию чуть подробнее — о чём она и для кого 🙂")
        trial = self.allow(ctx)
        if trial:
            ctx.put("about", text[:1500], 86400)
            self.run(ctx, self.make, text[:1500], "insta", trial)

    def on_media(self, ctx, msg):
        f = msg.get("voice") or msg.get("audio") or msg.get("video") or msg.get("video_note") or msg.get("document")
        if not f or msg.get("photo"):
            return ctx.send("Пришли текстом, голосовым или видео, о чём публикация 🙂")
        if (f.get("file_size") or 0) > 20 * 1024 * 1024:
            return ctx.send("Файл больше 20 МБ — Telegram не даёт ботам скачать его. Сожми видео или пришли голосовое.")
        trial = self.allow(ctx)
        if trial:
            self.run(ctx, self.from_media, f["file_id"], bool(msg.get("voice") or msg.get("audio")), trial)

    def from_media(self, ctx, file_id, is_audio, trial):
        ctx.typing()
        data = ctx.bot.tg.download(file_id)
        mp3 = media.audio_mp3(data)
        segs = ai.transcribe_segments(mp3, language="")
        if not segs:
            self.refund(ctx, trial)
            return ctx.send("Не разобрал речь 😕 Попытка не засчитана — опиши публикацию текстом, сделаю подпись.")
        about = " ".join(s["text"] for s in segs)[:1500]
        ctx.send(f"🎧 Услышал: «{about[:300]}{'…' if len(about) > 300 else ''}»")
        about = ("Автор рассказывает: " if is_audio else "В ролике автор говорит: ") + about
        ctx.put("about", about, 86400)
        self.make(ctx, about, "insta", trial)

    def on_button(self, ctx, data):
        if data.startswith("cap:"):
            about = ctx.get("about")
            if not about:
                return ctx.send("Пришли описание публикации ещё раз 🙂")
            trial = self.allow(ctx)
            if trial:
                self.run(ctx, self.make, about, data[4:], trial)

    def make(self, ctx, about, platform, trial):
        ctx.typing()
        style, niche = ctx.get("style"), ctx.get("niche")
        prompt = PROMPT.format(
            about=about, platform=PLATFORMS.get(platform, PLATFORMS["insta"]),
            niche=f"Ниша автора: {niche}\n" if niche else "",
            style=f"\nПРИМЕРЫ ПОДПИСЕЙ АВТОРА (повтори тон, длину фраз, лексику, эмодзи — но не копируй):\n{style}\n" if style else "")
        text = ai.ask(prompt, SYSTEM, max_tokens=1600, heavy=True, tag="captions")
        others = [(k, n) for k, n in (("insta", "Для Instagram"), ("tg", "Для Telegram"), ("tiktok", "Для TikTok")) if k != platform]
        kb = [[{"text": n, "callback_data": f"cap:{k}"} for k, n in others]]
        ctx.send(text, self.result_keyboard("captions", kb))
        if not ctx.get("style") and not ctx.get("style_tip"):
            ctx.put("style_tip", "1")
            ctx.send("Совет: пришли /style и 2–3 свои подписи — следующие будут звучать точно как ты.")
        self.after_result(ctx, trial)


BOT = Captions()
