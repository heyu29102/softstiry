"""
Редирект: каждый GET → random ссылка из bots.txt (живые — bot_watcher).
Один redirect.py на порту 8090 — несколько доменов через nginx.
"""

import random
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import config


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False

def _block_hosts():
    env_hosts = [
        h.strip().lower()
        for h in (config.REDIRECT_BLOCK_HOSTS or "").split(",")
        if h.strip()
    ]
    domain_hosts = config.domain_hosts_from_links()
    merged = list(dict.fromkeys(env_hosts + domain_hosts))
    return tuple(merged)


_links = []
_links_lock = threading.Lock()
_last_reload = 0.0
_BLOCK_HOSTS = _block_hosts()


def _is_self_link(url, blocked_hosts=None):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    if not host:
        return True
    blocked = blocked_hosts if blocked_hosts is not None else _BLOCK_HOSTS
    for blocked_host in blocked:
        if host == blocked_host or host.endswith("." + blocked_host):
            return True
    return False


def reload_links():
    global _links, _last_reload, _BLOCK_HOSTS
    links = config.load_bot_links()
    block_hosts = _block_hosts()
    with _links_lock:
        _links = links
        _last_reload = time.time()
        _BLOCK_HOSTS = block_hosts
    return len(links)


def pick_link():
    with _links_lock:
        if not _links:
            return None
        blocked = _BLOCK_HOSTS
        pool = [u for u in _links if not _is_self_link(u, blocked)]
        if not pool:
            return None
        return random.choice(pool)


def reload_loop():
    while True:
        try:
            n = reload_links()
            print(f"[redirect] пул ботов: {n}")
        except Exception as e:
            print(f"[redirect] reload error: {e}")
        time.sleep(config.BOTS_RELOAD_INTERVAL)


class RedirectHandler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, fmt, *args):
        print(f"[redirect] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        link = pick_link()
        if not link:
            body = b"bot pool empty or only self-links in bots.txt"
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(302)
        self.send_header("Location", link)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

    def do_HEAD(self):
        link = pick_link()
        if not link:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", link)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()


def main():
    n = reload_links()
    if n == 0:
        print("[redirect] WARNING: bots.txt пуст — редирект будет 503")

    t = threading.Thread(target=reload_loop, daemon=True)
    t.start()

    host = config.REDIRECT_HOST
    port = config.REDIRECT_PORT
    server = ThreadingHTTPServer((host, port), RedirectHandler)
    print(f"[redirect] http://{host}:{port} | ботов в пуле: {n}")
    server.serve_forever()


if __name__ == "__main__":
    main()
