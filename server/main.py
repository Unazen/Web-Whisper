import logging
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, config, jobs, models, storage
from .export import to_srt
from .transcriber import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

LANGUAGES = {"auto", "ru", "en", "uk", "de", "fr", "es", "it", "pl", "zh", "ja"}
SOURCES = {"recording", "upload"}
AUDIO_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".webm", ".weba",
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wma", ".amr", ".3gp", ".aif", ".aiff",
    ".caf", ".mpeg", ".mpg", ".mpga", ".ts", ".wmv", ".flv",
}
MIME_EXT = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}


@asynccontextmanager
async def lifespan(app):
    config.RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    storage.cleanup(config.RETENTION_DAYS)
    jobs.recover()
    jobs.start()
    yield


app = FastAPI(title="Whisper local", lifespan=lifespan)


@app.middleware("http")
async def limit_upload(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/transcribe":
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > config.MAX_UPLOAD_MB * 1024 * 1024 + 1024 * 1024:
            return JSONResponse({"detail": "Файл больше %d МБ" % config.MAX_UPLOAD_MB}, status_code=413)
    return await call_next(request)


class TextBody(BaseModel):
    text: str = Field(max_length=2_000_000)


def _get_meta(rid):
    if not storage.valid_id(rid):
        raise HTTPException(404, "Запись не найдена")
    meta = storage.read_meta(rid)
    if meta is None:
        raise HTTPException(404, "Запись не найдена")
    return meta


def _display_name(meta):
    if meta.get("source") == "upload" and meta.get("original_name"):
        return meta["original_name"]
    return None


def _decorate(item):
    item["name"] = _display_name(item)
    if item["status"] in ("queued", "processing"):
        item["position"] = jobs.position(item["id"])
    return item


def _download_name(meta, ext):
    stem = None
    if meta.get("source") == "upload" and meta.get("original_name"):
        stem = Path(meta["original_name"]).stem
        stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", stem).strip(" .")[:80]
    return (stem or f"whisper-{meta['id']}") + ext


def _content_disposition(name):
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _ensure_mp3(rid, meta):
    out = storage.record_dir(rid) / "audio.mp3"
    if out.exists():
        return out
    if shutil.which("ffmpeg") is None:
        raise HTTPException(501, "ffmpeg не установлен: sudo pacman -S ffmpeg")
    tmp = storage.record_dir(rid) / "audio.part.mp3"
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(storage.audio_path(rid, meta)),
            "-vn", "-codec:a", "libmp3lame", "-q:a", "2",
            str(tmp),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, "ffmpeg: " + result.stderr[-300:])
    tmp.replace(out)
    return out


def _suffix_for(upload, mime):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in AUDIO_EXT:
        return suffix
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    if mime.startswith("audio/") or mime.startswith("video/"):
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix or "") else ".bin"
    return None


@app.post("/api/transcribe", status_code=201)
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form("auto"),
    model: str = Form(""),
    source: str = Form("upload"),
):
    if language not in LANGUAGES:
        raise HTTPException(400, "Неизвестный язык")
    if source not in SOURCES:
        raise HTTPException(400, "Неизвестный источник")
    model_id = model or models.default_id()
    if not model_id:
        raise HTTPException(409, "Нет установленных моделей. Скачайте модель в разделе «Модели Whisper».")
    entry = catalog.get(model_id)
    if entry is None:
        raise HTTPException(400, "Неизвестная модель")
    if not models.installed(model_id):
        raise HTTPException(409, "Модель %s не установлена" % model_id)
    if entry["lang"] == "en" and language not in ("auto", "en"):
        raise HTTPException(400, "Эта модель понимает только английский")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    suffix = _suffix_for(file, mime)
    if suffix is None:
        raise HTTPException(415, "Это не аудио и не видео. Подойдут mp3, wav, m4a, ogg, flac, mp4, mkv и похожие.")
    rid = storage.new_id()
    folder = storage.record_dir(rid)
    folder.mkdir(parents=True)
    audio_name = f"audio{suffix}"
    dest = folder / audio_name
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, "Файл больше %d МБ" % config.MAX_UPLOAD_MB)
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "Пустой файл")
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(507, "Не удалось сохранить файл: проверь свободное место на диске")
    storage.create(rid, audio_name, mime or "application/octet-stream", language, file.filename, model_id, source, size)
    jobs.submit(rid)
    return {"id": rid}


@app.get("/api/records")
def list_records():
    items = []
    for rid in storage.list_ids():
        meta = storage.read_meta(rid)
        if meta is None:
            continue
        item = _decorate(storage.summary(meta))
        if item["status"] in ("queued", "processing"):
            state = jobs.live.get(rid)
            item["text"] = state["text"] if state else ""
        items.append(item)
    return items


@app.get("/api/records/{rid}")
def get_record(rid: str):
    meta = _get_meta(rid)
    item = _decorate({**storage.summary(meta), **storage.read_result(rid)})
    if item["status"] in ("queued", "processing"):
        state = jobs.live.get(rid)
        if state:
            item["text"] = state["text"]
            item["segments"] = list(state["segments"])
    return item


@app.put("/api/records/{rid}/text")
def save_text(rid: str, body: TextBody):
    meta = _get_meta(rid)
    if meta["status"] != "done":
        raise HTTPException(409, "Обработка ещё не закончена")
    storage.set_edited(rid, body.text)
    return {"ok": True}


@app.post("/api/records/{rid}/cancel")
def cancel_record(rid: str):
    _get_meta(rid)
    outcome = jobs.cancel(rid)
    if outcome is None:
        raise HTTPException(409, "Запись уже не в обработке")
    return {"ok": True, "state": outcome}


@app.post("/api/records/{rid}/prepare/mp3")
def prepare_mp3(rid: str):
    meta = _get_meta(rid)
    _ensure_mp3(rid, meta)
    return {"ok": True}


@app.delete("/api/records/{rid}")
def delete_record(rid: str):
    meta = _get_meta(rid)
    if meta["status"] in ("queued", "processing"):
        raise HTTPException(409, "Сначала отмени обработку")
    storage.delete(rid)
    return {"ok": True}


@app.get("/api/records/{rid}/download/{kind}")
def download(rid: str, kind: str):
    meta = _get_meta(rid)
    if kind in ("txt", "srt"):
        if meta["status"] != "done":
            raise HTTPException(409, "Текст ещё не готов")
        result = storage.read_result(rid)
        if kind == "txt":
            return Response(
                storage.effective_text(result) + "\n",
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": _content_disposition(_download_name(meta, ".txt"))},
            )
        return Response(
            to_srt(result["segments"]),
            media_type="application/x-subrip; charset=utf-8",
            headers={"Content-Disposition": _content_disposition(_download_name(meta, ".srt"))},
        )
    if kind == "audio":
        path = storage.audio_path(rid, meta)
        if not path.exists():
            raise HTTPException(404, "Исходный файл не найден")
        return FileResponse(
            path,
            media_type=meta.get("audio_mime") or "application/octet-stream",
            filename=_download_name(meta, path.suffix),
        )
    if kind == "mp3":
        path = _ensure_mp3(rid, meta)
        return FileResponse(path, media_type="audio/mpeg", filename=_download_name(meta, ".mp3"))
    raise HTTPException(404, "Неизвестный формат")


def _model_item(entry):
    model_id = entry["id"]
    status = engine.status()
    is_installed = models.installed(model_id)
    return {
        "id": model_id,
        "title": entry["title"],
        "badge": entry["badge"],
        "desc": entry["desc"],
        "size_mb": entry["size_mb"],
        "vram_mb": entry["vram_mb"],
        "speed": entry["speed"],
        "quality": entry["quality"],
        "lang": entry["lang"],
        "installed": is_installed,
        "disk_bytes": models.dir_bytes(models.path(model_id)) if is_installed else 0,
        "download": models.download_state(model_id),
        "loaded": status["loaded"] and status["model"] == model_id,
    }


@app.get("/api/models")
def list_models():
    return {
        "models": [_model_item(entry) for entry in catalog.MODELS],
        "default": models.default_id() or config.DEFAULT_MODEL,
        "dir": str(config.MODELS_DIR),
        "idle_unload_seconds": config.IDLE_UNLOAD_SECONDS,
    }


@app.post("/api/models/{model_id}/download", status_code=202)
def download_model(model_id: str):
    if catalog.get(model_id) is None:
        raise HTTPException(404, "Неизвестная модель")
    try:
        models.start_download(model_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@app.delete("/api/models/{model_id}")
def delete_model(model_id: str):
    if catalog.get(model_id) is None:
        raise HTTPException(404, "Неизвестная модель")
    if jobs.model_in_use(model_id):
        raise HTTPException(409, "Модель используется записью в очереди")
    try:
        engine.unload_model(model_id)
        models.remove(model_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


@app.get("/api/status")
def status():
    current = jobs.current_id()
    current_item = None
    if current:
        meta = storage.read_meta(current)
        if meta:
            current_item = {"id": current, "name": _display_name(meta) or "запись с микрофона"}
    try:
        disk_free = shutil.disk_usage(config.DATA_DIR if config.DATA_DIR.exists() else config.BASE_DIR).free
    except OSError:
        disk_free = None
    return {
        "engine": engine.status(),
        "queued": jobs.queued(),
        "current": current_item,
        "disk_free": disk_free,
        "models_installed": models.installed_ids(),
        "download": models.active_download(),
        "retention_days": config.RETENTION_DAYS,
        "limits": {
            "recording_seconds": config.MAX_DURATION_SECONDS,
            "upload_seconds": config.MAX_UPLOAD_DURATION_SECONDS,
            "upload_bytes": config.MAX_UPLOAD_MB * 1024 * 1024,
        },
    }


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
