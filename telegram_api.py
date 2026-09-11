"""Правильные api_id/api_hash для сессии — из .json рядом или из .env."""

import json
import os
from pathlib import Path

from opentele.api import API

import config


def _parse_credentials_string(raw: str) -> tuple[int, str] | None:
    raw = (raw or "").strip()
    if not raw or ":" not in raw:
        return None
    left, right = raw.split(":", 1)
    left, right = left.strip(), right.strip()
    if left.isdigit() and right:
        return int(left), right
    return None


def _load_session_meta(session_path: str) -> dict:
    base = os.path.splitext(session_path)[0]
    for candidate in (f"{base}.json", f"{session_path}.json"):
        p = Path(candidate)
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _credentials_from_meta(meta: dict) -> tuple[int, str] | None:
    api_id = meta.get("app_id") or meta.get("api_id")
    api_hash = meta.get("app_hash") or meta.get("api_hash")
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        return None
    api_hash = str(api_hash or "").strip()
    if api_id > 0 and api_hash:
        return api_id, api_hash
    return None


def _credentials_from_env() -> tuple[int, str] | None:
    if config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH:
        return int(config.TELEGRAM_API_ID), str(config.TELEGRAM_API_HASH)
    parsed = _parse_credentials_string(config.TELEGRAM_API_CREDENTIALS)
    if parsed:
        return parsed
    return None


def client_api(session_id: str, session_path: str | None = None):
    """
    API-профиль для opentele TelegramClient.
    Приоритет: <session>.json → TELEGRAM_API_ID/HASH в .env → Generate (последний resort).
    """
    creds = None
    meta: dict = {}
    if session_path:
        meta = _load_session_meta(session_path)
        creds = _credentials_from_meta(meta)
    if creds is None:
        creds = _credentials_from_env()

    api = API.TelegramDesktop.Generate(unique_id=session_id)
    if creds:
        api.api_id, api.api_hash = creds[0], creds[1]
        for key, attr in (
            ("device", "device_model"),
            ("sdk", "system_version"),
            ("app_version", "app_version"),
            ("lang_pack", "lang_code"),
            ("system_lang_pack", "system_lang_code"),
        ):
            val = meta.get(key)
            if val and hasattr(api, attr):
                setattr(api, attr, str(val))
    return api
