"""Telegram Bot API без зависимостей. Один объект TG на каждого бота сети."""
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("tg")


class TG:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    def api(self, method: str, **params):
        req = urllib.request.Request(self.base + method, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.loads(r.read().decode()).get("result")
        except urllib.error.HTTPError as e:
            log.warning("TG %s %s: %s", method, e.code, e.read()[:300])
        except Exception as e:
            log.warning("TG %s failed: %s", method, e)
        return None

    def send(self, chat_id: int, text: str, keyboard=None):
        """Длинный текст режется на части по ~3900 знаков; клавиатура — под последней частью."""
        parts, buf = [], ""
        for line in text.split("\n"):
            while len(line) > 3800:
                parts.append(buf + line[:3800])
                buf, line = "", line[3800:]
            if len(buf) + len(line) + 1 > 3900:
                parts.append(buf)
                buf = ""
            buf += line + "\n"
        if buf.strip():
            parts.append(buf)
        for i, part in enumerate(parts):
            extra = {"reply_markup": {"inline_keyboard": keyboard}} if keyboard and i == len(parts) - 1 else {}
            self.api("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True, **extra)

    def typing(self, chat_id: int, action: str = "typing"):
        self.api("sendChatAction", chat_id=chat_id, action=action)

    def download(self, file_id: str) -> bytes:
        info = self.api("getFile", file_id=file_id) or {}
        path = info.get("file_path")
        if not path:
            raise ValueError("file too big or unavailable")
        with urllib.request.urlopen(self.file_base + path, timeout=120) as r:
            return r.read()

    def send_file(self, chat_id: int, method: str, field: str, data: bytes, filename: str, mime: str,
                  caption: str = "", keyboard=None):
        """sendDocument / sendVideo с файлом из памяти (multipart)."""
        boundary = uuid.uuid4().hex
        parts = []

        def add(name, value):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

        add("chat_id", chat_id)
        if caption:
            add("caption", caption[:1000])
        if keyboard:
            add("reply_markup", json.dumps({"inline_keyboard": keyboard}))
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                      f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.base + method, data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode()).get("result")
        except urllib.error.HTTPError as e:
            log.warning("TG %s %s: %s", method, e.code, e.read()[:300])
        except Exception as e:
            log.warning("TG %s failed: %s", method, e)
        return None


def token_for(bot_id: str) -> str:
    """Токен бота из переменной окружения TOKEN_<ID> (например TOKEN_HOOKS)."""
    return os.getenv(f"TOKEN_{bot_id.upper()}", "")
