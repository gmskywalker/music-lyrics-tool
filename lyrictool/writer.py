from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, SYLT, USLT
from mutagen.mp4 import MP4

from .lrc import normalize_text, parse_lrc
from .metadata import nonlyrics_snapshot, read_embedded_lyrics, read_text_file
from .settings import DataPaths


@dataclass(slots=True)
class WriteResult:
    path: Path
    success: bool
    message: str
    backup_dir: Path | None = None


def _embed(path: Path, lrc_text: str) -> None:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError("无法识别音频，未写入")
    if audio.tags is None:
        audio.add_tags()
    parsed = parse_lrc(lrc_text)

    if isinstance(audio.tags, ID3):
        audio.tags.delall("USLT")
        audio.tags.delall("SYLT")
        audio.tags.add(USLT(encoding=3, lang="chi", desc="Lyrics", text=lrc_text))
        synchronized = [(line.text, line.timestamp_ms) for line in parsed.lines if line.text.strip()]
        if synchronized:
            audio.tags.add(SYLT(encoding=3, lang="chi", format=2, type=1, desc="Lyrics", text=synchronized))
        audio.save(v2_version=3)
        return
    if isinstance(audio, MP4):
        audio.tags["\xa9lyr"] = [lrc_text]
        audio.save()
        return
    if isinstance(audio, ASF):
        audio.tags["WM/Lyrics"] = [lrc_text]
        audio.save()
        return
    if hasattr(audio.tags, "__setitem__"):
        audio.tags["LYRICS"] = lrc_text
        audio.tags["SYNCEDLYRICS"] = lrc_text
        audio.save()
        return
    raise ValueError(f"暂不支持向 {type(audio).__name__} 嵌入歌词")


def _verify_embedded(path: Path, expected: str) -> None:
    audio = MutagenFile(path, easy=False)
    actual = read_embedded_lyrics(audio)
    if normalize_text(actual) != normalize_text(expected):
        raise ValueError("写入后复读歌词不一致")


def _unique_backup_dir(paths: DataPaths, stem: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = paths.backups_dir / f"{stamp}-{stem}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def _temp_path(directory: Path, suffix: str) -> Path:
    handle, name = tempfile.mkstemp(prefix=".lyrics-tool-", suffix=suffix, dir=directory)
    os.close(handle)
    return Path(name)


def _commit_file(source: Path, target: Path) -> None:
    for delay in (0.2, 0.4, 0.8, 1.2, None):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            if delay is None:
                raise PermissionError(f"无法替换 {target.name}：文件仍被其他播放器或程序占用，或当前文件夹没有写入权限") from exc
            time.sleep(delay)


def write_outputs(
    audio_path: Path,
    lrc_text: str,
    *,
    embed: bool,
    create_lrc: bool,
    data_paths: DataPaths,
) -> WriteResult:
    if not embed and not create_lrc:
        return WriteResult(audio_path, False, "至少选择一种输出方式")
    data_paths.ensure()
    backup_dir: Path | None = None
    lrc_path = audio_path.with_suffix(".lrc")
    lrc_backup: Path | None = None
    audio_temp: Path | None = None
    audio_rollback: Path | None = None
    lrc_temp: Path | None = None
    original_snapshot: dict[str, Any] | None = None
    audio_staged = False
    lrc_committed = False

    try:
        if embed:
            original_snapshot = nonlyrics_snapshot(audio_path)
            audio_temp = _temp_path(audio_path.parent, audio_path.suffix)
            shutil.copy2(audio_path, audio_temp)
            _embed(audio_temp, lrc_text)
            _verify_embedded(audio_temp, lrc_text)
            if nonlyrics_snapshot(audio_temp) != original_snapshot:
                raise ValueError("写入校验发现原有标签、封面或音频时长发生变化")
        if create_lrc:
            if lrc_path.exists():
                backup_dir = _unique_backup_dir(data_paths, audio_path.stem)
                lrc_backup = backup_dir / lrc_path.name
                shutil.copy2(lrc_path, lrc_backup)
            lrc_temp = _temp_path(audio_path.parent, ".lrc")
            lrc_temp.write_text(lrc_text, encoding="utf-8-sig", newline="\n")
            if normalize_text(read_text_file(lrc_temp)) != normalize_text(lrc_text):
                raise ValueError("LRC 临时文件复读不一致")

        if embed and audio_temp:
            audio_rollback = _temp_path(audio_path.parent, audio_path.suffix)
            _commit_file(audio_path, audio_rollback)
            audio_staged = True
            _commit_file(audio_temp, audio_path)
            audio_temp = None
        if create_lrc and lrc_temp:
            _commit_file(lrc_temp, lrc_path)
            lrc_temp = None
            lrc_committed = True

        if embed:
            _verify_embedded(audio_path, lrc_text)
            if nonlyrics_snapshot(audio_path) != original_snapshot:
                raise ValueError("最终复读发现非歌词信息发生变化")
        if create_lrc and normalize_text(read_text_file(lrc_path)) != normalize_text(lrc_text):
            raise ValueError("最终 LRC 复读不一致")
        if audio_rollback:
            audio_rollback.unlink()
            audio_rollback = None
        return WriteResult(audio_path, True, "写入并复读验证成功", backup_dir)
    except Exception as exc:
        try:
            if audio_staged and audio_rollback and audio_rollback.exists():
                _commit_file(audio_rollback, audio_path)
                audio_rollback = None
            if lrc_committed:
                if lrc_backup and lrc_backup.exists():
                    restore_lrc = _temp_path(audio_path.parent, ".lrc")
                    shutil.copy2(lrc_backup, restore_lrc)
                    _commit_file(restore_lrc, lrc_path)
                elif lrc_path.exists():
                    lrc_path.unlink()
        except OSError as rollback_error:
            recovery_note = ""
            if audio_staged and audio_rollback and audio_rollback.exists():
                recovery_note = f"；原音频仍保留在 {audio_rollback}，请勿删除"
                audio_rollback = None
            return WriteResult(
                audio_path,
                False,
                f"写入失败：{exc}；回滚也失败：{rollback_error}{recovery_note}",
                backup_dir,
            )
        return WriteResult(audio_path, False, f"写入失败并已回滚：{exc}", backup_dir)
    finally:
        for temporary in (audio_temp, audio_rollback, lrc_temp):
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
