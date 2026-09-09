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
    UserBannedInChannelError,
)

try:
    from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
except ImportError:
    class UsernameInvalidError(RPCError):
        pass

    class UsernameNotOccupiedError(RPCError):
        pass
from telethon.tl import functions
from telethon.tl.types import InputMediaStory

import config
from proxies import ProxyPool
from story_refs import StoryRef, load_story_refs
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
        self.bad_stories: set[tuple[str, int]] = set()
        self._all_stories_bad_logged = False

    def _story_key(self, story: StoryRef) -> tuple[str, int]:
        return (story.peer.lower(), story.story_id)

    def mark_story_bad(self, story: StoryRef):
        key = self._story_key(story)
        if key not in self.bad_stories:
            self.bad_stories.add(key)
            alive = len(self.stories) - len(self.bad_stories)
            log.warning(f"📖 story {story.label} — битая, осталось: {max(0, alive)}/{len(self.stories)}")
            if alive <= 0 and not self._all_stories_bad_logged:
                self._all_stories_bad_logged = True
                log.error("❌ ВСЕ stories битые — обнови stories.txt")

    def pick_story(self, rng: random.Random, exclude: set[tuple[str, int]] | None = None) -> StoryRef | None:
        exclude = exclude or set()
        pool = [
            s for s in self.stories
            if self._story_key(s) not in self.bad_stories and self._story_key(s) not in exclude
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
        self.stories = load_story_refs(config.STORIES_FILE)
        self.bad_stories.clear()
        self._all_stories_bad_logged = False

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
                f"активных: {self.active}/{config.MAX_SESSIONS} | очередь: {len(self.pending)} | "
                f"всего: {self.total} | прокси в кулдауне: {self.proxies.cooldown_count()} | "
                f"историй: {len(self.stories)}"
            )

    async def reload_stories_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(config.STORIES_RELOAD_INTERVAL)
            try:
                stories = await asyncio.to_thread(load_story_refs, config.STORIES_FILE)
                if stories:
                    self.stories = stories
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

    async def resolve_story_peer(self, client, story: StoryRef, story_cache: dict):
        key = story.peer.lower()
        cached = story_cache.get(key)
        if cached is not None:
            return cached
        try:
            entity = await self._get_story_entity(client, story)
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

    async def send_story(self, client, target_entity, story: StoryRef, story_cache: dict):
        story_peer = await self.resolve_story_peer(client, story, story_cache)
        media = InputMediaStory(peer=story_peer, id=story.story_id)
        await client.send_file(target_entity, file=media)

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
            log.info(f"{sid} | 🎯 целей: {users_n} взаимных контактов + {groups_n} групп (круги)")
            return await self.send_loop(client, sid, path, targets, rng)
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

    async def send_loop(self, client, sid, path, targets, rng):
        stint = rng.randint(int(config.REFRESH_INTERVAL * 0.9), int(config.REFRESH_INTERVAL * 1.1))
        errors = 0
        bad_peers = 0
        start = time.time()
        sent_local = 0
        target_idx = 0
        total = len(targets)
        story_cache: dict[str, object] = {}

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
            kind_tag = "ЛС" if target.kind == "user" else "группа"
            pos = f"{target_idx}/{total}"

            try:
                story: StoryRef | None = None
                tried: set[tuple[str, int]] = set()
                sent_ok = False
                target_bad = False
                attempts = min(5, max(1, len(self.stories)))

                for _ in range(attempts):
                    story = self.pick_story(rng, exclude=tried)
                    if story is None:
                        break
                    tried.add(self._story_key(story))
                    try:
                        async with self.sema:
                            await self.send_story(client, target.entity, story, story_cache)
                        sent_ok = True
                        break
                    except StoryPeerError:
                        self.mark_story_bad(story)
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
                        raise

                if sent_ok and story is not None:
                    n = self.mark_sent()
                    sent_local += 1
                    errors = bad_peers = 0
                    log.info(
                        f"{sid} | ✅ story → {kind_tag} {target.label} "
                        f"({target_idx}/{total}) | {story.label} | всего: {n}"
                    )
                    if target.kind == "user":
                        await self.delete_dm_for_me(client, sid, target)
                    await asyncio.sleep(jitter(config.DELAY_MESSAGES, 0.3, rng, 1.0))
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

                if len(self.bad_stories) >= len(self.stories):
                    log.error(f"{sid} | ❌ нет живых stories — обнови stories.txt")
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
                if "STORY_ID_INVALID" in msg.upper():
                    await asyncio.sleep(jitter(2, 0.2, rng, 0.5))
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
            self.push_back(path)
        elif result == "retry":
            rest = self.flood_rest.pop(os.path.basename(path), 30)
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
        bg = [
            asyncio.create_task(self.dispatcher()),
            asyncio.create_task(self.watch_loop()),
            asyncio.create_task(self.write_stats_loop()),
            asyncio.create_task(self.log_stats_loop()),
            asyncio.create_task(self.reload_stories_loop()),
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
