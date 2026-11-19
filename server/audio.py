import av
import numpy as np

from .errors import UserError

SAMPLE_RATE = 16000

UNREADABLE = "Не удалось прочитать файл. Возможно, этот формат не поддерживается."


def probe(path):
    try:
        with av.open(str(path)) as container:
            if not any(stream.type == "audio" for stream in container.streams):
                raise UserError("В файле нет звуковой дорожки")
            if container.duration:
                return container.duration / 1_000_000
    except UserError:
        raise
    except Exception as exc:
        raise UserError(UNREADABLE) from exc
    return None


def _frames(resampler, frame):
    out = resampler.resample(frame)
    if out is None:
        return []
    if isinstance(out, (list, tuple)):
        return out
    return [out]


def decode(path, rate=SAMPLE_RATE):
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
    chunks = []
    try:
        with av.open(str(path), mode="r") as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise UserError("В файле нет звуковой дорожки")
            for frame in container.decode(stream):
                frame.pts = None
                for piece in _frames(resampler, frame):
                    chunks.append(piece.to_ndarray().reshape(-1))
            for piece in _frames(resampler, None):
                chunks.append(piece.to_ndarray().reshape(-1))
    except UserError:
        raise
    except Exception as exc:
        raise UserError("Не удалось прочитать звук из файла.") from exc
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0
