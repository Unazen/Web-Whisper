import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import audio, catalog, config, models
from server.errors import UserError

parser = argparse.ArgumentParser(description="Консольная проверка Whisper")
parser.add_argument("audio", help="путь к аудио или видеофайлу")
parser.add_argument("--model", default=config.DEFAULT_MODEL, help="id модели: " + ", ".join(catalog.ids()))
parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
parser.add_argument("--language", default=None)
args = parser.parse_args()

entry = catalog.get(args.model)
if entry is None:
    sys.exit(f"Неизвестная модель {args.model}")

if not models.installed(args.model):
    sys.exit(f"Модель {args.model} не скачана. Запусти: python scripts/download_model.py {args.model}")

if not Path(args.audio).exists():
    sys.exit(f"Файл не найден: {args.audio}")


def run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


gpu = run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"])
print("GPU:", gpu or "nvidia-smi недоступен")

try:
    samples = audio.decode(args.audio)
except UserError as exc:
    sys.exit(str(exc))
print(f"Звук прочитан: {len(samples) / audio.SAMPLE_RATE:.1f}с")

from faster_whisper import WhisperModel

path = str(models.path(args.model))
device = args.device
started = time.monotonic()
model = None
if device == "cuda":
    try:
        model = WhisperModel(path, device="cuda", compute_type=config.GPU_COMPUTE_TYPE)
    except Exception as exc:
        print(f"\nНе удалось загрузить модель на видеокарту: {exc}")
        print(f"Модели {args.model} нужно около {(entry['vram_mb'] + config.VRAM_MARGIN_MB) / 1024:.1f} ГБ свободной видеопамяти.")
        busy = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
        if busy:
            print("Видеопамять сейчас держат процессы:")
            print(busy)
        print("Освободи видеопамять (закрой Open WebUI, ollama, рендер) или возьми модель полегче, например --model large-v3-turbo.")
        print("Продолжаю на процессоре.\n")
        device = "cpu"
if model is None:
    model = WhisperModel(path, device="cpu", compute_type=config.CPU_COMPUTE_TYPE, cpu_threads=config.CPU_THREADS)
print(f"Модель {args.model} загружена на {device} за {time.monotonic() - started:.1f}с")

language = "en" if entry["lang"] == "en" else args.language

started = time.monotonic()
segments, info = model.transcribe(
    samples,
    language=language,
    beam_size=config.BEAM_SIZE,
    vad_filter=True,
    condition_on_previous_text=False,
)
print(f"Язык: {info.language} ({info.language_probability:.2f}), длина: {info.duration:.1f}с")

for seg in segments:
    text = seg.text.strip()
    if text:
        print(f"[{seg.start:7.2f} -> {seg.end:7.2f}] {text}")

elapsed = time.monotonic() - started
speed = info.duration / elapsed if elapsed else 0
print(f"\nТранскрипция заняла {elapsed:.1f}с на {device}, скорость x{speed:.1f} от реального времени")
