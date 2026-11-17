import os
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

base = Path(__file__).resolve().parent.parent
name = os.environ.get("WHISPER_MODEL", "large-v3")
target = Path(os.environ.get("WHISPER_MODEL_DIR", base / "models" / name))
repo = os.environ.get("WHISPER_REPO", f"Systran/faster-whisper-{name}")

if (target / "model.bin").exists():
    print(f"Модель уже на месте: {target}")
    sys.exit(0)

target.mkdir(parents=True, exist_ok=True)
print(f"Скачиваю {repo} в {target}")
snapshot_download(
    repo,
    local_dir=str(target),
    allow_patterns=[
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.*",
    ],
)

if not (target / "model.bin").exists():
    print("model.bin не появился, проверь название модели и соединение", file=sys.stderr)
    sys.exit(1)

print("Готово")
