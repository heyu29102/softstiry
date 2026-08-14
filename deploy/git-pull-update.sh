#!/bin/bash
# Безопасный pull на сервере: stash локальных правок → pull → restart
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
BRANCH="${2:-cursor/domain-pool-mailing-9c6d}"

cd "$ROOT"

echo "=== git status (before) ==="
git status -sb

DIRTY=$(git status --porcelain | grep -E '^( M| M|M |MM|AM|A )' || true)
if [[ -n "$DIRTY" ]]; then
  STASH_NAME="server-local-$(date +%Y%m%d-%H%M%S)"
  echo ""
  echo "Локальные изменения — stash: $STASH_NAME"
  git stash push -u -m "$STASH_NAME"
  echo "Список stash (если нужно вернуть): git stash list"
fi

echo ""
echo "=== git pull ==="
git fetch origin "$BRANCH"
git pull origin "$BRANCH"

echo ""
echo "=== restart services ==="
systemctl restart redirect.service || true
systemctl restart panel.service || true

echo ""
echo "=== done ==="
echo "Проверка redirect:"
curl -sI --max-time 5 http://127.0.0.1:8090/ | head -5 || true
echo ""
echo "Рассылку (app.py) перезапусти в панели, если была запущена."
