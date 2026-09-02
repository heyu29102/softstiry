"""Выбор целей: взаимные контакты (свежие) + пользователи/группы из диалогов."""

from telethon_patch import apply_telethon_patch

apply_telethon_patch()

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import (
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

log = logging.getLogger("spam")


def _is_unregistered_key_error(exc: BaseException | str | None) -> bool:
    msg = str(exc or "").lower()
    return "key is not registered" in msg or "auth key unregistered" in msg


@dataclass
class Target:
    entity: object
    kind: str  # "user" | "group"
    score: int
    label: str


@dataclass
class CollectResult:
    targets: list[Target] = field(default_factory=list)
    users: int = 0
    groups: int = 0
    contacts_error: str = ""
    dialogs_error: str = ""


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
        return 0, False

    if isinstance(status, UserStatusEmpty):
        return 0, False

    return 80, True


def _user_label(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = " ".join(
        x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x
    ).strip()
    if name:
        return name
    return str(getattr(user, "id", "?"))


def _entity_public_username(entity) -> str | None:
    """Публичный @username с entity (включая usernames[])."""
    uname = getattr(entity, "username", None)
    if uname:
        return str(uname).lstrip("@")
    for item in getattr(entity, "usernames", None) or []:
        u = getattr(item, "username", None)
        if not u:
            continue
        if getattr(item, "active", True):
            return str(u).lstrip("@")
    return None


def _group_may_have_username(entity) -> bool:
    """Базовый Chat без username никогда; supergroup/channel — может."""
    from telethon.tl.types import Channel

    if isinstance(entity, Channel):
        return bool(getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False))
    return getattr(entity, "megagroup", False)


def _group_label(entity) -> str:
    uname = _entity_public_username(entity)
    if uname:
        return f"@{uname}"
    title = getattr(entity, "title", None)
    return title or str(getattr(entity, "id", "?"))


async def _group_target_entity(client, entity):
    """Диалоги часто без username — дотягиваем через get_entity для supergroup."""
    if _entity_public_username(entity):
        return entity
    if not _group_may_have_username(entity):
        return entity
    try:
        refreshed = await client.get_entity(entity)
        if _entity_public_username(refreshed):
            return refreshed
    except Exception:
        pass
    return entity


async def _group_label_resolved(client, entity) -> tuple[object, str]:
    entity = await _group_target_entity(client, entity)
    return entity, _group_label(entity)


def _group_members_count(entity) -> int | None:
    """Число участников, если Telegram отдал в entity (без лишних RPC)."""
    count = getattr(entity, "participants_count", None)
    if count is None:
        return None
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def _group_eligible(entity) -> bool:
    """Пропускаем мелкие группы (< GROUP_MIN_MEMBERS). Неизвестный размер — оставляем."""
    min_n = config.GROUP_MIN_MEMBERS
    if min_n <= 0:
        return True
    count = _group_members_count(entity)
    if count is None:
        return True
    return count >= min_n


def target_username(target) -> str | None:
    entity = getattr(target, "entity", None)
    uname = _entity_public_username(entity) if entity is not None else None
    if uname:
        return f"@{uname}"
    label = (getattr(target, "label", "") or "").strip()
    if label.startswith("@"):
        return label.split()[0]
    return None


def _add_user(targets: list[Target], seen_users: set[int], user, score_bonus: int, max_offline_days: int) -> bool:
    uid = getattr(user, "id", None)
    if uid is None or uid in seen_users:
        return False
    if getattr(user, "bot", False) or getattr(user, "deleted", False):
        return False
    if getattr(user, "self", False):
        return False
    score, ok = score_user_status(user.status, max_offline_days)
    if not ok:
        return False
    targets.append(Target(user, "user", score + score_bonus, _user_label(user)))
    seen_users.add(uid)
    return True


async def collect_targets(
    client,
    rng,
    max_offline_days: int,
    include_dialogs: bool = True,
    dialogs_limit: int | None = None,
) -> CollectResult:
    result = CollectResult()
    targets: list[Target] = []
    seen_users: set[int] = set()

    try:
        contacts = await client(GetContactsRequest(hash=0))
        for user in contacts.users:
            if not getattr(user, "mutual_contact", False):
                continue
            _add_user(targets, seen_users, user, 150, max_offline_days)
    except Exception as exc:
        result.contacts_error = str(exc)
        if not _is_unregistered_key_error(exc):
            log.warning(f"collect_targets: contacts failed: {exc}")

    try:
        limit = dialogs_limit if dialogs_limit and dialogs_limit > 0 else None
        dialogs = await client.get_dialogs(limit=limit)
        for dialog in dialogs:
            entity = dialog.entity
            if entity is None:
                continue
            if dialog.is_group:
                if not _group_eligible(entity):
                    continue
                entity, label = await _group_label_resolved(client, entity)
                targets.append(Target(entity, "group", 400, label))
                continue
            if include_dialogs and dialog.is_user:
                _add_user(targets, seen_users, entity, 0, max_offline_days)
    except Exception as exc:
        result.dialogs_error = str(exc)
        if not _is_unregistered_key_error(exc):
            log.warning(f"collect_targets: dialogs failed: {exc}")

    users = [t for t in targets if t.kind == "user"]
    groups = [t for t in targets if t.kind == "group"]
    users.sort(key=lambda t: t.score, reverse=True)
    rng.shuffle(groups)
    if config.TARGET_GROUPS_FIRST:
        result.targets = groups + users
    else:
        result.targets = users + groups
    result.users = len(users)
    result.groups = len(groups)
    return result


def reshuffle_target_order(targets: list[Target], rng) -> None:
    """Перемешать группы на новом круге — иначе порядок фиксируется на весь слот."""
    users = [t for t in targets if t.kind == "user"]
    groups = [t for t in targets if t.kind == "group"]
    rng.shuffle(groups)
    if config.TARGET_GROUPS_FIRST:
        targets[:] = groups + users
    else:
        targets[:] = users + groups
