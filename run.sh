#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$APP_DIR"
if [ ! -d .venv ]; then
  echo "Не найдено окружение .venv. Сначала запусти ./setup.sh" >&2
  exit 1
fi
source "$APP_DIR/env.sh"
export TMPDIR="${WHISPER_DATA_DIR:-$APP_DIR/data}/tmp"
mkdir -p "$TMPDIR"
HOST="${WHISPER_HOST:-127.0.0.1}"
PORT="${WHISPER_PORT:-8765}"
echo "Whisper local: http://$HOST:$PORT  (проверка: curl http://$HOST:$PORT/api/status)"
exec python -m uvicorn server.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --log-level info
