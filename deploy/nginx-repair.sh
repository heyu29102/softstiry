#!/bin/bash
# Быстрый ремонт redirect: nginx → 127.0.0.1:8090 для всех доменов из domains.txt
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
cd "$ROOT"

echo "=== redirect local ==="
curl -sI --max-time 5 http://127.0.0.1:8090/ | head -5 || echo "FAIL: redirect не отвечает на 8090"

echo "=== domains.txt ==="
cat domains.txt 2>/dev/null || echo "(пусто)"

echo "=== rebuild nginx from domains.txt ==="
python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from domain_setup import sync_nginx_redirect
ok, msg = sync_nginx_redirect(auto_reload=False)
print(msg)
if not ok:
    sys.exit(1)
PY

CONF="$ROOT/deploy/nginx-redirect-live.conf"
TARGET="/etc/nginx/sites-enabled/redirect-domains.conf"

echo "=== symlink ==="
rm -f /etc/nginx/sites-enabled/redirect-domains
ln -sf "$CONF" "$TARGET"
echo "$TARGET -> $CONF"

echo "=== nginx -t ==="
nginx -t

echo "=== reload nginx ==="
systemctl reload nginx

echo "=== test proxy ==="
curl -sI --max-time 5 -H "Host: look-now.pro" http://127.0.0.1/ | head -8

echo ""
echo "Если Location: https://t.me/... — origin ок."
echo "Если снаружи https:// 504 — Cloudflare SSL → Flexible (не Full)."
