"""Пул текстов, фото и ссылок для рассылки (t.me, share.google и т.д.)."""

from __future__ import annotations

import random
from pathlib import Path

import config
from textgen import unique_text

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def load_photo_paths(photo_dir: Path | None = None) -> list[Path]:
    photo_dir = Path(photo_dir or config.PHOTO_DIR)
    if not photo_dir.exists():
        return []
    paths: list[Path] = []
    for entry in photo_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in PHOTO_EXTENSIONS:
            paths.append(entry)
    paths.sort(key=lambda p: p.name.lower())
    return paths


def load_mailing_content():
    blocks = config.load_blocks()
    photos = load_photo_paths()
    share_links = config.load_share_links()
    use_links = config.texts_use_domain_placeholder(blocks)
    return blocks, photos, share_links, use_links


def pick_share_link(share_links: list[str], rng: random.Random) -> str:
    if not share_links:
        return ""
    return rng.choice(share_links)


def render_caption(block: str, rng: random.Random, share_links: list[str], use_link_ph: bool) -> str:
    text = (block or "").strip()
    if not text:
        return ""
    if use_link_ph and config.DOMAIN_LINK_PLACEHOLDER in text:
        link = pick_share_link(share_links, rng)
        if link:
            text = text.replace(config.DOMAIN_LINK_PLACEHOLDER, link)
    return unique_text(text, rng)


def pick_photo(photos: list[Path], rng: random.Random) -> Path | None:
    if not photos:
        return None
    return rng.choice(photos)


def choose_delivery_mode(
    rng: random.Random,
    *,
    text_only_group: bool,
    has_photos: bool,
) -> str:
    """Возвращает 'photo' или 'text'."""
    if text_only_group or not has_photos:
        return "text"
    if rng.random() < config.PHOTO_SEND_RATIO:
        return "photo"
    return "text"
