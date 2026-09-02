import asyncio
import json
import logging
import os
import random
import secrets
import shutil
import signal
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler

import colorama
from opentele.tl import TelegramClient
from opentele.api import API
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatAdminRequiredError,
    ChatSendMediaForbiddenError,
    ChatSendPhotosForbiddenError,
    ChatWriteForbiddenError,
    ChannelInvalidError,
    FloodWaitError,
    PeerFloodError,
    PeerIdInvalidError,
    PhoneNumberBannedError,
    RPCError,
    SlowModeWaitError,
    TypeNotFoundError,
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl import functions
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import SendMediaRequest
from telethon.tl.functions.stories import GetStoriesByIDRequest
from telethon.tl.types import InputMediaStory
from telethon import helpers

import config
from telethon_patch import apply_telethon_patch, is_tl_schema_error, schema_error_label
from proxies import ProxyPool
from story_refs import StoryRef, load_story_refs
from target_select import collect_targets, reshuffle_target_order, target_username
from textgen import jitter, jitter_up

colorama.init(autoreset=True)
apply_telethon_patch()

FLOOD_SOFT_LIMIT = config.FLOOD_SOFT_LIMIT
SESSION_DEAD_ERRORS = (
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    PhoneNumberBannedError,
)


class Color(logging.Formatter):
    C = {"INFO": colorama.Fore.GREEN, "WARNING": colorama.Fore.YELLOW, "ERROR": colorama.Fore.RED}

    def format(self, record):
        return self.C.get(record.levelname, colorama.Fore.WHITE) + super().format(record)


def setup_logger():
    log = logging.getLogger("spam")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt, datefmt = "[%(asctime)s] %(message)s", "%H:%M:%S"
    fh = RotatingFileHandler(str(config.APP_LOG), maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    log.addHandler(fh)
    # stdout панели тоже пишет в app.log — без TTY не дублируем в файл.
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(Color(fmt, datefmt))
        log.addHandler(sh)
    return log


log = setup_logger()


def humanize(e):
    if isinstance(e, UserBannedInChannelError):
        return "заблокирован в чате"
    if isinstance(e, ChatWriteForbiddenError):
        return "нет права писать"
    if isinstance(e, (ChatSendMediaForbiddenError, ChatSendPhotosForbiddenError)):
        return "медиа/story запрещено в чате"
    if isinstance(e, ChatAdminRequiredError):
        return "нужен админ"
    if isinstance(e, FloodWaitError):
        return f"FloodWait {getattr(e, 'seconds', 0)}с"
    if isinstance(e, SlowModeWaitError):
        return f"SlowMode {getattr(e, 'seconds', 0)}с"
    if isinstance(e, PeerFloodError):
        return "PeerFlood"
    if isinstance(e, PeerIdInvalidError):
        return "битая цель (peer invalid)"
    if isinstance(e, ChannelInvalidError):
        return "битый канал/story peer"
    if isinstance(e, (UsernameInvalidError, UsernameNotOccupiedError)):
        return "лимит resolve у сессии (канал скорее живой)"
    if isinstance(e, TypeNotFoundError):
        return schema_error_label(e)
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    if "STORY_ID_INVALID" in msg:
        return "история истекла/невалидна"
    if "CHAT_SEND_MEDIA_FORBIDDEN" in msg:
        return "медиа запрещено"
    return f"{type(e).__name__}: {e}"


def is_story_peer_error(exc: BaseException) -> bool:
    if isinstance(exc, (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError)):
        return True
    if isinstance(exc, ValueError):
        msg = str(exc).lower()
        return "username" in msg or "as username" in msg
    return False


def is_permanent_story_peer_error(exc: BaseException) -> bool:
    return is_story_peer_error(exc)


def delete_session_files(path):
    try:
        for t in (path, path + ".journal", path + "-journal", os.path.splitext(path)[0] + ".json"):
            for _ in range(3):
                try:
                    if os.path.exists(t):
                        os.remove(t)
                    break
                except Exception:
                    time.sleep(0.2)
        if os.path.exists(path):
            dst = os.path.join(str(config.BAD_DIR), os.path.basename(path))
            if os.path.exists(dst):
                dst = os.path.join(str(config.BAD_DIR), f"{os.path.basename(path)}_{int(time.time())}")
            shutil.move(path, dst)
    except Exception:
        log.exception("не смог убрать файлы сессии")
    return not os.path.exists(path)


class Spammer:
    def __init__(self):
        self.proxies = None
        self.stories: list[StoryRef] = []
        self.pending = deque()
        self.wake = asyncio.Event()
        self.seen = set()
        self.slots = asyncio.Semaphore(config.MAX_SESSIONS)
        self.sema = asyncio.Semaphore(config.MAX_CONCURRENT)
        self.total = 0
        self.active = 0
        self.sent_ts = deque()
        self.flood_ts = deque()
        self.started = time.time()
        self.tasks = set()
        self.stop = asyncio.Event()
        self.flood_rest = {}
        self.bad_stories: dict[tuple[str, int], tuple[str, float]] = {}
        self.story_warm_full = 0
        self.story_warm_partial = 0
        self.story_warm_zero = 0
        self.story_err_ts = deque()
        self.story_resolve_sema = asyncio.Semaphore(config.STORY_RESOLVE_CONCURRENT)
        self._resolve_channel_locks: dict[str, asyncio.Lock] = {}
        self._resolve_channel_guard = asyncio.Lock()
        self._channel_resolve_last_ts: dict[str, float] = {}
        self.story_channel_last_ok: dict[str, float] = {}
        self.auth_strikes: dict[str, int] = {}
        self.recent_group_hits: deque = deque(maxlen=config.RECENT_GROUP_HITS)

    def push_front(self, path):
        self.pending.appendleft(path)
        self.wake.set()

    def push_back(self, path):
        self.pending.append(path)
        self.wake.set()

    async def next_path(self):
        while True:
            if self.pending:
                return self.pending.popleft()
            self.wake.clear()
            if self.pending:
                continue
            await self.wake.wait()

    def load_seen(self):
        try:
            if config.SEEN_FILE.exists():
                with config.SEEN_FILE.open("r", encoding="utf-8") as f:
                    for line in f:
                        name = line.strip()
                        if name:
                            self.seen.add(name)
        except Exception:
            pass

    def save_seen(self):
        try:
            config.atomic_write(config.SEEN_FILE, ("\n".join(sorted(self.seen)) + "\n").encode("utf-8"))
        except Exception:
            pass

    def load_stories(self):
        self.stories = load_story_refs(config.STORIES_FILE)

    def _session_key(self, path: str) -> str:
        return os.path.basename(path)

    def _handle_session_dead(self, sid: str, path: str, reason: str) -> tuple[bool, str]:
        """Returns (delete_files, worker_result)."""
        key = self._session_key(path)
        self.auth_strikes[key] = self.auth_strikes.get(key, 0) + 1
        strikes = self.auth_strikes[key]
        if config.DELETE_SESSION_ON_AUTH_FAIL and strikes >= config.AUTH_DELETE_AFTER:
            log.error(f"{sid} | ❌ удаляю после {strikes} подтверждений: {reason}")
            return True, "drop"
        rest = config.SESSION_AUTH_REST
        self.flood_rest[key] = rest
        log.warning(f"{sid} | 💤 {reason} — отдых {rest}с (strike {strikes}, не удаляю)")
        return False, "retry"

    def _clear_auth_strike(self, path: str):
        self.auth_strikes.pop(self._session_key(path), None)

    async def _session_auth_status(self, client, sid: str) -> str:
        """ok | retry | dead"""
        for attempt in range(1, config.AUTH_CHECK_RETRIES + 1):
            try:
                if not await client.is_user_authorized():
                    try:
                        await client.get_me()
                    except SESSION_DEAD_ERRORS as exc:
                        log.warning(f"{sid} | 💀 {type(exc).__name__}")
                        return "dead"
                    except Exception as exc:
                        if attempt >= config.AUTH_CHECK_RETRIES:
                            log.warning(f"{sid} | ⚠️ не авторизована ({exc}), в очередь")
                            return "retry"
                    else:
                        return "ok"
                else:
                    await client.get_me()
                    return "ok"
            except SESSION_DEAD_ERRORS as exc:
                log.warning(f"{sid} | 💀 {type(exc).__name__}")
                return "dead"
            except Exception as exc:
                if attempt >= config.AUTH_CHECK_RETRIES:
                    log.warning(f"{sid} | ⚠️ авторизация: {exc}, в очередь")
                    return "retry"
            if attempt < config.AUTH_CHECK_RETRIES:
                await asyncio.sleep(config.AUTH_CHECK_DELAY * attempt)
        return "retry"

    def story_key(self, story: StoryRef) -> tuple[str, int]:
        return (story.peer.lower(), story.story_id)

    def is_story_bad(self, story: StoryRef) -> bool:
        key = self.story_key(story)
        item = self.bad_stories.get(key)
        if not item:
            return False
        reason, until = item
        if until <= time.time():
            self.bad_stories.pop(key, None)
            return False
        return True

    def _story_err_label(self, story: StoryRef, reason: str) -> str:
        key = story.peer.lower()
        if key in self.story_channel_last_ok and time.time() - self.story_channel_last_ok[key] < 600:
            return "лимит resolve у сессии (канал живой, retry)"
        return reason

    def _channel_recently_ok(self, peer: str, within: float = 300.0) -> bool:
        key = peer.lstrip("@").lower()
        ts = self.story_channel_last_ok.get(key)
        return ts is not None and time.time() - ts < within

    async def _get_resolve_channel_lock(self, username: str) -> asyncio.Lock:
        key = username.lstrip("@").lower()
        async with self._resolve_channel_guard:
            lock = self._resolve_channel_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._resolve_channel_locks[key] = lock
            return lock

    async def _channel_gap_wait(self, username: str):
        gap = config.STORY_RESOLVE_CHANNEL_GAP
        if gap <= 0:
            return
        key = username.lstrip("@").lower()
        last = self._channel_resolve_last_ts.get(key, 0.0)
        wait = gap - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def _touch_channel_resolve(self, username: str):
        self._channel_resolve_last_ts[username.lstrip("@").lower()] = time.time()

    def mark_story_error(self):
        now = time.time()
        self.story_err_ts.append(now)
        while self.story_err_ts and self.story_err_ts[0] < now - 60:
            self.story_err_ts.popleft()

    def _record_warm(self, ready: int, total: int):
        if total <= 0:
            return
        if ready >= total:
            self.story_warm_full += 1
        elif ready > 0:
            self.story_warm_partial += 1
        else:
            self.story_warm_zero += 1

    def mark_story_bad(self, story: StoryRef, reason: str, ttl: int | None = None):
        """Глобальный бан — только для подтверждённо мёртвой story (STORY_ID_INVALID)."""
        ttl = config.STORY_BAD_TTL if ttl is None else ttl
        self.bad_stories[self.story_key(story)] = (reason, time.time() + ttl)
        log.warning(f"📖 story истекла: {story.url} ({reason}, пауза {ttl}с)")

    def pick_story(
        self,
        rng: random.Random,
        fail_counts: dict[tuple[str, int], int] | None = None,
        story_cache: dict | None = None,
        rr_idx: int | None = None,
    ) -> StoryRef | None:
        fail_counts = fail_counts or {}
        limit = config.STORY_SKIP_AFTER
        pool = [
            s
            for s in self.stories
            if fail_counts.get(self.story_key(s), 0) < limit and not self.is_story_bad(s)
        ]
        if not pool and self.stories:
            pool = [s for s in self.stories if not self.is_story_bad(s)]
        if not pool:
            return None
        pool.sort(key=lambda s: (s.peer.lower(), s.story_id))
        if len(pool) > 1 and rr_idx is not None:
            return pool[rr_idx % len(pool)]
        if story_cache and config.STORY_PREFER_CACHED:
            cached_pool = [s for s in pool if s.peer.lower() in story_cache]
            if cached_pool:
                return rng.choice(cached_pool)
        return rng.choice(pool)

    def load(self):
        self.proxies = ProxyPool.from_file()
        self.load_stories()
        log.info(
            f"📖 историй в пуле: {len(self.stories)} | "
            f"🛰 прокси: {len(self.proxies)} | "
            f"story: warm={config.STORY_WARM_ON_START} join={config.STORY_JOIN_CHANNEL} "
            f"retries={config.STORY_RESOLVE_RETRIES} | "
            f"оффлайн лимит: {config.CONTACT_MAX_OFFLINE_DAYS}д | "
            f"auth-delete={config.DELETE_SESSION_ON_AUTH_FAIL} peer-skip-users={config.PEER_FLOOD_SKIP_USERS}"
        )
        if not self.proxies.proxies:
            log.error("❌ Нет прокси, выхожу")
            sys.exit(1)
        if not self.stories:
            log.error("❌ stories.txt пуст — добавь истории (канал|id или t.me/.../s/id)")
            sys.exit(1)

    def mark_sent(self):
        now = time.time()
        self.total += 1
        self.sent_ts.append(now)
        self._trim(self.sent_ts, now)
        return self.total

    def mark_flood(self):
        now = time.time()
        self.flood_ts.append(now)
        self._trim(self.flood_ts, now)

    @staticmethod
    def _trim(buf, now):
        while buf and buf[0] < now - 60:
            buf.popleft()

    async def write_stats_loop(self):
        while not self.stop.is_set():
            now = time.time()
            self._trim(self.sent_ts, now)
            self._trim(self.flood_ts, now)
            data = {
                "ts": int(now),
                "uptime_sec": int(now - self.started),
                "total_sent": self.total,
                "sent_per_min": len(self.sent_ts),
                "flood_per_min": len(self.flood_ts),
                "active_sessions": self.active,
                "max_sessions": config.MAX_SESSIONS,
                "pending": len(self.pending),
                "proxies_total": len(self.proxies),
                "proxies_in_cooldown": self.proxies.cooldown_count(),
                "stories_in_pool": len(self.stories),
                "stories_bad": sum(1 for _, (_, until) in self.bad_stories.items() if until > now),
                "story_warm_full": self.story_warm_full,
                "story_warm_partial": self.story_warm_partial,
                "story_warm_zero": self.story_warm_zero,
                "story_errors_per_min": len(self.story_err_ts),
                "story_send_mode": "native_share",
                "recent_group_hits": list(self.recent_group_hits),
                "mailing_mode": "stories",
            }
            try:
                await asyncio.to_thread(
                    config.atomic_write,
                    config.STATS_FILE,
                    json.dumps(data, ensure_ascii=False).encode("utf-8"),
                )
            except Exception:
                pass
            await asyncio.sleep(5)

    async def log_stats_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(30)
            self._trim(self.sent_ts, time.time())
            self._trim(self.flood_ts, time.time())
            log.info(
                f"📊 в минуту: {len(self.sent_ts)} | flood/мин: {len(self.flood_ts)} | "
                f"story-ошибок/мин: {len(self.story_err_ts)} | "
                f"активных: {self.active}/{config.MAX_SESSIONS} | очередь: {len(self.pending)} | "
                f"всего: {self.total} | прокси в кулдауне: {self.proxies.cooldown_count()} | "
                f"историй: {len(self.stories)} | "
                f"warm OK/частично/0: {self.story_warm_full}/{self.story_warm_partial}/{self.story_warm_zero}"
            )

    async def reload_stories_loop(self):
        last_keys = {self.story_key(s) for s in self.stories}
        while not self.stop.is_set():
            await asyncio.sleep(config.STORIES_RELOAD_INTERVAL)
            try:
                stories = await asyncio.to_thread(load_story_refs, config.STORIES_FILE)
                if not stories:
                    continue
                new_keys = {self.story_key(s) for s in stories}
                if new_keys != last_keys:
                    self.bad_stories.clear()
                    log.info(f"📖 stories.txt обновлён: {len(stories)} историй, сброс bad")
                self.stories = stories
                last_keys = new_keys
            except Exception:
                pass

    async def watch_loop(self):
        while not self.stop.is_set():
            try:
                added = False
                for entry in os.scandir(str(config.SESSIONS_DIR)):
                    if entry.is_file() and entry.name.endswith(".session") and entry.name not in self.seen:
                        self.seen.add(entry.name)
                        self.push_front(entry.path)
                        log.info(f"🚀 Новая сессия → {entry.name} (в начало очереди)")
                        added = True
                if added:
                    self.save_seen()
            except Exception:
                pass
            await asyncio.sleep(1)

    def bootstrap(self):
        self.load_seen()
        files = []
        for entry in os.scandir(str(config.SESSIONS_DIR)):
            if entry.is_file() and entry.name.endswith(".session"):
                try:
                    files.append((entry.stat().st_mtime, entry.path, entry.name))
                except Exception:
                    continue
        files.sort()
        for _, path, name in files:
            self.seen.add(name)
            self.push_front(path)
        self.save_seen()
        log.info(f"📂 Загружено {len(files)} сессий, лимит одновременно: {config.MAX_SESSIONS}")

    async def _ensure_joined(self, client, username: str, joined_keys: set[str]):
        if not config.STORY_JOIN_CHANNEL:
            return
        key = username.lstrip("@").lower()
        if key in joined_keys:
            return
        uname = username.lstrip("@")
        try:
            entity = await client.get_entity(uname)
            await client(JoinChannelRequest(entity))
            joined_keys.add(key)
        except Exception as exc:
            if config.LOG_STORY_EVENTS:
                log.warning(f"📖 join @{uname}: {type(exc).__name__}")

    async def _session_sees_story(self, client, story_peer, story_id: int) -> bool:
        if not config.STORY_VERIFY_SESSION:
            return True
        try:
            result = await client(GetStoriesByIDRequest(peer=story_peer, id=[story_id]))
            items = getattr(result, "stories", None) or []
            return any(getattr(item, "id", None) == story_id for item in items)
        except RPCError as exc:
            msg = (getattr(exc, "message", "") or str(exc)).upper()
            if "STORY_ID_INVALID" in msg or "STORY_NOT_FOUND" in msg:
                return False
            return True
        except Exception:
            return True

    async def _prime_story_access(
        self,
        client,
        story: StoryRef,
        story_cache: dict,
        joined_keys: set[str],
    ) -> bool:
        """Join канал + resolve peer + проверка что эта сессия видит story ID."""
        await self._ensure_joined(client, story.peer, joined_keys)
        peer = await self.resolve_story_peer(client, story, story_cache, joined_keys)
        if not await self._session_sees_story(client, peer, story.story_id):
            story_cache.pop(story.peer.lower(), None)
            joined_keys.discard(story.peer.lower())
            await self._ensure_joined(client, story.peer, joined_keys)
            peer = await self.resolve_story_peer(client, story, story_cache, joined_keys)
            return await self._session_sees_story(client, peer, story.story_id)
        return True

    async def _resolve_peer_from_dialogs(self, client, username: str):
        uname = username.lstrip("@").lower()
        limit = min(config.DIALOGS_LIMIT, 200) if config.DIALOGS_LIMIT > 0 else 200
        try:
            async for dialog in client.iter_dialogs(limit=limit):
                entity = dialog.entity
                ent_user = getattr(entity, "username", None)
                if ent_user and ent_user.lower() == uname:
                    return await client.get_input_entity(entity)
        except Exception:
            pass
        return None

    async def _resolve_peer_by_entity(self, client, username: str):
        uname = username.lstrip("@")
        if not uname:
            return None
        entity = await client.get_entity(uname)
        return await client.get_input_entity(entity)

    async def _resolve_peer_by_username(self, client, username: str):
        uname = username.lstrip("@")
        if not uname:
            return None
        resolved = await client(ResolveUsernameRequest(username=uname))
        return await client.get_input_entity(resolved.peer)

    async def _resolve_peer_fast(self, client, username: str):
        """Без ResolveUsername — не бьёт в лимит lookup по username."""
        for resolver in (
            self._resolve_peer_by_entity,
            self._resolve_peer_from_dialogs,
        ):
            try:
                peer = await resolver(client, username)
                if peer is not None:
                    return peer
            except (UsernameInvalidError, UsernameNotOccupiedError, FloodWaitError):
                raise
            except Exception:
                continue
        return None

    async def _resolve_peer_username_throttled(self, client, username: str) -> object | None:
        lock = await self._get_resolve_channel_lock(username)
        acquired = False
        try:
            await asyncio.wait_for(lock.acquire(), timeout=config.STORY_RESOLVE_LOCK_TIMEOUT)
            acquired = True
            await self._channel_gap_wait(username)
            async with self.story_resolve_sema:
                peer = await self._resolve_peer_by_username(client, username)
            self._touch_channel_resolve(username)
            return peer
        except asyncio.TimeoutError:
            return None
        finally:
            if acquired:
                lock.release()

    async def resolve_story_peer(
        self,
        client,
        story: StoryRef,
        story_cache: dict,
        joined_keys: set[str] | None = None,
    ):
        joined_keys = joined_keys if joined_keys is not None else set()
        key = story.peer.lower()
        cached = story_cache.get(key)
        if cached is not None:
            await self._ensure_joined(client, story.peer, joined_keys)
            return cached

        await self._ensure_joined(client, story.peer, joined_keys)
        uname = story.peer.lstrip("@")
        last_exc: BaseException | None = None
        retries = max(1, config.STORY_RESOLVE_RETRIES)

        # Быстрый путь: get_entity / dialogs — без очереди по каналу.
        try:
            async with self.story_resolve_sema:
                peer = await self._resolve_peer_fast(client, uname)
            if peer is not None:
                await self._ensure_joined(client, uname, joined_keys)
                story_cache[key] = peer
                self.story_channel_last_ok[key] = time.time()
                return peer
        except FloodWaitError as exc:
            last_exc = exc
            await asyncio.sleep(min(getattr(exc, "seconds", 5) or 5, 30))
        except (UsernameInvalidError, UsernameNotOccupiedError):
            raise

        # Медленный путь: ResolveUsername — короткий lock, без долгой очереди.
        for attempt in range(1, retries + 1):
            try:
                peer = await self._resolve_peer_username_throttled(client, uname)
                if peer is not None:
                    await self._ensure_joined(client, uname, joined_keys)
                    story_cache[key] = peer
                    self.story_channel_last_ok[key] = time.time()
                    return peer
            except FloodWaitError as exc:
                last_exc = exc
                await asyncio.sleep(min(getattr(exc, "seconds", 5) or 5, 60))
            except (UsernameInvalidError, UsernameNotOccupiedError):
                raise
            except Exception as exc:
                last_exc = exc

            if attempt < retries:
                await asyncio.sleep(config.STORY_RESOLVE_DELAY * attempt)

        if last_exc and not is_permanent_story_peer_error(last_exc):
            raise last_exc
        raise UsernameNotOccupiedError(request=None)

    async def warm_story_peers(
        self,
        client,
        sid: str,
        story_cache: dict,
        joined_keys: set[str],
    ) -> int:
        """Перед рассылкой: join + resolve stories для этой сессии."""
        if not config.STORY_WARM_ON_START:
            return 0
        mode = config.STORY_WARM_MODE
        if config.STORY_WARM_STAGGER_MAX > 0:
            await asyncio.sleep(random.uniform(0, config.STORY_WARM_STAGGER_MAX))
        pending = [s for s in self.stories if not self.is_story_bad(s)]
        if not pending:
            return 0

        passes = 1 if mode == "lazy" else max(1, config.STORY_WARM_PASSES)
        for pass_n in range(1, passes + 1):
            still_pending = []
            for story in pending:
                key = story.peer.lower()
                if key in story_cache:
                    peer = story_cache[key]
                    if await self._session_sees_story(client, peer, story.story_id):
                        continue
                    story_cache.pop(key, None)
                    joined_keys.discard(key)
                try:
                    ok = await self._prime_story_access(client, story, story_cache, joined_keys)
                    if not ok:
                        still_pending.append(story)
                except Exception as exc:
                    still_pending.append(story)
                    if pass_n == passes:
                        log.warning(f"{sid} | 📖 warm pass {pass_n}: {story.url} — {humanize(exc)}")
                await asyncio.sleep(config.STORY_RESOLVE_DELAY)
            pending = still_pending
            if not pending:
                break
            if pass_n < passes:
                await asyncio.sleep(1.0 * pass_n)

        ready = sum(1 for s in self.stories if not self.is_story_bad(s) and s.peer.lower() in story_cache)
        total = sum(1 for s in self.stories if not self.is_story_bad(s))
        self._record_warm(ready, total)

        if ready >= total:
            tag, detail = "✅", "все истории видит"
        elif ready > 0:
            tag, detail = "⚠️", f"видит {ready}/{total}"
        else:
            tag, detail = "❌", "не видит истории — проверь прокси/сессию"

        join_note = f", join={len(joined_keys)}" if config.STORY_JOIN_CHANNEL else ""
        if config.LOG_STORY_EVENTS or ready < total:
            log.info(f"{sid} | 📖 warm {tag} {ready}/{total} ({detail}{join_note})")
            if ready < total:
                missing = [
                    s.url
                    for s in self.stories
                    if not self.is_story_bad(s) and s.peer.lower() not in story_cache
                ]
                log.warning(f"{sid} | 📖 не резолвятся: {', '.join(missing[:5])}")
        return ready

    async def send_story(
        self,
        client,
        target_entity,
        story: StoryRef,
        story_cache: dict,
        joined_keys: set[str],
    ):
        last_exc = None
        for attempt in range(2):
            try:
                story_peer = await self.resolve_story_peer(client, story, story_cache, joined_keys)
                if config.STORY_VERIFY_SESSION and not await self._session_sees_story(
                    client, story_peer, story.story_id
                ):
                    story_cache.pop(story.peer.lower(), None)
                    joined_keys.discard(story.peer.lower())
                    await self._ensure_joined(client, story.peer, joined_keys)
                    story_peer = await self.resolve_story_peer(client, story, story_cache, joined_keys)
                    if not await self._session_sees_story(client, story_peer, story.story_id):
                        raise UsernameNotOccupiedError(request=None)
            except TypeNotFoundError:
                apply_telethon_patch()
                raise
            except ValueError as exc:
                if is_story_peer_error(exc):
                    raise UsernameNotOccupiedError(request=None) from exc
                raise
            try:
                media = InputMediaStory(peer=story_peer, id=story.story_id)
                target_peer = await client.get_input_entity(target_entity)
                await client(
                    SendMediaRequest(
                        peer=target_peer,
                        media=media,
                        message="",
                        random_id=helpers.generate_random_long(),
                    )
                )
                return
            except (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError) as exc:
                last_exc = exc
                if not self._channel_recently_ok(story.peer):
                    story_cache.pop(story.peer.lower(), None)
                    joined_keys.discard(story.peer.lower())
                if attempt == 0:
                    await asyncio.sleep(config.STORY_RESOLVE_DELAY)
                    continue
                raise
        if last_exc:
            raise last_exc

    async def run_session(self, path):
        rng = random.Random(os.urandom(16))
        sid = os.path.splitext(os.path.basename(path))[0]
        if config.SESSION_START_STAGGER_MAX > 0:
            await asyncio.sleep(random.uniform(0, config.SESSION_START_STAGGER_MAX))
        proxy = self.proxies.acquire()
        if proxy is None:
            log.error(f"{sid} | ❌ нет свободных прокси")
            return "retry"
        api = API.TelegramDesktop.Generate(unique_id=sid)
        client = TelegramClient(path, api=api, proxy=self.proxies.to_dict(proxy))
        self.active += 1
        start = time.time()
        delete_after = False
        try:
            try:
                await client.connect()
            except Exception as e:
                log.warning(f"{sid} | ❌ подключение: {e}")
                self.proxies.mark_bad(proxy)
                self.flood_rest[self._session_key(path)] = config.WORKER_RETRY_SLEEP
                return "retry"
            auth = await self._session_auth_status(client, sid)
            if auth == "dead":
                delete_after, result = self._handle_session_dead(sid, path, "бан/ключ мёртв")
                return result
            if auth != "ok":
                self.flood_rest[self._session_key(path)] = config.SESSION_AUTH_REST
                return "retry"
            self._clear_auth_strike(path)
            try:
                me = await client.get_me()
                log.info(f"{sid} | 🟢 {getattr(me, 'first_name', '?')} ({getattr(me, 'id', '?')})")
            except SESSION_DEAD_ERRORS as exc:
                delete_after, result = self._handle_session_dead(sid, path, type(exc).__name__)
                return result
            except Exception as e:
                log.warning(f"{sid} | ❌ get_me: {e}")
                self.flood_rest[self._session_key(path)] = config.SESSION_AUTH_REST
                return "retry"

            collected = await collect_targets(
                client,
                rng,
                config.CONTACT_MAX_OFFLINE_DAYS,
                include_dialogs=config.CONTACT_INCLUDE_DIALOGS,
                dialogs_limit=config.DIALOGS_LIMIT,
            )
            targets = collected.targets
            users_n = collected.users
            groups_n = collected.groups
            if not targets:
                if collected.contacts_error or collected.dialogs_error:
                    log.warning(
                        f"{sid} | ⚠️ не удалось собрать цели "
                        f"(contacts: {collected.contacts_error or 'ok'}, "
                        f"dialogs: {collected.dialogs_error or 'ok'})"
                    )
                    return "retry"
                log.warning(f"{sid} | ⚠️ нет целей (контакты/группы) — в очередь позже")
                return "retry"
            if users_n or groups_n:
                log.info(f"{sid} | 🎯 целей: {users_n} ЛС + {groups_n} групп → рассылка")
            if config.LOG_SESSION_EVENTS and not (users_n or groups_n):
                log.info(f"{sid} | 🎯 целей: 0")
            result = await self.send_loop(client, sid, path, targets, rng)
            if result == "dead":
                delete_after, result = self._handle_session_dead(sid, path, "бан во время рассылки")
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"{sid} | критическая ошибка")
            return "retry"
        finally:
            self.proxies.release(proxy)
            self.active -= 1
            try:
                await client.disconnect()
            except Exception:
                pass
            if delete_after:
                try:
                    client.session.close()
                except Exception:
                    pass
                delete_session_files(path)
            if config.LOG_SESSION_EVENTS:
                log.info(f"{sid} | ⏹ завершена ({int(time.time() - start)}с)")

    def _error_pause(self, rng, mult: float = 1.0):
        d = config.ERROR_DELAY * mult
        return jitter(d, 0.15, rng) if d > 0 else 0

    def _message_pause(self, rng, target_kind: str = "group"):
        d = config.DELAY_MESSAGES
        if target_kind == "user" and config.DELAY_MESSAGES_USER > 0:
            d += config.DELAY_MESSAGES_USER
        return jitter(d, 0.15, rng) if d > 0 else 0

    def _cycle_pause(self, rng):
        d = config.DELAY_CYCLES
        return jitter(d, 0.1, rng) if d > 0 else 0

    async def send_loop(self, client, sid, path, targets, rng):
        stint = rng.randint(int(config.REFRESH_INTERVAL * 0.9), int(config.REFRESH_INTERVAL * 1.1))
        errors = 0
        bad_peers = 0
        start = time.time()
        sent_local = 0
        target_idx = 0
        total = len(targets)
        story_cache: dict[str, object] = {}
        joined_keys: set[str] = set()
        dead_targets: set[int] = set()
        peer_flood_total = 0
        skip_users_after_flood = False
        story_fail_counts: dict[tuple[str, int], int] = {}
        story_rr = 0
        cycle_num = 0

        await self.warm_story_peers(client, sid, story_cache, joined_keys)
        alive_total = sum(1 for s in self.stories if not self.is_story_bad(s))
        if alive_total and len(story_cache) == 0 and config.STORY_WARM_MODE != "lazy":
            log.warning(f"{sid} | 📖 повторный warm — первая попытка 0/{alive_total}")
            await asyncio.sleep(2.0)
            await self.warm_story_peers(client, sid, story_cache, joined_keys)

        def _bump_story_fail(story: StoryRef, reason: str):
            self.mark_story_error()
            reason = self._story_err_label(story, reason)
            key = self.story_key(story)
            story_fail_counts[key] = story_fail_counts.get(key, 0) + 1
            if not self._channel_recently_ok(story.peer):
                story_cache.pop(story.peer.lower(), None)
                joined_keys.discard(story.peer.lower())
            n = story_fail_counts[key]
            if n >= config.STORY_SKIP_AFTER:
                log.warning(
                    f"{sid} | ⚠ story {story.url}: пауза у сессии после {n} ошибок ({reason})"
                )
            elif n <= 3 or n % 5 == 0:
                log.warning(f"{sid} | ⚠ story {story.url}: retry ({reason}), попытка {n}")

        while True:
            if target_idx >= total:
                target_idx = 0
                cycle_num += 1
                skip_users_after_flood = False
                reshuffle_target_order(targets, rng)
                if (
                    config.TARGET_REFRESH_CYCLES > 0
                    and cycle_num % config.TARGET_REFRESH_CYCLES == 0
                ):
                    collected = await collect_targets(
                        client,
                        rng,
                        config.CONTACT_MAX_OFFLINE_DAYS,
                        include_dialogs=config.CONTACT_INCLUDE_DIALOGS,
                        dialogs_limit=config.DIALOGS_LIMIT,
                    )
                    if collected.targets:
                        targets[:] = collected.targets
                        total = len(targets)
                        dead_targets.clear()
                        if config.LOG_SESSION_EVENTS:
                            log.info(
                                f"{sid} | 🔄 цели обновлены: "
                                f"{collected.users} ЛС + {collected.groups} групп"
                            )
                if config.LOG_SESSION_EVENTS:
                    log.info(f"{sid} | 🔁 круг #{cycle_num} готов, отправлено {sent_local}")
                pause = self._cycle_pause(rng)
                if pause > 0:
                    await asyncio.sleep(pause)

            if time.time() - start >= stint:
                if config.LOG_SESSION_EVENTS or config.LOG_STORY_EVENTS:
                    log.info(
                        f"{sid} | ♻️ слот ~{stint}с: отправлено {sent_local}, "
                        f"story в кэше {len(story_cache)}/{alive_total or '?'}"
                    )
                return "rotate"

            target = targets[target_idx]
            target_idx += 1
            target_id = getattr(target.entity, "id", None)
            if target_id is not None and target_id in dead_targets:
                continue
            if skip_users_after_flood and target.kind == "user":
                continue

            kind_tag = "ЛС" if target.kind == "user" else "группа"

            try:
                story = self.pick_story(rng, story_fail_counts, story_cache, rr_idx=story_rr)
                story_rr += 1
                if story is None:
                    if story_fail_counts:
                        story_fail_counts.clear()
                        story_cache.clear()
                        joined_keys.clear()
                        story = self.pick_story(rng, story_cache=story_cache, rr_idx=story_rr)
                        story_rr += 1
                if story is None:
                    alive = len(self.stories) - sum(1 for s in self.stories if self.is_story_bad(s))
                    log.error(
                        f"{sid} | ❌ нет рабочих историй "
                        f"(в пуле {len(self.stories)}, живых {alive}) — обнови stories.txt"
                    )
                    return "retry"

                try:
                    async with self.sema:
                        await self.send_story(client, target.entity, story, story_cache, joined_keys)
                    n = self.mark_sent()
                    sent_local += 1
                    errors = bad_peers = 0
                    if config.LOG_STORY_FIRST_SEND and sent_local == 1:
                        log.info(f"{sid} | 📖 первая story ушла → {kind_tag} {target.label} | {story.url}")
                    every = config.LOG_SUCCESS_EVERY
                    log_ok = (
                        every == 0
                        or (every > 0 and n % every == 0)
                        or (config.LOG_SUCCESS_GROUPS and target.kind == "group")
                    )
                    if log_ok:
                        log.info(
                            f"{sid} | ✅ story → {kind_tag} {target.label} "
                            f"({target_idx}/{total}) | {story.url} | всего: {n}"
                        )
                    if target.kind == "group":
                        group_uname = target_username(target)
                        if group_uname:
                            hit = {
                                "ts": int(time.time()),
                                "sid": sid,
                                "group": group_uname,
                                "story": story.url,
                                "mode": "native_share",
                            }
                            self.recent_group_hits.appendleft(hit)
                            log.info(
                                f"{sid} | 📋 group-hit {group_uname} | story-share | {story.url}"
                            )
                except FloodWaitError as fw:
                    secs = getattr(fw, "seconds", 0) or 10
                    self.mark_flood()
                    if secs > FLOOD_SOFT_LIMIT:
                        self.flood_rest[self._session_key(path)] = secs
                        log.warning(f"{sid} | ⏳ FloodWait {secs}с — увожу сессию на отдых")
                        return "retry"
                    w = jitter_up(secs, rng)
                    log.warning(f"{sid} | ⏳ FloodWait {secs}с, пауза ~{int(w)}с")
                    await asyncio.sleep(w)
                    continue
                except SESSION_DEAD_ERRORS as exc:
                    log.warning(f"{sid} | 💀 {type(exc).__name__} во время отправки")
                    return "dead"
                except (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError, TypeNotFoundError) as ue:
                    msg = self._story_err_label(story, humanize(ue))
                    _bump_story_fail(story, msg)
                    if config.LOG_STORY_EVENTS:
                        log.warning(f"{sid} | 📖 story err: {story.url} — {msg}")
                    pause = self._error_pause(rng)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    continue
                except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError) as ue:
                    log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                    if target_id is not None:
                        dead_targets.add(target_id)
                    pause = self._error_pause(rng)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    continue
                except (ChatSendMediaForbiddenError, ChatSendPhotosForbiddenError) as ue:
                    log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                    if target_id is not None:
                        dead_targets.add(target_id)
                    pause = self._error_pause(rng)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    continue
                except PeerIdInvalidError as ue:
                    log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                    if target_id is not None:
                        dead_targets.add(target_id)
                    bad_peers += 1
                    if bad_peers >= 20:
                        collected = await collect_targets(
                            client,
                            rng,
                            config.CONTACT_MAX_OFFLINE_DAYS,
                            include_dialogs=config.CONTACT_INCLUDE_DIALOGS,
                            dialogs_limit=config.DIALOGS_LIMIT,
                        )
                        targets[:] = collected.targets
                        total = len(targets)
                        target_idx = 0
                        bad_peers = 0
                        dead_targets.clear()
                    pause = self._error_pause(rng)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    continue
                except PeerFloodError:
                    self.mark_flood()
                    peer_flood_total += 1
                    if config.PEER_FLOOD_SKIP_USERS:
                        skip_users_after_flood = True
                    log.warning(f"{sid} | ⚠ PeerFlood: {target.label}")
                    if peer_flood_total >= config.PEER_FLOOD_ROTATE_AFTER:
                        self.flood_rest[self._session_key(path)] = config.PEER_FLOOD_REST
                        log.warning(f"{sid} | ⏳ PeerFlood — отдых {config.PEER_FLOOD_REST}с")
                        return "retry"
                    pause = jitter(18, 0.2, rng)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    continue
                except RPCError as te:
                    msg = humanize(te)
                    if "STORY_ID_INVALID" in msg.upper():
                        self.mark_story_bad(story, "история истекла", ttl=300)
                        self.mark_story_error()
                        log.warning(f"{sid} | 📖 story ИСТЕКЛА: {story.url} — обнови ID в stories.txt")
                        pause = self._error_pause(rng, 2.0)
                        if pause > 0:
                            await asyncio.sleep(pause)
                        continue
                    log.error(f"{sid} | ✖ {target.label}: {msg}")
                    errors += 1
                    pause = self._error_pause(rng, 3.0)
                    if pause > 0:
                        await asyncio.sleep(pause)
                except ConnectionError as ex:
                    log.warning(f"{sid} | ⚠ отключение: {ex}")
                    self.flood_rest[self._session_key(path)] = config.WORKER_RETRY_SLEEP
                    return "retry"
                except Exception as ex:
                    if is_story_peer_error(ex):
                        _bump_story_fail(story, humanize(ex))
                        pause = self._error_pause(rng)
                        if pause > 0:
                            await asyncio.sleep(pause)
                        continue
                    if is_tl_schema_error(ex):
                        log.warning(f"{sid} | ⚠ story {story.url}: {schema_error_label(ex)}")
                        story_cache.pop(story.peer.lower(), None)
                        apply_telethon_patch()
                        pause = self._error_pause(rng)
                        if pause > 0:
                            await asyncio.sleep(pause)
                        continue
                    log.exception(f"{sid} | ✖ {target.label}: {ex}")
                    errors += 1
                    pause = self._error_pause(rng, 2.0)
                    if pause > 0:
                        await asyncio.sleep(pause)

                if errors >= config.MAX_ERRORS:
                    log.error(f"{sid} | 🚨 {errors} ошибок подряд — стоп")
                    return "retry"
                pause = self._message_pause(rng, target.kind)
                if pause > 0:
                    await asyncio.sleep(pause)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(f"{sid} | цикл: {e}")
                errors += 1
                if errors >= config.MAX_ERRORS:
                    return "retry"
                pause = self._error_pause(rng, 2.0)
                if pause > 0:
                    await asyncio.sleep(pause)

    async def worker(self, path):
        try:
            result = await self.run_session(path)
        except asyncio.CancelledError:
            self.slots.release()
            raise
        except Exception:
            result = "retry"
        self.slots.release()
        if self.stop.is_set():
            return
        if result == "rotate":
            self.push_back(path)
        elif result in ("retry", "drop"):
            rest = self.flood_rest.pop(os.path.basename(path), config.WORKER_RETRY_SLEEP)
            await asyncio.sleep(min(rest, 3600))
            if not self.stop.is_set():
                self.push_back(path)

    async def dispatcher(self):
        while not self.stop.is_set():
            path = await self.next_path()
            if self.stop.is_set():
                return
            if config.DISPATCHER_PROXY_WAIT:
                while not self.proxies.has_free():
                    if self.stop.is_set():
                        return
                    await asyncio.sleep(1)
            await self.slots.acquire()
            if self.stop.is_set():
                self.slots.release()
                return
            t = asyncio.create_task(self.worker(path))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def main(self):
        self.load()
        self.bootstrap()
        loop = asyncio.get_running_loop()
        if os.name != "nt":
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, self.stop.set)
                except NotImplementedError:
                    pass
        bg = [
            asyncio.create_task(self.dispatcher()),
            asyncio.create_task(self.watch_loop()),
            asyncio.create_task(self.write_stats_loop()),
            asyncio.create_task(self.log_stats_loop()),
            asyncio.create_task(self.reload_stories_loop()),
        ]
        log.info(
            f"💬 Старт (stories). Слоты: {config.MAX_SESSIONS}, "
            f"параллельно: {config.MAX_CONCURRENT}, delay: {config.DELAY_MESSAGES}s, "
            f"историй: {len(self.stories)}, warm={config.STORY_WARM_MODE}, "
            f"группы→ЛС={config.TARGET_GROUPS_FIRST}"
        )
        try:
            await self.stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        for t in bg + list(self.tasks):
            t.cancel()
        await asyncio.gather(*bg, *self.tasks, return_exceptions=True)
