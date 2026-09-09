import time

import config

PROXY_COOLDOWN = 120
MAX_IN_USE_PER_PROXY = 50


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


class ProxyPool:
    def __init__(self, proxies):
        self.proxies = proxies

    @classmethod
    def from_file(cls):
        proxies = []
        if config.PROXY_FILE.exists():
            with config.PROXY_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    p = parse_proxy(line)
                    if p:
                        proxies.append(p)
        return cls(proxies)

    def __len__(self):
        return len(self.proxies)

    def acquire(self):
        now = time.time()
        free = [p for p in self.proxies if p["bad_until"] <= now]
        if not free:
            return None
        p = min(free, key=lambda x: x["in_use"])
        p["in_use"] += 1
        return p

    def release(self, p):
        if p:
            p["in_use"] = max(0, p["in_use"] - 1)

    def mark_bad(self, p):
        if p:
            p["bad_until"] = time.time() + PROXY_COOLDOWN

    def decay_cooldowns(self):
        """Снимаем просроченный cooldown и чуть ускоряем восстановление пула."""
        now = time.time()
        for p in self.proxies:
            if 0 < p["bad_until"] <= now:
                p["bad_until"] = 0.0
            elif p["bad_until"] > now:
                # мягкий decay: −30с каждые 5 мин maintenance
                p["bad_until"] = max(now, p["bad_until"] - 30)

    def reset_stuck(self):
        """Сброс зависшего in_use (утечка при обрывах)."""
        for p in self.proxies:
            if p["in_use"] > MAX_IN_USE_PER_PROXY:
                p["in_use"] = 0
            elif p["in_use"] < 0:
                p["in_use"] = 0

    def pool_stats(self):
        now = time.time()
        free = sum(1 for p in self.proxies if p["bad_until"] <= now)
        cooldown = sum(1 for p in self.proxies if p["bad_until"] > now)
        in_use = sum(p["in_use"] for p in self.proxies)
        return free, cooldown, in_use

    def cooldown_count(self):
        now = time.time()
        return sum(1 for p in self.proxies if p["bad_until"] > now)

    def has_free(self):
        now = time.time()
        return any(p["bad_until"] <= now for p in self.proxies)

    def to_dict(self, p):
        return {k: p[k] for k in ("proxy_type", "addr", "port", "username", "password")}
