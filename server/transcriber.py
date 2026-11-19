import gc
import logging
import subprocess
import threading
import time

from . import catalog, config, models

log = logging.getLogger("whisper-app.engine")


class Engine:
    def __init__(self):
        self._model = None
        self._model_id = None
        self._device = None
        self._lock = threading.RLock()
        self._active = 0
        self._last_used = time.monotonic()
        self._force_cpu = False
        self._reason = None
        self._gpu_cache = (0.0, None)

    @property
    def reason(self):
        return self._reason

    def _gpu_info(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode != 0:
                return None
            free, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
            return {"free_mb": free, "total_mb": total}
        except Exception:
            return None

    def gpu_info(self):
        stamp, info = self._gpu_cache
        if time.monotonic() - stamp > 5:
            info = self._gpu_info()
            self._gpu_cache = (time.monotonic(), info)
        return info

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

    def _needed_vram_mb(self, model_id):
        if config.MIN_FREE_VRAM_MB:
            return config.MIN_FREE_VRAM_MB
        entry = catalog.get(model_id)
        return (entry["vram_mb"] if entry else 5000) + config.VRAM_MARGIN_MB

    def _cuda_available(self, model_id):
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() < 1:
                return False
        except Exception:
            return False
        free = self._free_vram_mb()
        if free is None:
            return True
        need = self._needed_vram_mb(model_id)
        if free < need:
            log.info("GPU busy: %s MB free, %s needs %s MB", free, model_id, need)
            return False
        return True

    def _cpu_reason(self, model_id):
        if self._force_cpu:
            return "на GPU произошла ошибка"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() < 1:
                return "видеокарта не найдена"
        except Exception:
            return "CUDA недоступна"
        free = self._free_vram_mb()
        need = self._needed_vram_mb(model_id)
        if free is not None and free < need:
            return "мало свободной видеопамяти: %.1f ГБ из нужных %.1f ГБ" % (free / 1024, need / 1024)
        return "видеокарта недоступна"

    def _load(self, model_id):
        from faster_whisper import WhisperModel

        path = str(models.path(model_id))
        self._reason = None
        if not self._force_cpu and self._cuda_available(model_id):
            try:
                started = time.monotonic()
                self._model = WhisperModel(path, device="cuda", compute_type=config.GPU_COMPUTE_TYPE)
                self._device = "cuda"
                self._model_id = model_id
                log.info("%s loaded on GPU in %.1fs", model_id, time.monotonic() - started)
                return
            except Exception:
                log.exception("GPU load failed, falling back to CPU")
                self._reason = "не удалось загрузить модель на GPU (возможно, не хватило видеопамяти)"
        else:
            self._reason = self._cpu_reason(model_id)
        started = time.monotonic()
        self._model = WhisperModel(
            path,
            device="cpu",
            compute_type=config.CPU_COMPUTE_TYPE,
            cpu_threads=config.CPU_THREADS,
        )
        self._device = "cpu"
        self._model_id = model_id
        log.info("%s loaded on CPU in %.1fs", model_id, time.monotonic() - started)

    def _unload(self):
        self._model = None
        self._model_id = None
        self._device = None
        gc.collect()

    def acquire(self, model_id):
        with self._lock:
            if not models.installed(model_id):
                raise RuntimeError("Модель %s не установлена. Скачайте её в разделе «Модели Whisper»." % model_id)
            if self._model is not None and self._model_id != model_id:
                log.info("switching model %s -> %s", self._model_id, model_id)
                self._unload()
            if (
                self._model is not None
                and self._device == "cpu"
                and self._active == 0
                and not self._force_cpu
                and self._cuda_available(model_id)
            ):
                log.info("GPU is free again, reloading %s on GPU", model_id)
                self._unload()
            if self._model is None:
                self._load(model_id)
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

    def unload_model(self, model_id):
        with self._lock:
            if self._model_id == model_id and self._active == 0:
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
                "model": self._model_id,
                "reason": self._reason if self._device == "cpu" else None,
                "gpu": self.gpu_info(),
                "idle_seconds": int(time.monotonic() - self._last_used),
            }


engine = Engine()
