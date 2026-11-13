"""💬 Субтитры — присылаешь ролик, получаешь его же с крупными субтитрами + SRT-файл для CapCut."""
import logging

from core import ai, media
from core.bot import Bot

log = logging.getLogger("subtitles")
MAX_MB = 20          # Telegram отдаёт ботам файлы до 20 МБ
MAX_SEC = 180

STYLES = {"classic": "⚪ Белые", "yellow": "🟡 Жёлтые", "box": "⬛ С подложкой"}


class Subtitles(Bot):
    id = "subs"
    title = "Субтитры"
    emoji = "💬"
    pitch = "крупные субтитры на твоё видео за минуту + SRT для CapCut"
    description = ("Пришли видео до 20 МБ и 3 минут — верну его с крупными субтитрами по твоей речи "
                   "и SRT-файл, чтобы поправить текст в CapCut или Premiere.")
    start_text = ("💬 Привет! Я делаю субтитры для твоих видео.\n\n"
                  "Пришли ролик (до 20 МБ и до 3 минут), где ты говоришь, — верну:\n"
                  "• видео с крупными субтитрами (можно выбрать стиль);\n"
                  "• SRT-файл — если захочешь поправить слово в CapCut.\n\n"
                  "Музыку без слов я не распознаю — только речь.")
    use_name = "видео"

    def on_text(self, ctx, text):
        ctx.send("Пришли видео — сделаю субтитры 🎬 (до 20 МБ и до 3 минут)")

    def on_media(self, ctx, msg):
        f = msg.get("video") or msg.get("video_note") or msg.get("document")
        if not f or msg.get("photo") or msg.get("voice") or msg.get("audio"):
            return ctx.send("Мне нужно видео 🎬 Пришли ролик файлом или как видео.")
        if (f.get("file_size") or 0) > MAX_MB * 1024 * 1024:
            return ctx.send(f"Видео больше {MAX_MB} МБ — Telegram не даёт ботам его скачать. "
                            "Сожми его (например, в CapCut экспорт 720p) и пришли снова.")
        if (f.get("duration") or 0) > MAX_SEC:
            return ctx.send("Видео длиннее 3 минут — пришли покороче (для рилс так и нужно 🙂).")
        trial = self.allow(ctx)
        if trial:
            ctx.put("file", f["file_id"], 86400)
            self.run(ctx, self.make, f["file_id"], trial)

    def make(self, ctx, file_id, trial):
        ctx.send("Делаю субтитры… это займёт около минуты ⏳")
        ctx.typing("upload_video")
        video = ctx.bot.tg.download(file_id)
        segs = ai.transcribe_segments(media.audio_mp3(video), language="")
        if not segs:
            self.refund(ctx, trial)
            return ctx.send("Не услышал речь в этом видео 😕 Субтитры делаются по голосу. "
                            "Попытка не засчитана — пришли ролик, где ты говоришь.")
        srt = media.to_srt(media.split_for_screen(segs, max_words=5))
        ctx.put("srt", srt, 86400)
        self.deliver(ctx, video, srt, "classic")
        self.after_result(ctx, trial)

    def deliver(self, ctx, video, srt, style):
        kb = [[{"text": n, "callback_data": f"subs:{k}"} for k, n in STYLES.items() if k != style],
              [{"text": "📄 SRT-файл", "callback_data": "subs:srt"}]]
        try:
            out = media.burn(video, srt, style)
            ctx.bot.tg.send_file(ctx.chat_id, "sendVideo", "video", out, "subtitles.mp4", "video/mp4",
                                 caption="Готово ✅ Другой стиль или файл для CapCut — кнопками ниже.",
                                 keyboard=self.result_keyboard("subs", kb))
        except Exception as e:
            log.warning("burn failed: %s", e)
            ctx.bot.tg.send_file(ctx.chat_id, "sendDocument", "document", srt.encode(), "subtitles.srt",
                                 "application/x-subrip",
                                 caption="Вшить в видео не получилось 😕 Вот субтитры файлом — их можно "
                                         "загрузить в CapCut («Текст» → «Субтитры» → импорт).",
                                 keyboard=self.result_keyboard("subs"))

    def on_button(self, ctx, data):
        if not data.startswith("subs:"):
            return
        srt, file_id = ctx.get("srt"), ctx.get("file")
        if not srt or not file_id:
            return ctx.send("Прошло много времени — пришли видео ещё раз 🙂")
        what = data[5:]
        if what == "srt":
            ctx.bot.tg.send_file(ctx.chat_id, "sendDocument", "document", srt.encode(), "subtitles.srt",
                                 "application/x-subrip", caption="SRT-файл: в CapCut — «Текст» → «Субтитры» → импорт.")
            return
        if what in STYLES:  # смена стиля — без новой попытки, речь уже распознана
            self.run(ctx, self._restyle, file_id, srt, what)

    def _restyle(self, ctx, file_id, srt, style):
        ctx.typing("upload_video")
        self.deliver(ctx, ctx.bot.tg.download(file_id), srt, style)


BOT = Subtitles()
