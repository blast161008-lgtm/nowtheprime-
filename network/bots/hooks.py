"""🪝 Хуки — первые секунды ролика, текст на экране, обложка и первая строка поста."""
from core import ai
from core.bot import Bot

SYSTEM = """Ты — сценарист коротких видео (Reels, TikTok, Shorts) и постов. Твоя специальность — ХУКИ: то, что
удерживает зрителя в первые 1–2 секунды. Хороший хук: конкретный, короткий (до 10–12 слов), про выгоду или интригу
для зрителя, на языке ниши, звучит как живой человек. Плохой хук: общий («Сегодня расскажу…», «Всем привет»),
кликбейт-обман, обещания, которые ролик не выполнит."""

PROMPT = """Тема ролика/поста: {topic}
{niche}
Сделай хуки — каждый короткий и конкретно про эту тему, без повторов приёма:

🎙 ПЕРВАЯ ФРАЗА ВСЛУХ (4 варианта) — что сказать в первые 2 секунды.
📝 ТЕКСТ НА ЭКРАНЕ (3 варианта) — до 7 слов, крупно поверх видео.
🖼 ОБЛОЖКА (2 варианта) — надпись на обложку, 2–5 слов.
✍️ ПЕРВАЯ СТРОКА ПОСТА (2 варианта) — для Telegram/подписи, чтобы хотелось раскрыть текст.

Используй разные приёмы: ошибка, вопрос, цифра, неожиданный факт, «до/после», личная история, спор с мифом.
После каждого варианта в скобках 2–4 слова — какой приём. Нумерация сквозная 1–11.
{avoid}"""


class Hooks(Bot):
    id = "hooks"
    title = "Хуки"
    emoji = "🪝"
    pitch = "цепляющие первые секунды ролика, текст на экран и обложку"
    description = ("Пришли тему ролика или поста — получишь 11 хуков: первая фраза вслух, текст на экране, "
                   "надпись на обложку и первая строка поста. Для Reels, TikTok, Shorts и Telegram.")
    start_text = ("🪝 Привет! Я придумываю хуки — то, что держит зрителя первые 2 секунды.\n\n"
                  "Напиши, о чём твой ролик или пост, одним сообщением. Например:\n"
                  "«3 ошибки новичков в хип-хопе» или «как я открыла маникюрный кабинет за 2 месяца».\n\n"
                  "Чем конкретнее тема — тем сильнее хуки. Можно указать нишу: /niche танцы")
    commands = [("niche", "Моя ниша")]
    use_name = "набор хуков"

    def on_command(self, ctx, text):
        if text.startswith("/niche"):
            niche = text[len("/niche"):].strip()
            if niche:
                ctx.put("niche", niche[:80])
                return ctx.send(f"Запомнил нишу: {niche[:80]} ✅ Теперь пришли тему ролика.")
            return ctx.send("Напиши нишу после команды, например: /niche фитнес для мам")
        super().on_command(ctx, text)

    def on_text(self, ctx, text):
        if len(text) < 4:
            return ctx.send("Напиши тему чуть подробнее — хотя бы пару слов 🙂")
        trial = self.allow(ctx)
        if trial:
            ctx.put("topic", text[:500], 86400)
            ctx.put("seen", "", 86400)
            self.run(ctx, self.make, text[:500], True, trial)

    def on_button(self, ctx, data):
        if data == "hooks_more":
            topic = ctx.get("topic")
            if not topic:
                return ctx.send("Пришли тему ролика ещё раз 🙂")
            trial = self.allow(ctx)
            if trial:
                self.run(ctx, self.make, topic, False, trial)

    def make(self, ctx, topic, first, trial):
        ctx.typing()
        niche = ctx.get("niche")
        seen = ctx.get("seen", "") or ""
        prompt = PROMPT.format(
            topic=topic,
            niche=f"Ниша автора: {niche}" if niche else "",
            avoid=f"\nНЕ повторяй эти уже предложенные хуки и их приёмы:\n{seen[-2500:]}" if seen else "")
        text = ai.ask(prompt, SYSTEM, max_tokens=1400, heavy=first, tag="hooks")
        ctx.put("seen", (seen + "\n" + text)[-4000:], 86400)
        ctx.send(text, self.result_keyboard("hooks", [[{"text": "🔁 Ещё варианты", "callback_data": "hooks_more"}]]))
        self.after_result(ctx, trial)


BOT = Hooks()
