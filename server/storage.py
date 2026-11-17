import json
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
_lock = threading.RLock()


def valid_id(rid):
    return bool(ID_RE.match(rid))


def new_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def record_dir(rid):
    return config.RECORDS_DIR / rid


def _meta_path(rid):
    return record_dir(rid) / "meta.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(rid, audio_name, mime, language, original_name):
    meta = {
        "id": rid,
        "created": _now_iso(),
        "status": "queued",
        "progress": 0.0,
        "language_requested": language,
        "language": None,
        "language_probability": None,
        "duration": None,
        "text": "",
        "edited_text": None,
        "segments": [],
        "device": None,
        "audio_file": audio_name,
        "audio_mime": mime,
        "original_name": original_name,
        "processing_seconds": None,
        "error": None,
    }
    write(rid, meta)
    return meta


def read(rid):
    try:
        with _meta_path(rid).open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, NotADirectoryError):
        return None


def write(rid, meta):
    path = _meta_path(rid)
    tmp = path.with_suffix(".tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        os.replace(tmp, path)


def update(rid, **fields):
    with _lock:
        meta = read(rid)
        if meta is None:
            return None
        meta.update(fields)
        write(rid, meta)
        return meta


def audio_path(rid, meta):
    return record_dir(rid) / meta["audio_file"]


def summary(meta):
    text = meta.get("edited_text")
    if text is None:
        text = meta.get("text", "")
    return {
        "id": meta["id"],
        "created": meta["created"],
        "status": meta["status"],
        "progress": meta.get("progress", 0.0),
        "duration": meta.get("duration"),
        "language": meta.get("language"),
        "device": meta.get("device"),
        "snippet": text[:140],
    }


def list_ids():
    if not config.RECORDS_DIR.exists():
        return []
    return sorted(
        (p.name for p in config.RECORDS_DIR.iterdir() if p.is_dir() and valid_id(p.name)),
        reverse=True,
    )


def list_all():
    items = []
    for rid in list_ids():
        meta = read(rid)
        if meta is not None:
            items.append(summary(meta))
    return items


def delete(rid):
    with _lock:
        shutil.rmtree(record_dir(rid), ignore_errors=True)


def cleanup(days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    removed = 0
    for rid in list_ids():
        meta = read(rid)
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
