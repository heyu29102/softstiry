"""
Редирект: GET → random бот из пула.
- домены groups (domains.txt) → bots.txt
- домены contacts (domains_contacts.txt) → bots_contacts.txt
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


_group_links: list[str] = []
_contact_links: list[str] = []
_group_hosts: set[str] = set()
_contact_hosts: set[str] = set()
_links_lock = threading.Lock()
_BLOCK_HOSTS: tuple[str, ...] = ()


def _block_hosts():
    env_hosts = [
        h.strip().lower()
        for h in (config.REDIRECT_BLOCK_HOSTS or "").split(",")
        if h.strip()
    ]
    domain_hosts = config.domain_hosts_from_links()
    merged = list(dict.fromkeys(env_hosts + domain_hosts))
    return tuple(merged)


def _normalize_host(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("www."):
        return host[4:]
    return host


def _host_in_pool(host: str, pool_hosts: set[str]) -> bool:
    host = _normalize_host(host)
    if not host:
        return False
    for item in pool_hosts:
        item = _normalize_host(item)
        if host == item or host.endswith("." + item):
            return True
    return False


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
    global _group_links, _contact_links, _group_hosts, _contact_hosts, _BLOCK_HOSTS
    group_links = config.load_bot_links(config.BOTS_FILE)
    contact_links = config.load_bot_links(config.BOTS_CONTACTS_FILE)
    group_hosts = {
        _normalize_host(h)
        for h in config.domain_hosts_from_links(config.load_domain_links(config.DOMAINS_FILE))
    }
    contact_hosts = {
        _normalize_host(h)
        for h in config.domain_hosts_from_links(config.load_domain_links(config.DOMAINS_CONTACTS_FILE))
    }
    block_hosts = _block_hosts()
    with _links_lock:
        _group_links = group_links
        _contact_links = contact_links
        _group_hosts = {h for h in group_hosts if h}
        _contact_hosts = {h for h in contact_hosts if h}
        _BLOCK_HOSTS = block_hosts
    return len(group_links), len(contact_links)


def pick_link(host: str | None = None):
    host = (host or "").split(":")[0]
    with _links_lock:
        if _host_in_pool(host, _contact_hosts):
            pool_name = "contacts"
            raw = _contact_links
        else:
            pool_name = "groups"
            raw = _group_links
        if not raw:
            return None, pool_name
        blocked = _BLOCK_HOSTS
        pool = [u for u in raw if not _is_self_link(u, blocked)]
        if not pool:
            return None, pool_name
        return random.choice(pool), pool_name


def reload_loop():
    while True:
        try:
            g, c = reload_links()
            print(f"[redirect] пул ботов: группы={g}, контакты={c}")
        except Exception as e:
            print(f"[redirect] reload error: {e}")
        time.sleep(config.BOTS_RELOAD_INTERVAL)


class RedirectHandler(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, fmt, *args):
        print(f"[redirect] {self.address_string()} - {fmt % args}")

    def _redirect(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        link, pool = pick_link(host)
        if not link:
            body = f"bot pool empty ({pool}) or only self-links"
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        self.send_response(302)
        self.send_header("Location", link)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

    def do_GET(self):
        self._redirect()

    def do_HEAD(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        link, pool = pick_link(host)
        if not link:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", link)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()


def main():
    g, c = reload_links()
    if g == 0 and c == 0:
        print("[redirect] WARNING: bots.txt и bots_contacts.txt пусты — редирект будет 503")

    t = threading.Thread(target=reload_loop, daemon=True)
    t.start()

    host = config.REDIRECT_HOST
    port = config.REDIRECT_PORT
    server = ThreadingHTTPServer((host, port), RedirectHandler)
    print(f"[redirect] http://{host}:{port} | группы={g} | контакты={c}")
    server.serve_forever()


if __name__ == "__main__":
    main()
