"""Подключение сети ботов к уже работающему серверу (Improve Lab), чтобы не заводить второй сервис на Render.

В bot.py Improve Lab:  import network_mount → network_mount.start(base_url) при запуске,
а в do_POST для чужих путей: network_mount.dispatch(path, secret_header, body) → True, если запрос наш.
Ошибки сети ботов никогда не роняют Improve Lab: всё в try/except.
"""
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

log = logging.getLogger("network")
_routes: dict = {}
_pool = ThreadPoolExecutor(max_workers=8)


def start(base_url: str) -> int:
    """Запускает ботов сети, у которых есть токены. Возвращает их число (0 — сеть выключена)."""
    try:
        from core import media, store
        from core.bot import daily_summary_loop, network
        from core.tg import token_for
        import importlib
        from bots import BOT_IDS
        import main as net_main

        store.db()
        for module in BOT_IDS:
            bot = importlib.import_module(f"bots.{module}").BOT
            network.bots.append(bot)
            if token_for(bot.id):
                bot.attach(token_for(bot.id))
        live = network.live()
        if not live:
            log.info("network: нет токенов TOKEN_<ID> — сеть ботов выключена")
            return 0
        for bot in live:
            path, secret = net_main.secrets(bot)
            _routes[path] = (bot, secret)
            bot.tg.api("setWebhook", url=base_url.rstrip("/") + path, secret_token=secret, drop_pending_updates=False)
        media.warmup()
        threading.Thread(target=daily_summary_loop, daemon=True).start()
        log.info("network: запущено ботов — %s", len(live))
        return len(live)
    except Exception as e:
        log.error("network start failed: %s", e)
        return 0


def reregister(base_url: str):
    """Повторная регистрация webhook (Improve Lab делает так же в keep-alive)."""
    for path, (bot, secret) in list(_routes.items()):
        try:
            bot.tg.api("setWebhook", url=base_url.rstrip("/") + path, secret_token=secret, drop_pending_updates=False)
        except Exception:
            pass


def dispatch(path: str, secret_header: str | None, body: bytes) -> bool | None:
    """True — наш запрос, принят; False — наш путь, но неверный секрет; None — не наш путь."""
    hit = _routes.get(path)
    if not hit:
        return None
    if secret_header != hit[1]:
        return False
    try:
        _pool.submit(hit[0].handle_update, json.loads(body))
    except Exception:
        pass
    return True
