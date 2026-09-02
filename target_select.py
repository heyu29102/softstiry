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


def _group_label(entity) -> str:
    if getattr(entity, "username", None):
        return f"@{entity.username}"
    title = getattr(entity, "title", None)
    return title or str(getattr(entity, "id", "?"))


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
    uname = getattr(entity, "username", None) if entity is not None else None
    if uname:
        return f"@{uname}" if not str(uname).startswith("@") else str(uname)
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
        if not is_unregistered_key_error(exc):
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
                targets.append(Target(entity, "group", 400, _group_label(entity)))
                continue
            if include_dialogs and dialog.is_user:
                _add_user(targets, seen_users, entity, 0, max_offline_days)
    except Exception as exc:
        result.dialogs_error = str(exc)
        if not is_unregistered_key_error(exc):
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
