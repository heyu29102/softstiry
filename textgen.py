import re

import config

URL_RE = re.compile(r"https?://[^\s\"<>]+")
ANCHOR_RE = re.compile(r"<a\s+href=\"[^\"]*\".*?</a>", re.IGNORECASE | re.DOTALL)
TOKEN_RE = re.compile(r"\[\[\[T(\d+)\]\]\]")

EMOJI_POOL = ["🔥", "✨", "😈", "🥵", "💦", "💋", "⭐", "⚡"]

TEXT_VARIANTS = {
    re.compile(p, re.IGNORECASE): v
    for p, v in {
        r"\bVIDEO\b": ["video", "hot video", "spicy video", "exclusive video", "NSFW video", "crazy video"],
        r"\bPHOTOS\b": ["photos", "pics", "photo set", "photo pack", "hot photos", "exclusive photos", "photo gallery"],
        r"\bPHOTO\b": ["photo", "pic", "spicy photo", "hot shot", "exclusive photo"],
        r"\bGALLERY\b": ["gallery", "photo gallery", "full gallery", "hot gallery"],
        r"\bPRIVATE\b": ["private", "exclusive", "secret", "hidden", "off-record"],
        r"\bLEAKED\b": ["leaked", "exposed", "dropped", "went public"],
        r"\bCLIP\b": ["clip", "short video", "spicy clip", "hot clip"],
        r"\bSET\b": ["set", "pack", "bundle"],
    }.items()
}


def jitter(base, spread, rng, low=0.5):
    if base <= 0:
        return 0
    d = base * spread
    return max(low, rng.uniform(max(low, base - d), base + d))


def jitter_up(base, rng, ratio=0.2, low=1.0):
    if base <= 0:
        return low
    return max(low, base + rng.uniform(0, base * ratio))


def _stash(vault, value):
    vault.append(value)
    return f"[[[T{len(vault) - 1}]]]"


def protect_sensitive(text):
    """
    Скрывает от шумов и замен:
    - HTML-ссылки <a href="...">...</a>
    - {{DOMAIN_LINK}}, {{CHANNEL_LINK}}
    - http(s) URL в тексте
    """
    vault = []

    def stash_match(m):
        return _stash(vault, m.group(0))

    text = ANCHOR_RE.sub(stash_match, text)

    for ph in (config.DOMAIN_LINK_PLACEHOLDER, config.CHANNEL_LINK_PLACEHOLDER):
        while ph in text:
            idx = text.index(ph)
            token = _stash(vault, ph)
            text = text[:idx] + token + text[idx + len(ph):]

    text = URL_RE.sub(stash_match, text)
    return text, vault


def restore_sensitive(text, vault):
    def repl(m):
        i = int(m.group(1))
        return vault[i] if i < len(vault) else m.group(0)

    return TOKEN_RE.sub(repl, text)


def dict_variants(text, rng):
    for pat, variants in TEXT_VARIANTS.items():
        if pat.search(text) and rng.random() < 0.6:
            text = pat.sub(lambda m: rng.choice(variants), text, count=2)
    return text


def _skip_token(text, i):
    end = text.find("]]]", i)
    if end == -1:
        return text[i:], len(text)
    return text[i:end + 3], end + 3


def char_noise(text, rng):
    res, i, n, in_tag = [], 0, len(text), False

    while i < n:
        ch = text[i]

        if not in_tag and ch == "<":
            in_tag = True
            res.append(ch)
            i += 1
            continue

        if in_tag:
            res.append(ch)
            if ch == ">":
                in_tag = False
            i += 1
            continue

        if ch == "&":
            semi = text.find(";", i + 1, i + 10)
            if semi != -1:
                res.append(text[i:semi + 1])
                i = semi + 1
                continue

        if text.startswith("[[[T", i):
            chunk, i = _skip_token(text, i)
            res.append(chunk)
            continue

        if ch in "!?":
            res.append(ch * (1 + rng.randint(1, 2)) if rng.random() < 0.5 else ch)
            i += 1
            continue

        res.append(ch)
        if ch.isalnum() and rng.random() < 0.08:
            res.append("​")
        i += 1

    return "".join(res)


def decorate(text, rng):
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s or len(s) < 25:
            continue
        if "<a " in s.lower() or "[[[T" in s:
            continue
        if rng.random() < 0.35:
            e = rng.choice(EMOJI_POOL)
            mode = rng.randint(0, 2)
            if mode == 0:
                lines[idx] = f"{e} {line}"
            elif mode == 1:
                lines[idx] = f"{line} {e}"
            else:
                lines[idx] = f"{e} {line} {e}"
    return "\n".join(lines)


def unique_text(text, rng):
    if not text:
        return text

    masked, vault = protect_sensitive(text)
    masked = dict_variants(masked, rng)
    masked = decorate(char_noise(masked, rng), rng)
    return restore_sensitive(masked, vault)