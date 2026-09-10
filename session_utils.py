"""Помощники для Telethon session SQLite и переподключения."""

import asyncio
import sqlite3
from collections import defaultdict

import config


def tune_session_sqlite(client) -> None:
    """WAL + busy_timeout — меньше unable to open database file при нагрузке."""
    conn = getattr(client.session, "_conn", None)
    if conn is None:
        return
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        pass


def is_session_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, sqlite3.OperationalError, OSError)):
        return True
    msg = (str(exc) or "").lower()
    return (
        "unable to open database file" in msg
        or "disconnected" in msg
        or "database is locked" in msg
        or "disk i/o error" in msg
    )


def session_error_label(exc: BaseException) -> str:
    if isinstance(exc, sqlite3.OperationalError):
        return "sqlite session"
    if isinstance(exc, ConnectionError):
        return "disconnected"
    msg = (str(exc) or "").lower()
    if "unable to open database file" in msg:
        return "sqlite session"
    if "disconnected" in msg:
        return "disconnected"
    return type(exc).__name__


class SessionSendGuard:
    """Один lock на .session — без гонок SQLite внутри аккаунта."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock_for(self, session_path: str) -> asyncio.Lock:
        return self._locks[session_path]


async def ensure_connected(client, sid: str, log) -> bool:
    try:
        if client.is_connected():
            return True
        await client.connect()
        tune_session_sqlite(client)
        return client.is_connected()
    except Exception as e:
        log.warning(f"{sid} | reconnect fail: {e}")
        return False


async def with_reconnect(client, sid: str, log, op):
    """Выполняет op(); при session/disconnect ошибке — reconnect и повтор."""
    last_exc = None
    attempts = max(1, config.SEND_RETRIES + 1)
    for attempt in range(attempts):
        if not await ensure_connected(client, sid, log):
            await asyncio.sleep(0.3 * (attempt + 1))
            continue
        try:
            return await op()
        except Exception as e:
            last_exc = e
            if not is_session_error(e) or attempt + 1 >= attempts:
                raise
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(0.4 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise ConnectionError("disconnected")
