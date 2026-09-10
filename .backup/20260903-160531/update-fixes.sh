#!/bin/bash
# Ночная сборка stories (03.09) — контакты + группы, verify
# Запуск: sudo bash /opt/new-soft/deploy/update-fixes.sh

set -euo pipefail

DIR="${1:-/opt/new-soft}"
REF="main"
FILES=(
  sender.py
  config.py
  target_select.py
  story_refs.py
  panel_control.py
  panel_texts.py
  redirect.py
)

cd "$DIR"

echo "=== Бэкап ==="
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p ".backup/$STAMP"
for f in "${FILES[@]}" deploy/update-fixes.sh; do
  [ -f "$f" ] && cp -a "$f" ".backup/$STAMP/"
done
echo "Бэкап: $DIR/.backup/$STAMP"

echo "=== Обновление из git ($REF) ==="
if [ ! -d .git ]; then
  echo "❌ $DIR не git-репозиторий"
  exit 1
fi

git_ref=""
for remote in origin fixes upstream aktual; do
  if git remote get-url "$remote" >/dev/null 2>&1; then
    echo "  git fetch $remote $REF ..."
    if git fetch "$remote" "$REF" 2>/dev/null; then
      if git show-ref --verify --quiet "refs/remotes/$remote/$REF"; then
        git_ref="$remote/$REF"
        echo "  ✓ $git_ref"
        break
      fi
    fi
  fi
done

if [ -z "$git_ref" ]; then
  echo "❌ не удалось получить $REF"
  git remote -v 2>/dev/null || true
  exit 1
fi

git checkout "$git_ref" -- "${FILES[@]}" deploy/update-fixes.sh

echo "=== Проверка синтаксиса ==="
if [ -d "$DIR/venv" ]; then
  # shellcheck disable=SC1091
  source "$DIR/venv/bin/activate"
fi
python3 -m py_compile sender.py config.py target_select.py story_refs.py panel_control.py panel_texts.py redirect.py
echo "Синтаксис OK"

echo "=== Перезапуск app.py ==="
if [ -f "$DIR/app.pid" ]; then
  PID=$(cat "$DIR/app.pid" 2>/dev/null || true)
  if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    sleep 2
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$DIR/app.pid"
fi
nohup python3 "$DIR/app.py" >> "$DIR/app.log" 2>&1 &
sleep 2
[ -f "$DIR/app.pid" ] && echo "app.py PID $(cat "$DIR/app.pid")" || echo "WARN: нет app.pid"

echo "=== Перезапуск redirect.py ==="
pkill -f "python3.*redirect.py" 2>/dev/null || true
sleep 1
nohup python3 "$DIR/redirect.py" >> "$DIR/redirect.log" 2>&1 &
sleep 1
pgrep -f "python3.*redirect.py" >/dev/null && echo "redirect.py OK" || echo "WARN: redirect не стартовал"

echo ""
echo "=== Готово (stories → ЛС + группы) ==="
echo "tail -f $DIR/app.log"
