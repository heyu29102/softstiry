#!/bin/bash
# Errno 24 Too many open files — поднять лимиты для рассылки.
set -euo pipefail

echo "=== текущий ulimit -n ==="
ulimit -n

LIMITS=/etc/security/limits.d/99-new-soft.conf
cat > "$LIMITS" <<'EOF'
* soft nofile 1048576
* hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
EOF
echo "wrote $LIMITS"

mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/99-nofile.conf <<'EOF'
[Manager]
DefaultLimitNOFILE=1048576
EOF
echo "wrote systemd DefaultLimitNOFILE"

ROOT="${1:-/opt/new-soft}"
if [[ -f "$ROOT/deploy/panel.service" ]]; then
  cp "$ROOT/deploy/panel.service" /etc/systemd/system/panel.service
  echo "updated panel.service (LimitNOFILE + venv)"
fi

systemctl daemon-reexec 2>/dev/null || systemctl daemon-reload

echo ""
echo "=== перелогинься или выполни: ulimit -n 1048576 ==="
echo "=== потом: systemctl restart panel.service ==="
echo "=== в .env рекомендуется: MAX_CONNECT_PARALLEL=80 (не 900) ==="
