import logging
import queue
import threading
import time

from . import config, storage
from .transcriber import engine

log = logging.getLogger("whisper-app.jobs")

_queue = queue.Queue()
_running = threading.Event()
_started = False


class DurationError(Exception):
    pass


def submit(rid):
    _queue.put(rid)


def pending():
    return _queue.qsize() + (1 if _running.is_set() else 0)


def recover():
    for rid in reversed(storage.list_ids()):
        meta = storage.read(rid)
        if meta and meta.get("status") in ("queued", "processing"):
            storage.update(rid, status="queued", progress=0.0)
            submit(rid)


def _transcribe(rid, meta, audio, model, device, started):
    language = meta.get("language_requested")
    language = None if language in (None, "auto") else language
    storage.update(rid, text="", segments=[], progress=0.0, device=device)
    segments, info = model.transcribe(
        str(audio),
        language=language,
        beam_size=config.BEAM_SIZE,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    duration = float(info.duration or 0)
    if duration > config.MAX_DURATION_SECONDS:
        raise DurationError(
            "Запись длиннее %d минут" % (config.MAX_DURATION_SECONDS // 60)
        )
    storage.update(
        rid,
        duration=round(duration, 2),
        language=info.language,
        language_probability=round(float(info.language_probability or 0), 3),
    )
    collected = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        collected.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
        progress = min(0.99, seg.end / duration) if duration else 0.0
        storage.update(
            rid,
            segments=collected,
            text=" ".join(item["text"] for item in collected),
            progress=round(progress, 3),
        )
    storage.update(
        rid,
        status="done",
        progress=1.0,
        device=device,
        processing_seconds=round(time.monotonic() - started, 1),
    )


def _run(rid):
    meta = storage.read(rid)
    if meta is None:
        return
    audio = storage.audio_path(rid, meta)
    storage.update(rid, status="processing", progress=0.0, error=None)
    started = time.monotonic()
    try:
        for attempt in (0, 1):
            model, device = engine.acquire()
            try:
                _transcribe(rid, meta, audio, model, device, started)
                break
            except DurationError:
                raise
            except Exception:
                if device == "cuda" and attempt == 0:
                    log.exception("GPU transcription failed, retrying on CPU")
                    engine.fallback_to_cpu()
                    continue
                raise
            finally:
                engine.release()
    except Exception as exc:
        log.exception("job %s failed", rid)
        storage.update(rid, status="error", error=str(exc) or exc.__class__.__name__)


def _worker():
    while True:
        rid = _queue.get()
        _running.set()
        try:
            _run(rid)
        except Exception:
            log.exception("worker crashed on %s", rid)
        finally:
            _running.clear()
            _queue.task_done()


def _janitor():
    last_cleanup = time.monotonic()
    while True:
        time.sleep(30)
        try:
            engine.idle_unload()
            if time.monotonic() - last_cleanup > 6 * 3600:
                storage.cleanup(config.RETENTION_DAYS)
                last_cleanup = time.monotonic()
        except Exception:
            log.exception("janitor error")


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True, name="whisper-worker").start()
    threading.Thread(target=_janitor, daemon=True, name="whisper-janitor").start()
