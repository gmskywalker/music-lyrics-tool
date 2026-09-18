from __future__ import annotations

import re
import statistics
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher

from .models import LyricLine, ParsedLyrics, StructureReport


TIME_TAG_RE = re.compile(r"\[(?:(\d{1,2}):)?(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
SIMPLE_TIME_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
META_RE = re.compile(r"^\[([A-Za-z][A-Za-z0-9_-]*):(.*)\]$")
WORD_TAG_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
PUNCT_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
CREDIT_RE = re.compile(
    r"(?:作词|填词|词\s*[:：]|作曲|曲\s*[:：]|编曲|制作人|监制|混音|母带|录音|演唱|歌手\s*[:：])",
    re.IGNORECASE,
)
_OPENCC = None


def _fraction_to_ms(value: str | None) -> int:
    if not value:
        return 0
    if len(value) == 1:
        return int(value) * 100
    if len(value) == 2:
        return int(value) * 10
    return int(value[:3])


def parse_timestamp(tag: str) -> int | None:
    match = TIME_TAG_RE.fullmatch(tag)
    if not match:
        simple = SIMPLE_TIME_RE.fullmatch(tag)
        if not simple:
            return None
        minutes, seconds, fraction = simple.groups()
        if int(seconds) >= 60:
            return None
        return int(minutes) * 60_000 + int(seconds) * 1_000 + _fraction_to_ms(fraction)
    hours, minutes, seconds, fraction = match.groups()
    if int(seconds) >= 60 or (hours is not None and int(minutes) >= 60):
        return None
    total_minutes = int(minutes) + (int(hours) * 60 if hours else 0)
    return total_minutes * 60_000 + int(seconds) * 1_000 + _fraction_to_ms(fraction)


def parse_lrc(text: str) -> ParsedLyrics:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    metadata: dict[str, str] = {}
    lines: list[LyricLine] = []
    untimed: list[str] = []

    for raw_line in normalized.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        meta = META_RE.fullmatch(stripped)
        if meta and not SIMPLE_TIME_RE.match(stripped):
            metadata[meta.group(1).lower()] = meta.group(2).strip()
            continue
        tags = [m.group(0) for m in TIME_TAG_RE.finditer(stripped)]
        if not tags:
            if not stripped.startswith("[") or "]" not in stripped:
                untimed.append(WORD_TAG_RE.sub("", stripped).strip())
            else:
                untimed.append(stripped)
            continue
        lyric_text = TIME_TAG_RE.sub("", stripped)
        lyric_text = WORD_TAG_RE.sub("", lyric_text).strip()
        for tag in tags:
            timestamp = parse_timestamp(tag)
            if timestamp is not None:
                lines.append(LyricLine(timestamp, lyric_text))

    try:
        offset_ms = int(metadata.get("offset", "0"))
    except ValueError:
        offset_ms = 0
    if offset_ms:
        lines = [replace(line, timestamp_ms=line.timestamp_ms + offset_ms) for line in lines]

    return ParsedLyrics(normalized, lines, metadata, offset_ms, [x for x in untimed if x])


def validate_structure(
    parsed: ParsedLyrics,
    *,
    audio_duration: float | None = None,
    candidate_duration: float | None = None,
    strict_online: bool = True,
) -> StructureReport:
    issues: list[str] = []
    warnings: list[str] = []
    nonempty = [line for line in parsed.lines if normalize_text(line.text)]

    if len(nonempty) < 4:
        issues.append("有效时间歌词不足 4 行")
    total_chars = sum(len(normalize_text(line.text)) for line in nonempty)
    if total_chars < 20:
        issues.append("有效歌词内容过少")
    if parsed.offset_ms and not (-120_000 <= parsed.offset_ms <= 120_000):
        issues.append("offset 超出合理范围")
    if any(line.timestamp_ms < 0 for line in parsed.lines):
        issues.append("应用 offset 后出现负时间戳")

    timestamps = [line.timestamp_ms for line in nonempty]
    if any(current < previous for previous, current in zip(timestamps, timestamps[1:])):
        issues.append("时间戳存在倒序")

    duplicate_groups = 0
    for previous, current in zip(timestamps, timestamps[1:]):
        if current == previous:
            duplicate_groups += 1
    if duplicate_groups > max(4, len(timestamps) // 3):
        issues.append("重复时间戳过多")
    elif duplicate_groups:
        warnings.append(f"存在 {duplicate_groups} 处同时间戳歌词")

    if parsed.untimed_text_lines:
        ratio = len(parsed.untimed_text_lines) / max(1, len(parsed.untimed_text_lines) + len(nonempty))
        if strict_online and ratio > 0.35:
            issues.append("未带时间戳的歌词行过多")
        else:
            warnings.append(f"有 {len(parsed.untimed_text_lines)} 行未带时间戳")

    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    if audio_duration and last is not None:
        duration_ms = round(audio_duration * 1_000)
        allowed_tail = max(5_000, round(duration_ms * 0.03))
        if last > duration_ms + allowed_tail:
            issues.append("歌词时间戳明显超过音频时长")
        if first is not None and first > min(90_000, duration_ms * 0.45):
            warnings.append("第一句歌词出现得较晚")
        if last < duration_ms * 0.25:
            warnings.append("歌词只覆盖了音频前段")

    if audio_duration and candidate_duration:
        delta = abs(audio_duration - candidate_duration)
        allowed = max(8.0, audio_duration * 0.04)
        if delta > allowed:
            issues.append(f"候选时长与音频相差 {delta:.1f} 秒")

    return StructureReport(
        valid=not issues,
        issues=issues,
        warnings=warnings,
        timed_line_count=len(nonempty),
        first_timestamp_ms=first,
        last_timestamp_ms=last,
    )


def normalize_text(text: str) -> str:
    global _OPENCC
    value = unicodedata.normalize("NFKC", text).lower()
    value = WORD_TAG_RE.sub("", value)
    if any("\u3400" <= character <= "\u9fff" for character in value):
        try:
            if _OPENCC is None:
                from opencc import OpenCC

                _OPENCC = OpenCC("t2s")
            value = _OPENCC.convert(value)
        except ImportError:
            pass
    return PUNCT_RE.sub("", value)


def metadata_similarity(left: str, right: str) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.88
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def format_timestamp(timestamp_ms: int) -> str:
    timestamp_ms = max(0, int(round(timestamp_ms)))
    minutes, remainder = divmod(timestamp_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]"


def serialize_lrc(parsed: ParsedLyrics, transform=None) -> str:
    output: list[str] = []
    for key in ("ar", "ti", "al", "by"):
        if parsed.metadata.get(key):
            output.append(f"[{key}:{parsed.metadata[key]}]")
    for line in parsed.lines:
        timestamp = transform(line.timestamp_ms) if transform else line.timestamp_ms
        output.append(f"{format_timestamp(timestamp)}{line.text}")
    return "\n".join(output).rstrip() + "\n"


def shift_lrc(text: str, offset_ms: int) -> str:
    parsed = parse_lrc(text)
    parsed.metadata.pop("offset", None)
    return serialize_lrc(parsed, lambda value: value + offset_ms)


def shift_lyric_timeline(text: str, offset_ms: int) -> str:
    """Move sung lyrics while keeping opening credit lines in their title block."""
    return _transform_lyric_timeline(text, lambda timestamp: timestamp + offset_ms)


def affine_lyric_timeline(text: str, slope: float, intercept_ms: float) -> str:
    return _transform_lyric_timeline(text, lambda timestamp: timestamp * slope + intercept_ms)


def opening_credit_end(parsed: ParsedLyrics) -> int:
    first_credit = next(
        (index for index, line in enumerate(parsed.lines) if line.timestamp_ms <= 45_000 and CREDIT_RE.search(line.text)),
        None,
    )
    if first_credit is None:
        return -1
    last_credit = parsed.lines[first_credit].timestamp_ms
    for line in parsed.lines[first_credit + 1:]:
        if line.timestamp_ms > 45_000 or (normalize_text(line.text) and not CREDIT_RE.search(line.text)):
            break
        if CREDIT_RE.search(line.text):
            last_credit = line.timestamp_ms
    return last_credit


def _transform_lyric_timeline(text: str, transform) -> str:
    parsed = parse_lrc(text)
    parsed.metadata.pop("offset", None)
    credit_block_end = opening_credit_end(parsed)
    credit_indexes = {
        index for index, line in enumerate(parsed.lines) if line.timestamp_ms <= credit_block_end
    }
    last_credit = max((parsed.lines[index].timestamp_ms for index in credit_indexes), default=-100)
    first_body_time = last_credit + 100
    body_indexes = [index for index in range(len(parsed.lines)) if index not in credit_indexes]
    desired_times = [round(transform(parsed.lines[index].timestamp_ms)) for index in body_indexes]
    boundary_adjustment = max(0, first_body_time - min(desired_times, default=first_body_time))
    for index, desired in zip(body_indexes, desired_times):
        parsed.lines[index].timestamp_ms = desired + boundary_adjustment
    parsed.lines.sort(key=lambda line: line.timestamp_ms)
    return serialize_lrc(parsed)


def affine_lrc(text: str, slope: float, intercept_ms: float) -> str:
    parsed = parse_lrc(text)
    parsed.metadata.pop("offset", None)
    return serialize_lrc(parsed, lambda value: value * slope + intercept_ms)


def estimate_consensus_offset(first: ParsedLyrics, second: ParsedLyrics) -> tuple[int, int] | None:
    pairs: list[int] = []
    second_lines = [(normalize_text(line.text), line.timestamp_ms) for line in second.lines]
    for line in first.lines:
        text = normalize_text(line.text)
        if len(text) < 2:
            continue
        matches = [time for other, time in second_lines if other == text]
        if len(matches) == 1:
            pairs.append(matches[0] - line.timestamp_ms)
    if len(pairs) < 4:
        return None
    median = int(statistics.median(pairs))
    mad = int(statistics.median(abs(value - median) for value in pairs))
    return median, mad
