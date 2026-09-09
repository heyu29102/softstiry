#!/bin/bash
# Очистка мёртвых сессий из /opt/new-soft/sessions/ (удаление навсегда)
# Перед запуском лучше остановить app.py (рассылку).
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
cd "$ROOT"

if [[ -f app.pid ]] && kill -0 "$(cat app.pid)" 2>/dev/null; then
  echo "⚠️  app.py запущен (pid $(cat app.pid)). Лучше остановить рассылку перед очисткой."
  echo "    Продолжить? Ctrl+C для отмены, Enter — продолжить"
  read -r
fi

echo "=== cleanup_sessions.py ==="
python3 cleanup_sessions.py --workers "${WORKERS:-80}" "$@"
