"""Память бота, которая переживает перезапуски: Redis / Render Key Value (REDIS_URL).

Без внешних библиотек — минимальный клиент протокола Redis. Если REDIS_URL не задан
или база недоступна, работает в оперативной памяти (как раньше).
"""
import logging
import os
import socket
import ssl
import threading
from urllib.parse import urlparse

log = logging.getLogger("storage")


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
    def __init__(self):
        self.d = {}
        self.lock = threading.Lock()

    def cmd(self, op, key, *args):
        with self.lock:
            if op == "GET":
                return self.d.get(key)
            if op == "SET":
                self.d[key] = str(args[0])
                return "OK"
            if op == "HINCRBY":
                h = self.d.setdefault(key, {})
                h[args[0]] = str(int(h.get(args[0], 0)) + int(args[1]))
                return int(h[args[0]])
            if op == "HSET":
                h = self.d.setdefault(key, {})
                for i in range(0, len(args), 2):
                    h[args[i]] = str(args[i + 1])
                return 1
            if op == "HSETNX":
                h = self.d.setdefault(key, {})
                if args[0] in h:
                    return 0
                h[args[0]] = str(args[1])
                return 1
            if op == "HGETALL":
                h = self.d.get(key, {})
                return [x for kv in h.items() for x in kv]
            if op == "SADD":
                s = self.d.setdefault(key, set())
                before = len(s)
                s.update(args)
                return len(s) - before
            if op == "SMEMBERS":
                return list(self.d.get(key, set()))
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
                log.info("storage: redis")
            except Exception as e:
                log.warning("redis unavailable (%s), using memory", e)
        if _db is None:
            _db = _Memory()
    return _db


def _safe(*args, default=None):
    try:
        return db().cmd(*args)
    except Exception as e:
        log.warning("storage %s failed: %s", args[0], e)
        return default


def get_channel(uid: int) -> str | None:
    return _safe("GET", f"u:{uid}:chan")


def set_channel(uid: int, name: str):
    _safe("SET", f"u:{uid}:chan", name)


def get_home(uid: int) -> tuple[str | None, float]:
    """Свой канал пользователя и время, когда его назначили."""
    name = _safe("GET", f"u:{uid}:home")
    ts = _safe("GET", f"u:{uid}:home_ts")
    return name, float(ts or 0)


def set_home(uid: int, name: str, ts: float):
    _safe("SET", f"u:{uid}:home", name)
    _safe("SET", f"u:{uid}:home_ts", int(ts))
    set_channel(uid, name)


def sub_until(uid) -> float:
    return float(_safe("GET", f"u:{uid}:sub") or 0)


def set_sub(uid, until: float):
    _safe("SET", f"u:{uid}:sub", int(until))


def free_used(uid, kind: str) -> int:
    return int(_safe("GET", f"u:{uid}:free:{kind}") or 0)


def use_free(uid, kind: str):
    _safe("SET", f"u:{uid}:free:{kind}", free_used(uid, kind) + 1)


def first_time(uid, task: str, key: str, ts: float, days: int = 7) -> bool:
    """True, если пользователь делает это (задача + канал/тема) впервые за `days` дней. Отмечает выполнение."""
    k = f"u:{uid}:seen:{task}:{key.lower()}"
    last = float(_safe("GET", k) or 0)
    _safe("SET", k, int(ts))
    return ts - last > days * 86400


def set_ref(uid, ref) -> bool:
    """Кто пригласил пользователя (только один раз и не сам себя)."""
    if str(uid) == str(ref) or _safe("GET", f"u:{uid}:ref"):
        return False
    _safe("SET", f"u:{uid}:ref", ref)
    _safe("SADD", f"u:{ref}:invited", uid)
    return True


def ref_of(uid):
    return _safe("GET", f"u:{uid}:ref")


def invited(uid) -> list:
    return _safe("SMEMBERS", f"u:{uid}:invited", default=[]) or []


def ref_rewarded(uid) -> bool:
    """Отмечает, что за оплату пользователя уже наградили пригласившего. True — если раньше не награждали."""
    if _safe("GET", f"u:{uid}:ref_paid"):
        return False
    _safe("SET", f"u:{uid}:ref_paid", 1)
    return True


def track(user: dict, event: str, ts: float) -> bool:
    """Считает события пользователя. Возвращает True, если пользователь новый."""
    uid = user["id"]
    is_new = _safe("SADD", "users", uid, default=0) == 1
    key = f"u:{uid}:stats"
    if is_new:
        _safe("HSET", key, "first", int(ts), "name", user.get("username") or user.get("first_name") or "")
    _safe("HINCRBY", key, event, 1)
    _safe("HSET", key, "last", int(ts))
    return is_new


def all_stats() -> dict:
    out = {}
    for uid in _safe("SMEMBERS", "users", default=[]) or []:
        flat = _safe("HGETALL", f"u:{uid}:stats", default=[]) or []
        out[uid] = dict(zip(flat[::2], flat[1::2]))
    return out
