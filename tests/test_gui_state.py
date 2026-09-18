from pathlib import Path
import threading

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from lyrictool.gui import ClickableSlider, LyricsPreviewList, MainWindow, TaskCancelled, TaskControl, TrackTableModel
from lyrictool.lrc import parse_lrc, validate_structure
from lyrictool.models import LyricCandidate, TrackRecord, VerificationResult
from lyrictool.settings import AppSettings, DataPaths
from lyrictool.theme import FluentThemeManager


def _app():
    return QApplication.instance() or QApplication([])


def _record(path: Path):
    return TrackRecord(path, path.stem, "歌手", "专辑", 180.0, "FLAC")


def test_at_least_one_output_option_stays_checked(tmp_path):
    app = _app()
    settings = AppSettings(embed_lyrics=True, create_lrc=True)
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), settings, manager)
    window.embed_checkbox.setChecked(False)
    window.lrc_checkbox.setChecked(False)
    assert window.embed_checkbox.isChecked() or window.lrc_checkbox.isChecked()
    window.deleteLater()


def test_rescan_preserves_existing_checks_but_not_new_tracks(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    first_path = tmp_path / "第一首.flac"
    second_path = tmp_path / "第二首.flac"
    old = _record(first_path)
    old.checked = True
    window.model.replace([old])
    window._replace_scan_result(([_record(first_path), _record(second_path)], []))
    assert window.model.records[0].checked is True
    assert window.model.records[1].checked is False
    window.deleteLater()


def test_table_checkbox_updates_record_state():
    _app()
    model = TrackTableModel()
    model.replace([_record(Path("测试.flac"))])
    index = model.index(0, 0)
    assert model.setData(index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert model.records[0].checked is True


def test_volume_setting_is_applied_and_updated(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    settings = AppSettings(playback_volume=35)
    window = MainWindow(DataPaths(tmp_path), settings, manager)
    assert window.volume_slider.value() == 35
    assert round(window.audio_output.volume() * 100) == 35
    window.volume_slider.setValue(70)
    assert settings.playback_volume == 70
    assert round(window.audio_output.volume() * 100) == 70
    window.deleteLater()


def test_preview_line_and_global_timing_edits_use_tenth_second_steps(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    text = "[00:01.000]第一句测试歌词\n[00:02.000]第二句测试歌词\n[00:03.000]第三句测试歌词\n[00:04.000]第四句测试歌词\n[00:05.000]第五句测试歌词\n"
    parsed = parse_lrc(text)
    structure = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
    record.candidates.append(LyricCandidate("测试", "1", record.title, record.artist, record.album, 180, text, parsed, structure, 100))
    record.selected_candidate = 0
    window.model.replace([record])
    window.table.selectRow(0)
    window.preview_source.setCurrentIndex(window.preview_source.findData("candidate"))
    window.preview_lines.setCurrentRow(0)
    window._shift_preview_line(100)
    assert parse_lrc(record.candidate.lrc_text).lines[0].timestamp_ms == 1100
    window._shift_preview_all(-100)
    assert [line.timestamp_ms for line in parse_lrc(record.candidate.lrc_text).lines] == [1000, 1900, 2900, 3900, 4900]
    window.deleteLater()


def test_preview_history_and_anchor_shift_keep_credits(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    text = "[00:00.000]作词：甲\n[00:01.000]编曲：乙\n[00:25.000]第一句歌词测试\n[00:30.000]第二句歌词测试\n[00:35.000]第三句歌词测试\n[00:40.000]第四句歌词测试\n"
    parsed = parse_lrc(text)
    record.candidates.append(LyricCandidate("测试", "1", record.title, record.artist, record.album, 180, text, parsed, validate_structure(parsed, audio_duration=record.duration), 100))
    record.selected_candidate = 0
    window.model.replace([record])
    window.table.selectRow(0)
    window.detail_tabs.setCurrentIndex(3)
    window.preview_source.setCurrentIndex(window.preview_source.findData("candidate"))
    window.preview_lines.setCurrentRow(3)
    window.shift_with_current.setChecked(True)
    window.player.position = lambda: 31_000
    window._set_preview_line_to_current_position()
    assert [line.timestamp_ms for line in parse_lrc(record.candidate.lrc_text).lines] == [0, 1000, 26000, 31000, 36000, 41000]
    assert window.preview_undo_button.isEnabled()
    window._undo_edit()
    assert [line.timestamp_ms for line in parse_lrc(record.candidate.lrc_text).lines] == [0, 1000, 25000, 30000, 35000, 40000]
    window._redo_edit()
    assert [line.timestamp_ms for line in parse_lrc(record.candidate.lrc_text).lines] == [0, 1000, 26000, 31000, 36000, 41000]
    window.deleteLater()


def test_preview_edit_does_not_discard_unsaved_editor_changes(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    text = "[00:01.000]第一句测试歌词\n[00:02.000]第二句测试歌词\n[00:03.000]第三句测试歌词\n[00:04.000]第四句测试歌词\n"
    parsed = parse_lrc(text)
    record.candidates.append(LyricCandidate("测试", "1", record.title, record.artist, record.album, 180, text, parsed, validate_structure(parsed, audio_duration=record.duration), 100))
    record.selected_candidate = 0
    window.model.replace([record])
    window.table.selectRow(0)
    window.preview_source.setCurrentIndex(window.preview_source.findData("candidate"))
    window.editor.insertPlainText("尚未保存")
    monkeypatch.setattr("lyrictool.gui.QMessageBox.information", lambda *_args: None)
    window._shift_preview_all(1_000)
    assert "尚未保存" in window.editor.toPlainText()
    assert record.candidate.lrc_text == text
    window.deleteLater()


def test_write_unloads_media_source_before_starting_worker(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    text = "[00:01.000]第一句歌词测试\n[00:02.000]第二句歌词测试\n[00:03.000]第三句歌词测试\n[00:04.000]第四句歌词测试\n"
    parsed = parse_lrc(text)
    record.candidates.append(LyricCandidate("测试", "1", record.title, record.artist, record.album, 180, text, parsed, validate_structure(parsed, audio_duration=record.duration), 100))
    record.selected_candidate = 0
    record.verification = VerificationResult("通过", "", 1, 8, 0.8)
    window.model.replace([record])
    window.table.selectRow(0)
    assert Path(window.player.source().toLocalFile()) == record.path
    observed = []
    monkeypatch.setattr(window, "_start_task", lambda *_args: observed.append((window.player.source().isEmpty(), window.player.audioOutput() is None)))
    window._write_records([record])
    assert observed == [(True, True)]
    window._task_finished()
    assert Path(window.player.source().toLocalFile()) == record.path
    window.deleteLater()


def test_unverified_write_confirmation_can_be_remembered_for_session(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    calls = []

    def accept_and_remember(dialog):
        calls.append(dialog.windowTitle())
        dialog.checkBox().setChecked(True)
        return QMessageBox.StandardButton.Yes.value

    monkeypatch.setattr(QMessageBox, "exec", accept_and_remember)
    assert window._confirm_unverified_write(["第一首.flac"])
    assert window._allow_unverified_writes_this_session
    assert window._confirm_unverified_write(["第二首.flac"])
    assert calls == ["音频复核尚未通过"]
    window.deleteLater()


def test_cancel_does_not_remember_unverified_write_confirmation(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    calls = []

    def cancel_with_checkbox(dialog):
        calls.append(dialog.windowTitle())
        dialog.checkBox().setChecked(True)
        return QMessageBox.StandardButton.Cancel.value

    monkeypatch.setattr(QMessageBox, "exec", cancel_with_checkbox)
    assert not window._confirm_unverified_write(["第一首.flac"])
    assert not window._allow_unverified_writes_this_session
    assert not window._confirm_unverified_write(["第二首.flac"])
    assert len(calls) == 2
    window.deleteLater()


def test_editor_timeline_shift_can_be_undone_and_redone(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    text = "[00:01.000]第一句测试歌词\n[00:02.000]第二句测试歌词\n[00:03.000]第三句测试歌词\n[00:04.000]第四句测试歌词\n"
    parsed = parse_lrc(text)
    structure = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
    record.candidates.append(LyricCandidate("测试", "1", record.title, record.artist, record.album, 180, text, parsed, structure, 100))
    record.selected_candidate = 0
    window.model.replace([record])
    window.table.selectRow(0)
    original = window.editor.toPlainText()
    window.apply_offset(1_000)
    shifted = window.editor.toPlainText()
    assert shifted != original
    assert window.editor.document().isUndoAvailable()
    window.editor.undo()
    assert window.editor.toPlainText() == original
    window.editor.redo()
    assert window.editor.toPlainText() == shifted
    window.deleteLater()


def test_clickable_slider_jumps_to_clicked_position():
    _app()
    slider = ClickableSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 1_000)
    slider.resize(200, 24)
    slider.show()
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(150, 12))
    assert 700 <= slider.value() <= 800
    slider.deleteLater()


def test_preview_list_stops_following_while_user_browses_and_double_click_seeks(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    window._preview_timestamps = [1_000, 2_000, 3_000]
    window.preview_lines.addItems(["第一句", "第二句", "第三句"])
    window.preview_lines.setCurrentRow(0)
    window._pause_preview_follow()
    window._playback_position(3_000)
    assert window.preview_lines.currentRow() == 0
    positions = []
    window.player.setPosition = positions.append
    window._preview_line_activated(2)
    assert positions == [3_000]
    assert window._preview_follow_playback
    assert window.preview_lines.currentRow() == 2
    window.deleteLater()


def test_double_click_seek_continues_following_into_next_lyric(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    window._preview_timestamps = [1_000, 2_000, 3_000]
    window.preview_lines.addItems(["第一句", "第二句", "第三句"])
    window._pause_preview_follow()
    window.player.setPosition = lambda _position: None
    window._preview_line_activated(1)
    assert window._preview_follow_playback
    assert window.preview_lines.currentRow() == 1
    window._playback_position(3_100)
    assert window.preview_lines.currentRow() == 2
    window.deleteLater()


def test_preview_follow_resumes_after_manual_browsing_timeout(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    window._preview_timestamps = [1_000, 2_000, 3_000]
    window.preview_lines.addItems(["第一句", "第二句", "第三句"])
    window.preview_lines.setCurrentRow(0)
    clock = [100.0]
    monkeypatch.setattr("lyrictool.gui.time.monotonic", lambda: clock[0])
    window._pause_preview_follow()
    window._playback_position(3_000)
    assert window.preview_lines.currentRow() == 0
    clock[0] = 104.1
    window._playback_position(3_000)
    assert window._preview_follow_playback
    assert window.preview_lines.currentRow() == 2
    window.deleteLater()


def test_follow_scrolls_current_lyric_back_into_view_even_when_row_is_unchanged(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    window.preview_lines.resize(260, 90)
    timestamps = [index * 1_000 for index in range(30)]
    window._preview_timestamps = timestamps
    window.preview_lines.addItems([f"第 {index} 句" for index in range(30)])
    window.preview_lines.setCurrentRow(2)
    window.preview_lines.scrollToBottom()
    app.processEvents()
    assert not window.preview_lines.viewport().rect().contains(window.preview_lines.visualItemRect(window.preview_lines.item(2)))
    window._resume_preview_follow()
    window._playback_position(2_500)
    app.processEvents()
    assert window.preview_lines.viewport().rect().contains(window.preview_lines.visualItemRect(window.preview_lines.item(2)))
    window.deleteLater()


def test_preview_list_double_click_reports_visible_row():
    _app()
    widget = LyricsPreviewList()
    widget.addItems(["第一句", "第二句", "第三句"])
    widget.resize(240, 120)
    widget.show()
    activated = []
    widget.row_activated.connect(activated.append)
    point = widget.visualItemRect(widget.item(1)).center()
    QTest.mouseDClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert activated == [1]
    widget.deleteLater()


def test_audio_verification_does_not_block_playback_or_track_selection(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    window.model.replace([record])
    window.table.selectRow(0)
    played = []
    window.player.play = lambda: played.append(True)
    window.busy = True
    window.active_task_kind = "verify"
    window._update_actions()
    assert window.table.isEnabled()
    assert window.play_button.isEnabled()
    assert window.seek_slider.isEnabled()
    window._toggle_playback()
    assert played == [True]
    window.deleteLater()


def test_completed_track_can_start_current_write_while_later_tracks_are_verifying(tmp_path, monkeypatch):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "已完成.flac")
    text = "[00:01.000]第一句歌词测试\n[00:02.000]第二句歌词测试\n[00:03.000]第三句歌词测试\n[00:04.000]第四句歌词测试\n"
    parsed = parse_lrc(text)
    record.candidates.append(
        LyricCandidate(
            "测试", "1", record.title, record.artist, record.album, 180, text, parsed,
            validate_structure(parsed, audio_duration=record.duration), 100,
        )
    )
    record.selected_candidate = 0
    record.verification = VerificationResult("通过", "", 1, 8, 0.8)
    window.model.replace([record])
    window.table.selectRow(0)
    window.busy = True
    window.active_task_kind = "verify"
    window._verification_pending_paths = {str(tmp_path / "后面的歌曲.flac")}
    started = []
    monkeypatch.setattr(window.pool, "start", started.append)
    window._update_actions()
    assert window.write_current_button.isEnabled()
    window.write_current_candidate()
    assert len(started) == 1
    assert window._inline_write_in_progress
    assert window.player.source().isEmpty()
    assert window.player.audioOutput() is None
    window._task_finished()
    assert window.player.source().isEmpty()
    assert not window.search_button.isEnabled()
    assert not window.verify_button.isEnabled()
    assert not window.write_button.isEnabled()
    window._inline_write_finished()
    assert Path(window.player.source().toLocalFile()) == record.path
    window.deleteLater()


def test_file_writing_still_blocks_playback_and_track_selection(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "测试.flac")
    window.model.replace([record])
    window.table.selectRow(0)
    played = []
    window.player.play = lambda: played.append(True)
    window.busy = True
    window.active_task_kind = "write"
    window._update_actions()
    assert not window.table.isEnabled()
    assert not window.play_button.isEnabled()
    assert not window.seek_slider.isEnabled()
    window._toggle_playback()
    assert not played
    window.deleteLater()


def test_task_control_pause_resume_and_cancel():
    control = TaskControl()
    control.set_paused(True)
    assert control.paused
    control.set_paused(False)
    control.checkpoint()
    control.cancel()
    try:
        control.checkpoint()
    except TaskCancelled:
        pass
    else:
        raise AssertionError("cancelled task did not stop at checkpoint")


def test_task_control_releases_paused_worker_on_resume():
    control = TaskControl()
    control.set_paused(True)
    completed = threading.Event()

    def run_checkpoint():
        control.checkpoint()
        completed.set()

    worker = threading.Thread(target=run_checkpoint)
    worker.start()
    assert not completed.wait(0.05)
    control.set_paused(False)
    assert completed.wait(1)
    worker.join(timeout=1)


def test_select_verified_includes_passed_and_auto_corrected(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    records = [_record(tmp_path / f"{index}.flac") for index in range(3)]
    records[0].verification = VerificationResult("通过", "", 1, 8, 0.8)
    records[1].verification = VerificationResult("已自动校正", "", 1, 8, 0.8)
    records[2].verification = VerificationResult("疑似异常", "", 1, 8, 0.8)
    window.model.replace(records)
    window._set_checks("verified")
    assert [record.checked for record in records] == [True, True, False]
    window.deleteLater()


def test_better_candidate_is_selected_after_verification(tmp_path):
    app = _app()
    manager = FluentThemeManager(app, "light")
    manager.apply()
    window = MainWindow(DataPaths(tmp_path), AppSettings(), manager)
    record = _record(tmp_path / "候选测试.flac")
    text = "[00:01.000]第一句测试歌词\n[00:02.000]第二句测试歌词\n[00:03.000]第三句测试歌词\n[00:04.000]第四句测试歌词\n"
    parsed = parse_lrc(text)
    structure = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
    record.candidates = [
        LyricCandidate("来源一", "1", record.title, record.artist, record.album, 180, text, parsed, structure, 90),
        LyricCandidate("来源二", "2", record.title, record.artist, record.album, 180, text, parsed, structure, 91),
    ]
    record.selected_candidate = 0
    window.model.replace([record])
    window.table.selectRow(0)
    results = [
        VerificationResult("低置信度", "", 0.30, 4, 0.3),
        VerificationResult("通过", "", 0.80, 10, 0.7),
    ]
    window._apply_verification_result(record, (1, results))
    assert record.selected_candidate == 1
    assert record.verification is results[1]
    window.deleteLater()
