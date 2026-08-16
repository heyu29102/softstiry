import asyncio
import os
import tempfile

import config
from telethon_patch import apply_telethon_patch

apply_telethon_patch()
from sender import Spammer

_lock = None


def lock_instance():
    f = open(os.path.join(tempfile.gettempdir(), "spammer_app.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            if not f.read(1):
                f.write(" ")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        f.close()
        raise SystemExit("app.py уже запущен")
    return f


def main():
    global _lock
    if os.name == "nt":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    _lock = lock_instance()
    try:
        config.APP_PID.write_text(str(os.getpid()))
    except Exception:
        pass
    try:
        asyncio.run(Spammer().main())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            config.APP_PID.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
