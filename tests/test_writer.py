from pathlib import Path

import pytest

from lyrictool import writer
from lyrictool.settings import DataPaths


def test_commit_retries_a_transient_windows_file_lock(monkeypatch, tmp_path):
    source, target = tmp_path / "temporary.flac", tmp_path / "music.flac"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    replace = writer.os.replace
    attempts = []

    def locked_then_replace(src, dst):
        attempts.append(1)
        if len(attempts) <= 2:
            error = PermissionError(13, "file busy")
            error.winerror = 32
            raise error
        replace(src, dst)

    monkeypatch.setattr(writer.os, "replace", locked_then_replace)
    monkeypatch.setattr(writer.time, "sleep", lambda _: None)
    writer._commit_file(source, target)
    assert len(attempts) == 3
    assert target.read_bytes() == b"new"


def test_commit_does_not_mask_persistent_file_lock(monkeypatch, tmp_path):
    source, target = tmp_path / "temporary.flac", tmp_path / "music.flac"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    attempts = []

    def locked(*_args):
        attempts.append(1)
        error = PermissionError(13, "file busy")
        error.winerror = 32
        raise error

    monkeypatch.setattr(writer.os, "replace", locked)
    monkeypatch.setattr(writer.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        writer._commit_file(source, target)
    assert len(attempts) == 5
    assert target.read_bytes() == b"old"


def test_embedded_write_does_not_keep_an_audio_backup(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    paths = DataPaths(tmp_path / "data")
    monkeypatch.setattr(writer, "nonlyrics_snapshot", lambda _path: {"tags": "unchanged"})
    monkeypatch.setattr(writer, "_embed", lambda path, _lyrics: path.write_bytes(b"audio with lyrics"))
    monkeypatch.setattr(writer, "_verify_embedded", lambda _path, _lyrics: None)

    result = writer.write_outputs(audio, "[00:01.00]歌词", embed=True, create_lrc=False, data_paths=paths)

    assert result.success
    assert result.backup_dir is None
    assert audio.read_bytes() == b"audio with lyrics"
    assert not list(paths.backups_dir.rglob("*.flac"))
    assert not list(tmp_path.glob(".lyrics-tool-*"))


def test_failed_final_verification_restores_audio_without_a_persistent_backup(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    paths = DataPaths(tmp_path / "data")
    monkeypatch.setattr(writer, "nonlyrics_snapshot", lambda _path: {"tags": "unchanged"})
    monkeypatch.setattr(writer, "_embed", lambda path, _lyrics: path.write_bytes(b"audio with lyrics"))

    def verify(path, _lyrics):
        if path == audio:
            raise ValueError("final verification failed")

    monkeypatch.setattr(writer, "_verify_embedded", verify)

    result = writer.write_outputs(audio, "[00:01.00]歌词", embed=True, create_lrc=False, data_paths=paths)

    assert not result.success
    assert "已回滚" in result.message
    assert audio.read_bytes() == b"original audio"
    assert not list(paths.backups_dir.rglob("*.flac"))
    assert not list(tmp_path.glob(".lyrics-tool-*"))


def test_failed_audio_staging_never_replaces_the_original_with_an_empty_temp(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    paths = DataPaths(tmp_path / "data")
    monkeypatch.setattr(writer, "nonlyrics_snapshot", lambda _path: {"tags": "unchanged"})
    monkeypatch.setattr(writer, "_embed", lambda path, _lyrics: path.write_bytes(b"audio with lyrics"))
    monkeypatch.setattr(writer, "_verify_embedded", lambda _path, _lyrics: None)
    real_commit = writer._commit_file

    def fail_staging(source, target):
        if source == audio:
            raise PermissionError("audio is busy")
        real_commit(source, target)

    monkeypatch.setattr(writer, "_commit_file", fail_staging)

    result = writer.write_outputs(audio, "[00:01.00]歌词", embed=True, create_lrc=False, data_paths=paths)

    assert not result.success
    assert audio.read_bytes() == b"original audio"
    assert not list(tmp_path.glob(".lyrics-tool-*"))


def test_failed_rollback_preserves_the_only_original_audio_for_manual_recovery(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    paths = DataPaths(tmp_path / "data")
    monkeypatch.setattr(writer, "nonlyrics_snapshot", lambda _path: {"tags": "unchanged"})
    monkeypatch.setattr(writer, "_embed", lambda path, _lyrics: path.write_bytes(b"audio with lyrics"))

    def verify(path, _lyrics):
        if path == audio:
            raise ValueError("final verification failed")

    monkeypatch.setattr(writer, "_verify_embedded", verify)
    real_commit = writer._commit_file
    commits = []

    def fail_rollback(source, target):
        commits.append((source, target))
        if len(commits) == 3:
            raise PermissionError("rollback target is busy")
        real_commit(source, target)

    monkeypatch.setattr(writer, "_commit_file", fail_rollback)

    result = writer.write_outputs(audio, "[00:01.00]歌词", embed=True, create_lrc=False, data_paths=paths)

    recovery_files = list(tmp_path.glob(".lyrics-tool-*"))
    assert not result.success
    assert "请勿删除" in result.message
    assert len(recovery_files) == 1
    assert str(recovery_files[0]) in result.message
    assert recovery_files[0].read_bytes() == b"original audio"
    assert audio.read_bytes() == b"audio with lyrics"


def test_existing_lrc_backup_does_not_include_the_audio(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    lrc = tmp_path / "music.lrc"
    lrc.write_text("[00:01.00]旧歌词", encoding="utf-8")
    paths = DataPaths(tmp_path / "data")

    result = writer.write_outputs(audio, "[00:02.00]新歌词", embed=False, create_lrc=True, data_paths=paths)

    assert result.success
    assert result.backup_dir is not None
    assert [path.name for path in result.backup_dir.iterdir()] == ["music.lrc"]
    assert "新歌词" in lrc.read_text(encoding="utf-8-sig")


def test_lrc_verification_failure_restores_both_audio_and_old_lrc(monkeypatch, tmp_path):
    audio = tmp_path / "music.flac"
    audio.write_bytes(b"original audio")
    lrc = tmp_path / "music.lrc"
    lrc.write_text("[00:01.00]旧歌词", encoding="utf-8")
    paths = DataPaths(tmp_path / "data")
    new_lyrics = "[00:02.00]新歌词"
    monkeypatch.setattr(writer, "nonlyrics_snapshot", lambda _path: {"tags": "unchanged"})
    monkeypatch.setattr(writer, "_embed", lambda path, _lyrics: path.write_bytes(b"audio with lyrics"))
    monkeypatch.setattr(writer, "_verify_embedded", lambda _path, _lyrics: None)
    real_read = writer.read_text_file

    def fail_final_lrc_read(path):
        if path == lrc:
            return "corrupted"
        return real_read(path)

    monkeypatch.setattr(writer, "read_text_file", fail_final_lrc_read)

    result = writer.write_outputs(audio, new_lyrics, embed=True, create_lrc=True, data_paths=paths)

    assert not result.success
    assert audio.read_bytes() == b"original audio"
    assert "旧歌词" in lrc.read_text(encoding="utf-8")
    assert not list(paths.backups_dir.rglob("*.flac"))
    assert not list(tmp_path.glob(".lyrics-tool-*"))
