"""Сеть Telegram-ботов на одном сервере.

Каждый бот из bots/ запускается, если задан его токен TOKEN_<ID> (TOKEN_HOOKS, TOKEN_CAPTIONS, TOKEN_SUBS…).
Режимы: webhook (есть RENDER_EXTERNAL_URL или WEBHOOK_URL) — все боты на одном порту, у каждого свой путь;
иначе long polling (для запуска на компьютере).
"""
import hashlib
import importlib
import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bots import BOT_IDS
from core import media, store
from core.bot import daily_summary_loop, network
from core.tg import token_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")
updates = ThreadPoolExecutor(max_workers=16)


def load_bots():
    for module in BOT_IDS:
        bot = importlib.import_module(f"bots.{module}").BOT
        network.bots.append(bot)
        token = token_for(bot.id)
        if token:
            bot.attach(token)
        else:
            log.info("bot %s: нет TOKEN_%s — пропускаю", bot.id, bot.id.upper())
    if not network.live():
        raise SystemExit("Ни одного токена TOKEN_<ID> — запускать нечего")


def secrets(bot):
    digest = hashlib.sha256(bot.tg.token.encode()).hexdigest()
    return f"/tg/{bot.id}/{digest[32:48]}", digest[:32]


def run_webhook(base: str, port: int):
    routes = {}
    for bot in network.live():
        path, secret = secrets(bot)
        routes[path] = (bot, secret)

    def register():
        for path, (bot, secret) in routes.items():
            bot.tg.api("setWebhook", url=base.rstrip("/") + path, secret_token=secret, drop_pending_updates=False)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def do_POST(self):
            hit = routes.get(self.path)
            if not hit or self.headers.get("X-Telegram-Bot-Api-Secret-Token") != hit[1]:
                self.send_response(403)
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.end_headers()
            try:
                updates.submit(hit[0].handle_update, json.loads(body))
            except Exception:
                pass

        def log_message(self, *a):
            pass

    def keepalive():
        # По умолчанию выключено: бесплатный Render даёт 750 ч в месяц на ВСЕ сервисы, и их уже тратит Improve Lab.
        # Спящий сервис будит первое сообщение (ответ через ~30–60 с). KEEPALIVE=1 — не засыпать (лучше на платном тарифе).
        while True:
            time.sleep(600)
            try:
                urllib.request.urlopen(base.rstrip("/") + "/", timeout=30).read()
            except Exception as e:
                log.warning("keepalive failed: %s", e)

    if os.getenv("KEEPALIVE", "0") == "1":
        threading.Thread(target=keepalive, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), H)
    register()
    log.info("webhook: %s ботов на :%s", len(routes), port)
    server.serve_forever()


def run_polling():
    def loop(bot):
        bot.tg.api("deleteWebhook", drop_pending_updates=False)
        offset = 0
        while True:
            ups = bot.tg.api("getUpdates", offset=offset, timeout=50) or []
            for u in ups:
                offset = u["update_id"] + 1
                updates.submit(bot.handle_update, u)
            if not ups:
                time.sleep(0.5)

    for bot in network.live():
        threading.Thread(target=loop, args=(bot,), daemon=True).start()
    log.info("polling: %s ботов", len(network.live()))
    while True:
        time.sleep(3600)


def main():
    store.db()
    load_bots()
    media.warmup()
    threading.Thread(target=daily_summary_loop, daemon=True).start()
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        run_webhook(base, int(os.getenv("PORT", "10000")))
    else:
        run_polling()


if __name__ == "__main__":
    main()
