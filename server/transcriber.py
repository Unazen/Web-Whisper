import gc
import logging
import subprocess
import threading
import time

from . import config

log = logging.getLogger("whisper-app.engine")


class Engine:
    def __init__(self):
        self._model = None
        self._device = None
        self._lock = threading.RLock()
        self._active = 0
        self._last_used = time.monotonic()
        self._force_cpu = False

    def model_present(self):
        return (config.MODEL_DIR / "model.bin").exists()

    def _free_vram_mb(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                return None
            return int(out.stdout.strip().splitlines()[0])
        except Exception:
            return None

    def _cuda_available(self):
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() < 1:
                return False
        except Exception:
            return False
        free = self._free_vram_mb()
        if free is None:
            return True
        if free < config.MIN_FREE_VRAM_MB:
            log.info("GPU busy: %s MB free, need %s MB", free, config.MIN_FREE_VRAM_MB)
            return False
        return True

    def _load(self):
        from faster_whisper import WhisperModel

        path = str(config.MODEL_DIR)
        if not self._force_cpu and self._cuda_available():
            try:
                started = time.monotonic()
                self._model = WhisperModel(path, device="cuda", compute_type=config.GPU_COMPUTE_TYPE)
                self._device = "cuda"
                log.info("model loaded on GPU in %.1fs", time.monotonic() - started)
                return
            except Exception:
                log.exception("GPU load failed, falling back to CPU")
        started = time.monotonic()
        self._model = WhisperModel(
            path,
            device="cpu",
            compute_type=config.CPU_COMPUTE_TYPE,
            cpu_threads=config.CPU_THREADS,
        )
        self._device = "cpu"
        log.info("model loaded on CPU in %.1fs", time.monotonic() - started)

    def _unload(self):
        self._model = None
        self._device = None
        gc.collect()

    def acquire(self):
        with self._lock:
            if not self.model_present():
                raise RuntimeError(
                    "Модель не найдена в %s. Запусти ./setup.sh, чтобы скачать её." % config.MODEL_DIR
                )
            if (
                self._model is not None
                and self._device == "cpu"
                and self._active == 0
                and not self._force_cpu
                and self._cuda_available()
            ):
                log.info("GPU is free again, reloading model on GPU")
                self._unload()
            if self._model is None:
                self._load()
            self._active += 1
            self._last_used = time.monotonic()
            return self._model, self._device

    def release(self):
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_used = time.monotonic()

    def fallback_to_cpu(self):
        with self._lock:
            self._force_cpu = True
            self._unload()

    def idle_unload(self):
        with self._lock:
            if (
                self._model is not None
                and self._active == 0
                and time.monotonic() - self._last_used > config.IDLE_UNLOAD_SECONDS
            ):
                log.info("unloading idle model")
                self._unload()
                self._force_cpu = False

    def status(self):
        with self._lock:
            return {
                "loaded": self._model is not None,
                "device": self._device,
                "model": config.MODEL_NAME,
                "model_present": self.model_present(),
                "idle_seconds": int(time.monotonic() - self._last_used),
            }


engine = Engine()
