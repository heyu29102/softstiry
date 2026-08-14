import asyncio
import hashlib
import os
import zipfile
from pathlib import Path

import config
from panel_control import clean_name, rm
from session_route import should_route_to_peer

try:
    import rarfile
    RAR_OK = True
except Exception:
    rarfile = None
    RAR_OK = False

import_lock = asyncio.Lock()
hash_cache = {}


def _route_enabled():
    return config.SESSION_ROUTE_ENABLED and config.SESSIONS_PEER_DIR is not None


def _target_dir(base: str, data: bytes, phone_hint: str) -> Path:
    if _route_enabled() and should_route_to_peer(base, data, phone_hint, config.SESSION_ROUTE_ROLE):
        return config.SESSIONS_PEER_DIR
    return config.SESSIONS_DIR


def import_sync(fname, tmp, is_rar, phone_hint=""):
    res = {
        "files": 0,
        "added": 0,
        "updated": 0,
        "same": 0,
        "routed": 0,
        "routed_added": 0,
        "routed_updated": 0,
    }

    def write(base, data):
        base = clean_name(base)
        if not base.lower().endswith(".session"):
            return
        res["files"] += 1
        dest_dir = _target_dir(base, data, phone_hint)
        if dest_dir != config.SESSIONS_DIR:
            res["routed"] += 1
        target = dest_dir / base
        sig = (len(data), hashlib.sha256(data).digest())
        cache_key = (str(dest_dir), base)
        if not target.exists():
            config.atomic_write(target, data)
            hash_cache[cache_key] = sig
            res["added"] += 1
            if dest_dir != config.SESSIONS_DIR:
                res["routed_added"] += 1
            return
        old = hash_cache.get(cache_key)
        if old is None:
            try:
                d = target.read_bytes()
                old = (len(d), hashlib.sha256(d).digest())
                hash_cache[cache_key] = old
            except Exception:
                old = None
        if old == sig:
            res["same"] += 1
            return
        config.atomic_write(target, data)
        hash_cache[cache_key] = sig
        res["updated"] += 1
        if dest_dir != config.SESSIONS_DIR:
            res["routed_updated"] += 1

    low = fname.lower()
    if low.endswith(".session"):
        write(fname, tmp.read_bytes())
    elif low.endswith(".zip") and not is_rar:
        with zipfile.ZipFile(tmp) as z:
            for m in z.infolist():
                b = os.path.basename(m.filename)
                if b.lower().endswith(".session"):
                    with z.open(m) as src:
                        write(b, src.read())
    elif low.endswith(".rar") and is_rar:
        if not RAR_OK:
            raise RuntimeError("rarfile не установлен")
        with rarfile.RarFile(tmp) as rf:
            for m in rf.infolist():
                b = os.path.basename(m.filename)
                if b.lower().endswith(".session"):
                    with rf.open(m) as src:
                        write(b, src.read())
    else:
        raise ValueError("Только .session / .zip / .rar")
    return res


async def import_package(fname, tmp, is_rar=False, phone_hint=""):
    try:
        async with import_lock:
            return await asyncio.to_thread(import_sync, fname, Path(tmp), is_rar, phone_hint)
    finally:
        rm(tmp)


def archive_has_session(path, is_rar=False):
    try:
        opener = rarfile.RarFile if is_rar else zipfile.ZipFile
        with opener(path) as a:
            for m in a.infolist():
                if os.path.basename(m.filename).lower().endswith(".session"):
                    return True
    except Exception:
        pass
    return False


def texts_from_upload(fname, tmp, is_rar=False):
    tmp = Path(tmp)
    if fname.lower().endswith(".txt"):
        try:
            return tmp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    parts = []
    try:
        opener = rarfile.RarFile if is_rar else zipfile.ZipFile
        with opener(tmp) as a:
            for m in a.infolist():
                if os.path.basename(m.filename).lower().endswith(".txt"):
                    with a.open(m) as src:
                        parts.append(src.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    return "\n---\n".join(parts) if parts else None
