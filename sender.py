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
    TypeNotFoundError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl import functions
from telethon.tl.types import InputMediaStory

import config
from telethon_patch import apply_telethon_patch, is_tl_schema_error, schema_error_label
from proxies import ProxyPool
from story_refs import StoryRef, load_story_refs
from target_select import collect_targets
from textgen import jitter, jitter_up

colorama.init(autoreset=True)
apply_telethon_patch()

FLOOD_SOFT_LIMIT = 300


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
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(Color(fmt, datefmt))
    fh = RotatingFileHandler(str(config.APP_LOG), maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    log.addHandler(sh)
    log.addHandler(fh)
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
    if isinstance(e, (UsernameInvalidError, UsernameNotOccupiedError)):
        return "битый username story-канала"
    if isinstance(e, TypeNotFoundError):
        return schema_error_label(e)
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    if "STORY_ID_INVALID" in msg:
        return "история истекла/невалидна"
    if "CHAT_SEND_MEDIA_FORBIDDEN" in msg:
        return "медиа запрещено"
    return f"{type(e).__name__}: {e}"


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

    def mark_story_bad(self, story: StoryRef, reason: str, ttl: int | None = None):
        ttl = config.STORY_BAD_TTL if ttl is None else ttl
        self.bad_stories[self.story_key(story)] = (reason, time.time() + ttl)
        log.warning(f"📖 story помечена битой: {story.url} ({reason}, пауза {ttl}с)")

    def pick_story(self, rng: random.Random) -> StoryRef | None:
        pool = [s for s in self.stories if not self.is_story_bad(s)]
        if not pool and self.stories:
            log.warning(f"♻ все {len(self.stories)} stories в bad — сброс blacklist")
            self.bad_stories.clear()
            pool = self.stories[:]
        if not pool:
            return None
        return rng.choice(pool)

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
                "stories_bad": sum(1 for _, (_, until) in self.bad_stories.items() if until > now),
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

    async def resolve_story_peer(self, client, story: StoryRef, story_cache: dict):
        key = story.peer.lower()
        cached = story_cache.get(key)
        if cached is not None:
            return cached

        try:
            peer = await client.get_input_entity(story.peer)
        except Exception:
            peer = await self._resolve_peer_from_dialogs(client, story.peer)
            if peer is None:
                raise
        story_cache[key] = peer
        return peer

    async def send_story(self, client, target_entity, story: StoryRef, story_cache: dict):
        try:
            story_peer = await self.resolve_story_peer(client, story, story_cache)
        except TypeNotFoundError:
            apply_telethon_patch()
            raise
        except (UsernameInvalidError, UsernameNotOccupiedError) as exc:
            self.mark_story_bad(story, humanize(exc), ttl=3600)
            raise
        except ChannelInvalidError as exc:
            self.mark_story_bad(story, humanize(exc), ttl=3600)
            story_cache.pop(story.peer.lower(), None)
            raise
        media = InputMediaStory(peer=story_peer, id=story.story_id)
        await client.send_file(target_entity, file=media)

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
                if config.LOG_SESSION_EVENTS:
                    log.info(f"{sid} | 🟢 {getattr(me, 'first_name', '?')} ({getattr(me, 'id', '?')})")
            except Exception as e:
                log.warning(f"{sid} | ❌ get_me: {e}")
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
                log.warning(f"{sid} | ⚠️ нет целей (контакты/группы)")
                return "drop"
            if config.LOG_SESSION_EVENTS:
                log.info(f"{sid} | 🎯 целей: {users_n} ЛС + {groups_n} групп")
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
            if config.LOG_SESSION_EVENTS:
                log.info(f"{sid} | ⏹ завершена ({int(time.time() - start)}с)")

    def _error_pause(self, rng, mult: float = 1.0):
        d = config.ERROR_DELAY * mult
        return jitter(d, 0.15, rng) if d > 0 else 0

    def _message_pause(self, rng):
        d = config.DELAY_MESSAGES
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
        dead_targets: set[int] = set()

        while True:
            if target_idx >= total:
                target_idx = 0
                if config.LOG_SESSION_EVENTS:
                    log.info(f"{sid} | 🔁 круг готов, отправлено {sent_local}")
                pause = self._cycle_pause(rng)
                if pause > 0:
                    await asyncio.sleep(pause)

            if time.time() - start >= stint:
                if config.LOG_SESSION_EVENTS:
                    log.info(f"{sid} | ♻️ смена слота (~{stint}с), отправлено {sent_local}")
                return "rotate"

            target = targets[target_idx]
            target_idx += 1
            target_id = getattr(target.entity, "id", None)
            if target_id is not None and target_id in dead_targets:
                continue

            kind_tag = "ЛС" if target.kind == "user" else "группа"

            try:
                story = self.pick_story(rng)
                if story is None:
                    alive = len(self.stories) - sum(1 for s in self.stories if self.is_story_bad(s))
                    log.error(
                        f"{sid} | ❌ нет рабочих историй "
                        f"(в пуле {len(self.stories)}, живых {alive}) — обнови stories.txt"
                    )
                    return "retry"

                try:
                    async with self.sema:
                        await self.send_story(client, target.entity, story, story_cache)
                    n = self.mark_sent()
                    sent_local += 1
                    errors = bad_peers = 0
                    every = config.LOG_SUCCESS_EVERY
                    if every == 0 or (every > 0 and sent_local % every == 0):
                        log.info(
                            f"{sid} | ✅ story → {kind_tag} {target.label} "
                            f"({target_idx}/{total}) | {story.url} | всего: {n}"
                        )
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
                except (UsernameInvalidError, UsernameNotOccupiedError, ChannelInvalidError, TypeNotFoundError) as ue:
                    log.warning(f"{sid} | ⚠ story {story.url}: {humanize(ue)}")
                    story_cache.pop(story.peer.lower(), None)
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
                except PeerFloodError as te:
                    log.error(f"{sid} | ✖ {target.label}: {humanize(te)}")
                    errors += 1
                    pause = self._error_pause(rng, 3.0)
                    if pause > 0:
                        await asyncio.sleep(pause)
                except RPCError as te:
                    msg = humanize(te)
                    if "STORY_ID_INVALID" in msg.upper():
                        self.mark_story_bad(story, "история истекла", ttl=300)
                        log.warning(f"{sid} | ⚠ story {story.url}: {msg}")
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
                    return "retry"
                except Exception as ex:
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
                pause = self._message_pause(rng)
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
            f"💬 Старт TURBO (stories). Лимит: {config.MAX_SESSIONS}, "
            f"параллельно: {config.MAX_CONCURRENT}, delay: {config.DELAY_MESSAGES}s, "
            f"историй: {len(self.stories)}"
        )
        try:
            await self.stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        for t in bg + list(self.tasks):
            t.cancel()
        await asyncio.gather(*bg, *self.tasks, return_exceptions=True)
