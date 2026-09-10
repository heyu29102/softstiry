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
from telethon.tl.types import Channel, InputMediaStory, MessageMediaStory

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
STORY_GLOBAL_BAD_THRESHOLD = 3


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
    """Битая цель — пропускаем, не копим errors и не стопаем сессию."""
    if isinstance(e, (PeerIdInvalidError, UsernameInvalidError, UsernameNotOccupiedError)):
        return True
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    return "USERNAMEINVALID" in msg or "NOBODY IS USING THIS USERNAME" in msg


def is_story_id_invalid_error(e) -> bool:
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    return "STORY_ID_INVALID" in msg


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
        self.api_accepted = 0
        self.shadow_total = 0
        self.active = 0
        self.sent_ts = deque()
        self.shadow_ts = deque()
        self.flood_ts = deque()
        self.started = time.time()
        self.tasks = set()
        self.stop = asyncio.Event()
        self.flood_rest = {}
        self.rest_strikes: dict[str, int] = {}
        self.global_bad_stories: set[tuple[str, int]] = set()
        self.story_fail_counts: dict[tuple[str, int], int] = {}
        self._all_stories_bad_logged = False
        self._stories_fp: tuple = ()

    def _story_key(self, story: StoryRef) -> tuple[str, int]:
        return (story.peer.lower(), story.story_id)

    def mark_story_bad_global(self, story: StoryRef):
        key = self._story_key(story)
        if key not in self.global_bad_stories:
            self.global_bad_stories.add(key)
            alive = len(self.stories) - len(self.global_bad_stories)
            log.warning(f"📖 story {story.label} — глобально битая, осталось: {max(0, alive)}/{len(self.stories)}")
            if alive <= 0 and not self._all_stories_bad_logged:
                self._all_stories_bad_logged = True
                log.error("❌ ВСЕ stories битые — обнови stories.txt")

    def mark_story_fail(self, story: StoryRef):
        """Счётчик провалов; глобальный bad только после порога с разных попыток."""
        key = self._story_key(story)
        self.story_fail_counts[key] = self.story_fail_counts.get(key, 0) + 1
        if self.story_fail_counts[key] >= STORY_GLOBAL_BAD_THRESHOLD:
            self.mark_story_bad_global(story)

    def pick_story(
        self,
        rng: random.Random,
        session_ready: set[tuple[str, int]],
        exclude: set[tuple[str, int]] | None = None,
    ) -> StoryRef | None:
        exclude = exclude or set()
        pool = [
            s for s in self.stories
            if self._story_key(s) in session_ready
            and self._story_key(s) not in self.global_bad_stories
            and self._story_key(s) not in exclude
        ]
        if not pool:
            return None
        return rng.choice(pool)

    @staticmethod
    def _drop_target(targets: list, target_idx: int) -> int:
        """Убрать битую цель из списка, вернуть новый target_idx."""
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

    def load_stories(self):
        refs = load_story_refs(config.STORIES_FILE)
        fp = stories_fingerprint(refs)
        if fp != self._stories_fp:
            self._stories_fp = fp
            self.global_bad_stories.clear()
            self.story_fail_counts.clear()
            self._all_stories_bad_logged = False
        self.stories = refs

    def adaptive_delay_factor(self) -> float:
        """Множитель паузы при высокой доле тени (антифрод)."""
        if not config.ADAPTIVE_THROTTLE:
            return 1.0
        now = time.time()
        self._trim(self.sent_ts, now)
        self._trim(self.shadow_ts, now)
        verified = len(self.sent_ts)
        shadow = len(self.shadow_ts)
        total = verified + shadow
        if total < 30:
            return 1.0
        ratio = shadow / total
        if ratio <= config.ADAPTIVE_SHADOW_RATIO:
            return 1.0
        return 1.0 + min(3.0, (ratio - config.ADAPTIVE_SHADOW_RATIO) * 8.0)

    def shadow_rest_seconds(self, session_file: str) -> int:
        strikes = self.rest_strikes.get(session_file, 0)
        base = config.SHADOW_REST_SEC
        rest = int(base * (1.0 + strikes * 0.5))
        return min(rest, config.SHADOW_REST_MAX_SEC)

    def load(self):
        self.proxies = ProxyPool.from_file()
        self.load_stories()
        log.info(
            f"📖 историй в пуле: {len(self.stories)} | "
            f"🛰 прокси: {len(self.proxies)} | "
            f"оффлайн лимит: {config.CONTACT_MAX_OFFLINE_DAYS}д"
        )
        if not self.proxies.proxies:
            log.error("❌ Нет прокси, выхожу")
            sys.exit(1)
        if not self.stories:
            log.error("❌ stories.txt пуст — добавь истории (канал|id или t.me/.../s/id)")
            sys.exit(1)

    def mark_verified_sent(self):
        now = time.time()
        self.total += 1
        self.sent_ts.append(now)
        self._trim(self.sent_ts, now)
        return self.total

    def mark_api_accepted(self):
        self.api_accepted += 1

    def mark_shadow(self):
        now = time.time()
        self.shadow_total += 1
        self.shadow_ts.append(now)
        self._trim(self.shadow_ts, now)

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
            self._trim(self.shadow_ts, now)
            self._trim(self.flood_ts, now)
            verified_min = len(self.sent_ts)
            shadow_min = len(self.shadow_ts)
            api_min = verified_min + shadow_min
            verify_rate = round(100.0 * verified_min / api_min, 1) if api_min else 100.0
            data = {
                "ts": int(now),
                "uptime_sec": int(now - self.started),
                "total_sent": self.total,
                "api_accepted": self.api_accepted,
                "shadow_total": self.shadow_total,
                "sent_per_min": verified_min,
                "shadow_per_min": shadow_min,
                "verify_rate_pct": verify_rate,
                "flood_per_min": len(self.flood_ts),
                "active_sessions": self.active,
                "max_sessions": config.MAX_SESSIONS,
                "pending": len(self.pending),
                "proxies_total": len(self.proxies),
                "proxies_in_cooldown": self.proxies.cooldown_count(),
                "stories_in_pool": len(self.stories),
                "stories_global_bad": len(self.global_bad_stories),
                "sessions_resting": len(self.flood_rest),
                "adaptive_delay": round(self.adaptive_delay_factor(), 2),
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
            self._trim(self.shadow_ts, time.time())
            self._trim(self.flood_ts, time.time())
            verified_min = len(self.sent_ts)
            shadow_min = len(self.shadow_ts)
            api_min = verified_min + shadow_min
            rate = round(100.0 * verified_min / api_min, 1) if api_min else 100.0
            free_p, cool_p, in_use_p = self.proxies.pool_stats()
            adapt = self.adaptive_delay_factor()
            log.info(
                f"📊 реально/мин: {verified_min} | 👻 тень/мин: {shadow_min} | "
                f"доставка: {rate}% | flood/мин: {len(self.flood_ts)} | "
                f"активных: {self.active}/{config.MAX_SESSIONS} | очередь: {len(self.pending)} | "
                f"отдых: {len(self.flood_rest)} | throttle: x{adapt:.1f} | "
                f"прокси: своб {free_p} / кд {cool_p} / in_use {in_use_p} | "
                f"всего реально: {self.total} | тень всего: {self.shadow_total} | "
                f"историй: {len(self.stories)} (битых: {len(self.global_bad_stories)})"
            )

    async def maintenance_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(config.MAINTENANCE_INTERVAL)
            try:
                self.proxies.decay_cooldowns()
                self.proxies.reset_stuck()
                now = time.time()
                stale = [k for k, v in self.flood_rest.items() if v < now]
                for k in stale:
                    self.flood_rest.pop(k, None)
                    self.rest_strikes.pop(k, None)
                if stale:
                    log.info(f"🧹 maintenance: сброшено {len(stale)} устаревших отдыхов")
                free_p, cool_p, in_use_p = self.proxies.pool_stats()
                if cool_p > len(self.proxies) * 0.6:
                    log.warning(
                        f"🧹 maintenance: прокси в кулдауне {cool_p}/{len(self.proxies)} — "
                        f"ускоряю decay"
                    )
                    for p in self.proxies.proxies:
                        if p["bad_until"] > now:
                            p["bad_until"] = max(now, p["bad_until"] - 60)
            except Exception:
                pass

    async def reload_stories_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(config.STORIES_RELOAD_INTERVAL)
            try:
                refs = await asyncio.to_thread(load_story_refs, config.STORIES_FILE)
                if refs:
                    fp = stories_fingerprint(refs)
                    if fp != self._stories_fp:
                        self._stories_fp = fp
                        self.stories = refs
                        self.global_bad_stories.clear()
                        self.story_fail_counts.clear()
                        self._all_stories_bad_logged = False
                        log.info(f"📖 stories.txt обновлён: {len(refs)} в пуле")
                    else:
                        self.stories = refs
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
        peer = story.peer.strip().lstrip("@")
        candidates: list = []
        if peer.lstrip("-").isdigit():
            candidates.append(int(peer))
        candidates.extend([peer, f"@{peer}", f"https://t.me/{peer}"])
        seen: set[str] = set()
        last_err = None
        for cand in candidates:
            key = str(cand)
            if key in seen:
                continue
            seen.add(key)
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

    async def prepare_stories(self, client, sid, story_cache: dict) -> set[tuple[str, int]]:
        """Подписка на каналы и проверка доступности stories для этой сессии."""
        ready: set[tuple[str, int]] = set()
        peers_resolved: dict[str, object] = {}

        available = [s for s in self.stories if self._story_key(s) not in self.global_bad_stories]
        for story in available:
            key = self._story_key(story)
            peer_key = story.peer.lower()
            try:
                if peer_key not in peers_resolved:
                    entity = await self._get_story_entity(client, story)
                    await self._ensure_joined(client, entity)
                    input_peer = await client.get_input_entity(entity)
                    peers_resolved[peer_key] = input_peer
                    story_cache[peer_key] = input_peer
                input_peer = peers_resolved[peer_key]
                if await self._story_exists_for_account(client, input_peer, story.story_id):
                    ready.add(key)
                else:
                    log.warning(f"{sid} | story {story.label} не видна этому аккаунту")
                    self.mark_story_fail(story)
            except StoryPeerError as e:
                log.warning(f"{sid} | peer {story.label}: {e}")
            except RPCError as e:
                log.warning(f"{sid} | story {story.label}: {humanize(e)}")
            except Exception as e:
                log.warning(f"{sid} | story {story.label}: {e}")

        log.info(f"{sid} | 📖 stories готовы: {len(ready)}/{len(available)}")
        return ready

    async def resolve_story_peer(self, client, story: StoryRef, story_cache: dict):
        key = story.peer.lower()
        cached = story_cache.get(key)
        if cached is not None:
            return cached
        try:
            entity = await self._get_story_entity(client, story)
            await self._ensure_joined(client, entity)
            peer = await client.get_input_entity(entity)
        except StoryPeerError:
            story_cache.pop(key, None)
            raise
        except RPCError as e:
            story_cache.pop(key, None)
            if is_soft_target_error(e):
                raise StoryPeerError(story.label) from e
            raise
        story_cache[key] = peer
        return peer

    @staticmethod
    def _extract_sent_message(result):
        if result is None:
            return None
        if isinstance(result, list):
            for item in result:
                if getattr(item, "id", None):
                    return item
            return result[0] if result else None
        return result

    @staticmethod
    def _message_has_story(msg, story_id: int) -> bool:
        if not msg or getattr(msg, "action", None):
            return False
        if getattr(msg, "deleted", False):
            return False
        media = getattr(msg, "media", None)
        if isinstance(media, MessageMediaStory):
            return getattr(media, "id", None) == story_id
        return media is not None and getattr(msg, "id", None)

    async def verify_story_sent(self, client, target_entity, story_id: int, sent_msg, rng) -> bool:
        if not config.VERIFY_SEND:
            return sent_msg is not None and getattr(sent_msg, "id", None)

        msg_id = getattr(sent_msg, "id", None)
        if not msg_id:
            return False

        retries = max(1, config.VERIFY_SEND_RETRIES)
        for _ in range(retries):
            await asyncio.sleep(jitter(config.VERIFY_SEND_DELAY, 0.2, rng, 0.15))
            try:
                found = await client.get_messages(target_entity, ids=msg_id)
                if isinstance(found, list):
                    found = found[0] if found else None
                if self._message_has_story(found, story_id):
                    return True
            except Exception:
                pass

            try:
                async for msg in client.iter_messages(target_entity, limit=5):
                    if getattr(msg, "id", None) == msg_id and self._message_has_story(msg, story_id):
                        return True
            except Exception:
                pass

        return False

    async def send_story(self, client, target_entity, story: StoryRef, story_cache: dict):
        story_peer = await self.resolve_story_peer(client, story, story_cache)
        media = InputMediaStory(peer=story_peer, id=story.story_id)
        result = await client.send_file(target_entity, file=media)
        return self._extract_sent_message(result)

    async def delete_dm_for_me(self, client, sid, target):
        """Удалить диалог только у себя (revoke=False), не у собеседника."""
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
                return "retry"
            try:
                if not await client.is_user_authorized():
                    log.error(f"{sid} | ❌ не авторизована, удаляю")
                    delete_after = True
                    return "drop"
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
                log.warning(f"{sid} | ⚠️ нет целей (контакты/группы)")
                return "drop"
            log.info(f"{sid} | 🎯 целей: {users_n} взаимных контактов + {groups_n} групп/супергрупп")

            story_cache: dict[str, object] = {}
            session_ready = await self.prepare_stories(client, sid, story_cache)
            if not session_ready:
                log.warning(f"{sid} | ⚠️ нет доступных stories для этого аккаунта")
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
                delete_session_files(path)
            log.info(f"{sid} | ⏹ завершена ({int(time.time() - start)}с)")

    async def send_loop(self, client, sid, path, targets, rng, story_cache, session_ready):
        stint = rng.randint(int(config.REFRESH_INTERVAL * 0.9), int(config.REFRESH_INTERVAL * 1.1))
        errors = 0
        bad_peers = 0
        start = time.time()
        sent_local = 0
        shadow_local = 0
        shadow_streak = 0
        target_idx = 0
        total = len(targets)

        while True:
            if target_idx >= total:
                target_idx = 0
                log.info(f"{sid} | 🔁 круг готов, реально {sent_local}, тень {shadow_local}")
                await asyncio.sleep(jitter(config.DELAY_CYCLES, 0.0, rng, 5.0))

            if time.time() - start >= stint:
                log.info(f"{sid} | ♻️ смена слота (~{stint}с), реально {sent_local}, тень {shadow_local}")
                return "rotate"

            target = targets[target_idx]
            target_idx += 1

            if target.kind == "user":
                if shadow_streak >= config.SHADOW_SKIP_CONTACT_STREAK:
                    continue
                if rng.random() > config.CONTACT_SEND_RATIO:
                    continue

            kind_tag = "ЛС" if target.kind == "user" else "группа"
            pos = f"{target_idx}/{total}"

            try:
                story: StoryRef | None = None
                tried: set[tuple[str, int]] = set()
                verified_ok = False
                api_shadow = False
                target_bad = False
                attempts = min(5, max(1, len(session_ready)))

                for _ in range(attempts):
                    story = self.pick_story(rng, session_ready, exclude=tried)
                    if story is None:
                        break
                    tried.add(self._story_key(story))
                    try:
                        async with self.sema:
                            sent_msg = await self.send_story(client, target.entity, story, story_cache)
                        self.mark_api_accepted()
                        verified_ok = await self.verify_story_sent(
                            client, target.entity, story.story_id, sent_msg, rng
                        )
                        if verified_ok:
                            break
                        api_shadow = True
                        self.mark_shadow()
                        shadow_local += 1
                        shadow_streak += 1
                        log.warning(
                            f"{sid} | 👻 тень → {kind_tag} {target.label} "
                            f"({target_idx}/{total}) | {story.label} | серия: {shadow_streak}"
                        )
                        if shadow_streak >= config.SHADOW_SESSION_LIMIT:
                            sess_file = os.path.basename(path)
                            rest_sec = self.shadow_rest_seconds(sess_file)
                            self.rest_strikes[sess_file] = self.rest_strikes.get(sess_file, 0) + 1
                            self.flood_rest[sess_file] = time.time() + rest_sec
                            log.error(
                                f"{sid} | 🌑 {shadow_streak} теней подряд — "
                                f"отдых {rest_sec}с (strike {self.rest_strikes[sess_file]})"
                            )
                            return "retry"
                        break
                    except StoryPeerError:
                        if story is not None:
                            session_ready.discard(self._story_key(story))
                            story_cache.pop(story.peer.lower(), None)
                    except (UsernameInvalidError, UsernameNotOccupiedError, PeerIdInvalidError) as ue:
                        log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                        target_bad = True
                        break
                    except RPCError as te:
                        if is_soft_target_error(te):
                            log.warning(f"{sid} | ⚠ {target.label}: {humanize(te)}")
                            target_bad = True
                            break
                        if story is not None and is_story_id_invalid_error(te):
                            self.mark_story_fail(story)
                            session_ready.discard(self._story_key(story))
                            story_cache.pop(story.peer.lower(), None)
                            continue
                        raise

                if verified_ok and story is not None:
                    n = self.mark_verified_sent()
                    sent_local += 1
                    shadow_streak = 0
                    errors = bad_peers = 0
                    log.info(
                        f"{sid} | ✅ реально → {kind_tag} {target.label} "
                        f"({target_idx}/{total}) | {story.label} | всего: {n}"
                    )
                    if target.kind == "user":
                        await self.delete_dm_for_me(client, sid, target)
                    delay_mul = self.adaptive_delay_factor()
                    if sent_local < config.SESSION_WARMUP_SENDS:
                        delay_mul *= config.WARMUP_DELAY_MULTIPLIER
                    await asyncio.sleep(
                        jitter(config.DELAY_MESSAGES * delay_mul, 0.3, rng, max(0.15, config.DELAY_MESSAGES * 0.25))
                    )
                    continue

                if api_shadow:
                    shadow_pause = 2.0 * self.adaptive_delay_factor()
                    await asyncio.sleep(jitter(shadow_pause, 0.3, rng, 1.0))
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

                if len(session_ready) == 0:
                    log.error(f"{sid} | ❌ нет stories для этого аккаунта — rotate")
                    return "rotate"

                if len(self.global_bad_stories) >= len(self.stories):
                    log.error(f"{sid} | ❌ все stories глобально битые — обнови stories.txt")
                    await asyncio.sleep(30)
                continue

            except FloodWaitError as fw:
                secs = getattr(fw, "seconds", 0) or 10
                self.mark_flood()
                if secs > FLOOD_SOFT_LIMIT:
                    self.flood_rest[os.path.basename(path)] = time.time() + secs
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
                    story_cache.pop(story.peer.lower(), None)
                await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
                continue
            except PeerFloodError as te:
                log.error(f"{sid} | ✖ {target.label}: {humanize(te)}")
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))
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
                        story_cache.pop(story.peer.lower(), None)
                    await asyncio.sleep(jitter(1, 0.2, rng, 0.3))
                    continue
                log.error(f"{sid} | ✖ {target.label}: {msg}")
                if story is not None and is_story_id_invalid_error(te):
                    self.mark_story_fail(story)
                    session_ready.discard(self._story_key(story))
                    story_cache.pop(story.peer.lower(), None)
                    await asyncio.sleep(jitter(2, 0.2, rng, 0.5))
                    continue
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))
            except Exception as ex:
                log.exception(f"{sid} | ✖ {target.label}: {ex}")
                errors += 1
                await asyncio.sleep(jitter(3, 0.2, rng, 0.5))

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
            self.rest_strikes.pop(os.path.basename(path), None)
            self.push_back(path)
        elif result == "retry":
            sess = os.path.basename(path)
            expiry = self.flood_rest.pop(sess, 0)
            if expiry > time.time():
                rest = min(int(expiry - time.time()), 3600)
            else:
                rest = 30
                self.rest_strikes[sess] = 0
            await asyncio.sleep(rest)
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
        bg = [
            asyncio.create_task(self.dispatcher()),
            asyncio.create_task(self.watch_loop()),
            asyncio.create_task(self.write_stats_loop()),
            asyncio.create_task(self.log_stats_loop()),
            asyncio.create_task(self.reload_stories_loop()),
            asyncio.create_task(self.maintenance_loop()),
        ]
        log.info(
            f"💬 Старт (stories). Лимит сессий: {config.MAX_SESSIONS}, "
            f"параллельно: {config.MAX_CONCURRENT}, историй: {len(self.stories)}"
        )
        try:
            await self.stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        for t in bg + list(self.tasks):
            t.cancel()
        await asyncio.gather(*bg, *self.tasks, return_exceptions=True)
