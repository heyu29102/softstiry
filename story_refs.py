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

    @property
    def url(self) -> str:
        return f"https://t.me/{self.peer}/s/{self.story_id}"


def parse_story_line(line: str) -> StoryRef | None:
    line = (line or "").strip()
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


def format_story_line(ref: StoryRef) -> str:
    return ref.url


def save_story_refs(refs: list[StoryRef], path: Path) -> None:
    from config import atomic_write

    lines = [format_story_line(r) for r in refs]
    body = "\n".join(lines)
    if body:
        body += "\n"
    atomic_write(path, body.encode("utf-8"))


def rewrite_story_file_as_urls(path: Path) -> int:
    """Перезаписать stories.txt в формате https://t.me/peer/s/id (миграция старых peer|id)."""
    refs = load_story_refs(path)
    if not refs:
        return 0
    save_story_refs(refs, path)
    return len(refs)
