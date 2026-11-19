import fnmatch
import logging
import os
import shutil
import threading

from . import catalog, config

log = logging.getLogger("whisper-app.models")

ALLOW = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

_lock = threading.RLock()
_slot = threading.Lock()
_state = {}


def path(model_id):
    return config.MODELS_DIR / model_id


def installed(model_id):
    folder = path(model_id)
    return (folder / "model.bin").is_file() and (folder / "config.json").is_file()


def installed_ids():
    return [m["id"] for m in catalog.MODELS if installed(m["id"])]


def default_id():
    have = installed_ids()
    if config.DEFAULT_MODEL in have:
        return config.DEFAULT_MODEL
    return have[0] if have else None


def dir_bytes(folder):
    total = 0
    if not folder.exists():
        return 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _wanted(name):
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALLOW)


def _remote_total(repo, approx):
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True)
        total = sum((s.size or 0) for s in info.siblings if _wanted(s.rfilename))
        return total or approx
    except Exception:
        return approx


def fetch(model_id):
    from huggingface_hub import snapshot_download

    entry = catalog.get(model_id)
    target = path(model_id)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(entry["repo"], local_dir=str(target), allow_patterns=ALLOW)
    if not installed(model_id):
        raise RuntimeError("model.bin не появился после загрузки")


def _run(model_id):
    entry = catalog.get(model_id)
    approx = entry["size_mb"] * 1024 * 1024
    with _slot:
        with _lock:
            _state[model_id] = {"state": "downloading", "error": None, "total": approx}
        total = _remote_total(entry["repo"], approx)
        with _lock:
            _state[model_id]["total"] = total
        try:
            fetch(model_id)
            with _lock:
                _state.pop(model_id, None)
            log.info("model %s downloaded", model_id)
        except Exception as exc:
            log.exception("model %s download failed", model_id)
            text = str(exc).strip().splitlines()
            with _lock:
                _state[model_id] = {
                    "state": "failed",
                    "error": (text[-1] if text else exc.__class__.__name__)[:240],
                    "total": approx,
                }


def start_download(model_id):
    entry = catalog.get(model_id)
    if entry is None:
        raise KeyError(model_id)
    if installed(model_id):
        raise ValueError("Модель уже установлена")
    with _lock:
        current = _state.get(model_id)
        if current and current["state"] in ("queued", "downloading"):
            raise ValueError("Модель уже скачивается")
        free = shutil.disk_usage(_existing_parent(config.MODELS_DIR)).free
        if free < entry["size_mb"] * 1024 * 1024 * 1.15:
            raise ValueError("Не хватает места на диске для этой модели")
        _state[model_id] = {"state": "queued", "error": None, "total": entry["size_mb"] * 1024 * 1024}
    threading.Thread(target=_run, args=(model_id,), daemon=True, name=f"download-{model_id}").start()


def _existing_parent(folder):
    folder = folder.resolve()
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return folder


def download_state(model_id):
    with _lock:
        state = dict(_state.get(model_id) or {})
    if not state:
        return None
    if state["state"] in ("queued", "downloading"):
        state["downloaded"] = min(dir_bytes(path(model_id)), int(state["total"] * 0.995))
    return state


def active_download():
    with _lock:
        ids = [k for k, v in _state.items() if v["state"] in ("queued", "downloading")]
    for model_id in ids:
        state = download_state(model_id)
        if state and state["state"] == "downloading":
            return {"id": model_id, **state}
    if ids:
        return {"id": ids[0], **(download_state(ids[0]) or {})}
    return None


def remove(model_id):
    with _lock:
        current = _state.get(model_id)
        if current and current["state"] in ("queued", "downloading"):
            raise RuntimeError("Модель сейчас скачивается")
        _state.pop(model_id, None)
    shutil.rmtree(path(model_id), ignore_errors=True)
