#!/bin/bash
# Патч sender.py + config.py с ветки ghost-fix (приватный репо — curl raw не работает).
set -euo pipefail
cd /opt/new-soft

BRANCH="cursor/min-members-story-rr-c290"
BACKUP="/tmp/bak-$(date +%F-%H%M)"
mkdir -p "$BACKUP"
cp -a sender.py config.py .env "$BACKUP/" 2>/dev/null || true
echo "Бэкап: $BACKUP"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Ошибка: /opt/new-soft не git-репозиторий"
  exit 1
fi

git fetch origin "$BRANCH"
git checkout "origin/$BRANCH" -- sender.py config.py

grep -q '^STORY_CONFIRM_IN_CHAT=' .env 2>/dev/null || echo 'STORY_CONFIRM_IN_CHAT=1' >> .env

echo "Готово. Перезапусти app через панель."
echo "Лайв: tail -f app.log | grep --line-buffered -a -E 'group-hit @|пустая отправка|не найден в чате'"
