#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$APP_DIR"
if [ ! -d .venv ]; then
  echo "Не найдено окружение .venv. Сначала запусти ./setup.sh" >&2
  exit 1
fi
source "$APP_DIR/env.sh"
exec python -m uvicorn server.main:app \
  --host "${WHISPER_HOST:-127.0.0.1}" \
  --port "${WHISPER_PORT:-8765}" \
  --log-level info
