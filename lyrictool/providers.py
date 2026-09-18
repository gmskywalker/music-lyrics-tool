from __future__ import annotations

import base64
import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable

import requests

from .lrc import metadata_similarity, normalize_text, parse_lrc, validate_structure
from .models import LyricCandidate, TrackRecord


USER_AGENT = "MusicLyricsTool/1.0 (+local Windows application)"
TIMEOUT = (2, 6)
MAX_DETAILS = 4
VERSION_MARKERS = {
    "live",
    "现场",
    "演唱会",
    "伴奏",
    "纯音乐",
    "instrumental",
    "翻唱",
    "cover",
    "remix",
    "mix版",
    "dj",
    "acoustic",
    "清唱",
    "特别版",
    "希望版",
    "demo",
    "粤语",
    "国语",
    "中文版",
    "英文版",
    "日语版",
}


@dataclass(slots=True)
class SearchOutcome:
    candidates: list[LyricCandidate]
    errors: list[str]
    rejected_count: int = 0


def _match_score(track: TrackRecord, title: str, artist: str, album: str, duration: float | None) -> float:
    score = 45.0 * metadata_similarity(track.title, title)
    if track.artist and artist:
        score += 28.0 * metadata_similarity(track.artist, artist)
    elif not track.artist:
        score += 14.0
    if track.album and album:
        score += 9.0 * metadata_similarity(track.album, album)
    elif not track.album:
        score += 4.5
    if duration:
        delta = abs(track.duration - duration)
        score += 18.0 * max(0.0, 1.0 - delta / max(12.0, track.duration * 0.08))
    return round(min(100.0, score), 1)


def _has_unrequested_version(track_title: str, candidate_title: str) -> bool:
    track_normalized = normalize_text(track_title)
    candidate_normalized = normalize_text(candidate_title)
    return any(
        normalize_text(marker) in candidate_normalized and normalize_text(marker) not in track_normalized
        for marker in VERSION_MARKERS
    )


def _candidate(
    track: TrackRecord,
    *,
    source: str,
    source_id: str,
    title: str,
    artist: str,
    album: str,
    duration: float | None,
    lrc_text: str,
) -> LyricCandidate | None:
    if not lrc_text or "[" not in lrc_text:
        return None
    if _has_unrequested_version(track.title, title):
        return None
    if metadata_similarity(track.title, title) < 0.60:
        return None
    if track.artist and artist and metadata_similarity(track.artist, artist) < 0.45:
        return None
    parsed = parse_lrc(lrc_text)
    report = validate_structure(
        parsed,
        audio_duration=track.duration,
        candidate_duration=duration,
        strict_online=True,
    )
    if not report.valid:
        return None
    score = _match_score(track, title, artist, album, duration)
    if score < 52.0:
        return None
    return LyricCandidate(
        source=source,
        source_id=str(source_id),
        title=title or track.title,
        artist=artist or track.artist,
        album=album,
        duration=duration,
        lrc_text=lrc_text.replace("\r\n", "\n").replace("\r", "\n"),
        parsed=parsed,
        structure=report,
        match_score=score,
    )


def _plausible(track: TrackRecord, title: str, artist: str, duration: float | None) -> bool:
    if _has_unrequested_version(track.title, title) or metadata_similarity(track.title, title) < 0.60:
        return False
    if track.artist and artist and metadata_similarity(track.artist, artist) < 0.45:
        return False
    return not duration or abs(track.duration - duration) <= max(8.0, track.duration * 0.04)


class Provider:
    name = "Provider"

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"})
        return session

    def search(self, track: TrackRecord) -> list[LyricCandidate]:
        raise NotImplementedError


class LrclibProvider(Provider):
    name = "LRCLIB"

    def search(self, track: TrackRecord) -> list[LyricCandidate]:
        session = self._session()
        params = {"track_name": track.title, "artist_name": track.artist}
        response = session.get("https://lrclib.net/api/search", params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        output: list[LyricCandidate] = []
        for item in payload[:20]:
            candidate = _candidate(
                track,
                source=self.name,
                source_id=str(item.get("id", "")),
                title=item.get("trackName") or item.get("name") or "",
                artist=item.get("artistName") or "",
                album=item.get("albumName") or "",
                duration=float(item["duration"]) if item.get("duration") else None,
                lrc_text=item.get("syncedLyrics") or "",
            )
            if candidate:
                output.append(candidate)
        return output


class NeteaseProvider(Provider):
    name = "网易云"

    def search(self, track: TrackRecord) -> list[LyricCandidate]:
        session = self._session()
        session.headers["Referer"] = "https://music.163.com/"
        response = session.get(
            "https://music.163.com/api/search/get/web",
            params={"s": f"{track.title} {track.artist}".strip(), "type": 1, "offset": 0, "limit": 12},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        songs = (response.json().get("result") or {}).get("songs") or []
        output: list[LyricCandidate] = []
        matching = []
        for song in songs:
            song_id = song.get("id")
            if not song_id:
                continue
            artists = "/".join(item.get("name", "") for item in song.get("artists") or [] if item.get("name"))
            duration = float(song["duration"]) / 1_000 if song.get("duration") else None
            if _plausible(track, song.get("name") or "", artists, duration):
                matching.append((song, artists, duration))
        for song, artists, duration in matching[:MAX_DETAILS]:
            song_id = song["id"]
            lyric_response = session.get(
                "https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                timeout=TIMEOUT,
            )
            lyric_response.raise_for_status()
            lyric = ((lyric_response.json().get("lrc") or {}).get("lyric")) or ""
            album_info = song.get("album") or {}
            candidate = _candidate(
                track,
                source=self.name,
                source_id=str(song_id),
                title=song.get("name") or "",
                artist=artists,
                album=album_info.get("name") or "",
                duration=duration,
                lrc_text=lyric,
            )
            if candidate:
                output.append(candidate)
        return output


class QQProvider(Provider):
    name = "QQ音乐"

    def search(self, track: TrackRecord) -> list[LyricCandidate]:
        session = self._session()
        session.headers["Referer"] = "https://y.qq.com/"
        response = session.get(
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={
                "w": f"{track.title} {track.artist}".strip(),
                "p": 1,
                "n": 12,
                "format": "json",
                "new_json": 1,
                "cr": 1,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        songs = (((payload.get("data") or {}).get("song") or {}).get("list")) or []
        output: list[LyricCandidate] = []
        matching = []
        for song in songs:
            mid = song.get("mid") or song.get("songmid")
            if not mid:
                continue
            artist = "/".join(item.get("name", "") for item in song.get("singer") or [] if item.get("name"))
            duration = float(song["interval"]) if song.get("interval") else None
            title = song.get("title") or song.get("songname") or ""
            if _plausible(track, title, artist, duration):
                matching.append((song, artist, duration))
        for song, artist, duration in matching[:MAX_DETAILS]:
            mid = song.get("mid") or song.get("songmid")
            lyric_response = session.get(
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
                params={"songmid": mid, "format": "json", "nobase64": 1},
                timeout=TIMEOUT,
            )
            lyric_response.raise_for_status()
            data = lyric_response.json()
            lyric = html.unescape(data.get("lyric") or "")
            album_info = song.get("album") or {}
            candidate = _candidate(
                track,
                source=self.name,
                source_id=str(mid),
                title=song.get("title") or song.get("songname") or "",
                artist=artist,
                album=album_info.get("name") or song.get("albumname") or "",
                duration=duration,
                lrc_text=lyric,
            )
            if candidate:
                output.append(candidate)
        return output


class KugouProvider(Provider):
    name = "酷狗"

    def search(self, track: TrackRecord) -> list[LyricCandidate]:
        session = self._session()
        session.headers["Referer"] = "https://www.kugou.com/"
        response = session.get(
            "https://songsearch.kugou.com/song_search_v2",
            params={
                "keyword": f"{track.title} {track.artist}".strip(),
                "page": 1,
                "pagesize": 12,
                "platform": "WebFilter",
                "filter": 2,
                "iscorrection": 1,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        songs = ((response.json().get("data") or {}).get("lists")) or []
        output: list[LyricCandidate] = []
        matching = []
        for song in songs:
            file_hash = song.get("FileHash") or song.get("EMixSongID") or ""
            duration = float(song.get("Duration") or 0) or None
            filename = re.sub(r"<[^>]+>", "", song.get("FileName") or "")
            artist = song.get("SingerName") or ""
            title = song.get("SongName") or filename.partition(" - ")[2] or filename
            if _plausible(track, title, artist, duration):
                matching.append((song, file_hash, duration, title, artist))
        for song, file_hash, duration, title, artist in matching[:MAX_DETAILS]:
            search_response = session.get(
                "https://lyrics.kugou.com/search",
                params={
                    "ver": 1,
                    "man": "yes",
                    "client": "pc",
                    "keyword": song.get("FileName") or f"{track.artist} - {track.title}",
                    "duration": int((duration or track.duration) * 1_000),
                    "hash": file_hash,
                },
                timeout=TIMEOUT,
            )
            search_response.raise_for_status()
            lyric_items = search_response.json().get("candidates") or []
            for lyric_item in lyric_items[:1]:
                download = session.get(
                    "https://lyrics.kugou.com/download",
                    params={
                        "ver": 1,
                        "client": "pc",
                        "id": lyric_item.get("id"),
                        "accesskey": lyric_item.get("accesskey"),
                        "fmt": "lrc",
                        "charset": "utf8",
                    },
                    timeout=TIMEOUT,
                )
                download.raise_for_status()
                content = download.json().get("content") or ""
                try:
                    lyric = base64.b64decode(content).decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    continue
                candidate = _candidate(
                    track,
                    source=self.name,
                    source_id=str(lyric_item.get("id", "")),
                    title=title,
                    artist=artist,
                    album=song.get("AlbumName") or "",
                    duration=duration,
                    lrc_text=lyric,
                )
                if candidate:
                    output.append(candidate)
        return output


PROVIDER_FACTORIES: dict[str, Callable[[], Provider]] = {
    "lrclib": LrclibProvider,
    "netease": NeteaseProvider,
    "qq": QQProvider,
    "kugou": KugouProvider,
}


class SearchContext:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.unavailable: set[str] = set()

    def is_unavailable(self, name: str) -> bool:
        with self._lock:
            return name in self.unavailable

    def mark_unavailable(self, name: str) -> None:
        with self._lock:
            self.unavailable.add(name)


def search_all(track: TrackRecord, enabled: list[str], context: SearchContext | None = None) -> SearchOutcome:
    candidates: list[LyricCandidate] = []
    errors: list[str] = []
    context = context or SearchContext()

    def run(names: list[str]) -> None:
        providers = [(name, PROVIDER_FACTORIES[name]()) for name in names
                     if name in PROVIDER_FACTORIES and not context.is_unavailable(name)]
        if not providers:
            return
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {pool.submit(provider.search, track): key for key, provider in providers}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    candidates.extend(future.result())
                except requests.RequestException:
                    context.mark_unavailable(key)
                    errors.append(f"{PROVIDER_FACTORIES[key].name} 暂时不可连接，本批跳过")
                except Exception as exc:
                    errors.append(f"{PROVIDER_FACTORIES[key].name}: {exc}")

    run([name for name in ("netease", "qq", "kugou") if name in enabled])
    if not candidates and "lrclib" in enabled:
        run(["lrclib"])

    unique: dict[str, LyricCandidate] = {}
    for candidate in candidates:
        digest = sha256(normalize_text(candidate.lrc_text).encode("utf-8")).hexdigest()
        current = unique.get(digest)
        if current is None or candidate.match_score > current.match_score:
            unique[digest] = candidate
    ordered = sorted(unique.values(), key=lambda item: (-item.match_score, item.source, item.title))
    return SearchOutcome(ordered, errors)
