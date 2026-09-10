"""API-пара: из sidecar .json если есть, иначе TelegramDesktop.Generate как раньше."""

import json
import logging
from pathlib import Path

from opentele.api import API, APIData

log = logging.getLogger("spam")

_JSON_KEYS_ID = ("app_id", "api_id", "ApiId", "apiId")
_JSON_KEYS_HASH = ("app_hash", "api_hash", "ApiHash", "apiHash")


def json_path_for_session(session_path: str | Path) -> Path:
    p = Path(session_path)
    if p.suffix == ".session":
        return p.with_suffix(".json")
    return Path(f"{p}.json")


def load_session_json(session_path: str | Path) -> dict | None:
    jp = json_path_for_session(session_path)
    if not jp.is_file():
        return None
    try:
        with jp.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning(f"{jp.name}: не читается json: {e}")
        return None


def _first(data: dict, keys: tuple[str, ...]):
    for key in keys:
        val = data.get(key)
        if val not in (None, ""):
            return val
    return None


def _default_lang_pack(api_id: int) -> str:
    if api_id == 2040:
        return "tdesktop"
    if api_id in (6, 21724):
        return "android"
    if api_id == 10840:
        return "ios"
    if api_id == 2834:
        return "macos"
    return ""


def api_from_json(data: dict) -> APIData:
    api_id = _first(data, _JSON_KEYS_ID)
    api_hash = _first(data, _JSON_KEYS_HASH)
    if api_id in (None, "") or api_hash in (None, ""):
        raise ValueError("json без app_id/api_id или app_hash/api_hash")

    device = _first(data, ("device", "device_model", "Device", "user_agent")) or "Desktop"
    sdk = _first(data, ("sdk", "system_version", "SystemVersion", "system_ver")) or "Windows 10"
    app_version = _first(data, ("app_version", "AppVersion")) or "3.4.3 x64"
    lang_code = _first(data, ("lang_code", "LangCode")) or "en"
    system_lang_code = _first(
        data,
        ("system_lang_code", "system_lang_pack", "SystemLangCode", "system_lang"),
    ) or "en-US"
    lang_pack = _first(data, ("lang_pack", "LangPack")) or _default_lang_pack(int(api_id))

    return APIData(
        api_id=int(api_id),
        api_hash=str(api_hash),
        device_model=str(device),
        system_version=str(sdk),
        app_version=str(app_version),
        lang_code=str(lang_code),
        system_lang_code=str(system_lang_code),
        lang_pack=str(lang_pack),
    )


def client_api(session_path: str | Path) -> APIData:
    """JSON рядом с .session → APIData из него; иначе TelegramDesktop.Generate."""
    sid = Path(session_path).stem
    data = load_session_json(session_path)
    if data:
        try:
            return api_from_json(data)
        except ValueError as e:
            log.warning(f"{sid}: битый json, fallback Generate: {e}")
    return API.TelegramDesktop.Generate(unique_id=sid)
