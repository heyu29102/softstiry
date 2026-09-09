#!/bin/bash
# Stories stability: скорость, антифрод, панель
# Запуск: sudo bash /opt/new-soft/deploy/update-fixes.sh

set -euo pipefail

DIR="${1:-/opt/new-soft}"
REFS=(main stories-night-2026-09-03)
FILES=(
  sender.py
  config.py
  proxies.py
  target_select.py
  story_refs.py
  panel_control.py
  panel_texts.py
  panel_kb.py
  panel_menu.py
  panel_fallback.py
  redirect.py
)

cd "$DIR"

echo "=== Бэкап ==="
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p ".backup/$STAMP"
for f in "${FILES[@]}" deploy/update-fixes.sh; do
  [ -f "$f" ] && cp -a "$f" ".backup/$STAMP/"
done

git_ref=""
for remote in fixes origin; do
  git remote get-url "$remote" >/dev/null 2>&1 || continue
  for ref in "${REFS[@]}"; do
    echo "  fetch $remote $ref ..."
    git fetch "$remote" "$ref" 2>/dev/null || continue
    if git cat-file -e "$remote/$ref:sender.py" 2>/dev/null; then
      git_ref="$remote/$ref"
      echo "  ✓ $git_ref"
      break 2
    fi
  done
done

if [ -z "$git_ref" ]; then
  echo "❌ не удалось получить main"
  git remote -v
  exit 1
fi

git checkout "$git_ref" -- "${FILES[@]}" 2>/dev/null || true
git show "$git_ref:deploy/update-fixes.sh" > deploy/update-fixes.sh 2>/dev/null || true
chmod +x deploy/update-fixes.sh 2>/dev/null || true

if [ -f "$DIR/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$DIR/venv/bin/activate"
fi
python3 -m py_compile sender.py config.py proxies.py target_select.py story_refs.py \
  panel_control.py panel_texts.py panel_kb.py panel_menu.py panel_fallback.py redirect.py

if [ -f "$DIR/app.pid" ]; then
  PID=$(cat "$DIR/app.pid" 2>/dev/null || true)
  kill "$PID" 2>/dev/null || true
  sleep 2
  kill -9 "$PID" 2>/dev/null || true
  rm -f "$DIR/app.pid"
fi
nohup python3 "$DIR/app.py" >> "$DIR/app.log" 2>&1 &
sleep 2

pkill -f "python3.*redirect.py" 2>/dev/null || true
sleep 1
nohup python3 "$DIR/redirect.py" >> "$DIR/redirect.log" 2>&1 &

# panel.py отдельный процесс — перезапусти если есть panel.service
if systemctl is-active panel.service >/dev/null 2>&1; then
  systemctl restart panel.service && echo "panel.service restarted"
fi

echo "=== Готово ==="
echo "tail -f $DIR/app.log"
