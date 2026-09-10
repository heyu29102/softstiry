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
from pathlib import Path

import colorama
from opentele.tl import TelegramClient
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

import config
from mail_content import (
    choose_delivery_mode,
    load_mailing_content,
    pick_photo,
    render_caption,
)
from proxies import ProxyPool
from telegram_api import client_api
from session_utils import (
    SessionSendGuard,
    ensure_connected,
    is_session_error,
    session_error_label,
    tune_session_sqlite,
    with_reconnect,
)
from target_select import collect_targets
from textgen import jitter, jitter_up

colorama.init(autoreset=True)

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
        return "битый канал"
    msg = (getattr(e, "message", "") or str(e) or "").upper()
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


def is_media_forbidden_error(e) -> bool:
    msg = (getattr(e, "message", "") or str(e) or "").upper()
    return "CHAT_SEND_MEDIA_FORBIDDEN" in msg


def target_group_id(target) -> int | None:
    if target.kind != "group":
        return None
    return getattr(target.entity, "id", None)


def filter_targets(targets):
    if config.MAIL_CONTACTS and config.MAIL_GROUPS:
        return targets
    out = []
    for t in targets:
        if t.kind == "user" and config.MAIL_CONTACTS:
            out.append(t)
        elif t.kind == "group" and config.MAIL_GROUPS:
            out.append(t)
    return out


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
        self.blocks: list[str] = []
        self.photos: list[Path] = []
        self.domains: list[str] = []
        self.use_domains = False
        self.pending = deque()
        self.wake = asyncio.Event()
        self.seen = set()
        self.slots = asyncio.Semaphore(config.MAX_SESSIONS)
        self.sema = asyncio.Semaphore(config.MAX_CONCURRENT)
        self.total = 0
        self.total_groups = 0
        self.total_contacts = 0
        self.active = 0
        self.sent_ts = deque()
        self.flood_ts = deque()
        self.started = time.time()
        self.tasks = set()
        self.stop = asyncio.Event()
        self.flood_rest = {}
        self.session_guard = SessionSendGuard()
        self.session_errors = 0

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

    def load_content(self):
        blocks, photos, domains, use_domains = load_mailing_content()
        self.blocks = blocks
        self.photos = photos
        self.domains = domains
        self.use_domains = use_domains

    def load(self):
        self.proxies = ProxyPool.from_file()
        self.load_content()
        log.info(
            f"📝 блоков: {len(self.blocks)} | 🖼 фото: {len(self.photos)} | "
            f"🌐 доменов: {len(self.domains)} | 🛰 прокси: {len(self.proxies)} | "
            f"оффлайн лимит: {config.CONTACT_MAX_OFFLINE_DAYS}д"
        )
        if not self.proxies.proxies:
            log.error("❌ Нет прокси, выхожу")
            sys.exit(1)
        if not self.blocks:
            log.error("❌ text.txt пуст — добавь блоки (разделитель ---)")
            sys.exit(1)
        if self.use_domains and not self.domains:
            log.error("❌ domains.txt пуст — добавь домены для {{DOMAIN_LINK}}")
            sys.exit(1)

    def pick_block(self, rng: random.Random) -> str:
        return rng.choice(self.blocks)

    def mark_sent(self, target_kind: str):
        now = time.time()
        self.total += 1
        if target_kind == "user":
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
                "text_blocks": len(self.blocks),
                "photos_in_pool": len(self.photos),
                "domains_in_pool": len(self.domains),
                "total_groups": self.total_groups,
                "total_contacts": self.total_contacts,
                "mail_groups": config.MAIL_GROUPS,
                "mail_contacts": config.MAIL_CONTACTS,
                "mailing_mode": config.mailing_mode(),
                "session_errors": self.session_errors,
                "max_concurrent": config.MAX_CONCURRENT,
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
                f"текст: {len(self.blocks)} | фото: {len(self.photos)} | доменов: {len(self.domains)} | "
                f"группы: {self.total_groups} | ЛС: {self.total_contacts}"
            )

    async def reload_content_loop(self):
        while not self.stop.is_set():
            await asyncio.sleep(config.TEXT_RELOAD_INTERVAL)
            try:
                blocks, photos, domains, use_domains = await asyncio.to_thread(load_mailing_content)
                if blocks:
                    self.blocks = blocks
                self.photos = photos
                if domains:
                    self.domains = domains
                self.use_domains = use_domains
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

    async def send_delivery(self, client, target_entity, mode: str, caption: str, photo_path: Path | None):
        if mode == "photo" and photo_path is not None:
            await client.send_file(
                target_entity,
                file=str(photo_path),
                caption=caption or None,
                parse_mode="html",
                force_document=False,
            )
        else:
            await client.send_message(target_entity, caption, parse_mode="html")

    async def _guarded_send(self, client, sid, path, target_entity, mode: str, caption: str, photo_path: Path | None):
        async def _do():
            async with self.sema:
                await self.send_delivery(client, target_entity, mode, caption, photo_path)

        async with self.session_guard.lock_for(path):
            await with_reconnect(client, sid, log, _do)

    async def try_send(
        self,
        client,
        sid,
        path,
        target,
        caption: str,
        rng,
        text_only_groups: set[int],
    ) -> tuple[bool, str]:
        gid = target_group_id(target)
        text_only = gid is not None and gid in text_only_groups
        mode = choose_delivery_mode(
            rng,
            text_only_group=text_only,
            has_photos=bool(self.photos),
        )
        photo_path = pick_photo(self.photos, rng) if mode == "photo" else None
        mode_tag = "фото+текст" if mode == "photo" else "текст"

        try:
            await self._guarded_send(client, sid, path, target.entity, mode, caption, photo_path)
            return True, mode_tag
        except RPCError as e:
            if is_media_forbidden_error(e) and mode == "photo":
                if gid is not None:
                    text_only_groups.add(gid)
                await self._guarded_send(client, sid, path, target.entity, "text", caption, None)
                return True, "текст (fallback)"
            raise

    def _schedule_delete_dm(self, client, sid, path, target):
        if target.kind != "user" or not config.DELETE_DM_AFTER_SEND:
            return

        async def _job():
            try:
                async with self.session_guard.lock_for(path):
                    await with_reconnect(
                        client,
                        sid,
                        log,
                        lambda: client.delete_dialog(target.entity, revoke=False),
                    )
            except Exception as e:
                log.warning(f"{sid} | не удалил диалог {target.label}: {e}")

        asyncio.create_task(_job())

    async def run_session(self, path):
        rng = random.Random(os.urandom(16))
        sid = os.path.splitext(os.path.basename(path))[0]
        proxy = self.proxies.acquire()
        if proxy is None:
            log.error(f"{sid} | ❌ нет свободных прокси")
            return "retry"
        try:
            api = client_api(path)
        except (FileNotFoundError, ValueError) as e:
            log.error(f"{sid} | ❌ API из json: {e}")
            return "retry"
        client = TelegramClient(
            path,
            api=api,
            proxy=self.proxies.to_dict(proxy),
            connection_retries=3,
            retry_delay=1,
            auto_reconnect=True,
        )
        self.active += 1
        start = time.time()
        delete_after = False
        try:
            try:
                await client.connect()
                tune_session_sqlite(client)
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
                log.info(
                    f"{sid} | 🟢 {getattr(me, 'first_name', '?')} "
                    f"({getattr(me, 'id', '?')}) api_id={getattr(api, 'api_id', '?')}"
                )
            except Exception as e:
                log.warning(f"{sid} | ❌ get_me: {e}")
                return "retry"

            targets = filter_targets(
                await collect_targets(client, rng, config.CONTACT_MAX_OFFLINE_DAYS)
            )
            users_n = sum(1 for t in targets if t.kind == "user")
            groups_n = sum(1 for t in targets if t.kind == "group")
            if not targets:
                log.warning(f"{sid} | ⚠️ нет целей (контакты/группы)")
                return "drop"
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
            log.info(f"{sid} | ⏹ завершена ({int(time.time() - start)}с)")

    async def send_loop(self, client, sid, path, targets, rng):
        stint = rng.randint(int(config.REFRESH_INTERVAL * 0.9), int(config.REFRESH_INTERVAL * 1.1))
        errors = 0
        start = time.time()
        sent_local = 0
        target_idx = 0
        total = len(targets)
        text_only_groups: set[int] = set()

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

            block = self.pick_block(rng)
            caption = render_caption(block, rng, self.domains, self.use_domains)
            if not caption:
                continue

            try:
                ok, mode_tag = await self.try_send(client, sid, path, target, caption, rng, text_only_groups)
                if ok:
                    n = self.mark_sent(target.kind)
                    sent_local += 1
                    errors = 0
                    log.info(f"{sid} | ✅ → {kind_tag} {target.label} ({pos}) | {mode_tag} | всего: {n}")
                    self._schedule_delete_dm(client, sid, path, target)
                    await asyncio.sleep(jitter(config.DELAY_MESSAGES, 0.3, rng, 1.0))
                continue

            except (UsernameInvalidError, UsernameNotOccupiedError, PeerIdInvalidError) as ue:
                log.warning(f"{sid} | ⚠ {target.label}: {humanize(ue)}")
                target_idx = self._drop_target(targets, target_idx)
                total = len(targets)
                await asyncio.sleep(jitter(0.3, 0.1, rng, 0.2))
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

            except PeerFloodError as te:
                log.error(f"{sid} | ✖ {target.label}: {humanize(te)}")
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))

            except RPCError as te:
                if is_soft_target_error(te):
                    log.warning(f"{sid} | ⚠ {target.label}: {humanize(te)}")
                    target_idx = self._drop_target(targets, target_idx)
                    total = len(targets)
                    if total <= 0:
                        targets[:] = filter_targets(
                            await collect_targets(client, rng, config.CONTACT_MAX_OFFLINE_DAYS)
                        )
                        total = len(targets)
                        target_idx = 0
                    await asyncio.sleep(jitter(0.3, 0.1, rng, 0.2))
                    continue
                log.error(f"{sid} | ✖ {target.label}: {humanize(te)}")
                errors += 1
                await asyncio.sleep(jitter(5, 0.2, rng, 1.0))

            except Exception as ex:
                if is_session_error(ex):
                    self.session_errors += 1
                    errors += 1
                    log.warning(f"{sid} | ✖ {target.label}: {session_error_label(ex)}")
                    if errors >= 3:
                        log.warning(f"{sid} | ♻️ сессия нестабильна — ротация")
                        return "retry"
                    await asyncio.sleep(jitter(1.5, 0.3, rng, 1.0))
                    continue
                log.error(f"{sid} | ✖ {target.label}: {type(ex).__name__}: {ex}")
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
            asyncio.create_task(self.reload_content_loop()),
        ]
        log.info(
            f"💬 Старт: текст+фото → ЛС+группы (без теней). "
            f"Сессий: {config.MAX_SESSIONS}, параллельно: {config.MAX_CONCURRENT}, "
            f"блоков: {len(self.blocks)}, фото: {len(self.photos)}, доменов: {len(self.domains)}, "
            f"доля фото: {config.PHOTO_SEND_RATIO:.0%}"
        )
        try:
            await self.stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        for t in bg + list(self.tasks):
            t.cancel()
        await asyncio.gather(*bg, *self.tasks, return_exceptions=True)
