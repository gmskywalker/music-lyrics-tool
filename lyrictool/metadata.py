from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from mutagen import File as MutagenFile
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from .lrc import format_timestamp
from .models import TrackRecord


AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".m4a",
    ".mp4",
    ".aac",
    ".wma",
    ".asf",
    ".ogg",
    ".opus",
    ".ape",
    ".wav",
}


def _first(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


def _easy_value(tags: Any, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError):
            continue
        result = _first(value).strip()
        if result:
            return result
    return ""


def parse_filename(path: Path) -> tuple[str, str]:
    stem = path.stem.strip()
    for separator in (" - ", "-", "–", "—"):
        if separator in stem:
            artist, title = stem.split(separator, 1)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip()
    return "", stem


def find_external_lrc(path: Path) -> Path | None:
    direct = path.with_suffix(".lrc")
    if direct.exists():
        return direct
    target = direct.name.casefold()
    try:
        for sibling in path.parent.iterdir():
            if sibling.is_file() and sibling.name.casefold() == target:
                return sibling
    except OSError:
        return None
    return None


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_embedded_lyrics(audio: Any) -> str:
    tags = getattr(audio, "tags", None)
    if not tags:
        return ""
    if isinstance(tags, ID3):
        for frame in tags.getall("USLT"):
            if str(frame.text).strip():
                return str(frame.text)
        for frame in tags.getall("SYLT"):
            entries = getattr(frame, "text", None) or []
            if entries:
                return "\n".join(f"{format_timestamp(timestamp)}{text}" for text, timestamp in entries) + "\n"
    if isinstance(audio, MP4):
        return _first(tags.get("\xa9lyr")).strip()
    if isinstance(audio, ASF):
        return _first(tags.get("WM/Lyrics") or tags.get("Lyrics")).strip()
    for key in ("SYNCEDLYRICS", "LYRICS", "UNSYNCEDLYRICS"):
        try:
            value = tags.get(key)
        except (AttributeError, KeyError):
            continue
        text = _first(value).strip()
        if text:
            return text
    return ""


def inspect_track(path: Path) -> TrackRecord:
    audio = MutagenFile(path, easy=False)
    if audio is None or not getattr(audio, "info", None):
        raise ValueError("无法识别音频内容")
    easy = MutagenFile(path, easy=True)
    easy_tags = getattr(easy, "tags", None)
    raw_tags = getattr(audio, "tags", None)
    title = _easy_value(easy_tags, "title")
    artist = _easy_value(easy_tags, "artist", "albumartist")
    album = _easy_value(easy_tags, "album")

    if not title or not artist:
        filename_artist, filename_title = parse_filename(path)
        title = title or filename_title
        artist = artist or filename_artist

    external_path = find_external_lrc(path)
    external = read_text_file(external_path) if external_path else ""
    embedded = read_embedded_lyrics(audio)
    return TrackRecord(
        path=path.resolve(),
        title=title.strip(),
        artist=artist.strip(),
        album=album.strip(),
        duration=float(audio.info.length),
        audio_type=type(audio).__name__,
        embedded_lyrics=embedded,
        external_lyrics=external,
        extra={"extension": path.suffix.lower(), "raw_tag_type": type(raw_tags).__name__ if raw_tags else ""},
    )


def scan_folder(folder: Path, recursive: bool = False) -> tuple[list[TrackRecord], list[str]]:
    iterator: Iterable[Path] = folder.rglob("*") if recursive else folder.iterdir()
    paths = sorted(
        (path for path in iterator if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda value: value.name.casefold(),
    )
    records: list[TrackRecord] = []
    errors: list[str] = []
    for path in paths:
        try:
            records.append(inspect_track(path))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return records, errors


def scan_files(paths: Iterable[Path]) -> tuple[list[TrackRecord], list[str]]:
    records: list[TrackRecord] = []
    errors: list[str] = []
    for path in paths:
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            records.append(inspect_track(path))
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
    return records, errors


LYRIC_KEYS = {"lyrics", "unsyncedlyrics", "syncedlyrics", "wm/lyrics", "\xa9lyr"}


def nonlyrics_snapshot(path: Path) -> dict[str, Any]:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError("无法复读音频")
    tags = getattr(audio, "tags", None)
    result: dict[str, Any] = {
        "type": type(audio).__name__,
        "duration_ms": round(float(audio.info.length) * 1_000),
        "tags": [],
        "pictures": [],
    }
    if isinstance(tags, ID3):
        for frame in tags.values():
            if frame.FrameID in {"USLT", "SYLT"}:
                continue
            if frame.FrameID == "APIC":
                result["pictures"].append(hashlib.sha256(frame.data).hexdigest())
            else:
                result["tags"].append((frame.HashKey, str(frame)))
    elif tags:
        for key in sorted(tags.keys(), key=lambda item: str(item).casefold()):
            if str(key).casefold() in LYRIC_KEYS:
                continue
            value = tags[key]
            if str(key) == "covr":
                values = value if isinstance(value, list) else [value]
                result["pictures"].extend(hashlib.sha256(bytes(item)).hexdigest() for item in values)
            else:
                values = value if isinstance(value, list) else [value]
                result["tags"].append((str(key), tuple(str(item) for item in values)))
    if isinstance(audio, FLAC):
        result["pictures"].extend(hashlib.sha256(picture.data).hexdigest() for picture in audio.pictures)
    result["tags"] = sorted(result["tags"], key=lambda item: (item[0], str(item[1])))
    result["pictures"] = sorted(result["pictures"])
    return result
