#!/bin/bash
# Проверка что на сервере залит наш фикс (текст+фото, домены, без теней)
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
EXPECTED_BRANCH="${2:-cursor/text-photo-share-links-7694}"
FAIL=0

cd "$ROOT"

echo "=== git ==="
git log -1 --oneline
echo "branch: $(git branch --show-current 2>/dev/null || echo '?')"
if git rev-parse --verify "origin/$EXPECTED_BRANCH" >/dev/null 2>&1; then
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse "origin/$EXPECTED_BRANCH")
  if [[ "$LOCAL" == "$REMOTE" ]]; then
    echo "✅ commit совпадает с origin/$EXPECTED_BRANCH"
  else
    echo "❌ commit НЕ совпадает с origin/$EXPECTED_BRANCH"
    echo "   local:  $LOCAL"
    echo "   remote: $REMOTE"
    FAIL=1
  fi
else
  echo "⚠️  origin/$EXPECTED_BRANCH не найден (сделай git fetch)"
fi

echo ""
echo "=== ключевые признаки кода ==="
check() {
  if eval "$2" >/dev/null 2>&1; then
    echo "✅ $1"
  else
    echo "❌ $1"
    FAIL=1
  fi
}

check "mail_content.py есть" "test -f mail_content.py"
check "sender без shadow (нет mark_shadow)" "! grep -q 'mark_shadow' sender.py"
check "sender без verify (нет verify_message_sent)" "! grep -q 'verify_message_sent' sender.py"
check "domains в mail_content" "grep -q 'pick_domain_link' mail_content.py"
check "domains в sender" "grep -q 'self.domains' sender.py"
check "LINK_RANDOM_PATH в config" "grep -q 'LINK_RANDOM_PATH' config.py"
check "нет stories-only mode" "! grep -q 'return \"stories\"' config.py"
check "session_utils.py есть" "test -f session_utils.py"
check "sqlite WAL в sender" "grep -q 'tune_session_sqlite' sender.py"
check "reconnect в sender" "grep -q 'with_reconnect' sender.py"
check "SQLITE_BUSY_TIMEOUT в config" "grep -q 'SQLITE_BUSY_TIMEOUT_MS' config.py"

echo ""
echo "=== python import ==="
if python3 -c "import config; from sender import Spammer; assert config.mailing_mode()=='text+photo'; print('mode:', config.mailing_mode())"; then
  echo "✅ import OK, режим text+photo"
else
  echo "❌ import failed"
  FAIL=1
fi

echo ""
echo "=== сервисы ==="
for svc in redirect.service panel.service bridge.service; do
  if systemctl is-active "$svc" >/dev/null 2>&1; then
    echo "✅ $svc active"
  else
    echo "⚠️  $svc not active"
  fi
done

echo ""
if [[ "$FAIL" -eq 0 ]]; then
  echo "=== ИТОГ: код наш, всё ок ==="
else
  echo "=== ИТОГ: что-то не так — залей фикс заново ==="
  exit 1
fi
