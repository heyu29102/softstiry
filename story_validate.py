"""
Проверка stories.txt: peer существует, story ID жив.
Мёртвые (username not found, STORY_ID_INVALID и т.п.) — удаляются из файла.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import config
from story_refs import StoryRef, load_story_refs, save_story_refs

log = logging.getLogger("spam")

_story_lock = asyncio.Lock()

PERMANENT_RPC_MARKERS = (
    "STORY_ID_INVALID",
    "USERNAME_INVALID",
    "USERNAME_NOT_OCCUPIED",
    "CHANNEL_INVALID",
    "PEER_ID_INVALID",
    "STORIES_NEVER_CREATED",
    "MSG_ID_INVALID",
)

PERMANENT_EXCEPTIONS = (
    "UsernameInvalidError",
    "UsernameNotOccupiedError",
    "ChannelInvalidError",
    "PeerIdInvalidError",
)


def story_key(story: StoryRef) -> tuple[str, int]:
    return (story.peer.lower(), story.story_id)


def watcher_session_path() -> Path | None:
    raw = (config.STORY_VALIDATE_SESSION or config.DOMAINS_WATCHER_SESSION or "").strip()
    if raw:
        return Path(raw).expanduser()
    default = config.BASE_DIR / "watcher.session"
    if default.exists():
        return default
    return None


def is_permanent_story_error(exc: BaseException | None = None, msg: str = "") -> bool:
    if exc is not None:
        name = type(exc).__name__
        if name in PERMANENT_EXCEPTIONS:
            return True
        msg = (getattr(exc, "message", "") or str(exc) or msg).upper()
    upper = (msg or "").upper()
    return any(marker in upper for marker in PERMANENT_RPC_MARKERS)


async def remove_story_keys(keys: set[tuple[str, int]], reason: str) -> int:
    """Удаляет истории из stories.txt. Возвращает число удалённых."""
    if not keys:
        return 0
    async with _story_lock:
        refs = load_story_refs(config.STORIES_FILE)
        before = len(refs)
        new_refs = [r for r in refs if story_key(r) not in keys]
        removed = before - len(new_refs)
        if removed:
            save_story_refs(new_refs, config.STORIES_FILE)
            for key in keys:
                ref = StoryRef(peer=key[0], story_id=key[1])
                log.warning(f"🗑 story удалена из пула: {ref.url} ({reason})")
        return removed


async def validate_story(client, story: StoryRef) -> tuple[bool | None, str]:
    """
    True  — story жива.
    False — мёртвая (удалять).
    None  — не удалось проверить (сеть/временная ошибка).
    """
    from telethon.errors import RPCError
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.tl.functions.stories import GetStoriesByIDRequest

    peer = None
    uname = story.peer.lstrip("@")
    try:
        peer = await client.get_input_entity(story.peer)
    except Exception as exc:
        if is_permanent_story_error(exc):
            return False, type(exc).__name__
        try:
            resolved = await client(ResolveUsernameRequest(username=uname))
            peer = await client.get_input_entity(resolved.peer)
        except Exception as exc2:
            if is_permanent_story_error(exc2):
                return False, type(exc2).__name__
            return None, f"resolve: {exc2}"

    try:
        result = await client(GetStoriesByIDRequest(peer=peer, id=[story.story_id]))
    except RPCError as exc:
        if is_permanent_story_error(exc):
            return False, str(exc)
        return None, str(exc)
    except Exception as exc:
        if is_permanent_story_error(exc):
            return False, str(exc)
        return None, str(exc)

    items = getattr(result, "stories", None) or []
    found_ids = {getattr(item, "id", None) for item in items}
    if story.story_id not in found_ids:
        return False, "story не найдена (истекла или удалена)"
    return True, "ok"


async def check_all_stories(client, stories: list[StoryRef] | None = None) -> tuple[int, int]:
    """Проверяет все stories. Возвращает (живых, удалено)."""
    stories = stories if stories is not None else load_story_refs(config.STORIES_FILE)
    if not stories:
        return 0, 0

    to_remove: set[tuple[str, int]] = {}
    alive = 0
    for story in stories:
        key = story_key(story)
        if key in to_remove:
            continue
        status, reason = await validate_story(client, story)
        if status is True:
            alive += 1
            log.info(f"✅ story ok: {story.url}")
        elif status is False:
            to_remove.add(key)
            log.warning(f"❌ story мёртвая: {story.url} ({reason})")
        else:
            log.warning(f"⚠ story не проверена, оставляю: {story.url} ({reason})")
        await asyncio.sleep(config.STORY_VALIDATE_DELAY)

    removed = await remove_story_keys(to_remove, "валидация")
    return alive, removed


class StoryInvalidTracker:
    """Счётчик подтверждений мёртвой story от разных сессий рассылки."""

    def __init__(self):
        self._hits: dict[tuple[str, int], tuple[int, str, float]] = {}

    def _prune(self):
        now = time.time()
        ttl = config.STORY_INVALID_HIT_TTL
        dead = [k for k, (_, _, ts) in self._hits.items() if now - ts > ttl]
        for k in dead:
            self._hits.pop(k, None)

    def hit(self, story: StoryRef, reason: str) -> int:
        self._prune()
        key = story_key(story)
        count, prev_reason, _ = self._hits.get(key, (0, "", 0.0))
        count += 1
        self._hits[key] = (count, reason or prev_reason, time.time())
        return count

    def should_remove(self, story: StoryRef, reason: str) -> bool:
        if not config.STORY_REMOVE_ON_CONFIRM:
            return False
        count = self.hit(story, reason)
        upper = (reason or "").upper()
        if "STORY_ID" in upper or "ИСТЕК" in upper:
            threshold = 1
        elif is_permanent_story_error(msg=reason):
            threshold = config.STORY_CONFIRM_THRESHOLD
        else:
            threshold = config.STORY_CONFIRM_THRESHOLD
        return count >= threshold


async def connect_validator_client():
    from opentele.api import API
    from opentele.tl import TelegramClient
    from telethon_patch import apply_telethon_patch

    apply_telethon_patch()
    session_path = watcher_session_path()
    if not session_path or not session_path.exists():
        return None, None, None, "нет сессии валидатора (STORY_VALIDATE_SESSION / watcher.session)"

    from proxies import ProxyPool

    pool = ProxyPool.from_file()
    proxy = pool.acquire() if pool.proxies else None
    api = API.TelegramDesktop.Generate(unique_id="story_validator")
    proxy_dict = pool.to_dict(proxy) if proxy else None
    client = TelegramClient(str(session_path), api=api, proxy=proxy_dict)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            if proxy:
                pool.release(proxy)
            return None, None, None, f"сессия не авторизована: {session_path}"
        return client, pool, proxy, None
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        if proxy:
            pool.mark_bad(proxy)
            pool.release(proxy)
        return None, None, None, str(exc)


async def validate_loop(stop: asyncio.Event, on_removed=None):
    """Фоновая проверка stories.txt раз в STORY_VALIDATE_INTERVAL."""
    if not config.STORY_VALIDATE_ENABLED:
        return

    if config.STORY_VALIDATE_ON_START:
        await _run_once(on_removed)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.STORY_VALIDATE_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        await _run_once(on_removed)


async def _run_once(on_removed=None):
    client, pool, proxy, err = await connect_validator_client()
    if client is None:
        log.warning(f"📖 story-валидатор: {err}")
        return
    try:
        alive, removed = await check_all_stories(client)
        log.info(f"📖 story-валидатор: живых {alive}, удалено {removed}")
        if removed and on_removed:
            await on_removed()
    except Exception as exc:
        log.warning(f"📖 story-валидатор ошибка: {exc}")
    finally:
        if pool and proxy:
            pool.release(proxy)
        try:
            await client.disconnect()
        except Exception:
            pass


async def main():
    print("Story validator", flush=True)
    print(f"Файл: {config.STORIES_FILE}", flush=True)
    session = watcher_session_path()
    print(f"Сессия: {session or 'нет'}", flush=True)
    client, pool, proxy, err = await connect_validator_client()
    if client is None:
        print(f"Ошибка: {err}")
        return
    try:
        alive, removed = await check_all_stories(client)
        print(f"Готово: живых {alive}, удалено {removed}")
    finally:
        if pool and proxy:
            pool.release(proxy)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
