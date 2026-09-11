import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config
from story_refs import load_story_refs

APP_CMD = [sys.executable, str(config.BASE_DIR / "app.py")]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

APP_STARTUP_CHECK_SEC = 2.0
APP_STOP_WAIT_SEC = 20.0
APP_STOP_POLL_SEC = 0.2

_lock_handle = None


def esc(v):
    return html.escape(str(v), quote=True)


def clean_name(name):
    name = os.path.basename(name or "")
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"


def temp_path(suffix=""):
    fd, p = tempfile.mkstemp(prefix="panel_", suffix=suffix, dir=str(config.TMP_DIR))
    os.close(fd)
    return Path(p)


def rm(p):
    try:
        Path(p).unlink(missing_ok=True)
    except Exception:
        pass


def read_tail(path, lines=80):
    path = Path(path)
    if not path.exists():
        return "Лог пуст."
    with path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(4096, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        except Exception:
            f.seek(0)
            data = f.read()
    text = ANSI_RE.sub("", data.decode("utf-8", "ignore"))
    return "\n".join(text.splitlines()[-lines:]) or "Пусто."


def count_sessions():
    return sum(1 for f in config.SESSIONS_DIR.iterdir() if f.is_file() and f.suffix.lower() == ".session")


def single_instance():
    global _lock_handle
    digest = hashlib.sha256(config.BOT_TOKEN.encode()).hexdigest()[:20]
    lock_path = Path(tempfile.gettempdir()) / f"panel_{digest}.lock"
    f = lock_path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            if not f.read(1):
                f.seek(0)
                f.write(" ")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        f.close()
        raise SystemExit("Панель уже запущена с этим ботом.")
    _lock_handle = f


def read_pid():
    if not config.APP_PID.exists():
        return None
    try:
        pid = int(config.APP_PID.read_text().strip())
        return pid if pid > 0 else None
    except Exception:
        rm(config.APP_PID)
        return None


def _read_proc_state(pid):
    """Возвращает state из /proc/<pid>/stat (например, R/S/Z) для Linux."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        parts = stat.split()
        if len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return None


def _is_our_app_process(pid):
    """Проверяет, что PID действительно относится к нашему app.py."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
    except Exception:
        return False

    # На Linux argv в cmdline разделены NUL-байтами.
    cmdline = cmdline.replace("\x00", " ")
    app_path = str(config.BASE_DIR / "app.py")
    return "python" in cmdline.lower() and (app_path in cmdline or " app.py" in cmdline)


def pid_alive(pid):
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
            ).stdout
            return f'"{pid}"' in out and "python" in out.lower()
        os.kill(pid, 0)

        # Зомби имеет PID в таблице, но это уже "мертвый" процесс.
        if _read_proc_state(pid) == "Z":
            return False

        # Защита от устаревшего PID-файла и переиспользованного PID.
        if not _is_our_app_process(pid):
            return False

        return True
    except Exception:
        return False


def app_running():
    pid = read_pid()
    if not pid:
        return False
    if pid_alive(pid):
        return True
    rm(config.APP_PID)
    return False


def wait_pid_exit(pid, timeout=APP_STOP_WAIT_SEC):
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(APP_STOP_POLL_SEC)
    return not pid_alive(pid)


def _open_app_log_for_subprocess():
    config.APP_LOG.parent.mkdir(parents=True, exist_ok=True)
    # stdout/stderr app.py пойдут в app.log (видны ранние ошибки старта).
    return config.APP_LOG.open("ab", buffering=0)


def start_app():
    if app_running():
        return "Уже запущен."

    config.raise_nofile_limit()

    try:
        log_fh = _open_app_log_for_subprocess()
        proc = subprocess.Popen(
            APP_CMD,
            cwd=str(config.BASE_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )

        time.sleep(APP_STARTUP_CHECK_SEC)
        exit_code = proc.poll()
        if exit_code is not None:
            rm(config.APP_PID)
            tail = read_tail(config.APP_LOG, 15)
            return f"app.py завершился сразу после запуска (exit {exit_code})\n<pre>{esc(tail)}</pre>"

        config.APP_PID.write_text(str(proc.pid))
        return None
    except Exception as e:
        return f"Ошибка запуска: {e}"


def stop_app():
    pid = read_pid()
    if not pid:
        return "Не запущен."
    if not pid_alive(pid):
        rm(config.APP_PID)
        return "Не запущен."

    try:
        stopped = False

        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], capture_output=True)
            stopped = not pid_alive(pid)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                stopped = True

            if not stopped:
                stopped = wait_pid_exit(pid)

            if not stopped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    stopped = True

                if not stopped:
                    stopped = wait_pid_exit(pid, timeout=3.0)

        if stopped:
            rm(config.APP_PID)
            return None

        return f"Не удалось остановить PID {pid}"
    except Exception as e:
        return f"Ошибка остановки: {e}"


def read_stats():
    try:
        with config.STATS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fmt_uptime(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    return f"{d}d {h:02}:{m:02}:{sec:02}" if d else f"{h:02}:{m:02}:{sec:02}"


def status_text():
    run = app_running()
    s = read_stats() or {}
    age = int(time.time() - s.get("ts", 0)) if s.get("ts") else None
    fresh = bool(s) and age is not None and age < 120
    stale_note = ""
    if s and not fresh and run:
        stale_note = f"\n<i>⚠ stats {age}с назад</i>" if age is not None else "\n<i>⚠ stats нет</i>"
    g = lambda k: s.get(k) if s else None
    dash = lambda v: v if v is not None else "—"
    up = fmt_uptime(g("uptime_sec")) if g("uptime_sec") is not None else "—"
    log_kb = config.APP_LOG.stat().st_size // 1024 if config.APP_LOG.exists() else 0
    bad = sum(1 for _ in config.BAD_DIR.iterdir()) if config.BAD_DIR.exists() else 0
    stories_g = len(load_story_refs(config.STORIES_GROUPS_FILE))
    stories_c = len(load_story_refs(config.STORIES_CONTACTS_FILE))
    domains = len(config.load_domain_links())
    bots = len(config.load_bot_links())
    return (
        f"🖥 app.py: {'🟢 запущен' if run else '🔴 остановлен'} {('PID ' + str(read_pid())) if run else ''}\n"
        f"⏱ Аптайм: <b>{up}</b>\n"
        f"📩 Всего отправлено: <b>{dash(g('total_sent'))}</b>\n"
        f"📈 В минуту: <b>{dash(g('sent_per_min'))}</b> | ⏳ flood/мин: <b>{dash(g('flood_per_min'))}</b>\n"
        f"🧵 Активных: <b>{dash(g('active_sessions'))}/{dash(g('max_sessions'))}</b> | в очереди: <b>{dash(g('pending'))}</b>\n"
        f"📂 sessions: <b>{count_sessions()}</b>\n"
        f"📖 истории: группы <b>{stories_g}</b> | контакты <b>{stories_c}</b>\n"
        f"🌐 доменов (redirect): <b>{domains}</b> | 🤖 ботов: <b>{bots}</b>\n"
        f"📨 Режим: <b>📖 stories (отдельно группы / контакты)</b>\n"
        f"🛰 Прокси в кулдауне: <b>{dash(g('proxies_in_cooldown'))}</b>/<b>{dash(g('proxies_total'))}</b>\n"
        f"📜 app.log: <b>{log_kb} КБ</b> | 🧹 bad: <b>{bad}</b>"
        + stale_note
    )