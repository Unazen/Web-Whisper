import logging
import threading
import time
from collections import deque

from . import audio as audio_io
from . import catalog, config, storage
from .errors import UserError
from .transcriber import engine

log = logging.getLogger("whisper-app.jobs")

SAMPLE_RATE = audio_io.SAMPLE_RATE

_cond = threading.Condition()
_pending = deque()
_current = None
_cancel = set()
_started = False
live = {}


class Cancelled(Exception):
    pass


def submit(rid):
    with _cond:
        _pending.append(rid)
        _cond.notify()


def current_id():
    with _cond:
        return _current


def pending():
    with _cond:
        return len(_pending) + (1 if _current else 0)


def queued():
    with _cond:
        return len(_pending)


def position(rid):
    with _cond:
        if rid == _current:
            return 0
        try:
            return list(_pending).index(rid) + 1
        except ValueError:
            return None


def model_in_use(model_id):
    with _cond:
        rids = list(_pending) + ([_current] if _current else [])
    for rid in rids:
        meta = storage.read_meta(rid)
        if meta and meta.get("model") == model_id:
            return True
    return False


def cancel(rid):
    with _cond:
        if rid in _pending:
            _pending.remove(rid)
            removed = True
        elif rid == _current:
            _cancel.add(rid)
            return "cancelling"
        else:
            return None
    if removed:
        storage.update(rid, status="cancelled", stage=None, progress=0.0)
    return "cancelled"


def recover():
    for rid in reversed(storage.list_ids()):
        meta = storage.read_meta(rid)
        if meta and meta.get("status") in ("queued", "processing"):
            storage.update(rid, status="queued", stage=None, progress=0.0, started=None)
            submit(rid)


def _check_cancel(rid):
    with _cond:
        if rid in _cancel:
            raise Cancelled()


def _limit_for(meta):
    if meta.get("source") == "recording":
        return config.MAX_DURATION_SECONDS
    return config.MAX_UPLOAD_DURATION_SECONDS


def _too_long(limit):
    hours, minutes = divmod(limit // 60, 60)
    label = f"{hours} ч {minutes} мин" if hours and minutes else (f"{hours} ч" if hours else f"{minutes} мин")
    return UserError(f"Запись длиннее допустимых {label}")


def _consume(rid, model, audio, duration, language):
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=config.BEAM_SIZE,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    storage.update(
        rid,
        language=info.language,
        language_probability=round(float(info.language_probability or 0), 3),
    )
    state = live[rid]
    state["text"] = ""
    state["segments"] = []
    last_write = 0.0
    for seg in segments:
        _check_cancel(rid)
        text = seg.text.strip()
        if not text:
            continue
        state["segments"].append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
        state["text"] = " ".join(item["text"] for item in state["segments"])
        now = time.monotonic()
        if now - last_write > 0.4:
            last_write = now
            progress = min(0.99, seg.end / duration) if duration else 0.0
            storage.update(rid, progress=round(progress, 3))


def _transcribe(rid, meta, audio, duration):
    model_id = meta["model"]
    entry = catalog.get(model_id)
    language = meta.get("language_requested")
    language = None if language in (None, "auto") else language
    if entry and entry["lang"] == "en":
        language = "en"
    for attempt in (0, 1):
        _check_cancel(rid)
        storage.update(rid, stage="loading", progress=0.0)
        model, device = engine.acquire(model_id)
        try:
            _check_cancel(rid)
            storage.update(
                rid,
                stage="transcribing",
                device=device,
                device_note=engine.reason if device == "cpu" else None,
                progress=0.0,
            )
            _consume(rid, model, audio, duration, language)
            return device
        except (Cancelled, UserError):
            raise
        except Exception:
            if device == "cuda" and attempt == 0:
                log.exception("GPU transcription failed, retrying on CPU")
                engine.fallback_to_cpu()
                continue
            raise
        finally:
            engine.release()


def _finish(rid, started, device):
    state = live.get(rid, {"text": "", "segments": []})
    text = state["text"]
    storage.write_result(rid, text, state["segments"])
    storage.update(
        rid,
        status="done",
        stage=None,
        progress=1.0,
        device=device,
        snippet=text[: storage.SNIPPET_LEN],
        segments_count=len(state["segments"]),
        processing_seconds=round(time.monotonic() - started, 1),
    )


def _run(rid):
    meta = storage.read_meta(rid)
    if meta is None:
        return
    live[rid] = {"text": "", "segments": []}
    audio_file = storage.audio_path(rid, meta)
    started = time.monotonic()
    storage.update(rid, status="processing", stage="decoding", progress=0.0, error=None, started=storage.now_iso())
    device = meta.get("device")
    try:
        limit = _limit_for(meta)
        probed = audio_io.probe(audio_file)
        if probed is not None and probed > limit:
            raise _too_long(limit)
        _check_cancel(rid)
        audio = audio_io.decode(audio_file)
        duration = len(audio) / SAMPLE_RATE
        if duration > limit:
            raise _too_long(limit)
        storage.update(rid, duration=round(duration, 2))
        _check_cancel(rid)
        if duration < 0.3:
            _finish(rid, started, device)
            return
        device = _transcribe(rid, meta, audio, duration)
        _finish(rid, started, device)
    except Cancelled:
        _save_partial(rid)
        storage.update(rid, status="cancelled", stage=None)
    except UserError as exc:
        _save_partial(rid)
        storage.update(rid, status="error", stage=None, error=str(exc))
    except Exception as exc:
        log.exception("job %s failed", rid)
        _save_partial(rid)
        storage.update(rid, status="error", stage=None, error=str(exc) or exc.__class__.__name__)


def _save_partial(rid):
    state = live.get(rid)
    if state and state["segments"]:
        storage.write_result(rid, state["text"], state["segments"])
        storage.update(
            rid,
            snippet=state["text"][: storage.SNIPPET_LEN],
            segments_count=len(state["segments"]),
        )


def _worker():
    global _current
    while True:
        with _cond:
            while not _pending:
                _cond.wait()
            rid = _pending.popleft()
            _current = rid
        try:
            _run(rid)
        except Exception:
            log.exception("worker crashed on %s", rid)
        finally:
            with _cond:
                _current = None
                _cancel.discard(rid)
            live.pop(rid, None)


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
