import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv_safe():
    """Читает .env: UTF-8, при ошибке — cp1251 (nano/Windows с русскими комментариями)."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            load_dotenv(env_path, encoding=encoding)
            return
        except UnicodeDecodeError:
            continue
    # крайний случай — не падаем, подставляем replace
    text = env_path.read_bytes().decode("utf-8", errors="replace")
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".utf8", dir=str(BASE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        load_dotenv(tmp, encoding="utf-8")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


_load_dotenv_safe()


def _env(key, default=None):
    v = os.getenv(key)
    return v if v not in (None, "") else default


def _int(key, default):
    try:
        return int(_env(key, default))
    except (TypeError, ValueError):
        return int(default)


def _float(key, default):
    try:
        return float(_env(key, default))
    except (TypeError, ValueError):
        return float(default)


# --- paths ---
SESSIONS_DIR = BASE_DIR / "sessions"
BAD_DIR = BASE_DIR / "sessions_bad"
SESSION_ROUTE_ROLE = (_env("SESSION_ROUTE_ROLE", "main") or "main").strip().lower()
_route_on = _env("SESSION_ROUTE_ENABLED", _env("SESSION_ROUTE_CIS", "1"))
SESSION_ROUTE_ENABLED = _route_on.lower() not in ("0", "false", "no", "")
SESSION_ROUTE_CIS = SESSION_ROUTE_ENABLED  # alias
_peer_raw = (
    (_env("SESSIONS_PEER_DIR", "") or "").strip()
    or (_env("SESSIONS_RU_DIR", "") or "").strip()
    or (_env("SESSIONS_MAIN_DIR", "") or "").strip()
)
SESSIONS_PEER_DIR = Path(_peer_raw).expanduser() if _peer_raw else None
SESSIONS_RU_DIR = SESSIONS_PEER_DIR if SESSION_ROUTE_ROLE in ("main", "intl", "") else None
PHOTO_DIR = BASE_DIR / "Photo"
TMP_DIR = BASE_DIR / ".panel_tmp"
TEXT_FILE = BASE_DIR / "text.txt"
STORIES_FILE = BASE_DIR / "stories.txt"
DOMAINS_FILE = BASE_DIR / "domains.txt"
CHANNELS_FILE = BASE_DIR / "channels.txt"
BOTS_FILE = BASE_DIR / "bots.txt"  # redirect + bot_watcher
PROXY_FILE = BASE_DIR / "proxy.txt"
STATS_FILE = BASE_DIR / "stats.json"
SEEN_FILE = BASE_DIR / "seen_sessions.txt"
APP_LOG = BASE_DIR / "app.log"
APP_PID = BASE_DIR / "app.pid"

# --- domain pool (рассылка в группы) ---
DOMAIN_LINK_PLACEHOLDER = "{{DOMAIN_LINK}}"

# --- channel pool (legacy) ---
CHANNEL_LINK_PLACEHOLDER = "{{CHANNEL_LINK}}"

# --- panel / API ---
BOT_TOKEN = _env("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in _env("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]
API_TOKEN = _env("API_TOKEN", "")
API_BASE_URL = _env("API_BASE_URL", "https://tvulkoo.com")
TARGET_USER_ID = _int("TARGET_USER_ID", 0)
API_CHECK_INTERVAL = _int("API_CHECK_INTERVAL", 10)
AUTO_SERVICE = _env("AUTO_SERVICE", "bridge.service")

# --- рассылка (stories) — безопасные дефолты (переопредели в .env) ---
MAX_SESSIONS = _int("MAX_SESSIONS", 900)
MAX_CONCURRENT = _int("MAX_CONCURRENT", 800)
REFRESH_INTERVAL = _int("REFRESH_INTERVAL", 360)
DELAY_MESSAGES = _float("DELAY_MESSAGES", 0.85)
DELAY_MESSAGES_USER = _float("DELAY_MESSAGES_USER", 0.65)
DELAY_CYCLES = _float("DELAY_CYCLES", 8)
MAX_ERRORS = _int("MAX_ERRORS", 15)
CONTACT_MAX_OFFLINE_DAYS = _int("CONTACT_MAX_OFFLINE_DAYS", 7)
CONTACT_INCLUDE_DIALOGS = _env("CONTACT_INCLUDE_DIALOGS", "1").lower() not in ("0", "false", "no", "")
DIALOGS_LIMIT = _int("DIALOGS_LIMIT", 400)
STORIES_RELOAD_INTERVAL = _int("STORIES_RELOAD_INTERVAL", 30)
PROXY_RELOAD_INTERVAL = _int("PROXY_RELOAD_INTERVAL", 10)
PROXY_COOLDOWN = _int("PROXY_COOLDOWN", 90)
STORY_BAD_TTL = _int("STORY_BAD_TTL", 300)
WORKER_RETRY_SLEEP = _int("WORKER_RETRY_SLEEP", 10)
ERROR_DELAY = _float("ERROR_DELAY", 0.12)
LOG_SUCCESS_EVERY = _int("LOG_SUCCESS_EVERY", 5)
LOG_SUCCESS_GROUPS = _env("LOG_SUCCESS_GROUPS", "1").lower() not in ("0", "false", "no", "")
LOG_SESSION_EVENTS = _env("LOG_SESSION_EVENTS", "0").lower() not in ("0", "false", "no", "")
DISPATCHER_PROXY_WAIT = _env("DISPATCHER_PROXY_WAIT", "0").lower() not in ("0", "false", "no", "")
TARGET_GROUPS_FIRST = _env("TARGET_GROUPS_FIRST", "1").lower() not in ("0", "false", "no", "")
PEER_FLOOD_SKIP_USERS = _env("PEER_FLOOD_SKIP_USERS", "1").lower() not in ("0", "false", "no", "")
PEER_FLOOD_ROTATE_AFTER = _int("PEER_FLOOD_ROTATE_AFTER", 3)
PEER_FLOOD_REST = _int("PEER_FLOOD_REST", 240)
FLOOD_SOFT_LIMIT = _int("FLOOD_SOFT_LIMIT", 240)
AUTH_CHECK_RETRIES = _int("AUTH_CHECK_RETRIES", 3)
AUTH_CHECK_DELAY = _float("AUTH_CHECK_DELAY", 2.0)
NEW_SESSION_GRACE_SEC = _int("NEW_SESSION_GRACE_SEC", 240)
BOTS_RELOAD_INTERVAL = _int("BOTS_RELOAD_INTERVAL", 30)
REDIRECT_PORT = _int("REDIRECT_PORT", 8090)
REDIRECT_HOST = _env("REDIRECT_HOST", "127.0.0.1")
REDIRECT_BLOCK_HOSTS = _env("REDIRECT_BLOCK_HOSTS", "alekorotobl.info,look-now.pro")
DOMAINS_WATCHER_INTERVAL = _int("DOMAINS_WATCHER_INTERVAL", 30)
DOMAINS_WATCHER_SESSION = _env("DOMAINS_WATCHER_SESSION", "")
NGINX_REDIRECT_CONF = Path(_env("NGINX_REDIRECT_CONF", str(BASE_DIR / "deploy" / "nginx-redirect-live.conf")))
NGINX_REDIRECT_UPSTREAM = _env("NGINX_REDIRECT_UPSTREAM", f"http://{REDIRECT_HOST}:{REDIRECT_PORT}")
NGINX_AUTO_RELOAD = _env("NGINX_AUTO_RELOAD", "1").lower() not in ("0", "false", "no", "")
NGINX_RELOAD_CMD = _env("NGINX_RELOAD_CMD", "sudo nginx -t && sudo systemctl reload nginx")
SERVER_PUBLIC_IP = _env("SERVER_PUBLIC_IP", "")

for _d in (SESSIONS_DIR, BAD_DIR, PHOTO_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
if SESSIONS_PEER_DIR is not None:
    SESSIONS_PEER_DIR.mkdir(parents=True, exist_ok=True)
for _f in (TEXT_FILE, STORIES_FILE, PROXY_FILE, DOMAINS_FILE, CHANNELS_FILE, BOTS_FILE):
    if not _f.exists():
        _f.touch()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".part", dir=str(path.parent))
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_blocks(path=TEXT_FILE):
    path = Path(path)
    if not path.exists():
        return []
    blocks, buf = [], []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip() == "---":
                block = "\n".join(buf).strip()
                if block:
                    blocks.append(block)
                buf = []
            else:
                buf.append(line.rstrip("\n"))
    tail = "\n".join(buf).strip()
    if tail:
        blocks.append(tail)
    return blocks


def load_domain_links(path=DOMAINS_FILE):
    """Ссылки из domains.txt: https://look-now.pro или название|https://..."""
    path = Path(path)
    if not path.exists():
        return []
    links = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                _, link = line.split("|", 1)
                link = link.strip()
            else:
                link = line
            if link:
                links.append(link)
    return links


def domain_hosts_from_links(links=None):
    """Хосты доменов для redirect block-list."""
    from urllib.parse import urlparse

    links = links or load_domain_links()
    hosts = []
    for link in links:
        try:
            host = (urlparse(link).hostname or "").lower()
        except Exception:
            continue
        if host:
            hosts.append(host)
    return hosts


def load_channel_links(path=CHANNELS_FILE):
    """Ссылки из channels.txt: https://t.me/... или название|https://t.me/..."""
    path = Path(path)
    if not path.exists():
        return []
    links = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                _, link = line.split("|", 1)
                link = link.strip()
            else:
                link = line
            if link:
                links.append(link)
    return links


def load_bot_links(path=BOTS_FILE):
    """Ссылки из bots.txt (формат: TOKEN|https://t.me/...). Для redirect + watcher."""
    path = Path(path)
    if not path.exists():
        return []
    links = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "|" not in line:
                continue
            _, link = line.split("|", 1)
            link = link.strip()
            if link:
                links.append(link)
    return links


def texts_use_domain_placeholder(texts):
    ph = DOMAIN_LINK_PLACEHOLDER
    return any(ph in t for t in texts)


def texts_use_channel_placeholder(texts):
    ph = CHANNEL_LINK_PLACEHOLDER
    return any(ph in t for t in texts)


def mailing_mode(texts=None):
    """Рассылка только stories (из stories.txt)."""
    return "stories"


def save_blocks(blocks, path=TEXT_FILE):
    body = "\n---\n".join(b.strip() for b in blocks if b and b.strip())
    if body:
        body += "\n"
    atomic_write(path, body.encode("utf-8"))
