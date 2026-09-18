from pathlib import Path

import pytest

from lyrictool import writer


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
