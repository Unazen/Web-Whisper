def _timestamp(seconds):
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def to_srt(segments):
    lines = []
    for index, seg in enumerate(segments, 1):
        lines.append(str(index))
        lines.append(f"{_timestamp(seg['start'])} --> {_timestamp(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)
