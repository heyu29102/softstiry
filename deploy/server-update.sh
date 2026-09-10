#!/bin/bash
# Полное обновление /opt/new-soft с git (НЕ отдельные файлы — иначе exit 1).
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
BRANCH="${2:-cursor/min-members-story-rr-c290}"
BACKUP="/tmp/new-soft-backup-$(date +%F-%H%M%S)"

echo "=== backup data ==="
mkdir -p "$BACKUP"
for f in stories.txt stories_contacts.txt stories_groups.txt bots.txt domains.txt channels.txt proxy.txt .env text.txt share_links.txt; do
  [[ -f "$ROOT/$f" ]] && cp -a "$ROOT/$f" "$BACKUP/"
done
echo "Бэкап: $BACKUP"
ls -la "$BACKUP"

cd "$ROOT"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ОШИБКА: $ROOT не git-репозиторий."
  echo "Клонируй: git clone -b $BRANCH git@github.com:heyu29102/AktualSOFT-v2.git $ROOT"
  exit 1
fi

echo ""
echo "=== git fetch + reset ==="
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"

echo ""
echo "=== restore data ==="
for f in stories.txt stories_contacts.txt stories_groups.txt bots.txt domains.txt channels.txt proxy.txt .env text.txt share_links.txt; do
  [[ -f "$BACKUP/$f" ]] && cp -a "$BACKUP/$f" "$ROOT/"
done

# Рекомендуемые флаги ghost-fix (не перетирают существующие строки)
grep -q '^STORY_CONFIRM_IN_CHAT=' "$ROOT/.env" 2>/dev/null || echo 'STORY_CONFIRM_IN_CHAT=1' >> "$ROOT/.env"
grep -q '^STORY_JOIN_CHANNEL=' "$ROOT/.env" 2>/dev/null || echo 'STORY_JOIN_CHANNEL=1' >> "$ROOT/.env"
grep -q '^LOG_SUCCESS_GROUPS=' "$ROOT/.env" 2>/dev/null || echo 'LOG_SUCCESS_GROUPS=1' >> "$ROOT/.env"

echo ""
echo "=== import test ==="
if ! python3 -c "import config; from sender import Spammer; print('OK import')" 2>&1; then
  echo ""
  echo "ОШИБКА импорта. Последние строки app.log:"
  tail -30 "$ROOT/app.log" 2>/dev/null || true
  exit 1
fi

echo ""
echo "=== done ==="
echo "Ветка: $BRANCH"
git log -1 --oneline
echo ""
echo "Перезапусти app.py через панель (не nohup)."
echo "Лайв группы: tail -f $ROOT/app.log | grep --line-buffered -a -E 'group-hit @|пустая отправка|не найден в чате'"
