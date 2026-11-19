import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone

from . import config

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
SNIPPET_LEN = 160
_lock = threading.RLock()

SUMMARY_FIELDS = (
    "id",
    "created",
    "status",
    "stage",
    "progress",
    "duration",
    "language",
    "device",
    "device_note",
    "model",
    "source",
    "original_name",
    "size",
    "snippet",
    "segments_count",
    "processing_seconds",
    "error",
)


def valid_id(rid):
    return bool(ID_RE.match(rid))


def new_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def record_dir(rid):
    return config.RECORDS_DIR / rid


def _meta_path(rid):
    return record_dir(rid) / "meta.json"


def _result_path(rid):
    return record_dir(rid) / "result.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, NotADirectoryError):
        return None


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def create(rid, audio_name, mime, language, original_name, model, source, size):
    meta = {
        "id": rid,
        "created": now_iso(),
        "status": "queued",
        "stage": None,
        "progress": 0.0,
        "started": None,
        "language_requested": language,
        "language": None,
        "language_probability": None,
        "duration": None,
        "device": None,
        "device_note": None,
        "model": model,
        "source": source,
        "size": size,
        "audio_file": audio_name,
        "audio_mime": mime,
        "original_name": original_name,
        "snippet": "",
        "segments_count": 0,
        "processing_seconds": None,
        "error": None,
    }
    with _lock:
        _write_json(_meta_path(rid), meta)
    return meta


def read_meta(rid):
    return _read_json(_meta_path(rid))


def read_result(rid):
    data = _read_json(_result_path(rid))
    if not data:
        old = read_meta(rid) or {}
        data = {"text": old.get("text", ""), "segments": old.get("segments", []), "edited_text": old.get("edited_text")}
    return {
        "text": data.get("text", ""),
        "segments": data.get("segments", []),
        "edited_text": data.get("edited_text"),
    }


def read(rid):
    meta = read_meta(rid)
    if meta is None:
        return None
    meta.update(read_result(rid))
    return meta


def update(rid, **fields):
    with _lock:
        meta = read_meta(rid)
        if meta is None:
            return None
        meta.update(fields)
        _write_json(_meta_path(rid), meta)
        return meta


def effective_text(result):
    edited = result.get("edited_text")
    return edited if edited is not None else result.get("text", "")


def write_result(rid, text, segments):
    with _lock:
        if read_meta(rid) is None:
            return
        old = read_result(rid)
        _write_json(
            _result_path(rid),
            {"text": text, "segments": segments, "edited_text": old.get("edited_text")},
        )


def set_edited(rid, text):
    with _lock:
        meta = read_meta(rid)
        if meta is None:
            return False
        result = read_result(rid)
        result["edited_text"] = text
        _write_json(_result_path(rid), result)
        meta["snippet"] = text[:SNIPPET_LEN]
        _write_json(_meta_path(rid), meta)
        return True


def audio_path(rid, meta):
    return record_dir(rid) / meta["audio_file"]


def summary(meta):
    item = {key: meta.get(key) for key in SUMMARY_FIELDS}
    item["progress"] = item["progress"] or 0.0
    item["snippet"] = item["snippet"] or (meta.get("edited_text") or meta.get("text") or "")[:SNIPPET_LEN]
    item["segments_count"] = item["segments_count"] or len(meta.get("segments") or [])
    item["segments_count"] = item["segments_count"] or 0
    item["elapsed_seconds"] = None
    if meta.get("status") == "processing" and meta.get("started"):
        try:
            started = datetime.fromisoformat(meta["started"])
            item["elapsed_seconds"] = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except ValueError:
            pass
    return item


def list_ids():
    if not config.RECORDS_DIR.exists():
        return []
    return sorted(
        (p.name for p in config.RECORDS_DIR.iterdir() if p.is_dir() and valid_id(p.name)),
        reverse=True,
    )


def delete(rid):
    with _lock:
        shutil.rmtree(record_dir(rid), ignore_errors=True)


def cleanup(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for rid in list_ids():
        meta = read_meta(rid)
        created = None
        if meta is not None:
            try:
                created = datetime.fromisoformat(meta["created"])
            except (KeyError, ValueError):
                created = None
        if created is None:
            created = datetime.fromtimestamp(record_dir(rid).stat().st_mtime, timezone.utc)
        if created < cutoff:
            delete(rid)
            removed += 1
    return removed
