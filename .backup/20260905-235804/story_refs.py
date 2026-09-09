"""Парсинг строк stories.txt — одна история на строку."""

import re
from dataclasses import dataclass
from pathlib import Path

STORY_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t\.me/(?P<peer>[^/\s]+)/s/(?P<id>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StoryRef:
    peer: str
    story_id: int

    @property
    def label(self) -> str:
        return f"{self.peer}/s/{self.story_id}"


def normalize_story_input(text: str) -> str:
    """Вытащить ссылку/строку из сообщения (в т.ч. с пробелами и переносами)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    m = STORY_URL_RE.search(raw)
    if m:
        return f"https://t.me/{m.group('peer').lstrip('@')}/s/{m.group('id')}"
    return raw.split()[0] if raw.split() else raw


def parse_story_line(line: str) -> StoryRef | None:
    line = normalize_story_input(line)
    if not line or line.startswith("#"):
        return None

    m = STORY_URL_RE.search(line)
    if m:
        return StoryRef(peer=m.group("peer").lstrip("@"), story_id=int(m.group("id")))

    if "|" in line:
        peer, sid = line.split("|", 1)
        peer = peer.strip().lstrip("@")
        sid = sid.strip()
        if peer and sid.isdigit():
            return StoryRef(peer=peer, story_id=int(sid))

    parts = line.split()
    if len(parts) == 2 and parts[1].isdigit():
        return StoryRef(peer=parts[0].lstrip("@"), story_id=int(parts[1]))

    return None


def load_story_refs(path: Path) -> list[StoryRef]:
    path = Path(path)
    if not path.exists():
        return []
    refs: list[StoryRef] = []
    seen: set[tuple[str, int]] = set()
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            ref = parse_story_line(raw)
            if not ref:
                continue
            key = (ref.peer.lower(), ref.story_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


def stories_fingerprint(refs: list[StoryRef]) -> tuple:
    return tuple(sorted((r.peer.lower(), r.story_id) for r in refs))


def format_story_line(ref: StoryRef) -> str:
    return f"{ref.peer}|{ref.story_id}"


def save_story_refs(refs: list[StoryRef], path: Path) -> None:
    from config import atomic_write

    lines = [format_story_line(r) for r in refs]
    body = "\n".join(lines)
    if body:
        body += "\n"
    atomic_write(path, body.encode("utf-8"))


def add_story_ref(text: str, path: Path) -> tuple[str, int, str]:
    """Только добавление. Возвращает (status, total, label). status: added|exists|invalid."""
    ref = parse_story_line(text)
    if not ref:
        return "invalid", len(load_story_refs(path)), ""
    refs = load_story_refs(path)
    key = (ref.peer.lower(), ref.story_id)
    if any((r.peer.lower(), r.story_id) == key for r in refs):
        return "exists", len(refs), ref.label
    refs.append(ref)
    save_story_refs(refs, path)
    return "added", len(refs), ref.label


def remove_story_ref(text: str, path: Path) -> tuple[str, int, str]:
    """Только удаление. status: removed|missing|invalid."""
    ref = parse_story_line(text)
    if not ref:
        return "invalid", len(load_story_refs(path)), ""
    refs = load_story_refs(path)
    key = (ref.peer.lower(), ref.story_id)
    for i, r in enumerate(refs):
        if (r.peer.lower(), r.story_id) == key:
            refs.pop(i)
            save_story_refs(refs, path)
            return "removed", len(refs), ref.label
    return "missing", len(refs), ref.label


def toggle_story_line(text: str, path: Path | None = None) -> tuple[str | None, int, str]:
    """Совместимость: toggle add/remove."""
    path = Path(path) if path else None
    from config import STORIES_FILE

    path = path or STORIES_FILE
    ref = parse_story_line(text)
    if not ref:
        return None, len(load_story_refs(path)), ""
    refs = load_story_refs(path)
    key = (ref.peer.lower(), ref.story_id)
    for i, r in enumerate(refs):
        if (r.peer.lower(), r.story_id) == key:
            refs.pop(i)
            save_story_refs(refs, path)
            return "removed", len(refs), ref.label
    refs.append(ref)
    save_story_refs(refs, path)
    return "added", len(refs), ref.label
