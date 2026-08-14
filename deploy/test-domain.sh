#!/bin/bash
# Полная диагностика домена: сервер + сеть + сравнение с рабочим доменом
set -euo pipefail

DOMAIN="${1:?usage: test-domain.sh look-now.pro [compare-domain]}"
COMPARE="${2:-look-now.pro}"
ROOT="${3:-/opt/new-soft}"
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"
COMPARE="${COMPARE#https://}"
COMPARE="${COMPARE#http://}"
COMPARE="${COMPARE%%/*}"

cd "$ROOT"

echo "========== A. СЕРВЕР (воронка) =========="
echo "--- $DOMAIN in domains.txt ---"
grep -i "$DOMAIN" domains.txt || echo "NOT FOUND"

CONF="${ROOT}/deploy/nginx-redirect-live.conf"
echo "--- nginx server_name ---"
grep server_name "$CONF" 2>/dev/null || echo "no conf"

echo "--- redirect :8090 ---"
curl -sI --max-time 5 -H "Host: $DOMAIN" "http://127.0.0.1:8090/" | grep -iE 'HTTP|location' || echo "FAIL"

echo "--- nginx :80 ---"
curl -sI --max-time 5 -H "Host: $DOMAIN" "http://127.0.0.1/" | grep -iE 'HTTP|location' || echo "FAIL"

echo ""
echo "========== B. DNS (Google / Yandex) =========="
for ns in 8.8.8.8 77.88.8.8; do
  echo "$DOMAIN @$ns A: $(dig +short @$ns "$DOMAIN" A | tr '\n' ' ')"
  echo "$DOMAIN @$ns AAAA: $(dig +short @$ns "$DOMAIN" AAAA | tr '\n' ' ')"
done

echo ""
echo "========== C. СНАРУЖИ с сервера (IPv4 HTTPS) =========="
curl -4 -sI --max-time 15 "https://$DOMAIN/" | grep -iE 'HTTP|location|cf-ray' || echo "TIMEOUT/FAIL IPv4"

echo "--- сравнение: https://$COMPARE/ ---"
curl -4 -sI --max-time 15 "https://$COMPARE/" | grep -iE 'HTTP|location|cf-ray' || echo "TIMEOUT/FAIL"

echo ""
echo "========== D. ORIGIN IP (если не CF) =========="
ORIGIN_IP="${SERVER_PUBLIC_IP:-}"
if [[ -z "$ORIGIN_IP" ]] && [[ -f .env ]]; then
  ORIGIN_IP=$(grep -E '^SERVER_PUBLIC_IP=' .env | cut -d= -f2- | tr -d '"' || true)
fi
if [[ -n "$ORIGIN_IP" ]]; then
  echo "Direct to origin $ORIGIN_IP:80 Host=$DOMAIN"
  curl -sI --max-time 5 -H "Host: $DOMAIN" "http://$ORIGIN_IP/" | grep -iE 'HTTP|location' || echo "FAIL (норм если CF-only)"
else
  echo "SERVER_PUBLIC_IP не задан в .env"
fi

echo ""
echo "========== E. FIREWALL / ПОРТЫ =========="
ss -tlnp | grep -E ':80 |:443 ' || true
command -v ufw >/dev/null && ufw status 2>/dev/null | head -5 || true

echo ""
echo "========== F. ИНТЕРПРЕТАЦИЯ =========="
echo "• A–C OK, но в браузере ERR_TIMED_OUT → не сервер, а путь до пользователя:"
echo "  - РФ: зоны .icu часто не открываются (ТСПУ/провайдер), .pro обычно OK"
echo "  - IPv6: Chrome ждёт AAAA; тест с ПК: curl -4 -I https://$DOMAIN/"
echo "  - Сравни в ОДНОМ браузере: https://$COMPARE/ vs https://$DOMAIN/"
echo "• Проверка с разных стран: https://check-host.net/check-http?host=https://$DOMAIN/"
echo "• OK = везде HTTP 302 и Location: https://t.me/..."
