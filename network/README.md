# Сеть ботов Improve Lab

Маленькие боты для авторов, которые живут на том же сервере, что и @Improvelab_bot (Render-сервис `hookly-bot`),
и ведут людей друг к другу и в главный бот. Одна подписка (по умолчанию 199 ₽/мес, год = 10 месяцев) открывает все боты сети.

## Боты
| id | Файл | Что делает | Токен |
|---|---|---|---|
| hooks | `bots/hooks.py` | 🪝 Хуки: первая фраза, текст на экране, обложка, первая строка поста | `TOKEN_HOOKS` |
| captions | `bots/captions.py` | ✍️ Подписи + хэштеги в стиле автора (текст / голосовое / видео) | `TOKEN_CAPTIONS` |
| subs | `bots/subtitles.py` | 💬 Субтитры: видео с вшитыми субтитрами (3 стиля) + SRT | `TOKEN_SUBS` |

Бот без токена не запускается — сеть можно включать по одному боту.

## Как добавить нового бота (≈1 день)
1. Создать бота в @BotFather, токен — в Render → `hookly-bot` → Environment → `TOKEN_<ID>`.
2. Новый файл `bots/<имя>.py`: класс от `core.bot.Bot` (`id`, `title`, `emoji`, `pitch`, `start_text`, `on_text` / `on_media` / `on_button`),
   в конце `BOT = ...`. Внутри обработчика: `trial = self.allow(ctx)` → `self.run(ctx, self.make, ..., trial)` → в конце `self.after_result(ctx, trial)`.
3. Добавить имя файла в `bots/__init__.py` → `BOT_IDS`.
4. `python test_network.py` — оффлайн-проверка.

## Общее ядро (`core/`)
- `bot.py` — каркас: бесплатные попытки (`FREE_TRIES`, по умолчанию 3 в каждом боте), подписка сети (Redis `net:sub:<uid>`),
  оплата по СБП (код `NB<5 цифр>`, скриншот → админу ✅/❌), 👍/👎 с комментарием админу, «🆕» о новых людях, сводка в 21:00 МСК,
  перекрёстные ссылки (`/more`, подсказки после 2-го, 5-го и каждого 10-го ответа).
- `ai.py` — Claude через ProxyAPI (те же `AI_*`, что у Improve Lab), Whisper (`STT_MODEL`) с отрезками по времени.
- `media.py` — ffmpeg: звук → mp3, SRT, вшивание субтитров (шрифт `fonts/DejaVuSans-Bold.ttf`).
- `store.py` — Redis/Key Value (`REDIS_URL`), ключи `net:*` и `b:<бот>:*` — не пересекаются с ключами Improve Lab.

## Переменные (общие с Improve Lab)
`ADMIN_IDS`, `PAY_DETAILS`, `PAYWALL`, `AI_API_KEY`, `AI_BASE_URL`, `AI_MODEL`, `AI_MODEL_HEAVY`, `STT_MODEL`, `REDIS_URL`.
Свои: `TOKEN_<ID>`, `NET_PRICE` (199), `FREE_TRIES` (3), `SUB_DAILY` (40 в день на бота для подписчика), `IMPROVE_LAB` (Improvelab_bot).

## Подключение к серверу Improve Lab
`bot.py` загружает `network/mount.py`: при старте вебхука запускает ботов сети, а запросы на чужие пути `/tg/<бот>/…`
передаёт в `mount.dispatch`. Любая ошибка сети не роняет Improve Lab. Отдельно (для локального теста): `cd network && python main.py`.

## Почему не отдельный сервис
Бесплатный Render даёт 750 часов в месяц на ВСЕ сервисы, а Improve Lab с keep-alive уже тратит ~720.
Второй бесплатный сервис мог бы исчерпать лимит и остановить всё.
