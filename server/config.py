import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("WHISPER_DATA_DIR", BASE_DIR / "data"))
RECORDS_DIR = DATA_DIR / "records"
STATIC_DIR = BASE_DIR / "static"

MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
MODEL_DIR = Path(os.environ.get("WHISPER_MODEL_DIR", BASE_DIR / "models" / MODEL_NAME))

GPU_COMPUTE_TYPE = os.environ.get("WHISPER_GPU_COMPUTE", "float16")
CPU_COMPUTE_TYPE = os.environ.get("WHISPER_CPU_COMPUTE", "int8")
CPU_THREADS = _int("WHISPER_CPU_THREADS", 6)
BEAM_SIZE = _int("WHISPER_BEAM_SIZE", 5)

MIN_FREE_VRAM_MB = _int("WHISPER_MIN_FREE_VRAM_MB", 5000)
IDLE_UNLOAD_SECONDS = _int("WHISPER_IDLE_UNLOAD_SECONDS", 300)

RETENTION_DAYS = _int("WHISPER_RETENTION_DAYS", 30)
MAX_DURATION_SECONDS = _int("WHISPER_MAX_DURATION_SECONDS", 1800)
MAX_UPLOAD_MB = _int("WHISPER_MAX_UPLOAD_MB", 300)
