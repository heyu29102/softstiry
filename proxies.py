import logging
import time

import config

log = logging.getLogger("spam")
PROXY_COOLDOWN = config.PROXY_COOLDOWN


def parse_proxy(line):
    line = line.strip()
    if not line:
        return None
    low = line.lower()
    ptype = "socks5"
    if low.startswith("http://"):
        ptype, line = "http", line[7:]
    elif low.startswith("socks5://"):
        ptype, line = "socks5", line[9:]
    elif low.startswith("http//"):
        ptype, line = "http", line[6:]
    try:
        if "@" in line:
            auth, addr = line.split("@", 1)
            user, pw = auth.split(":", 1)
            host, port = addr.split(":", 1)
        else:
            parts = line.split(":")
            if len(parts) == 2:
                host, port = parts
                user = pw = None
            elif len(parts) == 4:
                host, port, user, pw = parts
            else:
                return None
        return {"proxy_type": ptype, "addr": host, "port": int(port), "username": user,
                "password": pw, "in_use": 0, "bad_until": 0.0}
    except Exception:
        return None


def _load_proxies_from_file():
    proxies = []
    if config.PROXY_FILE.exists():
        with config.PROXY_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                p = parse_proxy(line)
                if p:
                    proxies.append(p)
    return proxies


class ProxyPool:
    def __init__(self, proxies):
        self.proxies = proxies
        self._mtime = 0.0
        self._last_reload_check = 0.0
        self._touch_mtime()

    @classmethod
    def from_file(cls):
        return cls(_load_proxies_from_file())

    def _touch_mtime(self):
        try:
            self._mtime = config.PROXY_FILE.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def maybe_reload(self):
        now = time.time()
        if now - self._last_reload_check < config.PROXY_RELOAD_INTERVAL:
            return
        self._last_reload_check = now
        try:
            mtime = config.PROXY_FILE.stat().st_mtime
        except OSError:
            return
        if mtime == self._mtime:
            return
        fresh = _load_proxies_from_file()
        old_map = {(p["addr"], p["port"]): p for p in self.proxies}
        for p in fresh:
            old = old_map.get((p["addr"], p["port"]))
            if old:
                p["bad_until"] = old["bad_until"]
                p["in_use"] = old["in_use"]
        old_count = len(self.proxies)
        self.proxies = fresh
        self._mtime = mtime
        log.info(f"🛰 proxy.txt перезагружен: {len(self.proxies)} (было {old_count})")

    def __len__(self):
        return len(self.proxies)

    def acquire(self):
        self.maybe_reload()
        now = time.time()
        free = [p for p in self.proxies if p["bad_until"] <= now]
        if not free:
            # Все в кулдауне — берём с минимальным остатком, иначе 900 сессий простаивают.
            if not self.proxies:
                return None
            p = min(self.proxies, key=lambda x: (x["bad_until"], x["in_use"]))
            p["in_use"] += 1
            return p
        p = min(free, key=lambda x: x["in_use"])
        p["in_use"] += 1
        return p

    def release(self, p):
        if p:
            p["in_use"] = max(0, p["in_use"] - 1)

    def mark_bad(self, p):
        if p:
            p["bad_until"] = time.time() + PROXY_COOLDOWN

    def cooldown_count(self):
        now = time.time()
        return sum(1 for p in self.proxies if p["bad_until"] > now)

    def has_free(self):
        now = time.time()
        return any(p["bad_until"] <= now for p in self.proxies)

    def to_dict(self, p):
        return {k: p[k] for k in ("proxy_type", "addr", "port", "username", "password")}
