"""Патч TL-схемы Telethon под актуальные ответы Telegram API.

PyPI Telethon 1.44 отстаёт от сервера: новые constructor ID (channel, user, webPage…)
и новые типы (community) вызывают TypeNotFoundError при get_entity / get_dialogs / превью.
"""

from __future__ import annotations

import logging

log = logging.getLogger("spam")

_PATCHED = False
_APPLIED: list[str] = []

# Критичные для рассылки / диалогов / превью
CHANNEL_NEW_ID = 0xD49F34C6
USER_NEW_ID = 0xB1B8CC83
COMMUNITY_ID = 0x65EFE954
COMMUNITY_FORBIDDEN_ID = 0xFD3CDAB8

# Только сменился constructor ID, поля те же — достаточно alias
_ALIAS_IDS: dict[str, int] = {
    "WebPage": 0xE89C45B2,
    "PeerStories": 0x9A35E999,
    "StoryViews": 0x8D595CD6,
    "ChatFull": 0x2633421B,
    "Photo": 0xFB197A65,
    "PeerSettings": 0xF47741F7,
    "MessageViews": 0x455B853D,
    "GroupCall": 0xEFB2B617,
    "PhoneCall": 0x30535AF5,
    "StickerSet": 0x2DD14EDC,
    "BotInfo": 0x4D8A0299,
    "BotApp": 0x95FCD1D6,
    "AutoDownloadSettings": 0xBAA57628,
    "AutoSaveSettings": 0xC84834CE,
    "ExportedChatlistInvite": 0x0C5181AC,
    "StarGiftAuctionState": 0x771A4E66,
}

_KNOWN_SCHEMA_IDS = {
    CHANNEL_NEW_ID: "channel",
    USER_NEW_ID: "user",
    COMMUNITY_ID: "community",
    COMMUNITY_FORBIDDEN_ID: "communityForbidden",
}


def is_tl_schema_error(exc: BaseException) -> bool:
    if isinstance(exc, TypeNotFoundError):
        return True
    return "Constructor ID" in str(exc)


def schema_error_label(exc: BaseException) -> str:
    if isinstance(exc, TypeNotFoundError):
        cid = getattr(exc, "invalid_constructor_id", 0)
        name = _KNOWN_SCHEMA_IDS.get(cid)
        if name:
            return f"TL schema ({name} {cid:#010x})"
        return f"TL schema mismatch ({cid:#010x})"
    return str(exc)


def applied_patches() -> list[str]:
    return list(_APPLIED)


def _register_alias(registry: dict, base_cls, new_id: int, label: str) -> bool:
    if new_id in registry:
        return False
    alias = type(
        f"{base_cls.__name__}Alias{new_id:08x}",
        (base_cls,),
        {"CONSTRUCTOR_ID": new_id, "SUBCLASS_OF_ID": base_cls.SUBCLASS_OF_ID},
    )
    registry[new_id] = alias
    _APPLIED.append(label)
    return True


def _register_channel_new(registry: dict, Channel) -> bool:
    if CHANNEL_NEW_ID in registry:
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

    registry[CHANNEL_NEW_ID] = ChannelNew
    _APPLIED.append(f"channel:{CHANNEL_NEW_ID:#010x}")
    return True


def _register_user_new(registry: dict, User) -> bool:
    if USER_NEW_ID in registry:
        return False

    class UserNew(User):
        CONSTRUCTOR_ID = USER_NEW_ID
        SUBCLASS_OF_ID = User.SUBCLASS_OF_ID

        @classmethod
        def from_reader(cls, reader):
            flags2_holder: list[int | None] = [None]
            count = [0]
            orig_read_int = reader.read_int

            def patched_read_int(signed=True):
                value = orig_read_int(signed)
                count[0] += 1
                if count[0] == 2:
                    flags2_holder[0] = value
                return value

            reader.read_int = patched_read_int  # type: ignore[method-assign]
            try:
                obj = User.from_reader(reader)
            finally:
                reader.read_int = orig_read_int  # type: ignore[method-assign]

            if flags2_holder[0] is not None and flags2_holder[0] & 2097152:
                reader.read_long()  # linked_community_id
            return obj

    registry[USER_NEW_ID] = UserNew
    _APPLIED.append(f"user:{USER_NEW_ID:#010x}")
    return True


def _register_community_types(registry: dict, Chat) -> bool:
    added = False

    if COMMUNITY_ID not in registry:

        class Community(Chat):
            CONSTRUCTOR_ID = COMMUNITY_ID
            SUBCLASS_OF_ID = Chat.SUBCLASS_OF_ID

            @classmethod
            def from_reader(cls, reader):
                flags = reader.read_int()
                _creator = bool(flags & 1)
                _left = bool(flags & 4)
                _min = bool(flags & 4096)
                flags2 = reader.read_int()
                _collapsed = bool(flags2 & 1048576)
                _id = reader.read_long()
                _access_hash = reader.read_long() if flags & 8192 else None
                _title = reader.tgread_string()
                _photo = reader.tgread_object()
                _date = reader.tgread_date()
                _admin_rights = reader.tgread_object() if flags & 16384 else None
                _default_banned_rights = reader.tgread_object() if flags & 262144 else None
                return cls(
                    id=_id,
                    title=_title,
                    photo=_photo,
                    participants_count=0,
                    date=_date,
                    version=0,
                    creator=_creator,
                    left=_left,
                    deactivated=None,
                    call_active=None,
                    call_not_empty=None,
                    noforwards=None,
                    migrated_to=None,
                    admin_rights=_admin_rights,
                    default_banned_rights=_default_banned_rights,
                )

        registry[COMMUNITY_ID] = Community
        _APPLIED.append(f"community:{COMMUNITY_ID:#010x}")
        added = True

    if COMMUNITY_FORBIDDEN_ID not in registry:

        class CommunityForbidden(Chat):
            CONSTRUCTOR_ID = COMMUNITY_FORBIDDEN_ID
            SUBCLASS_OF_ID = Chat.SUBCLASS_OF_ID

            @classmethod
            def from_reader(cls, reader):
                flags = reader.read_int()
                _id = reader.read_long()
                _access_hash = reader.read_long() if flags & 8192 else None
                _title = reader.tgread_string()
                return cls(
                    id=_id,
                    title=_title,
                    photo=None,
                    participants_count=0,
                    date=None,
                    version=0,
                    creator=None,
                    left=None,
                    deactivated=None,
                    call_active=None,
                    call_not_empty=None,
                    noforwards=None,
                    migrated_to=None,
                    admin_rights=None,
                    default_banned_rights=None,
                )

        registry[COMMUNITY_FORBIDDEN_ID] = CommunityForbidden
        _APPLIED.append(f"communityForbidden:{COMMUNITY_FORBIDDEN_ID:#010x}")
        added = True

    return added


def apply_telethon_patch() -> bool:
    """Идемпотентно регистрирует недостающие TL-конструкторы. Возвращает True если что-то добавили."""
    global _PATCHED
    if _PATCHED:
        return False

    from telethon.errors import TypeNotFoundError
    from telethon.tl import alltlobjects
    from telethon.tl.types import Channel, Chat, User

    registry = alltlobjects.tlobjects
    before = len(_APPLIED)

    _register_channel_new(registry, Channel)
    _register_user_new(registry, User)
    _register_community_types(registry, Chat)

    for cls_name, new_id in _ALIAS_IDS.items():
        base = getattr(__import__("telethon.tl.types", fromlist=[cls_name]), cls_name, None)
        if base is None:
            continue
        if getattr(base, "CONSTRUCTOR_ID", None) == new_id:
            continue
        _register_alias(registry, base, new_id, f"{cls_name}:{new_id:#010x}")

    _PATCHED = True
    added = len(_APPLIED) - before
    if added:
        log.info(f"🩹 Telethon TL patch: +{added} ({', '.join(_APPLIED[before:])})")
    return added > 0
