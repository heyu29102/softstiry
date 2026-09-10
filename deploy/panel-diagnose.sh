#!/bin/bash
# Диагностика panel.service
set -uo pipefail

ROOT="${1:-/opt/new-soft}"
cd "$ROOT"

echo "========== 1. systemctl status =========="
systemctl status panel.service --no-pager -l 2>&1 | head -25

echo ""
echo "========== 2. journalctl (last 60 lines) =========="
journalctl -u panel.service -n 60 --no-pager 2>&1

echo ""
echo "========== 3. python import test =========="
python3 - <<'PY'
import sys
sys.path.insert(0, ".")
try:
    import config
    import panel_texts
    print("imports OK")
    print("BOT_TOKEN set:", bool(config.BOT_TOKEN))
    print("ADMIN_IDS:", config.ADMIN_IDS)
except Exception as e:
    print("IMPORT FAIL:", e)
    raise
PY

echo ""
echo "========== 4. manual start (5 sec) =========="
timeout 5 python3 panel.py 2>&1 || true

echo ""
echo "========== 5. lock file (две панели?) =========="
ls -la /tmp/panel_*.lock 2>/dev/null || echo "no lock files"

echo ""
echo "========== 6. .env keys (без секретов) =========="
grep -E '^[A-Z_]+=' .env 2>/dev/null | sed 's/=.*/=.../' | head -30
