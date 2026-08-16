"""Регистрирует новый TL-конструктор channel#d49f34c6 для свежих ответов Telegram API.

PyPI Telethon 1.44 ещё знает channel#1c32b11c. Сервер уже шлёт d49f34c6 (+ linked_community_id),
из-за чего get_entity/@username для story-канала падает с TypeNotFoundError.
"""

from __future__ import annotations

CHANNEL_NEW_ID = 0xD49F34C6
_PATCHED = False


def apply_telethon_patch() -> bool:
    global _PATCHED
    if _PATCHED:
        return False

    from telethon.tl import alltlobjects
    from telethon.tl.types import Channel

    if CHANNEL_NEW_ID in alltlobjects.tlobjects:
        _PATCHED = True
        return False

    class ChannelNew(Channel):
        CONSTRUCTOR_ID = CHANNEL_NEW_ID
        SUBCLASS_OF_ID = Channel.SUBCLASS_OF_ID

        @classmethod
        def from_reader(cls, reader):
            flags = reader.read_int()

            _creator = bool(flags & 1)
            _left = bool(flags & 4)
            _broadcast = bool(flags & 32)
            _verified = bool(flags & 128)
            _megagroup = bool(flags & 256)
            _restricted = bool(flags & 512)
            _signatures = bool(flags & 2048)
            _min = bool(flags & 4096)
            _scam = bool(flags & 524288)
            _has_link = bool(flags & 1048576)
            _has_geo = bool(flags & 2097152)
            _slowmode_enabled = bool(flags & 4194304)
            _call_active = bool(flags & 8388608)
            _call_not_empty = bool(flags & 16777216)
            _fake = bool(flags & 33554432)
            _gigagroup = bool(flags & 67108864)
            _noforwards = bool(flags & 134217728)
            _join_to_send = bool(flags & 268435456)
            _join_request = bool(flags & 536870912)
            _forum = bool(flags & 1073741824)
            flags2 = reader.read_int()

            _stories_hidden = bool(flags2 & 2)
            _stories_hidden_min = bool(flags2 & 4)
            _stories_unavailable = bool(flags2 & 8)
            _signature_profiles = bool(flags2 & 4096)
            _autotranslation = bool(flags2 & 32768)
            _broadcast_messages_allowed = bool(flags2 & 65536)
            _monoforum = bool(flags2 & 131072)
            _forum_tabs = bool(flags2 & 524288)
            _id = reader.read_long()
            _access_hash = reader.read_long() if flags & 8192 else None
            _title = reader.tgread_string()
            _username = reader.tgread_string() if flags & 64 else None
            _photo = reader.tgread_object()
            _date = reader.tgread_date()
            if flags & 512:
                reader.read_int()
                _restriction_reason = [reader.tgread_object() for _ in range(reader.read_int())]
            else:
                _restriction_reason = None
            _admin_rights = reader.tgread_object() if flags & 16384 else None
            _banned_rights = reader.tgread_object() if flags & 32768 else None
            _default_banned_rights = reader.tgread_object() if flags & 262144 else None
            _participants_count = reader.read_int() if flags & 131072 else None
            if flags2 & 1:
                reader.read_int()
                _usernames = [reader.tgread_object() for _ in range(reader.read_int())]
            else:
                _usernames = None
            _stories_max_id = reader.tgread_object() if flags2 & 16 else None
            _color = reader.tgread_object() if flags2 & 128 else None
            _profile_color = reader.tgread_object() if flags2 & 256 else None
            _emoji_status = reader.tgread_object() if flags2 & 512 else None
            _level = reader.read_int() if flags2 & 1024 else None
            _subscription_until_date = reader.tgread_date() if flags2 & 2048 else None
            _bot_verification_icon = reader.read_long() if flags2 & 8192 else None
            _send_paid_messages_stars = reader.read_long() if flags2 & 16384 else None
            _linked_monoforum_id = reader.read_long() if flags2 & 262144 else None
            if flags2 & 1048576:
                reader.read_long()  # linked_community_id

            return cls(
                id=_id,
                title=_title,
                photo=_photo,
                date=_date,
                creator=_creator,
                left=_left,
                broadcast=_broadcast,
                verified=_verified,
                megagroup=_megagroup,
                restricted=_restricted,
                signatures=_signatures,
                min=_min,
                scam=_scam,
                has_link=_has_link,
                has_geo=_has_geo,
                slowmode_enabled=_slowmode_enabled,
                call_active=_call_active,
                call_not_empty=_call_not_empty,
                fake=_fake,
                gigagroup=_gigagroup,
                noforwards=_noforwards,
                join_to_send=_join_to_send,
                join_request=_join_request,
                forum=_forum,
                stories_hidden=_stories_hidden,
                stories_hidden_min=_stories_hidden_min,
                stories_unavailable=_stories_unavailable,
                signature_profiles=_signature_profiles,
                autotranslation=_autotranslation,
                broadcast_messages_allowed=_broadcast_messages_allowed,
                monoforum=_monoforum,
                forum_tabs=_forum_tabs,
                access_hash=_access_hash,
                username=_username,
                restriction_reason=_restriction_reason,
                admin_rights=_admin_rights,
                banned_rights=_banned_rights,
                default_banned_rights=_default_banned_rights,
                participants_count=_participants_count,
                usernames=_usernames,
                stories_max_id=_stories_max_id,
                color=_color,
                profile_color=_profile_color,
                emoji_status=_emoji_status,
                level=_level,
                subscription_until_date=_subscription_until_date,
                bot_verification_icon=_bot_verification_icon,
                send_paid_messages_stars=_send_paid_messages_stars,
                linked_monoforum_id=_linked_monoforum_id,
            )

    alltlobjects.tlobjects[CHANNEL_NEW_ID] = ChannelNew
    _PATCHED = True
    return True
