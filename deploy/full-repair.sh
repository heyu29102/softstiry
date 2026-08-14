#!/bin/bash
# Полный ремонт воронки: redirect:8090 + nginx:80 → t.me
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
cd "$ROOT"

echo "========== 1. Порты =========="
ss -tlnp | grep -E ':80 |:8090 ' || echo "(порты 80/8090 не найдены!)"

echo ""
echo "========== 2. bots.txt =========="
if [[ ! -s bots.txt ]]; then
  echo "WARNING: bots.txt пуст — redirect будет 503"
else
  head -3 bots.txt
fi

echo ""
echo "========== 3. redirect kill + restart =========="
systemctl stop redirect.service || true
pkill -9 -f '/opt/new-soft/redirect.py' 2>/dev/null || true
sleep 2
ss -tlnp | grep 8090 || echo "8090 free"
systemctl start redirect.service
sleep 2
systemctl is-active redirect.service

echo ""
echo "========== 4. redirect :8090 (verbose) =========="
curl -v --max-time 5 http://127.0.0.1:8090/ 2>&1 | head -25

echo ""
echo "========== 5. nginx config =========="
DOMAINS=""
while IFS= read -r line; do
  line="${line//#/}"
  line="${line//|/ }"
  for part in $line; do
    part="${part#https://}"
    part="${part#http://}"
    part="${part%%/*}"
    if [[ "$part" == *.* ]]; then
      DOMAINS="$DOMAINS $part"
    fi
  done
done < domains.txt 2>/dev/null || true
DOMAINS="${DOMAINS:- alekorotobl.info look-now.pro}"
DOMAINS=$(echo "$DOMAINS" | xargs -n1 | sort -u | xargs)

cat > deploy/nginx-redirect-live.conf <<EOF
# repair $(date -Iseconds)
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $DOMAINS;

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header CF-Connecting-IP \$http_cf_connecting_ip;
        proxy_set_header CF-IPCountry \$http_cf_ipcountry;
        proxy_connect_timeout 10s;
        proxy_read_timeout 10s;
    }
}
EOF
echo "server_name:$DOMAINS"
cat deploy/nginx-redirect-live.conf

echo ""
echo "========== 6. nginx sites-enabled =========="
mkdir -p /etc/nginx/sites-enabled
# убрать старые redirect-конфиги (не default site)
for f in /etc/nginx/sites-enabled/*; do
  [[ -f "$f" ]] || continue
  if grep -qE '8090|alekorotobl|look-now' "$f" 2>/dev/null; then
    echo "remove: $f"
    rm -f "$f"
  fi
done
ln -sf "$ROOT/deploy/nginx-redirect-live.conf" /etc/nginx/sites-enabled/redirect-domains.conf
rm -f /etc/nginx/sites-enabled/redirect-domains
ls -la /etc/nginx/sites-enabled/

echo ""
echo "========== 7. nginx test + reload =========="
nginx -t
systemctl reload nginx

echo ""
echo "========== 8. nginx :80 proxy (verbose) =========="
curl -v --max-time 5 -H "Host: look-now.pro" http://127.0.0.1/ 2>&1 | head -30

echo ""
echo "========== DONE =========="
echo "Локально должен быть: Location: https://t.me/..."
echo "Снаружи: Cloudflare SSL → Flexible (не Full)"
echo "Проверка: curl -sI https://look-now.pro/ | grep -i location"
