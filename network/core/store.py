"""Общая память сети ботов: Render Key Value / Redis (REDIS_URL), иначе — оперативная память.
Ключи: net:* — общее для сети (подписка, пользователи), b:<бот>:* — данные конкретного бота."""
import logging
import os
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("store")


class _Redis:
    def __init__(self, url: str):
        self.u = urlparse(url)
        self.sock = None
        self.f = None
        self.lock = threading.Lock()

    def _connect(self):
        s = socket.create_connection((self.u.hostname, self.u.port or 6379), timeout=10)
        if self.u.scheme == "rediss":
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.u.hostname)
        self.sock, self.f = s, s.makefile("rb")
        if self.u.password:
            args = [self.u.username, self.u.password] if self.u.username else [self.u.password]
            self._send("AUTH", *args)
            self._read()

    def _send(self, *args):
        out = f"*{len(args)}\r\n".encode()
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode()
            out += f"${len(b)}\r\n".encode() + b + b"\r\n"
        self.sock.sendall(out)

    def _read(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("closed")
        t, rest = line[:1], line[1:-2]
        if t == b"+":
            return rest.decode()
        if t == b"-":
            raise RuntimeError(rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            data = self.f.read(n + 2)[:-2]
            return data.decode()
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise RuntimeError("bad reply")

    def cmd(self, *args):
        with self.lock:
            for attempt in range(2):
                try:
                    if not self.sock:
                        self._connect()
                    self._send(*args)
                    return self._read()
                except (OSError, ConnectionError):
                    self.sock = None
                    if attempt:
                        raise


class _Memory:
    """Замена Redis для локального запуска и тестов (только нужные команды)."""

    def __init__(self):
        self.d, self.exp = {}, {}
        self.lock = threading.Lock()

    def _alive(self, key):
        if key in self.exp and self.exp[key] < time.time():
            self.d.pop(key, None)
            self.exp.pop(key, None)
        return key in self.d

    def cmd(self, op, key=None, *args):
        with self.lock:
            if op == "PING":
                return "PONG"
            self._alive(key)
            if op == "GET":
                return self.d.get(key)
            if op == "SET":
                self.d[key] = str(args[0])
                if len(args) >= 3 and str(args[1]).upper() == "EX":
                    self.exp[key] = time.time() + int(args[2])
                return "OK"
            if op == "DEL":
                return 1 if self.d.pop(key, None) is not None else 0
            if op == "INCRBY":
                self.d[key] = str(int(self.d.get(key, 0)) + int(args[0]))
                return int(self.d[key])
            if op == "EXPIRE":
                self.exp[key] = time.time() + int(args[0])
                return 1
            if op == "HINCRBY":
                h = self.d.setdefault(key, {})
                h[args[0]] = str(int(h.get(args[0], 0)) + int(args[1]))
                return int(h[args[0]])
            if op == "HSETNX":
                h = self.d.setdefault(key, {})
                if args[0] in h:
                    return 0
                h[args[0]] = str(args[1])
                return 1
            if op == "HGETALL":
                return [x for kv in self.d.get(key, {}).items() for x in kv]
            if op == "SADD":
                s = self.d.setdefault(key, set())
                n = len(s)
                s.update(str(a) for a in args)
                return len(s) - n
            if op == "SCARD":
                return len(self.d.get(key, set()))
            raise ValueError(op)


_db = None


def db():
    global _db
    if _db is None:
        url = os.getenv("REDIS_URL")
        if url:
            try:
                r = _Redis(url)
                r.cmd("PING")
                _db = r
                log.info("store: redis")
            except Exception as e:
                log.warning("redis unavailable (%s), using memory", e)
        if _db is None:
            _db = _Memory()
    return _db


def q(*args, default=None):
    try:
        v = db().cmd(*args)
        return default if v is None else v
    except Exception as e:
        log.warning("store %s failed: %s", args[0], e)
        return default


def get(key, default=None):
    return q("GET", key, default=default)


def put(key, value, ttl: int | None = None):
    return q("SET", key, value, "EX", ttl) if ttl else q("SET", key, value)


def incr(key, n: int = 1, ttl: int | None = None) -> int:
    v = int(q("INCRBY", key, n, default=0))
    if ttl and v == n:
        q("EXPIRE", key, ttl)
    return v


def hincr(key, field, n: int = 1) -> int:
    return int(q("HINCRBY", key, field, n, default=0))


def hall(key) -> dict:
    flat = q("HGETALL", key, default=[]) or []
    return {flat[i]: flat[i + 1] for i in range(0, len(flat), 2)}


def add_to_set(key, *members) -> int:
    return int(q("SADD", key, *members, default=0))


def set_size(key) -> int:
    return int(q("SCARD", key, default=0))
