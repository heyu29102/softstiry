"""Выбор целей: взаимные контакты (свежие) + групповые чаты."""

from dataclasses import dataclass
from datetime import datetime, timezone

from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import (
    Channel,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)


@dataclass
class Target:
    entity: object
    kind: str  # "user" | "group"
    score: int
    label: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def score_user_status(status, max_offline_days: int) -> tuple[int, bool]:
    """Возвращает (score, eligible). Не шлём тем, кто давно не в сети."""
    if status is None:
        return 50, True

    if isinstance(status, UserStatusOnline):
        return 1000, True

    if isinstance(status, UserStatusRecently):
        return 850, True

    if isinstance(status, UserStatusOffline):
        was = status.was_online
        if was is None:
            return 100, True
        if was.tzinfo is None:
            was = was.replace(tzinfo=timezone.utc)
        age_sec = (_utcnow() - was).total_seconds()
        if age_sec > max_offline_days * 86400:
            return 0, False
        if age_sec < 3600:
            return 950, True
        if age_sec < 86400:
            return 750, True
        if age_sec < 3 * 86400:
            return 550, True
        if age_sec < 7 * 86400:
            return 350, True
        return 200, True

    if isinstance(status, UserStatusLastWeek):
        return 250, True

    if isinstance(status, UserStatusLastMonth):
        return 120, True

    if isinstance(status, UserStatusEmpty):
        return 40, True

    return 80, True


def _user_label(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(getattr(user, "id", "?"))


def _group_label(entity) -> str:
    if getattr(entity, "username", None):
        return f"@{entity.username}"
    title = getattr(entity, "title", None)
    return title or str(getattr(entity, "id", "?"))


def _is_group_chat(dialog) -> bool:
    """Обычная группа или супергруппа (megagroup). Каналы-вещалки не берём."""
    if dialog.is_group:
        return True
    entity = dialog.entity
    if entity is None:
        return False
    if isinstance(entity, Channel) and getattr(entity, "megagroup", False):
        return not getattr(entity, "broadcast", False)
    return False


async def collect_targets(client, rng, max_offline_days: int) -> list[Target]:
    targets: list[Target] = []
    all_mutual = config.CONTACT_ALL_MUTUAL

    try:
        contacts = await client(GetContactsRequest(hash=0))
        for user in contacts.users:
            if not getattr(user, "mutual_contact", False):
                continue
            if getattr(user, "bot", False) or getattr(user, "deleted", False):
                continue
            if all_mutual:
                score, ok = 100, True
            else:
                score, ok = score_user_status(user.status, max_offline_days)
            if not ok:
                continue
            targets.append(Target(user, "user", score, _user_label(user)))
    except Exception:
        pass

    try:
        dialogs = await client.get_dialogs()
        seen_group_ids: set[int] = set()
        for dialog in dialogs:
            if not _is_group_chat(dialog):
                continue
            entity = dialog.entity
            if entity is None:
                continue
            gid = getattr(entity, "id", None)
            if gid is not None:
                if gid in seen_group_ids:
                    continue
                seen_group_ids.add(gid)
            targets.append(Target(entity, "group", 400, _group_label(entity)))
    except Exception:
        pass

    users = [t for t in targets if t.kind == "user"]
    groups = [t for t in targets if t.kind == "group"]
    users.sort(key=lambda t: t.score, reverse=True)
    rng.shuffle(groups)
    return users + groups
