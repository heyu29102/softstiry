"""Пул текстов, фото и доменов для рассылки."""

from __future__ import annotations

import random
from pathlib import Path
from urllib.parse import urlparse, urlunparse

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
    domains_groups = config.load_domain_links(config.DOMAINS_FILE)
    domains_contacts = config.load_domain_links(config.DOMAINS_CONTACTS_FILE)
    use_domains = config.texts_use_domain_placeholder(blocks)
    return blocks, photos, domains_groups, domains_contacts, use_domains


def domain_base(url: str) -> str:
    """Базовый URL без path/query — для подстановки своего /слово."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    if not parsed.netloc:
        return raw.rstrip("/")
    return urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", "")).rstrip("/")


def pick_link_path(rng: random.Random) -> str:
    words = config.link_path_words()
    word = rng.choice(words)
    if rng.random() < 0.2:
        word = f"{word}-{rng.randint(2, 99)}"
    return word.strip("/").lower()


def build_domain_link(base_url: str, rng: random.Random) -> str:
    base = domain_base(base_url)
    if not base:
        return ""
    if not config.LINK_RANDOM_PATH:
        return base
    return f"{base}/{pick_link_path(rng)}"


def pick_domain_link(domain_links: list[str], rng: random.Random) -> str:
    if not domain_links:
        return ""
    return build_domain_link(rng.choice(domain_links), rng)


def render_caption(block: str, rng: random.Random, domain_links: list[str], use_domain_ph: bool) -> str:
    text = (block or "").strip()
    if not text:
        return ""
    if use_domain_ph and config.DOMAIN_LINK_PLACEHOLDER in text:
        link = pick_domain_link(domain_links, rng)
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
