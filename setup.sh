#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Не найден $PYTHON_BIN. Установи Python: sudo pacman -S python" >&2
  exit 1
fi

echo "==> Python: $("$PYTHON_BIN" --version)"

if [ ! -d .venv ]; then
  echo "==> Создаю окружение .venv"
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip

echo "==> Ставлю зависимости (cuDNN весит около 700 МБ, это нормально)"
if ! python -m pip install -r requirements.txt; then
  echo "" >&2
  echo "Установка зависимостей не удалась. Если ошибка про ctranslate2 или av," >&2
  echo "значит для твоей версии Python ещё нет готовых пакетов. Поставь другую версию" >&2
  echo "и повтори: rm -rf .venv && PYTHON_BIN=python3.12 ./setup.sh" >&2
  echo "(Python 3.12 на Arch: через uv: uv python install 3.12, или пакет python312 из AUR)" >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: nvidia-smi не найден, драйвер NVIDIA недоступен. Whisper будет работать на CPU." >&2
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: ffmpeg не найден. Он нужен только для скачивания в MP3: sudo pacman -S ffmpeg" >&2
fi

echo "==> Скачиваю модель по умолчанию (large-v3, около 3 ГБ). Другие модели можно скачать позже на сайте."
python scripts/download_model.py

chmod +x run.sh env.sh setup.sh
echo ""
echo "Готово. Проверка: source env.sh && python scripts/proto.py путь/к/аудио.mp3"
echo "Запуск:   ./run.sh   (откроется на http://127.0.0.1:8765)"
