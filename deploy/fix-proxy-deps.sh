#!/bin/bash
# Прокси без python-socks[asyncio] не работают в Telethon — рассылка встанет.
set -euo pipefail

ROOT="${1:-/opt/new-soft}"
PY="${ROOT}/venv/bin/python3"
PIP="${ROOT}/venv/bin/pip"

if [[ ! -x "$PY" ]]; then
  echo "Нет venv: $ROOT/venv"
  exit 1
fi

"$PIP" install -U 'python-socks[asyncio]>=2.4' 'PySocks>=1.7'

"$PY" - <<'PY'
import python_socks.async_.asyncio
print("OK: python-socks[asyncio] установлен")
PY
