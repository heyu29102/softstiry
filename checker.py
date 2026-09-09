# -*- coding: utf-8 -*-
import os
import sys
import glob
import time
import random
import asyncio
import logging
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple

import colorama
from colorama import Fore, Style

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    PhoneNumberBannedError,
    SessionPasswordNeededError,
    FloodWaitError,
    RPCError,
)

colorama.init(autoreset=True)

# ==========================
# НАСТРОЙКИ
# ==========================
API_ID = 35595498
API_HASH = 'ae320fdc0660988c908edf8474855887'

SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
BAD_SESSIONS_DIR = os.getenv("BAD_SESSIONS_DIR", "sessions_bad")
PROXY_FILE = os.getenv("PROXY_FILE", "proxy.txt")

CONCURRENCY = int(os.getenv("CONCURRENCY", "1000"))     # 1000 параллельных воркеров
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
ME_TIMEOUT = float(os.getenv("ME_TIMEOUT", "15"))

DELETE_BAD = os.getenv("DELETE_BAD", "1") == "1"
DELETE_NOT_AUTH = os.getenv("DELETE_NOT_AUTH", "0") == "1"  # удалять NOT_AUTH

# ==========================
# ЛОГГЕР
# ==========================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("checker")
    logger.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(h)
    return logger

log = setup_logger()

# ==========================
# ПРОКСИ
# ==========================
@dataclass
class ProxyItem:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None

def load_proxies(path: str) -> List[ProxyItem]:
    if not os.path.exists(path):
        log.warning(f"{Fore.YELLOW}⚠ proxy файл не найден: {path}{Style.RESET_ALL}")
        return []

    proxies: List[ProxyItem] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            line = line.replace("socks5://", "").replace("http://", "").replace("socks5//", "").replace("http//", "")
            parts = [p.strip() for p in line.split(":")]
            if len(parts) not in (2, 4):
                continue

            host = parts[0]
            try:
                port = int(parts[1])
            except ValueError:
                continue

            if len(parts) == 2:
                proxies.append(ProxyItem(host, port))
            else:
                proxies.append(ProxyItem(host, port, parts[2], parts[3]))

    random.shuffle(proxies)
    log.info(f"📡 Прокси загружено: {len(proxies)}")
    return proxies

def telethon_proxy_tuple(p: ProxyItem):
    import socks
    return (socks.SOCKS5, p.host, p.port, True, p.username, p.password)

# ==========================
# SQLITE CHECK (важно!)
# ==========================
SQLITE_MAGIC = b"SQLite format 3\x00"

def looks_like_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        return head.startswith(SQLITE_MAGIC)
    except Exception:
        return False

# ==========================
# УДАЛЕНИЕ “ЖЕЛЕЗНО”
# ==========================
def _safe_remove(path: str, tries: int = 6, delay: float = 0.12) -> bool:
    for _ in range(tries):
        try:
            if os.path.exists(path):
                os.remove(path)
            return True
        except Exception:
            time.sleep(delay)
    return not os.path.exists(path)

def nuke_session(session_path: str) -> Tuple[bool, List[str], Optional[str]]:
    deleted: List[str] = []
    moved_to: Optional[str] = None

    base = session_path
    targets = set()

    # стандартные хвосты sqlite
    targets.add(base)
    targets.add(base + "-journal")
    targets.add(base + "-wal")
    targets.add(base + "-shm")
    targets.add(base + ".journal")

    if not base.endswith(".session"):
        targets.add(base + ".session")
        targets.add(base + ".session-journal")
        targets.add(base + ".session-wal")
        targets.add(base + ".session-shm")
        targets.add(base + ".session.journal")

    for t in sorted(targets):
        if os.path.exists(t):
            if _safe_remove(t):
                if not os.path.exists(t):
                    deleted.append(t)

    main = session_path if session_path.endswith(".session") else session_path + ".session"

    # если не удалилось — переносим хотя бы главный файл
    if os.path.exists(main):
        try:
            os.makedirs(BAD_SESSIONS_DIR, exist_ok=True)
            dst = os.path.join(BAD_SESSIONS_DIR, os.path.basename(main))
            if os.path.exists(dst):
                name, ext = os.path.splitext(dst)
                dst = f"{name}_{int(time.time())}{ext}"
            shutil.move(main, dst)
            moved_to = dst

            _safe_remove(main + "-wal")
            _safe_remove(main + "-shm")
            _safe_remove(main + "-journal")
            _safe_remove(main + ".journal")
        except Exception:
            pass

    ok_final = not os.path.exists(main)
    return ok_final, deleted, moved_to

# ==========================
# ВЕРДИКТЫ
# ==========================
class SessionVerdict:
    OK = "ok"
    NOT_AUTH = "not_auth"
    NEED_2FA = "need_2fa"
    BANNED = "banned"
    DUPLICATED = "duplicated"
    DEAD = "dead"
    FLOODWAIT = "floodwait"
    RPC = "rpc"
    TIMEOUT = "timeout"
    INIT_FAIL = "init_fail"
    UNKNOWN = "unknown"

def is_bad_for_delete(verdict: str) -> bool:
    if verdict in (SessionVerdict.BANNED, SessionVerdict.DEAD, SessionVerdict.DUPLICATED, SessionVerdict.INIT_FAIL):
        return True
    if verdict == SessionVerdict.NOT_AUTH and DELETE_NOT_AUTH:
        return True
    return False

# ==========================
# CHECK ONE
# ==========================
async def check_one(session_path: str, proxy: Optional[ProxyItem]) -> Tuple[str, str, str]:
    # если не sqlite — сразу в мусор
    if not looks_like_sqlite(session_path):
        return session_path, SessionVerdict.INIT_FAIL, "not sqlite / corrupted session file"

    proxy_arg = None
    if proxy is not None:
        try:
            proxy_arg = telethon_proxy_tuple(proxy)
        except Exception as e:
            return session_path, SessionVerdict.UNKNOWN, f"proxy_err: {e}"

    # ВАЖНО: оборачиваем создание клиента
    try:
        client = TelegramClient(
            session_path,
            API_ID,
            API_HASH,
            proxy=proxy_arg,
            connection_retries=0,
            retry_delay=0,
            timeout=int(max(CONNECT_TIMEOUT, ME_TIMEOUT)),
        )
    except Exception as e:
        return session_path, SessionVerdict.INIT_FAIL, f"client init error: {type(e).__name__}: {e}"

    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)

        authed = await asyncio.wait_for(client.is_user_authorized(), timeout=ME_TIMEOUT)
        if not authed:
            return session_path, SessionVerdict.NOT_AUTH, "not authorized"

        me = await asyncio.wait_for(client.get_me(), timeout=ME_TIMEOUT)
        uname = getattr(me, "username", None)
        uid = getattr(me, "id", None)
        phone = getattr(me, "phone", None)
        return session_path, SessionVerdict.OK, f"id={uid} user={uname} phone={phone}"

    except SessionPasswordNeededError:
        return session_path, SessionVerdict.NEED_2FA, "2FA required"

    except (UserDeactivatedBanError, PhoneNumberBannedError):
        return session_path, SessionVerdict.BANNED, "banned/deactivated(ban)"

    except UserDeactivatedError:
        return session_path, SessionVerdict.DEAD, "deactivated"

    except AuthKeyDuplicatedError:
        return session_path, SessionVerdict.DUPLICATED, "auth key duplicated"

    except FloodWaitError as e:
        return session_path, SessionVerdict.FLOODWAIT, f"FloodWait {getattr(e, 'seconds', '?')}s"

    except asyncio.TimeoutError:
        return session_path, SessionVerdict.TIMEOUT, "timeout"

    except RPCError as e:
        return session_path, SessionVerdict.RPC, f"{type(e).__name__}: {e}"

    except Exception as e:
        return session_path, SessionVerdict.UNKNOWN, f"{type(e).__name__}: {e}"

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

# ==========================
# STATS + WORKERS
# ==========================
class Stats:
    def __init__(self):
        self.ok = self.not_auth = self.need2fa = 0
        self.banned = self.dup = self.dead = 0
        self.flood = self.rpc = self.timeout = 0
        self.init_fail = self.unknown = 0
        self.deleted_sessions = 0
        self.done = 0
        self.total = 0
        self._lock = asyncio.Lock()

    async def inc(self, field: str, n: int = 1):
        async with self._lock:
            setattr(self, field, getattr(self, field) + n)

async def worker(wid: int, q: asyncio.Queue, proxies: List[ProxyItem], stats: Stats):
    while True:
        session_path = await q.get()
        try:
            if session_path is None:
                return

            proxy = random.choice(proxies) if proxies else None

            sp, verdict, details = await check_one(session_path, proxy)
            name = os.path.basename(sp)

            # лог + счетчики
            if verdict == SessionVerdict.OK:
                await stats.inc("ok")
                log.info(f"{Fore.GREEN}✅ OK{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.NEED_2FA:
                await stats.inc("need2fa")
                log.info(f"{Fore.CYAN}🔐 2FA{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.NOT_AUTH:
                await stats.inc("not_auth")
                log.warning(f"{Fore.YELLOW}⚠ NOT_AUTH{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.BANNED:
                await stats.inc("banned")
                log.error(f"{Fore.RED}⛔ BANNED{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.DUPLICATED:
                await stats.inc("dup")
                log.error(f"{Fore.MAGENTA}♻ DUPLICATED{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.DEAD:
                await stats.inc("dead")
                log.error(f"{Fore.RED}💀 DEAD{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.FLOODWAIT:
                await stats.inc("flood")
                log.warning(f"{Fore.YELLOW}⏳ FLOODWAIT{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.RPC:
                await stats.inc("rpc")
                log.warning(f"{Fore.YELLOW}📨 RPC{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.TIMEOUT:
                await stats.inc("timeout")
                log.warning(f"{Fore.YELLOW}⌛ TIMEOUT{Style.RESET_ALL} {name} :: {details}")

            elif verdict == SessionVerdict.INIT_FAIL:
                await stats.inc("init_fail")
                log.error(f"{Fore.RED}🧨 CORRUPT{Style.RESET_ALL} {name} :: {details}")

            else:
                await stats.inc("unknown")
                log.warning(f"{Fore.YELLOW}❔ UNKNOWN{Style.RESET_ALL} {name} :: {details}")

            # удаление
            if DELETE_BAD and is_bad_for_delete(verdict):
                ok_del, deleted_files, moved_to = nuke_session(sp)
                if ok_del:
                    await stats.inc("deleted_sessions")
                    log.error(f"{Fore.RED}🧹 SESSION DELETED{Style.RESET_ALL} {name} | files={len(deleted_files)}")
                else:
                    if moved_to:
                        await stats.inc("deleted_sessions")
                        log.error(f"{Fore.RED}🧹 MOVED TO BAD{Style.RESET_ALL} {name} -> {moved_to}")
                    else:
                        log.error(f"{Fore.RED}❌ FAILED DELETE{Style.RESET_ALL} {name}")

            await stats.inc("done")

        except Exception as e:
            # ВАЖНО: воркер не должен умереть НИКОГДА
            log.error(f"{Fore.RED}🔥 Worker#{wid} crashed on {session_path}: {type(e).__name__}: {e}{Style.RESET_ALL}")
            await stats.inc("unknown")
            await stats.inc("done")

        finally:
            q.task_done()

async def progress_printer(stats: Stats, q: asyncio.Queue, every_sec: float = 2.0):
    while True:
        await asyncio.sleep(every_sec)
        done = stats.done
        total = stats.total
        qs = q.qsize()
        if total:
            pct = (done / total) * 100.0
            log.info(f"📈 Progress: {done}/{total} ({pct:.2f}%) | q={qs} | deleted_sessions={stats.deleted_sessions}")
        if total and done >= total:
            return

# ==========================
# MAIN
# ==========================
async def main():
    if API_ID == 0 or not API_HASH:
        log.error(f"{Fore.RED}❌ Заполни TG_API_ID / TG_API_HASH через env{Style.RESET_ALL}")
        return

    if not os.path.isdir(SESSIONS_DIR):
        log.error(f"{Fore.RED}❌ Папка сессий не найдена: {SESSIONS_DIR}{Style.RESET_ALL}")
        return

    os.makedirs(BAD_SESSIONS_DIR, exist_ok=True)

    proxies = load_proxies(PROXY_FILE)
    sessions = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.session")))

    log.info(f"📦 Сессий найдено: {len(sessions)}")
    if not sessions:
        return

    stats = Stats()
    stats.total = len(sessions)

    q: asyncio.Queue = asyncio.Queue(maxsize=CONCURRENCY * 5)

    workers = [asyncio.create_task(worker(i + 1, q, proxies, stats)) for i in range(CONCURRENCY)]
    prog = asyncio.create_task(progress_printer(stats, q, every_sec=2.0))

    start = time.time()

    for s in sessions:
        await q.put(s)

    for _ in range(CONCURRENCY):
        await q.put(None)

    await q.join()
    await prog
    for w in workers:
        await w

    elapsed = time.time() - start
    log.info("")
    log.info("======== SUMMARY ========")
    log.info(f"✅ OK: {stats.ok}")
    log.info(f"🔐 2FA: {stats.need2fa}")
    log.info(f"⚠ NOT_AUTH: {stats.not_auth}")
    log.info(f"⛔ BANNED: {stats.banned}")
    log.info(f"♻ DUPLICATED: {stats.dup}")
    log.info(f"💀 DEAD: {stats.dead}")
    log.info(f"🧨 CORRUPT/INIT_FAIL: {stats.init_fail}")
    log.info(f"⏳ FLOODWAIT: {stats.flood}")
    log.info(f"📨 RPC: {stats.rpc}")
    log.info(f"⌛ TIMEOUT: {stats.timeout}")
    log.info(f"❔ UNKNOWN: {stats.unknown}")
    log.info(f"🧹 Deleted sessions: {stats.deleted_sessions}")
    log.info(f"⏱ Time: {elapsed:.2f}s")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")