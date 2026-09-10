"""Канал/пост → reply → forward в ЛС."""

import re
import secrets
from urllib.parse import urlparse

from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel

INVITE_RE = re.compile(r"(?:t\.me/\+|joinchat/)([A-Za-z0-9_-]+)", re.IGNORECASE)


def normalize_source_link(link: str) -> str:
    link = (link or "").strip()
    if "|" in link:
        _, link = link.split("|", 1)
        link = link.strip()
    return link


def parse_post_message_id(link: str) -> int | None:
    """t.me/user/123 или t.me/c/123456/123 → id поста."""
    link = normalize_source_link(link)
    low = link.lower()
    if "t.me/" not in low and "telegram.me/" not in low:
        return None
    url = link if "://" in link else f"https://{link.lstrip('/')}"
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "c" and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[2])
    if len(parts) >= 2 and parts[0] not in ("c", "s", "+") and parts[-1].isdigit():
        if parts[0] == "s" and len(parts) >= 3:
            return None
        return int(parts[-1])
    return None


def parse_channel_peer(link: str):
    """Из ссылки channels.txt получить peer для get_entity."""
    link = normalize_source_link(link)
    if not link:
        return None
    if link.startswith("@"):
        return link
    if link.lstrip("-").isdigit():
        return int(link)
    low = link.lower()
    if "t.me/" in low or "telegram.me/" in low:
        url = link if "://" in link else f"https://{link.lstrip('/')}"
        path = urlparse(url).path.strip("/")
        if not path:
            return link
        parts = path.split("/")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int(f"-100{parts[1]}")
        if parts[0] == "s" and len(parts) >= 2:
            return parts[1]
        return parts[0]
    return link


def extract_invite_hash(link: str) -> str | None:
    link = normalize_source_link(link)
    m = INVITE_RE.search(link.replace("https://", "").replace("http://", ""))
    if m:
        return m.group(1)
    if link.startswith("+") and len(link) > 1:
        return link[1:]
    return None


async def ensure_channel_joined(client, channel_link: str, entity):
    """Подписать аккаунт на канал перед чтением постов."""
    invite = extract_invite_hash(channel_link)
    if invite:
        try:
            await client(ImportChatInviteRequest(invite))
            return
        except UserAlreadyParticipantError:
            return
        except Exception:
            pass

    if not isinstance(entity, Channel):
        return

    if getattr(entity, "left", True) is False:
        return

    try:
        await client(JoinChannelRequest(entity))
    except UserAlreadyParticipantError:
        pass
    except Exception:
        pass


async def resolve_channel_entity(client, channel_link: str):
    link = normalize_source_link(channel_link)
    invite = extract_invite_hash(link)
    if invite:
        try:
            updates = await client(ImportChatInviteRequest(invite))
            for chat in getattr(updates, "chats", []) or []:
                if isinstance(chat, Channel) and getattr(chat, "broadcast", False):
                    return chat
        except UserAlreadyParticipantError:
            pass

    peer = parse_channel_peer(link)
    if peer is None:
        raise ValueError(f"битая ссылка: {channel_link}")
    entity = await client.get_entity(peer)
    await ensure_channel_joined(client, link, entity)
    return await client.get_entity(peer)


def channel_label(link: str) -> str:
    link = normalize_source_link(link)
    msg_id = parse_post_message_id(link)
    peer = parse_channel_peer(link)
    if isinstance(peer, str) and peer.startswith("@"):
        base = peer
    elif isinstance(peer, str):
        base = f"@{peer}"
    else:
        base = str(peer or link)
    if msg_id is not None:
        return f"{base}/{msg_id}"
    return base


def entity_label(entity, link: str = "") -> str:
    title = getattr(entity, "title", None) or "?"
    username = getattr(entity, "username", None)
    msg_id = parse_post_message_id(link) if link else None
    post_tag = f" / пост #{msg_id}" if msg_id is not None else ""
    if username:
        return f"«{title}» @{username}{post_tag}"
    if link:
        return f"«{title}» ({channel_label(link)})"
    return f"«{title}»{post_tag}"


def _is_real_post(msg) -> bool:
    return bool(msg) and not getattr(msg, "action", None)


async def get_channel_post(client, source_link: str):
    """
    Пост по прямой ссылке (t.me/user/2) или первый пост канала, если ссылка без id.
    """
    link = normalize_source_link(source_link)
    entity = await resolve_channel_entity(client, link)
    msg_id = parse_post_message_id(link)

    if msg_id is not None:
        post = await client.get_messages(entity, ids=msg_id)
        if _is_real_post(post):
            return entity, post
        return entity, None

    msgs = await client.get_messages(entity, limit=20)
    if msgs:
        for msg in reversed(msgs):
            if _is_real_post(msg):
                return entity, msg
        for msg in msgs:
            if _is_real_post(msg):
                return entity, msg

    async for msg in client.iter_messages(entity, limit=20, reverse=True):
        if _is_real_post(msg):
            return entity, msg

    async for msg in client.iter_messages(entity, limit=20):
        if _is_real_post(msg):
            return entity, msg

    return entity, None


# alias
get_first_channel_post = get_channel_post


SAVED_FORWARD_BUILD = "20250821-r3"


def _telegram_random_id() -> int:
    """Совместимо с Telethon 1.34+ (без generate_random_id в helpers)."""
    return secrets.randbits(63)


async def _parse_html_caption(client, caption: str):
    text = caption or ""
    if hasattr(client, "_parse_message_text"):
        return await client._parse_message_text(text, "html")
    try:
        from telethon.extensions import html as telethon_html

        return telethon_html.parse(text)
    except Exception:
        return text, None


async def send_channel_reply(client, target_entity, channel_entity, channel_post, caption: str):
    """Одно сообщение: reply на пост канала + текст (превью поста сверху)."""
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import InputReplyToMessage

    target = await client.get_input_entity(target_entity)
    channel_peer = await client.get_input_entity(channel_entity)
    message, entities = await _parse_html_caption(client, caption)

    reply = InputReplyToMessage(
        reply_to_msg_id=channel_post.id,
        top_msg_id=channel_post.id,
        reply_to_peer_id=channel_peer,
    )

    result = await client(
        SendMessageRequest(
            peer=target,
            message=message,
            entities=entities,
            reply_to=reply,
            no_webpage=True,
            random_id=_telegram_random_id(),
        )
    )
    return result


async def build_saved_reply(client, channel_entity, channel_post, caption: str):
    """Legacy: то же самое в избранное (не используется рассылкой)."""
    return await send_channel_reply(client, "me", channel_entity, channel_post, caption)


async def cleanup_saved(client, *messages):
    ids = [m.id for m in messages if m is not None and getattr(m, "id", None)]
    if not ids:
        return
    try:
        await client.delete_messages("me", ids, revoke=True)
    except Exception:
        pass
