#!/bin/bash
# Деплой priority-queue rewrite на /opt/new-soft
set -euo pipefail
ROOT="${1:-/opt/new-soft}"
SRC="${2:-$(cd "$(dirname "$0")/.." && pwd)}"

echo "=== Priority queue deploy → $ROOT ==="
install -m 644 "$SRC/config.py" "$ROOT/"
install -m 644 "$SRC/sender.py" "$ROOT/"
install -m 644 "$SRC/session_registry.py" "$ROOT/"
install -m 644 "$SRC/session_import_gate.py" "$ROOT/"
install -m 644 "$SRC/panel_import.py" "$ROOT/"
install -m 644 "$SRC/panel_api.py" "$ROOT/"

grep -q 'PRIORITY_REFRESH_INTERVAL' "$ROOT/.env" 2>/dev/null || cat >>"$ROOT/.env" <<'ENV'

# priority queue (добавлено автоматически)
API_CHECK_INTERVAL=5
AUTH_FAIL_BEFORE_BAD=1
AUTH_FAIL_BEFORE_BAD_PRIORITY=1
AUTH_FAIL_BEFORE_BAD_BACKLOG=1
FRESH_SESSION_HOURS=72
PRIORITY_REFRESH_INTERVAL=420
REFRESH_INTERVAL=180
BAD_MOVE_MAX_PER_MIN=80
IMPORT_VALIDATE=1
NO_TARGETS_RETRY_SEC=600
ENV

echo "Перезапуск app.py..."
if systemctl is-active --quiet new-soft 2>/dev/null; then
  systemctl restart new-soft
elif systemctl is-active --quiet new-soft-app 2>/dev/null; then
  systemctl restart new-soft-app
else
  pkill -f "$ROOT/app.py" 2>/dev/null || true
  sleep 2
  cd "$ROOT" && nohup ./venv/bin/python3 app.py >> app.log 2>&1 &
fi

sleep 3
echo "=== tail app.log ==="
tail -5 "$ROOT/app.log" || true
echo "Готово. Проверь в логе: priority-queue и ⚡priority"
