import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description="Консольная проверка Whisper")
parser.add_argument("audio", help="путь к аудиофайлу")
parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
parser.add_argument("--language", default=None)
parser.add_argument("--model-dir", default=None)
args = parser.parse_args()

base = Path(__file__).resolve().parent.parent
model_dir = Path(
    args.model_dir
    or os.environ.get("WHISPER_MODEL_DIR")
    or base / "models" / os.environ.get("WHISPER_MODEL", "large-v3")
)

if not (model_dir / "model.bin").exists():
    sys.exit(f"Модель не найдена в {model_dir}. Запусти ./setup.sh")

if not Path(args.audio).exists():
    sys.exit(f"Файл не найден: {args.audio}")

try:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    print("GPU:", out.stdout.strip() or "нет данных")
except Exception:
    print("GPU: nvidia-smi недоступен")

from faster_whisper import WhisperModel

compute = "float16" if args.device == "cuda" else "int8"
started = time.monotonic()
model = WhisperModel(str(model_dir), device=args.device, compute_type=compute)
print(f"Модель загружена на {args.device} за {time.monotonic() - started:.1f}с")

started = time.monotonic()
segments, info = model.transcribe(
    args.audio,
    language=args.language,
    beam_size=5,
    vad_filter=True,
    condition_on_previous_text=False,
)
print(f"Язык: {info.language} ({info.language_probability:.2f}), длина: {info.duration:.1f}с")

parts = []
for seg in segments:
    text = seg.text.strip()
    if text:
        parts.append(text)
        print(f"[{seg.start:7.2f} -> {seg.end:7.2f}] {text}")

elapsed = time.monotonic() - started
speed = info.duration / elapsed if elapsed else 0
print(f"\nТранскрипция заняла {elapsed:.1f}с, скорость x{speed:.1f} от реального времени")
