from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class LyricLine:
    timestamp_ms: int
    text: str
    end_ms: int | None = None


@dataclass(slots=True)
class StructureReport:
    valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timed_line_count: int = 0
    first_timestamp_ms: int | None = None
    last_timestamp_ms: int | None = None

    @property
    def summary(self) -> str:
        if self.valid:
            return "结构有效" if not self.warnings else f"结构有效，{len(self.warnings)} 项提醒"
        return "；".join(self.issues[:3]) or "结构无效"


@dataclass(slots=True)
class ParsedLyrics:
    raw_text: str
    lines: list[LyricLine]
    metadata: dict[str, str] = field(default_factory=dict)
    offset_ms: int = 0
    untimed_text_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LyricCandidate:
    source: str
    source_id: str
    title: str
    artist: str
    album: str
    duration: float | None
    lrc_text: str
    parsed: ParsedLyrics
    structure: StructureReport
    match_score: float = 0.0
    corrected_lrc_text: str | None = None
    correction_description: str = ""

    @property
    def effective_lrc_text(self) -> str:
        return self.corrected_lrc_text or self.lrc_text


@dataclass(slots=True)
class TranscriptToken:
    text: str
    start_ms: int
    end_ms: int


@dataclass(slots=True)
class VerificationResult:
    status: str
    summary: str
    content_similarity: float
    anchor_count: int
    coverage: float
    median_offset_ms: int | None = None
    residual_mad_ms: int | None = None
    slope: float | None = None
    corrected_lrc_text: str | None = None
    recognized_text: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in {"通过", "已自动校正"}


@dataclass(slots=True)
class TrackRecord:
    path: Path
    title: str
    artist: str
    album: str
    duration: float
    audio_type: str
    embedded_lyrics: str = ""
    external_lyrics: str = ""
    checked: bool = False
    candidates: list[LyricCandidate] = field(default_factory=list)
    selected_candidate: int | None = None
    search_status: str = "未搜索"
    verification: VerificationResult | None = None
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def current_lyrics(self) -> str:
        return self.external_lyrics or self.embedded_lyrics

    @property
    def lyric_status(self) -> str:
        if self.embedded_lyrics and self.external_lyrics:
            return "内嵌 + LRC"
        if self.embedded_lyrics:
            return "仅内嵌"
        if self.external_lyrics:
            return "仅 LRC"
        return "无歌词"

    @property
    def candidate(self) -> LyricCandidate | None:
        if self.selected_candidate is None:
            return None
        if 0 <= self.selected_candidate < len(self.candidates):
            return self.candidates[self.selected_candidate]
        return None
