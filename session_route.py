"""RU / RB (+375) / KZ (+7) — перенаправление сессий в отдельную папку при импорте."""

import re
import sqlite3
import tempfile
from pathlib import Path

_DIGITS_RE = re.compile(r"\d+")


def digits_from_text(text: str) -> str:
    if not text:
        return ""
    parts = _DIGITS_RE.findall(text)
    if not parts:
        return ""
    return max(parts, key=len)


def normalize_phone_digits(digits: str) -> str:
    digits = (digits or "").strip()
    if not digits:
        return ""
    # 8XXXXXXXXXX (русский trunk без +) → 7XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits


def is_cis_phone_digits(digits: str) -> bool:
    """Беларусь 375, Россия и Казахстан +7."""
    digits = normalize_phone_digits(digits)
    if not digits:
        return False
    if digits.startswith("375") and len(digits) >= 11:
        return True
    if digits.startswith("7") and len(digits) >= 10:
        return True
    return False


def phone_from_session_bytes(data: bytes) -> str:
    """Пытается вытащить номер из sqlite .session (если в имени файла нет)."""
    if not data or len(data) < 16:
        return ""
    if data[:16] != b"SQLite format 3\x00":
        return ""
    fd, path = tempfile.mkstemp(prefix=".session.", suffix=".peek")
    tmp = Path(path)
    try:
        with open(fd, "wb") as f:
            f.write(data)
        conn = sqlite3.connect(str(tmp))
        try:
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                table = row[0]
                try:
                    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
                except sqlite3.Error:
                    continue
                for col in cols:
                    if col.lower() not in ("phone", "username", "name"):
                        continue
                    try:
                        for val in conn.execute(f"SELECT {col} FROM {table} LIMIT 20"):
                            d = digits_from_text(str(val[0] or ""))
                            if is_cis_phone_digits(d):
                                return d
                    except sqlite3.Error:
                        continue
        finally:
            conn.close()
    except Exception:
        return ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return ""


def is_cis_session(base_name: str, data: bytes, phone_hint: str = "") -> bool:
    base = Path(base_name).stem
    for candidate in (digits_from_text(base), digits_from_text(phone_hint), phone_from_session_bytes(data)):
        if is_cis_phone_digits(candidate):
            return True
    return False


def should_route_to_peer(base_name: str, data: bytes, phone_hint: str, role: str) -> bool:
    """
    main / intl: RU/RB/KZ → peer (RU-папка).
    ru: не CIS → peer (основной sessions/).
    """
    cis = is_cis_session(base_name, data, phone_hint)
    role = (role or "main").strip().lower()
    if role in ("ru", "cis", "rushki"):
        return not cis
    return cis
