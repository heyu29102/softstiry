#!/bin/bash
# Один раз на сервере: nginx читает live-конфиг из проекта (панель пишет туда автоматически)
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
CONF="$ROOT/deploy/nginx-redirect-live.conf"
TARGET="/etc/nginx/sites-enabled/redirect-domains.conf"

if [[ ! -d /etc/nginx/sites-enabled ]]; then
  echo "nginx sites-enabled не найден — установи nginx"
  exit 1
fi

mkdir -p "$ROOT/deploy"
touch "$CONF"
rm -f /etc/nginx/sites-enabled/redirect-domains
ln -sf "$CONF" "$TARGET"
echo "Symlink: $TARGET -> $CONF"

if command -v nginx >/dev/null; then
  nginx -t && systemctl reload nginx
  echo "nginx reloaded"
fi

echo "Готово. Добавь в .env при необходимости:"
echo "  SERVER_PUBLIC_IP=ваш.IP"
echo "  NGINX_RELOAD_CMD='sudo nginx -t && sudo systemctl reload nginx'"
