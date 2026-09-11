import asyncio
import json
import logging
import os
import random
import shutil
import signal
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler

import colorama
from opentele.tl import TelegramClient
from telegram_api import client_api
from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    ChannelInvalidError,
    FloodWaitError,
    PeerFloodError,
    PeerIdInvalidError,
    RPCError,
    SlowModeWaitError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)

try:
    from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
except ImportError:
    class UsernameInvalidError(RPCError):
        pass

    class UsernameNotOccupiedError(RPCError):
        pass

from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import Channel, InputMediaStory

try:
    from telethon.tl.functions.stories import GetStoriesByIDRequest
except ImportError:
    GetStoriesByIDRequest = None

import config
from proxies import ProxyPool
from story_refs import StoryRef, load_story_refs, stories_fingerprint
from target_select import collect_targets
from textgen import jitter, jitter_up

colorama.init(autoreset=True)

FLOOD_SOFT_LIMIT = 300


class StoryPeerError(Exception):
    def __init__(self, label: str):
        self.label = label
        super().__init__(label)


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
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(Color(fmt, datefmt))
        log.addHandler(sh)
    return log


log = setup_logger()


def proxy_deps_ok() -> bool:
    try:
        import importlib.util

        if importlib.util.find_spec("python_socks") is None:
            return False
        import python_socks.async_.asyncio  # noqa: F401

        return True
    except Exception:
        return False


def humanize(e):
    if isinstance(e, UserBannedInChannelError):
        return "заблокирован в чате"
    if isinstance(e, ChatWriteForbiddenError):
        return "нет права писать"
    if isinstance(e, ChatAdminRequiredError):
        return "нужен админ"
    if isinstance(e, FloodWaitError):
        return f"FloodWait {getattr(e, 'seconds', 0)}с"
    if isinstance(e, SlowModeWaitError):
        return f"SlowMode {getattr(e, 'seconds', 0)}с"
    if isinstance(e, PeerFloodError):
        return "PeerFlood"
    if isinstance(e, ChannelInvalidError):
        return "битый канал/story peer"
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    if "STORY_ID_INVALID" in msg:
        return "история истекла/невалидна"
    if "CHAT_SEND_MEDIA_FORBIDDEN" in msg:
        return "медиа запрещено"
    if "USERNAMEINVALID" in msg or "NOBODY IS USING THIS USERNAME" in msg:
        return "битый username"
    return f"{type(e).__name__}: {e}"


def is_soft_target_error(e) -> bool:
    if isinstance(e, (PeerIdInvalidError, UsernameInvalidError, UsernameNotOccupiedError)):
        return True
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    return "USERNAMEINVALID" in msg or "NOBODY IS USING THIS USERNAME" in msg


def is_story_id_invalid_error(e) -> bool:
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    return "STORY_ID_INVALID" in msg


def is_local_resource_error(e) -> bool:
    """Errno 24 и т.п. — вина сервера, не прокси."""
    msg = (str(e) or "").lower()
    return "too many open files" in msg or "errno 24" in msg


def is_disconnect_error(e) -> bool:
    msg = (str(e) or "").lower()
    return (
        isinstance(e, ConnectionError)
        or "disconnected" in msg
        or "connection reset" in msg
        or "connection closed" in msg
    )


def move_session_to_bad(path):
    """Перенос в sessions_bad (не удалять навсегда)."""
    moved = False
    try:
        for t in (path, path + ".journal", path + "-journal", os.path.splitext(path)[0] + ".json"):
            if not os.path.exists(t):
                continue
            dst = os.path.join(str(config.BAD_DIR), os.path.basename(t))
            if os.path.exists(dst):
                dst = os.path.join(str(config.BAD_DIR), f"{os.path.basename(t)}_{int(time.time())}")
            for _ in range(3):
                try:
                    shutil.move(t, dst)
                    moved = True
                    break
                except Exception:
                    time.sleep(0.2)
    except Exception:
        log.exception("не смог перенести сессию в bad")
    return moved or not os.path.exists(path)


class Spammer:
    KIND_GROUP = "group"
    KIND_CONTACT = "user"

    def __init__(self):
        self.proxies = None
        self.story_pools: dict[str, list[StoryRef]] = {self.KIND_GROUP: [], self.KIND_CONTACT: []}
        self.pending = deque()
        self.wake = asyncio.Event()
        self.seen = set()
        self._connect_limit = config.MAX_CONNECT_PARALLEL
        self.slots = asyncio.Semaphore(self._connect_limit)
        self.sema = asyncio.Semaphore(min(config.MAX_CONCURRENT, self._connect_limit))
        self.total = 0
        self.total_contacts = 0
        self.total_groups = 0
        self.active = 0
        self.sent_ts = deque()
        self.flood_ts = deque()
        self.started = time.time()
        self.tasks = set()
        self.stop = asyncio.Event()
        self.flood_rest = {}
        self.global_bad: dict[str, set[tuple[str, int]]] = {
            self.KIND_GROUP: set(),
            self.KIND_CONTACT: set(),
        }
        self.story_fail_counts: dict[tuple[str, tuple[str, int]], int] = {}
        self._stories_fp: dict[str, tuple] = {self.KIND_GROUP: (), self.KIND_CONTACT: ()}
        self._all_stories_bad_logged: dict[str, bool] = {
            self.KIND_GROUP: False,
            self.KIND_CONTACT: False,
        }
        self.auth_fail_counts: dict[str, int] = {}

    def _story_key(self, story: StoryRef) -> tuple[str, int]:
        return (story.peer.lower(), story.story_id)

    def _pool_label(self, kind: str) -> str:
        return "группы" if kind == self.KIND_GROUP else "контакты"

    def _pool_file_hint(self, kind: str) -> str:
        return "stories_groups.txt" if kind == self.KIND_GROUP else "stories_contacts.txt"

    def mark_story_bad_global(self, kind: str, story: StoryRef):
        key = self._story_key(story)
        bad = self.global_bad[kind]
        if key not in bad:
            bad.add(key)
            pool = self.story_pools[kind]
            alive = len(pool) - len(bad)
            log.warning(
                f"📖 [{self._pool_label(kind)}] story {story.label} — глобально битая, "
                f"осталось: {max(0, alive)}/{len(pool)}"
            )
            if alive <= 0 and not self._all_stories_bad_logged[kind]:
                self._all_stories_bad_logged[kind] = True
                log.error(f"❌ ВСЕ stories для {self._pool_label(kind)} битые — обнови {self._pool_file_hint(kind)}")

    def mark_story_fail(self, kind: str, story: StoryRef):
        key = self._story_key(story)
        fail_key = (kind, key)
        self.story_fail_counts[fail_key] = self.story_fail_counts.get(fail_key, 0) + 1
        if self.story_fail_counts[fail_key] >= config.STORY_GLOBAL_BAD_THRESHOLD:
            self.mark_story_bad_global(kind, story)

    def pick_story(
        self,
        kind: str,
        rng: random.Random,
        session_ready: set[tuple[str, int]],
        exclude: set[tuple[str, int]] | None = None,
    ) -> StoryRef | None:
        exclude = exclude or set()
        bad = self.global_bad[kind]
        pool = [
            s
            for s in self.story_pools[kind]
            if self._story_key(s) in session_ready
            and self._story_key(s) not in bad
            and self._story_key(s) not in exclude
        ]
        if not pool:
            return None
        return rng.choice(pool)

    @staticmethod
    def _drop_target(targets: list, target_idx: int) -> int:
        pos = target_idx - 1
        if 0 <= pos < len(targets):
            targets.pop(pos)
            return max(0, pos)
        return 0

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

    def _load_pool(self, kind: str):
        refs, _ = config.load_stories_pool(kind)
        fp = stories_fingerprint(refs)
        if fp != self._stories_fp[kind]:
            self._stories_fp[kind] = fp
            self.global_bad[kind].clear()
            for k in list(self.story_fail_counts):
                if k[0] == kind:
                    del self.story_fail_counts[k]
            self._all_stories_bad_logged[kind] = False
        self.story_pools[kind] = refs

    def load_stories(self):
        self._load_pool(self.KIND_GROUP)
        self._load_pool(self.KIND_CONTACT)

    def _compute_connect_limit(self) -> int:
        cap = min(config.MAX_CONNECT_PARALLEL, config.MAX_SESSIONS)
        n_proxy = len(self.proxies.proxies) if self.proxies else 0
        if n_proxy > 0:
            # Не больше N сессий на прокси — иначе timeout/disconnect
            cap = min(cap, n_proxy * config.MAX_SESSIONS_PER_PROXY)
        try:
            import resource

            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft > 0:
                cap = min(cap, max(10, (soft - 400) // 15))
        except Exception:
            pass
        return max(10, cap)

    def _apply_connect_limit(self):
        self._connect_limit = self._compute_connect_limit()
        self.slots = asyncio.Semaphore(self._connect_limit)
        self.sema = asyncio.Semaphore(min(config.MAX_CONCURRENT, self._connect_limit))

    def load(self):
        config.raise_nofile_limit()
        self.proxies = ProxyPool.from_file()
        self._apply_connect_limit()
        self.load_stories()
        g_n = len(self.story_pools[self.KIND_GROUP])
        c_n = len(self.story_pools[self.KIND_CONTACT])
        nofile = config.current_nofile_limit()
        log.info(
            f"📖 историй: группы {g_n} | контакты {c_n} | "
            f"🛰 прокси: {len(self.proxies)} | оффлайн лимит: {config.CONTACT_MAX_OFFLINE_DAYS}д | "
            f"подключений: {self._connect_limit} | на прокси: ≤{config.MAX_SESSIONS_PER_PROXY} | "
            f"nofile={nofile}"
        )
        if not self.proxies.proxies:
            log.error("❌ Нет прокси, выхожу")
            sys.exit(1)
        if not proxy_deps_ok():
            log.error(
                "❌ python-socks не установлен — прокси НЕ РАБОТАЮТ. "
                "Выполни: /opt/new-soft/venv/bin/pip install 'python-socks[asyncio]' PySocks"
            )
            sys.exit(1)
        if g_n == 0 and c_n == 0:
            log.error(
                "❌ Пусто: stories_groups.txt и stories_contacts.txt "
                "(или stories.txt как fallback)"
            )
            sys.exit(1)
        self.write_stats_now()

    def mark_sent(self, target_kind: str):
        now = time.time()
        self.total += 1
        if target_kind == self.KIND_CONTACT:
            self.total_contacts += 1
        else:
            self.total_groups += 1
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

    def _stats_payload(self) -> dict:
        now = time.time()
        self._trim(self.sent_ts, now)
        self._trim(self.flood_ts, now)
        proxies_total = len(self.proxies) if self.proxies else 0
        proxies_cd = self.proxies.cooldown_count() if self.proxies else 0
        return {
            "ts": int(now),
            "uptime_sec": int(now - self.started),
            "total_sent": self.total,
            "total_contacts": self.total_contacts,
            "total_groups": self.total_groups,
            "sent_per_min": len(self.sent_ts),
            "flood_per_min": len(self.flood_ts),
            "active_sessions": self.active,
                "max_sessions": config.MAX_SESSIONS,
                "connect_parallel": self._connect_limit,
                "nofile_limit": config.current_nofile_limit(),
            "pending": len(self.pending),
            "proxies_total": proxies_total,
            "proxies_in_cooldown": proxies_cd,
            "stories_groups": len(self.story_pools[self.KIND_GROUP]),
            "stories_contacts": len(self.story_pools[self.KIND_CONTACT]),
            "stories_groups_bad": len(self.global_bad[self.KIND_GROUP]),
            "stories_contacts_bad": len(self.global_bad[self.KIND_CONTACT]),
            "mailing_mode": "stories_split",
        }

    def write_stats_now(self):
        try:
            config.atomic_write(
                config.STATS_FILE,
                json.dumps(self._stats_payload(), ensure_ascii=False).encode("utf-8"),
            )
        except Exception as e:
            log.warning(f"не записал stats.json: {e}")

    async def write_stats_loop(self):
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self.write_stats_now)
            except Exception as e:
                log.warning(f"stats loop: {e}")
            await asyncio.sleep(5)

    async def log_stats_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(30)
            self._trim(self.sent_ts, time.time())
            self._trim(self.flood_ts, time.time())
            g = len(self.story_pools[self.KIND_GROUP])
            c = len(self.story_pools[self.KIND_CONTACT])
            log.info(
                f"📊 в минуту: {len(self.sent_ts)} | flood/мин: {len(self.flood_ts)} | "
                f"активных: {self.active}/{config.MAX_SESSIONS} | очередь: {len(self.pending)} | "
                f"всего: {self.total} (ЛС {self.total_contacts} / группы {self.total_groups}) | "
                f"истории: гр {g} (бит {len(self.global_bad[self.KIND_GROUP])}) | "
                f"ЛС {c} (бит {len(self.global_bad[self.KIND_CONTACT])})"
            )

    async def maintenance_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(60)
            try:
                self.proxies.decay_cooldowns(30)
                cool = self.proxies.cooldown_count()
                if cool >= len(self.proxies) * 0.9 and cool > 0:
                    log.warning(f"🧹 прокси в кулдауне {cool}/{len(self.proxies)} — ускоряю decay")
                    self.proxies.decay_cooldowns(60)
            except Exception:
                pass

    async def reload_stories_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(config.STORIES_RELOAD_INTERVAL)
            try:
                prev_g = self._stories_fp[self.KIND_GROUP]
                prev_c = self._stories_fp[self.KIND_CONTACT]
                self.load_stories()
                if self._stories_fp[self.KIND_GROUP] != prev_g:
                    log.info(
                        f"📖 stories_groups.txt: {len(self.story_pools[self.KIND_GROUP])} в пуле"
                    )
                if self._stories_fp[self.KIND_CONTACT] != prev_c:
                    log.info(
                        f"📖 stories_contacts.txt: {len(self.story_pools[self.KIND_CONTACT])} в пуле"
                    )
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

    async def _get_story_entity(self, client, story: StoryRef):
        last_err = None
        for cand in story.peer_candidates():
            try:
                return await client.get_entity(cand)
            except ValueError as e:
                last_err = e
                continue
            except RPCError as e:
                last_err = e
                if is_soft_target_error(e):
                    continue
                raise
        if last_err and is_soft_target_error(last_err):
            raise StoryPeerError(story.label) from last_err
        if last_err:
            raise last_err
        raise StoryPeerError(story.label)

    async def _ensure_joined(self, client, entity):
        if not isinstance(entity, Channel):
            return
        if getattr(entity, "left", True) is False:
            return
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        except RPCError as e:
            log.warning(f"join {getattr(entity, 'username', entity.id)}: {humanize(e)}")

    async def _story_exists_for_account(self, client, input_peer, story_id: int) -> bool:
        if GetStoriesByIDRequest is None:
            return True
        try:
            result = await client(GetStoriesByIDRequest(peer=input_peer, id=[story_id]))
            stories = getattr(result, "stories", None) or []
            return any(getattr(s, "id", None) == story_id for s in stories)
        except RPCError as e:
            if is_story_id_invalid_error(e):
                return False
            return True

    def session_ready_keys(self, kind: str) -> set[tuple[str, int]]:
        """Ключи stories для сессии — резолв peer ленивый при отправке."""
        return {
            self._story_key(s)
            for s in self.story_pools[kind]
            if self._story_key(s) not in self.global_bad[kind]
        }

    async def resolve_story_peer(self, client, kind: str, story: StoryRef, story_cache: dict):
        peer_key = story.peer.lower()
        cache_key = (kind, peer_key)
        cached = story_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            entity = await self._get_story_entity(client, story)
            await self._ensure_joined(client, entity)
            peer = await client.get_input_entity(entity)
        except StoryPeerError:
            story_cache.pop(cache_key, None)
            raise
        except RPCError as e:
            story_cache.pop(cache_key, None)
            if is_soft_target_error(e):
                raise StoryPeerError(story.label) from e
            raise
        story_cache[cache_key] = peer
        return peer

    async def send_story(self, client, target_entity, kind: str, story: StoryRef, story_cache: dict):
        story_peer = await self.resolve_story_peer(client, kind, story, story_cache)
        media = InputMediaStory(peer=story_peer, id=story.story_id)
        await client.send_file(target_entity, file=media)

    async def delete_dm_for_me(self, client, sid, target):
        if target.kind != "user" or not config.DELETE_DM_AFTER_SEND:
            return
        try:
            await client.delete_dialog(target.entity, revoke=False)
        except Exception as e:
            log.warning(f"{sid} | не удалил диалог {target.label}: {e}")

    async def run_session(self, path):
        rng = random.Random(os.urandom(16))
        sid = os.path.splitext(os.path.basename(path))[0]
        proxy = self.proxies.acquire()
        if proxy is None:
            log.error(f"{sid} | ❌ нет свободных прокси")
            return "retry"
        api = client_api(sid, path)
        client = TelegramClient(path, api=api, proxy=self.proxies.to_dict(proxy))
        self.active += 1
        start = time.time()
        delete_after = False
        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=config.CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning(f"{sid} | ❌ подключение: timeout {config.CONNECT_TIMEOUT}с")
                if proxy.get("in_use", 0) <= 1:
                    self.proxies.mark_bad(proxy)
                return "retry"
            except Exception as e:
                log.warning(f"{sid} | ❌ подключение: {e}")
                if not is_local_resource_error(e):
                    self.proxies.mark_bad(proxy)
                return "retry"
            try:
                if not await client.is_user_authorized():
                    fails = self.auth_fail_counts.get(sid, 0) + 1
                    self.auth_fail_counts[sid] = fails
                    if fails >= config.AUTH_FAIL_BEFORE_BAD:
                        log.error(
                            f"{sid} | ❌ не авторизована ({fails}x) → sessions_bad "
                            f"(api_id={getattr(api, 'api_id', '?')})"
                        )
                        delete_after = True
                        return "drop"
                    log.warning(
                        f"{sid} | ⚠️ не авторизована ({fails}/{config.AUTH_FAIL_BEFORE_BAD}), "
                        f"повтор позже"
                    )
                    return "retry"
                self.auth_fail_counts.pop(sid, None)
            except Exception as e:
                log.warning(f"{sid} | ❌ авторизация: {e}")
                return "retry"
            try:
                me = await client.get_me()
                log.info(f"{sid} | 🟢 {getattr(me, 'first_name', '?')} ({getattr(me, 'id', '?')})")
            except Exception as e:
                log.warning(f"{sid} | ❌ get_me: {e}")
                return "retry"

            targets = await collect_targets(client, rng, config.CONTACT_MAX_OFFLINE_DAYS)
            users_n = sum(1 for t in targets if t.kind == "user")
            groups_n = sum(1 for t in targets if t.kind == "group")
            if not targets:
                log.warning(
                    f"{sid} | ⚠️ нет целей (контакты/группы) — отложена на "
                    f"{config.NO_TARGETS_RETRY_SEC}с"
                )
                return "no_targets"
            log.info(f"{sid} | 🎯 целей: {users_n} контактов + {groups_n} групп")

            story_cache: dict = {}
            session_ready = {
                self.KIND_GROUP: self.session_ready_keys(self.KIND_GROUP) if groups_n > 0 else set(),
                self.KIND_CONTACT: self.session_ready_keys(self.KIND_CONTACT) if users_n > 0 else set(),
            }

            can_group = groups_n > 0 and bool(session_ready[self.KIND_GROUP])
            can_contact = users_n > 0 and bool(session_ready[self.KIND_CONTACT])
            if not can_group and not can_contact:
                log.warning(f"{sid} | ⚠️ пустые пулы stories для целей")
                return "retry"

            return await self.send_loop(client, sid, path, targets, rng, story_cache, session_ready)
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
                move_session_to_bad(path)
                self.seen.discard(os.path.basename(path))
            log.info(f"{sid} | ⏹ завершена ({int(time.time() - start)}с)")

    async def send_loop(self, client, sid, path, targets, rng, story_cache, session_ready):
        stint = rng.randint(int(config.REFRESH_INTERVAL * 0.9), int(config.REFRESH_INTERVAL * 1.1))
        errors = 0
        start = time.time()
        sent_local = 0
        target_idx = 0
        total = len(targets)

        while True:
            if target_idx >= total:
                target_idx = 0
                log.info(f"{sid} | 🔁 круг готов, отправлено {sent_local}")
                await asyncio.sleep(jitter(config.DELAY_CYCLES, 0.0, rng, 5.0))

            if time.time() - start >= stint:
                log.info(f"{sid} | ♻️ смена слота (~{stint}с), отправлено {sent_local}")
                return "rotate"

            target = targets[target_idx]
            target_idx += 1
            kind = target.kind
            pool_kind = self.KIND_GROUP if kind == "group" else self.KIND_CONTACT
            kind_tag = "ЛС" if kind == "user" else "группа"
            pos = f"{target_idx}/{total}"

            if not session_ready.get(pool_kind):
                continue

            try:
                story: StoryRef | None = None
                tried: set[tuple[str, int]] = set()
                sent_ok = False
                target_bad = False
                ready = session_ready[pool_kind]
                attempts = min(5, max(1, len(ready)))

                for _ in range(attempts):
                    story = self.pick_story(pool_kind, rng, ready, exclude=tried)
                    if story is None:
                        break
                    tried.add(self._story_key(story))
                    try:
                        async with self.sema:
                            await self.send_story(client, target.entity, pool_kind, story, story_cache)
                        sent_ok = True
                        break
                    except StoryPeerError:
                        ready.discard(self._story_key(story))
                        story_cache.pop((pool_kind, story.peer.lower()), None)
                    except (UsernameInvalidError, UsernameNotOccupiedError, PeerIdInvalidError) as ue:
                        log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                        target_bad = True
                        break
                    except RPCError as te:
                        if is_soft_target_error(te):
                            log.warning(f"{sid} | ⚠ {target.label}: {humanize(te)}")
                            target_bad = True
                            break
                        if is_story_id_invalid_error(te) and story is not None:
                            self.mark_story_fail(pool_kind, story)
                            ready.discard(self._story_key(story))
                            story_cache.pop((pool_kind, story.peer.lower()), None)
                            continue
                        raise

                if sent_ok and story is not None:
                    n = self.mark_sent(kind)
                    sent_local += 1
                    errors = 0
                    log.info(
                        f"{sid} | ✅ [{self._pool_file_hint(pool_kind)}] story → "
                        f"{kind_tag} {target.label} ({pos}) | {story.label} | всего: {n}"
                    )
                    if kind == "user" and config.DELETE_DM_AFTER_SEND:
                        asyncio.create_task(self.delete_dm_for_me(client, sid, target))
                    delay = config.DELAY_MESSAGES
                    if delay > 0:
                        await asyncio.sleep(jitter(delay, 0.2, rng, 0.05))
                    continue

                if target_bad:
                    target_idx = self._drop_target(targets, target_idx)
                    total = len(targets)
                    if total <= 0:
                        log.warning(f"{sid} | ⚠️ все цели битые — пересбор")
                        targets[:] = await collect_targets(client, rng, config.CONTACT_MAX_OFFLINE_DAYS)
                        total = len(targets)
                        target_idx = 0
                    await asyncio.sleep(jitter(0.3, 0.1, rng, 0.2))
                    continue

                if not ready:
                    log.warning(
                        f"{sid} | ⚠️ пул [{self._pool_label(pool_kind)}] исчерпан для сессии — rotate"
                    )
                    return "rotate"

                if len(self.global_bad[pool_kind]) >= len(self.story_pools[pool_kind]):
                    log.error(
                        f"{sid} | ❌ все stories [{self._pool_label(pool_kind)}] битые — "
                        f"обнови {self._pool_file_hint(pool_kind)}"
                    )
                    await asyncio.sleep(30)
                continue

            except FloodWaitError as fw:
                secs = getattr(fw, "seconds", 0) or 10
                self.mark_flood()
                if secs > FLOOD_SOFT_LIMIT:
                    self.flood_rest[os.path.basename(path)] = secs
                    log.warning(f"{sid} | ⏳ FloodWait {secs}с — увожу сессию на отдых")
                    return "retry"
                w = jitter_up(secs, rng)
                log.warning(f"{sid} | ⏳ FloodWait {secs}с, пауза ~{int(w)}с")
                await asyncio.sleep(w)
                continue
            except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError) as ue:
                log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
                continue
            except ChannelInvalidError as ue:
                log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                if story:
                    story_cache.pop((pool_kind, story.peer.lower()), None)
                await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
                continue
            except PeerFloodError as te:
                log.warning(f"{sid} | ✖ {target.label}: {humanize(te)}")
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))
            except (ConnectionError, OSError) as ex:
                if is_disconnect_error(ex):
                    log.warning(f"{sid} | 🔌 отвалился коннект → rotate ({ex})")
                    return "rotate"
                log.warning(f"{sid} | ✖ {target.label}: {ex}")
                errors += 1
                await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
            except RPCError as te:
                if is_soft_target_error(te):
                    log.warning(f"{sid} | ⚠ {target.label}: {humanize(te)}")
                    target_idx = self._drop_target(targets, target_idx)
                    total = len(targets)
                    await asyncio.sleep(jitter(0.3, 0.1, rng, 0.2))
                    continue
                msg = humanize(te)
                if isinstance(te, ChannelInvalidError):
                    log.warning(f"{sid} | ⚠ {target.label}: {msg}")
                    if story:
                        story_cache.pop((pool_kind, story.peer.lower()), None)
                    await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
                    continue
                log.error(f"{sid} | ✖ {target.label}: {msg}")
                if story is not None and is_story_id_invalid_error(te):
                    self.mark_story_fail(pool_kind, story)
                    session_ready[pool_kind].discard(self._story_key(story))
                    story_cache.pop((pool_kind, story.peer.lower()), None)
                    await asyncio.sleep(jitter(2, 0.2, rng, 0.5))
                    continue
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))
            except Exception as ex:
                if is_disconnect_error(ex):
                    log.warning(f"{sid} | 🔌 отвалился коннект → rotate")
                    return "rotate"
                log.warning(f"{sid} | ✖ {target.label}: {ex}")
                errors += 1
                await asyncio.sleep(jitter(2, 0.2, rng, 0.3))

            if errors >= config.MAX_ERRORS:
                log.error(f"{sid} | 🚨 {errors} ошибок подряд — стоп")
                return "retry"

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
        elif result == "no_targets":
            await asyncio.sleep(config.NO_TARGETS_RETRY_SEC)
            if not self.stop.is_set():
                self.push_back(path)
        elif result == "retry":
            rest = self.flood_rest.pop(os.path.basename(path), config.WORKER_RETRY_SLEEP)
            await asyncio.sleep(min(rest, 3600))
            if not self.stop.is_set():
                self.push_back(path)

    async def dispatcher(self):
        while not self.stop.is_set():
            path = await self.next_path()
            if self.stop.is_set():
                return
            while not self.proxies.has_free():
                if self.stop.is_set():
                    return
                await asyncio.sleep(5)
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
        self.write_stats_now()
        bg = [
            asyncio.create_task(self.dispatcher()),
            asyncio.create_task(self.watch_loop()),
            asyncio.create_task(self.write_stats_loop()),
            asyncio.create_task(self.log_stats_loop()),
            asyncio.create_task(self.reload_stories_loop()),
            asyncio.create_task(self.maintenance_loop()),
        ]
        log.info(
            f"💬 Старт (stories: группы + контакты). Подключений: {self._connect_limit}, "
            f"MAX_SESSIONS: {config.MAX_SESSIONS}, sem: {config.MAX_CONCURRENT} | "
            f"гр: {len(self.story_pools[self.KIND_GROUP])} | "
            f"ЛС: {len(self.story_pools[self.KIND_CONTACT])}"
        )
        try:
            await self.stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        for t in bg + list(self.tasks):
            t.cancel()
        await asyncio.gather(*bg, *self.tasks, return_exceptions=True)
