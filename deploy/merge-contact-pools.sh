#!/bin/bash
# Перенос доменов/ботов из contact-пулов в основные (groups) + nginx + redirect
# Запуск: bash /opt/new-soft/deploy/merge-contact-pools.sh

set -euo pipefail

DIR="${1:-/opt/new-soft}"
cd "$DIR"

python3 <<'PY'
from pathlib import Path
import config
from domain_setup import line_host, sync_nginx_redirect

def read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def merge_domains(target: Path, source: Path) -> tuple[int, int]:
    target_lines = read_lines(target)
    source_lines = read_lines(source)
    hosts = set()
    for ln in target_lines:
        h = line_host(ln)
        if h:
            hosts.add(h)
    added = 0
    for ln in source_lines:
        h = line_host(ln)
        if not h or h in hosts:
            continue
        target_lines.append(ln)
        hosts.add(h)
        added += 1
    body = "\n".join(target_lines)
    if body:
        body += "\n"
    config.atomic_write(target, body.encode("utf-8"))
    return added, len(target_lines)


def bot_link(line: str) -> str:
    if "|" not in line:
        return line.strip()
    return line.split("|", 1)[1].strip()


def merge_bots(target: Path, source: Path) -> tuple[int, int]:
    target_lines = read_lines(target)
    source_lines = read_lines(source)
    links = {bot_link(ln) for ln in target_lines if "|" in ln}
    added = 0
    for ln in source_lines:
        if "|" not in ln:
            continue
        link = bot_link(ln)
        if not link or link in links:
            continue
        target_lines.append(ln)
        links.add(link)
        added += 1
    body = "\n".join(target_lines)
    if body:
        body += "\n"
    config.atomic_write(target, body.encode("utf-8"))
    return added, len(target_lines)


da, dt = merge_domains(config.DOMAINS_FILE, config.DOMAINS_CONTACTS_FILE)
ba, bt = merge_bots(config.BOTS_FILE, config.BOTS_CONTACTS_FILE)
print(f"domains.txt: +{da} (всего {dt})")
print(f"bots.txt: +{ba} (всего {bt})")

ok, msg = sync_nginx_redirect()
print(f"nginx: {msg}" if ok else f"nginx WARN: {msg}")
PY

echo "=== Перезапуск redirect.py ==="
pkill -f "python3.*redirect.py" 2>/dev/null || true
sleep 2
nohup python3 "$DIR/redirect.py" >> "$DIR/redirect.log" 2>&1 &
sleep 2
tail -8 "$DIR/redirect.log" 2>/dev/null || true

echo ""
echo "=== Проверка (подставь свой контактный домен) ==="
DOMAIN=$(grep -oE 'https?://[^[:space:]]+' domains_contacts.txt 2>/dev/null | head -1 | sed -E 's|https?://||;s|/.*||' || true)
if [ -n "${DOMAIN:-}" ]; then
  echo "curl -sI -H Host: $DOMAIN http://127.0.0.1:8090/hot | grep -i location"
  curl -sI -H "Host: $DOMAIN" "http://127.0.0.1:8090/hot" | grep -i location || true
fi

echo ""
echo "Готово. domains_contacts/bots_contacts можно оставить или очистить."
