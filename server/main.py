import logging
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, jobs, storage
from .export import to_srt
from .transcriber import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

LANGUAGES = {"auto", "ru", "en", "uk", "de", "fr", "es", "it", "pl", "zh", "ja"}
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
    storage.cleanup(config.RETENTION_DAYS)
    jobs.recover()
    jobs.start()
    yield


app = FastAPI(title="Whisper local", lifespan=lifespan)


class TextBody(BaseModel):
    text: str = Field(max_length=2_000_000)


def _get(rid):
    if not storage.valid_id(rid):
        raise HTTPException(404, "Запись не найдена")
    meta = storage.read(rid)
    if meta is None:
        raise HTTPException(404, "Запись не найдена")
    return meta


def _final_text(meta):
    edited = meta.get("edited_text")
    return edited if edited is not None else meta.get("text", "")


def _attachment(name):
    return {"Content-Disposition": f'attachment; filename="{name}"'}


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
        timeout=900,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, "ffmpeg: " + result.stderr[-300:])
    tmp.replace(out)
    return out


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...), language: str = Form("auto")):
    if language not in LANGUAGES:
        raise HTTPException(400, "Неизвестный язык")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    suffix = MIME_EXT.get(mime) or Path(file.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix or ""):
        suffix = ".bin"
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
            raise HTTPException(400, "Пустая запись")
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    storage.create(rid, audio_name, mime or "application/octet-stream", language, file.filename)
    jobs.submit(rid)
    return {"id": rid}


@app.get("/api/records")
def list_records():
    return storage.list_all()


@app.get("/api/records/{rid}")
def get_record(rid: str):
    return _get(rid)


@app.put("/api/records/{rid}/text")
def save_text(rid: str, body: TextBody):
    meta = _get(rid)
    if meta["status"] != "done":
        raise HTTPException(409, "Обработка ещё не закончена")
    storage.update(rid, edited_text=body.text)
    return {"ok": True}


@app.delete("/api/records/{rid}")
def delete_record(rid: str):
    meta = _get(rid)
    if meta["status"] in ("queued", "processing"):
        raise HTTPException(409, "Дождись окончания обработки")
    storage.delete(rid)
    return {"ok": True}


@app.get("/api/records/{rid}/download/{kind}")
def download(rid: str, kind: str):
    meta = _get(rid)
    if kind in ("txt", "srt") and meta["status"] != "done":
        raise HTTPException(409, "Текст ещё не готов")
    if kind == "txt":
        return Response(
            _final_text(meta) + "\n",
            media_type="text/plain; charset=utf-8",
            headers=_attachment(f"whisper-{rid}.txt"),
        )
    if kind == "srt":
        return Response(
            to_srt(meta.get("segments", [])),
            media_type="application/x-subrip; charset=utf-8",
            headers=_attachment(f"whisper-{rid}.srt"),
        )
    if kind == "audio":
        path = storage.audio_path(rid, meta)
        if not path.exists():
            raise HTTPException(404, "Аудио не найдено")
        return FileResponse(
            path,
            media_type=meta.get("audio_mime") or "application/octet-stream",
            filename=f"whisper-{rid}{path.suffix}",
        )
    if kind == "mp3":
        path = _ensure_mp3(rid, meta)
        return FileResponse(path, media_type="audio/mpeg", filename=f"whisper-{rid}.mp3")
    raise HTTPException(404, "Неизвестный формат")


@app.get("/api/status")
def status():
    return {
        "engine": engine.status(),
        "pending": jobs.pending(),
        "max_duration": config.MAX_DURATION_SECONDS,
        "retention_days": config.RETENTION_DAYS,
    }


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
