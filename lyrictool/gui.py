from __future__ import annotations

import base64
import logging
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRunnable, QThreadPool, Qt, Signal, Slot, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyleOptionSlider,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .lrc import opening_credit_end, parse_lrc, serialize_lrc, shift_lyric_timeline, validate_structure
from .metadata import AUDIO_EXTENSIONS, inspect_track, read_text_file, scan_files, scan_folder
from .models import LyricCandidate, TrackRecord, VerificationResult
from .providers import SearchContext, SearchOutcome, search_all
from .settings import AppSettings, DataPaths, executable_dir, save_settings
from .theme import FluentThemeManager, set_fluent_property
from .verifier import verification_rank, verify_audio_candidates
from .writer import WriteResult, write_outputs


LOGGER = logging.getLogger("lyrics_tool")


class ClickableSlider(QSlider):
    seek_requested = Signal(int)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider,
                option,
                QStyle.SubControl.SC_SliderHandle,
                self,
            )
            if handle.contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return
            slider_length = handle.width()
            span = max(1, self.width() - slider_length)
            position = round(event.position().x() - slider_length / 2)
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), position, span, option.upsideDown
            )
            self.setValue(value)
            self.seek_requested.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)


class LyricsPreviewList(QListWidget):
    user_interacted = Signal()
    row_activated = Signal(int)

    def mousePressEvent(self, event) -> None:
        self.user_interacted.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if item is not None:
            self.row_activated.emit(self.row(item))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:
        self.user_interacted.emit()
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        self.user_interacted.emit()
        super().keyPressEvent(event)


class TrackTableModel(QAbstractTableModel):
    checked_changed = Signal()
    COLUMNS = ["选择", "文件名", "标题", "艺术家", "时长", "现有歌词", "联网结果", "音频复核"]

    def __init__(self) -> None:
        super().__init__()
        self.records: list[TrackRecord] = []

    def rowCount(self, _parent=QModelIndex()) -> int:
        return len(self.records)

    def columnCount(self, _parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.records)):
            return None
        record = self.records[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            return Qt.CheckState.Checked if record.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            values = [
                "",
                record.path.name,
                record.title or "（未知）",
                record.artist or "（未知）",
                _format_duration(record.duration),
                record.lyric_status,
                record.search_status,
                record.verification.status if record.verification else "未复核",
            ]
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            if index.column() == 1:
                return str(record.path)
            if index.column() == 6 and record.error:
                return record.error
            if index.column() == 7 and record.verification:
                return record.verification.summary
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in {0, 4}:
            return Qt.AlignmentFlag.AlignCenter
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            self.records[index.row()].checked = value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            self.checked_changed.emit()
            return True
        return False

    def replace(self, records: list[TrackRecord]) -> None:
        self.beginResetModel()
        self.records = records
        self.endResetModel()
        self.checked_changed.emit()

    def refresh(self, row: int | None = None) -> None:
        if not self.records:
            return
        first = 0 if row is None else row
        last = len(self.records) - 1 if row is None else row
        self.dataChanged.emit(self.index(first, 0), self.index(last, self.columnCount() - 1))
        self.checked_changed.emit()


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    progress = Signal(object)
    cancelled = Signal(object)
    finished = Signal()


class TaskCancelled(Exception):
    def __init__(self, partial_result=None) -> None:
        super().__init__("task cancelled")
        self.partial_result = partial_result


class TaskControl:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._paused = False
        self._cancelled = False

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    def set_paused(self, paused: bool) -> None:
        with self._condition:
            self._paused = paused
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._paused = False
            self._condition.notify_all()

    def checkpoint(self) -> None:
        with self._condition:
            while self._paused and not self._cancelled:
                self._condition.wait(timeout=0.25)
            if self._cancelled:
                raise TaskCancelled()


class Worker(QRunnable):
    def __init__(self, function: Callable[[Callable[[object], None], TaskControl], object], control: TaskControl) -> None:
        super().__init__()
        self.function = function
        self.control = control
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(self.signals.progress.emit, self.control)
            self.signals.result.emit(result)
        except TaskCancelled as exc:
            self.signals.cancelled.emit(exc.partial_result)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget, settings: AppSettings, paths: DataPaths, theme_manager: FluentThemeManager) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(480)
        self.settings = settings
        self.paths = paths
        self.theme_manager = theme_manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        source_group = QGroupBox("联网歌词来源")
        source_layout = QGridLayout(source_group)
        self.lrclib = QCheckBox("LRCLIB（备用：国内均无结果才尝试）")
        self.netease = QCheckBox("网易云音乐")
        self.qq = QCheckBox("QQ 音乐")
        self.kugou = QCheckBox("酷狗音乐")
        self.lrclib.setChecked(settings.provider_lrclib)
        self.netease.setChecked(settings.provider_netease)
        self.qq.setChecked(settings.provider_qq)
        self.kugou.setChecked(settings.provider_kugou)
        source_layout.addWidget(self.netease, 0, 0)
        source_layout.addWidget(self.qq, 0, 1)
        source_layout.addWidget(self.kugou, 1, 0)
        source_layout.addWidget(self.lrclib, 1, 1)
        layout.addWidget(source_group)

        verify_group = QGroupBox("音频识别与误差")
        verify_layout = QGridLayout(verify_group)
        self.model_combo = QComboBox()
        self.model_combo.addItem("tiny（较快，仅用于快速检查）", "tiny")
        self.model_combo.addItem("base（平衡）", "base")
        self.model_combo.addItem("small（推荐，中文更可靠）", "small")
        self.model_combo.addItem("medium（约 1.5 GB，复核较慢）", "medium")
        self.model_combo.addItem("large-v3（约 3 GB，需要较多显存）", "large-v3")
        model_index = max(0, self.model_combo.findData(settings.model_name))
        self.model_combo.setCurrentIndex(model_index)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.5, 5.0)
        self.tolerance.setSingleStep(0.1)
        self.tolerance.setSuffix(" 秒")
        self.tolerance.setValue(settings.tolerance_seconds)
        self.language_combo = QComboBox()
        self.language_combo.addItem("自动（由音频模型判断，推荐）", "auto")
        self.language_combo.addItem("国语", "zh")
        self.language_combo.addItem("粤语", "yue")
        self.language_combo.addItem("英语", "en")
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(settings.recognition_language)))
        self.device_combo = QComboBox()
        self.device_combo.addItem("自动：优先 NVIDIA GPU，失败用 CPU", "auto")
        self.device_combo.addItem("只用 CPU", "cpu")
        self.device_combo.setCurrentIndex(max(0, self.device_combo.findData(settings.compute_device)))
        verify_layout.addWidget(QLabel("识别模型"), 0, 0)
        verify_layout.addWidget(self.model_combo, 0, 1)
        verify_layout.addWidget(QLabel("歌曲语言"), 1, 0)
        verify_layout.addWidget(self.language_combo, 1, 1)
        verify_layout.addWidget(QLabel("允许时间误差"), 2, 0)
        verify_layout.addWidget(self.tolerance, 2, 1)
        verify_layout.addWidget(QLabel("运算设备"), 3, 0)
        verify_layout.addWidget(self.device_combo, 3, 1)
        note = QLabel("模型首次使用时下载到同级数据文件夹；音频不会上传。GPU 需要 CUDA 12/cuDNN 9 运行库，缺少时自动使用 CPU。")
        note.setWordWrap(True)
        note.setProperty("role", "secondary")
        verify_layout.addWidget(note, 4, 0, 1, 2)
        layout.addWidget(verify_group)

        general_group = QGroupBox("界面与扫描")
        general_layout = QGridLayout(general_group)
        self.recursive = QCheckBox("扫描子文件夹")
        self.recursive.setChecked(settings.recursive)
        self.theme = QComboBox()
        self.theme.addItem("跟随系统", "system")
        self.theme.addItem("浅色", "light")
        self.theme.addItem("深色", "dark")
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        general_layout.addWidget(self.recursive, 0, 0, 1, 2)
        general_layout.addWidget(QLabel("主题"), 1, 0)
        general_layout.addWidget(self.theme, 1, 1)
        layout.addWidget(general_group)

        data_row = QHBoxLayout()
        open_data = QPushButton("打开数据文件夹")
        clear_data = QPushButton("清理日志与缓存")
        open_data.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.base_dir))))
        clear_data.clicked.connect(self._clear_cache)
        data_row.addWidget(open_data)
        data_row.addWidget(clear_data)
        data_row.addStretch(1)
        layout.addLayout(data_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _clear_cache(self) -> None:
        answer = QMessageBox.question(self, "清理日志与缓存", "只清理日志和联网缓存，不删除识别模型与备份。继续吗？")
        if answer == QMessageBox.StandardButton.Yes:
            self.paths.clear_logs_and_cache()
            QMessageBox.information(self, "清理完成", "日志与缓存已清理。")

    def apply(self) -> None:
        self.settings.provider_lrclib = self.lrclib.isChecked()
        self.settings.provider_netease = self.netease.isChecked()
        self.settings.provider_qq = self.qq.isChecked()
        self.settings.provider_kugou = self.kugou.isChecked()
        self.settings.model_name = str(self.model_combo.currentData())
        self.settings.compute_device = str(self.device_combo.currentData())
        self.settings.recognition_language = str(self.language_combo.currentData())
        self.settings.tolerance_seconds = self.tolerance.value()
        self.settings.recursive = self.recursive.isChecked()
        self.settings.theme = str(self.theme.currentData())
        self.theme_manager.set_mode(self.settings.theme)


class MainWindow(QMainWindow):
    def __init__(self, paths: DataPaths, settings: AppSettings, theme_manager: FluentThemeManager) -> None:
        super().__init__()
        self.paths = paths
        self.settings = settings
        self.theme_manager = theme_manager
        self.pool = QThreadPool.globalInstance()
        self.busy = False
        self.active_task_kind: str | None = None
        self.task_control: TaskControl | None = None
        self._task_was_cancelled = False
        self._close_when_task_finishes = False
        self._allow_unverified_writes_this_session = False
        self._verification_pending_paths: set[str] = set()
        self._inline_write_in_progress = False
        self._inline_write_notice: tuple[str, str, str] | None = None
        self._preview_follow_playback = True
        self._preview_follow_resume_at = 0.0
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(max(0, min(100, settings.playback_volume)) / 100.0)
        self.player.setAudioOutput(self.audio_output)
        self._build_ui()
        self._restore_window_state()
        self._update_actions()

    def _build_ui(self) -> None:
        self.setWindowTitle("AAA 音乐歌词工具")
        self.resize(1440, 860)
        self.setMinimumSize(980, 650)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(10)
        self.setCentralWidget(central)

        top = QFrame()
        top.setProperty("fluentRole", "toolbar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)
        self.folder_edit = QLineEdit()
        initial = self.settings.last_folder or str(executable_dir())
        self.folder_edit.setText(initial)
        self.folder_edit.setPlaceholderText("音乐文件夹")
        self.folder_edit.setAccessibleName("音乐文件夹路径")
        choose = QPushButton("选择文件夹")
        choose.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        choose.clicked.connect(self.choose_folder)
        scan = QPushButton("扫描")
        scan.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        scan.clicked.connect(self.scan_current_folder)
        add_files = QPushButton("选择音乐")
        add_files.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        add_files.clicked.connect(self.choose_files)
        settings_button = QPushButton("设置")
        settings_button.clicked.connect(self.open_settings)
        help_button = QPushButton("使用说明")
        help_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogHelpButton))
        help_button.clicked.connect(self.show_help)
        top_layout.addWidget(self.folder_edit, 1)
        top_layout.addWidget(choose)
        top_layout.addWidget(scan)
        top_layout.addWidget(add_files)
        top_layout.addWidget(settings_button)
        top_layout.addWidget(help_button)
        root.addWidget(top)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        selection_row = QHBoxLayout()
        for label, callback in (
            ("全选", lambda: self._set_checks("all")),
            ("选择缺少歌词", lambda: self._set_checks("missing")),
            ("选择未通过复核", lambda: self._set_checks("unverified")),
            ("选择通过/自动修正", lambda: self._set_checks("verified")),
            ("清空选择", lambda: self._set_checks("none")),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            selection_row.addWidget(button)
        selection_row.addStretch(1)
        left_layout.addLayout(selection_row)

        self.model = TrackTableModel()
        self.model.checked_changed.connect(self._update_actions)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 48)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 145)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 64)
        self.table.setColumnWidth(5, 86)
        self.table.setColumnWidth(6, 145)
        self.table.selectionModel().currentRowChanged.connect(self._current_row_changed)
        left_layout.addWidget(self.table, 1)

        action_row = QHBoxLayout()
        self.search_button = QPushButton("① 联网搜索勾选项")
        self.verify_button = QPushButton("② 音频复核勾选项")
        set_fluent_property(self.search_button, "fluentAppearance", "primary")
        set_fluent_property(self.verify_button, "fluentAppearance", "primary")
        self.search_button.clicked.connect(self.search_checked)
        self.verify_button.clicked.connect(self.verify_checked)
        action_row.addWidget(self.search_button)
        action_row.addWidget(self.verify_button)
        action_row.addStretch(1)
        left_layout.addLayout(action_row)

        output_row = QHBoxLayout()
        self.embed_checkbox = QCheckBox("嵌入音频文件")
        self.lrc_checkbox = QCheckBox("生成同名 LRC")
        self.embed_checkbox.setChecked(self.settings.embed_lyrics)
        self.lrc_checkbox.setChecked(self.settings.create_lrc)
        self.embed_checkbox.toggled.connect(lambda checked: self._output_toggled(self.embed_checkbox, checked))
        self.lrc_checkbox.toggled.connect(lambda checked: self._output_toggled(self.lrc_checkbox, checked))
        self.write_button = QPushButton("③ 写入勾选项")
        set_fluent_property(self.write_button, "fluentAppearance", "primary")
        self.write_button.clicked.connect(self.write_checked)
        self.summary = QLabel("共 0 首")
        self.summary.setProperty("role", "secondary")
        output_row.addWidget(self.embed_checkbox)
        output_row.addWidget(self.lrc_checkbox)
        output_row.addSpacing(10)
        output_row.addWidget(self.summary, 1)
        output_row.addWidget(self.write_button)
        left_layout.addLayout(output_row)
        self.splitter.addWidget(left)

        right = self._build_detail_panel()
        self.splitter.addWidget(right)
        self.splitter.setSizes([900, 500])

        progress_row = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.status_label.setProperty("role", "secondary")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.pause_task_button = QPushButton("暂停")
        self.pause_task_button.setAccessibleName("暂停或继续当前任务")
        self.pause_task_button.clicked.connect(self._toggle_task_pause)
        self.stop_task_button = QPushButton("终止")
        self.stop_task_button.setAccessibleName("终止当前任务")
        set_fluent_property(self.stop_task_button, "fluentAppearance", "danger")
        self.stop_task_button.clicked.connect(self._cancel_task)
        progress_row.addWidget(self.status_label, 1)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.pause_task_button)
        progress_row.addWidget(self.stop_task_button)
        root.addLayout(progress_row)

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setProperty("fluentRole", "panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        heading = QLabel("当前曲目与歌词")
        font = heading.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1)
        heading.setFont(font)
        layout.addWidget(heading)
        self.track_info = QLabel("请从左侧选择一首音乐")
        self.track_info.setWordWrap(True)
        self.track_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.track_info)

        candidate_row = QHBoxLayout()
        self.candidate_combo = QComboBox()
        self.candidate_combo.setAccessibleName("歌词候选")
        self.candidate_combo.currentIndexChanged.connect(self._candidate_changed)
        self.import_button = QPushButton("导入 LRC")
        self.import_button.clicked.connect(self.import_lrc)
        self.use_current_button = QPushButton("载入当前歌词")
        self.use_current_button.clicked.connect(self.use_current_lyrics)
        candidate_row.addWidget(self.candidate_combo, 1)
        candidate_row.addWidget(self.import_button)
        candidate_row.addWidget(self.use_current_button)
        layout.addLayout(candidate_row)
        self.candidate_info = QLabel("尚无候选歌词")
        self.candidate_info.setProperty("role", "secondary")
        self.candidate_info.setWordWrap(True)
        layout.addWidget(self.candidate_info)

        tabs = self.detail_tabs = QTabWidget()
        current_page = QWidget()
        current_layout = QVBoxLayout(current_page)
        current_layout.setContentsMargins(8, 8, 8, 8)
        self.current_lyrics_box = QPlainTextEdit()
        self.current_lyrics_box.setReadOnly(True)
        self.current_lyrics_box.setPlaceholderText("音频及同名 LRC 中目前没有歌词")
        current_layout.addWidget(self.current_lyrics_box)
        tabs.addTab(current_page, "当前真实歌词")

        edit_page = QWidget()
        edit_layout = QVBoxLayout(edit_page)
        edit_layout.setContentsMargins(8, 8, 8, 8)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("联网候选或手工导入的 LRC 在这里预览和编辑")
        self.editor.setAccessibleName("候选歌词编辑器")
        edit_layout.addWidget(self.editor, 1)
        edit_buttons = QGridLayout()
        self.save_edit_button = QPushButton("保存编辑")
        self.undo_edit_button = QPushButton("撤销")
        self.redo_edit_button = QPushButton("重做")
        self.undo_edit_button.setEnabled(False)
        self.redo_edit_button.setEnabled(False)
        self.edit_offset = QDoubleSpinBox()
        self.edit_offset.setRange(0.1, 30.0)
        self.edit_offset.setDecimals(1)
        self.edit_offset.setSingleStep(0.1)
        self.edit_offset.setValue(0.1)
        self.edit_offset.setSuffix(" 秒")
        self.edit_offset.setAccessibleName("候选歌词整体移动秒数")
        self.minus_button = QPushButton("整体提前")
        self.plus_button = QPushButton("整体延后")
        self.save_edit_button.clicked.connect(self.save_editor)
        self.undo_edit_button.clicked.connect(self._undo_edit)
        self.redo_edit_button.clicked.connect(self._redo_edit)
        self.editor.undoAvailable.connect(self.undo_edit_button.setEnabled)
        self.editor.redoAvailable.connect(self.redo_edit_button.setEnabled)
        self.minus_button.clicked.connect(lambda: self.apply_offset(-round(self.edit_offset.value() * 1_000)))
        self.plus_button.clicked.connect(lambda: self.apply_offset(round(self.edit_offset.value() * 1_000)))
        edit_buttons.addWidget(self.save_edit_button, 0, 0)
        edit_buttons.addWidget(self.undo_edit_button, 0, 1)
        edit_buttons.addWidget(self.redo_edit_button, 0, 2)
        edit_buttons.addWidget(QLabel("移动量"), 1, 0)
        edit_buttons.addWidget(self.edit_offset, 1, 1)
        edit_buttons.addWidget(self.minus_button, 1, 2)
        edit_buttons.addWidget(self.plus_button, 1, 3)
        edit_buttons.setColumnStretch(4, 1)
        edit_layout.addLayout(edit_buttons)
        tabs.addTab(edit_page, "候选与编辑")

        report_page = QWidget()
        report_layout = QVBoxLayout(report_page)
        report_layout.setContentsMargins(8, 8, 8, 8)
        self.report_box = QPlainTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setPlaceholderText("音频复核后显示内容相似度、时间偏移、锚点覆盖和校正结果")
        report_layout.addWidget(self.report_box)
        tabs.addTab(report_page, "音频复核报告")

        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        controls = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.clicked.connect(self._toggle_playback)
        self.seek_slider = ClickableSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setAccessibleName("播放位置")
        self.seek_slider.sliderMoved.connect(self._seek_playback)
        self.seek_slider.seek_requested.connect(self._seek_playback)
        self.play_time = QLabel("0:00 / 0:00")
        controls.addWidget(self.play_button)
        controls.addWidget(self.seek_slider, 1)
        controls.addWidget(self.play_time)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(5)
        self.volume_slider.setPageStep(10)
        self.volume_slider.setValue(max(0, min(100, self.settings.playback_volume)))
        self.volume_slider.setMinimumWidth(150)
        self.volume_slider.setAccessibleName("试听音量")
        self.volume_label = QLabel(f"{self.volume_slider.value()}%")
        self.volume_slider.valueChanged.connect(self._set_playback_volume)
        self.volume_button = QToolButton()
        self.volume_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self.volume_button.setToolTip("音量")
        self.volume_button.setAccessibleName("打开音量调节")
        self.volume_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        volume_popup = QMenu(self.volume_button)
        volume_action = QWidgetAction(volume_popup)
        volume_widget = QWidget()
        volume_layout = QHBoxLayout(volume_widget)
        volume_layout.setContentsMargins(10, 8, 10, 8)
        volume_layout.addWidget(QLabel("音量"))
        volume_layout.addWidget(self.volume_slider)
        volume_layout.addWidget(self.volume_label)
        volume_action.setDefaultWidget(volume_widget)
        volume_popup.addAction(volume_action)
        self.volume_button.setMenu(volume_popup)
        controls.addWidget(self.volume_button)
        preview_layout.addLayout(controls)
        self.preview_source = QComboBox()
        self.preview_source.addItem("当前真实歌词", "current")
        self.preview_source.addItem("所选候选歌词", "candidate")
        self.preview_source.currentIndexChanged.connect(self._refresh_preview_lyrics)
        preview_layout.addWidget(self.preview_source)
        self.preview_lines = LyricsPreviewList()
        self.preview_lines.setAccessibleName("随播放位置同步的歌词")
        self.preview_lines.user_interacted.connect(self._pause_preview_follow)
        self.preview_lines.row_activated.connect(self._preview_line_activated)
        self.preview_lines.verticalScrollBar().sliderPressed.connect(self._pause_preview_follow)
        preview_layout.addWidget(self.preview_lines, 1)
        timing_controls = QGridLayout()
        self.preview_offset = QDoubleSpinBox()
        self.preview_offset.setRange(0.1, 30.0)
        self.preview_offset.setDecimals(1)
        self.preview_offset.setSingleStep(0.1)
        self.preview_offset.setValue(0.1)
        self.preview_offset.setSuffix(" 秒")
        self.preview_offset.setAccessibleName("试听页歌词移动秒数")
        self.line_earlier_button = QPushButton("本句提前")
        self.line_current_button = QPushButton("设为当前位置")
        self.line_current_button.setToolTip("把选中的歌词行设为当前播放位置")
        self.shift_with_current = QCheckBox("其余歌词跟随")
        self.shift_with_current.setToolTip("以所选句为锚点移动整段正文，开头署名保留原时间")
        self.line_later_button = QPushButton("本句延后")
        self.all_earlier_button = QPushButton("整体提前")
        self.all_later_button = QPushButton("整体延后")
        self.write_current_button = QPushButton("写入当前歌词")
        self.preview_undo_button = QPushButton("撤销")
        self.preview_redo_button = QPushButton("重做")
        self.preview_undo_button.setEnabled(False)
        self.preview_redo_button.setEnabled(False)
        self.preview_undo_button.clicked.connect(self._undo_edit)
        self.preview_redo_button.clicked.connect(self._redo_edit)
        self.editor.undoAvailable.connect(self.preview_undo_button.setEnabled)
        self.editor.redoAvailable.connect(self.preview_redo_button.setEnabled)
        self.write_current_button.setToolTip("写入试听页当前选择的候选歌词")
        set_fluent_property(self.write_current_button, "fluentAppearance", "primary")
        self.line_earlier_button.clicked.connect(lambda: self._shift_preview_line(-self._preview_offset_ms()))
        self.line_current_button.clicked.connect(self._set_preview_line_to_current_position)
        self.line_later_button.clicked.connect(lambda: self._shift_preview_line(self._preview_offset_ms()))
        self.all_earlier_button.clicked.connect(lambda: self._shift_preview_all(-self._preview_offset_ms()))
        self.all_later_button.clicked.connect(lambda: self._shift_preview_all(self._preview_offset_ms()))
        self.write_current_button.clicked.connect(self.write_current_candidate)
        timing_controls.addWidget(QLabel("调整量"), 0, 0)
        timing_controls.addWidget(self.preview_offset, 0, 1)
        timing_controls.addWidget(self.line_current_button, 0, 2)
        timing_controls.addWidget(self.write_current_button, 0, 3)
        timing_controls.addWidget(QLabel("当前句"), 1, 0)
        timing_controls.addWidget(self.line_earlier_button, 1, 1)
        timing_controls.addWidget(self.line_later_button, 1, 2)
        timing_controls.addWidget(self.shift_with_current, 1, 3)
        timing_controls.addWidget(QLabel("整个时间轴"), 2, 0)
        timing_controls.addWidget(self.all_earlier_button, 2, 1)
        timing_controls.addWidget(self.all_later_button, 2, 2)
        history = QHBoxLayout()
        history.setContentsMargins(0, 0, 0, 0)
        history.setSpacing(4)
        history.addWidget(self.preview_undo_button)
        history.addWidget(self.preview_redo_button)
        timing_controls.addLayout(history, 2, 3)
        timing_controls.setColumnStretch(4, 1)
        preview_layout.addLayout(timing_controls)
        self._preview_timestamps: list[int] = []
        self._preview_line_indices: list[int] = []
        self.player.positionChanged.connect(self._playback_position)
        self.player.durationChanged.connect(lambda duration: self.seek_slider.setMaximum(max(0, duration)))
        self.player.playbackStateChanged.connect(self._playback_state)
        self.player.errorOccurred.connect(lambda _error, message: self.status_label.setText(f"播放失败：{message}") if message else None)
        tabs.addTab(preview_page, "试听与歌词")
        layout.addWidget(tabs, 1)
        return panel

    def _restore_window_state(self) -> None:
        try:
            if self.settings.geometry:
                self.restoreGeometry(base64.b64decode(self.settings.geometry))
            if self.settings.splitter_state:
                self.splitter.restoreState(base64.b64decode(self.settings.splitter_state))
        except (ValueError, TypeError):
            pass

    def closeEvent(self, event) -> None:
        if self._inline_write_in_progress:
            QMessageBox.information(self, "歌词正在写入", "请等待当前歌曲写入并复读验证完成后再关闭。")
            event.ignore()
            return
        if self.busy and self.task_control:
            answer = QMessageBox.question(
                self,
                "任务仍在运行",
                "要终止当前任务并在安全检查点退出吗？已完成歌曲的结果会保留。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_when_task_finishes = True
            self._cancel_task()
            event.ignore()
            return
        self.player.stop()
        self.settings.last_folder = self.folder_edit.text().strip()
        self.settings.embed_lyrics = self.embed_checkbox.isChecked()
        self.settings.create_lrc = self.lrc_checkbox.isChecked()
        self.settings.playback_volume = self.volume_slider.value()
        self.settings.geometry = base64.b64encode(self.saveGeometry().data()).decode("ascii")
        self.settings.splitter_state = base64.b64encode(self.splitter.saveState().data()).decode("ascii")
        save_settings(self.paths, self.settings)
        super().closeEvent(event)

    def choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择音乐文件夹", self.folder_edit.text().strip())
        if selected:
            self.folder_edit.setText(selected)
            self.scan_current_folder()

    def choose_files(self) -> None:
        filters = "音乐文件 (" + " ".join(f"*{suffix}" for suffix in sorted(AUDIO_EXTENSIONS)) + ")"
        selected, _ = QFileDialog.getOpenFileNames(self, "选择音乐", self.folder_edit.text().strip(), filters)
        if not selected:
            return
        paths = [Path(value) for value in selected]

        def task(progress, control):
            control.checkpoint()
            progress((0, len(paths), "正在读取所选音乐"))
            result = scan_files(paths)
            control.checkpoint()
            return result

        self._start_task(task, self._append_scan_result, "正在读取所选音乐", "scan")

    def scan_current_folder(self) -> None:
        folder = Path(self.folder_edit.text().strip())
        if not folder.is_dir():
            QMessageBox.warning(self, "无法扫描", "请选择存在的音乐文件夹。")
            return
        recursive = self.settings.recursive

        def task(progress, control):
            control.checkpoint()
            progress((0, 0, f"正在扫描：{folder}"))
            result = scan_folder(folder, recursive)
            control.checkpoint()
            return result

        self.settings.last_folder = str(folder)
        self._start_task(task, self._replace_scan_result, "正在扫描音乐文件", "scan")

    def _replace_scan_result(self, result) -> None:
        records, errors = result
        old = {str(record.path).casefold(): record for record in self.model.records}
        for record in records:
            previous = old.get(str(record.path).casefold())
            if previous:
                record.checked = previous.checked
                record.candidates = previous.candidates
                record.selected_candidate = previous.selected_candidate
                record.search_status = previous.search_status
                record.verification = previous.verification
        self.model.replace(records)
        self._after_scan(errors)

    def _append_scan_result(self, result) -> None:
        records, errors = result
        current = {str(record.path).casefold(): record for record in self.model.records}
        for record in records:
            current.setdefault(str(record.path).casefold(), record)
        self.model.replace(sorted(current.values(), key=lambda item: item.path.name.casefold()))
        self._after_scan(errors)

    def _after_scan(self, errors: list[str]) -> None:
        if self.model.records:
            self.table.selectRow(0)
        self.status_label.setText(f"本地扫描完成：{len(self.model.records)} 首" + (f"，{len(errors)} 个错误" if errors else ""))
        if errors:
            LOGGER.warning("scan errors: %s", " | ".join(errors))
        self._update_actions()

    def _set_checks(self, mode: str) -> None:
        for record in self.model.records:
            if mode == "all":
                record.checked = True
            elif mode == "missing":
                record.checked = not bool(record.current_lyrics)
            elif mode == "unverified":
                record.checked = not record.verification or not record.verification.passed
            elif mode == "verified":
                record.checked = bool(record.verification and record.verification.passed)
            else:
                record.checked = False
        self.model.refresh()

    def _current_record(self) -> TrackRecord | None:
        row = self.table.currentIndex().row()
        if 0 <= row < len(self.model.records):
            return self.model.records[row]
        return None

    def _current_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid() and self.active_task_kind != "write":
            self._show_record(self.model.records[current.row()])

    def _show_record(self, record: TrackRecord) -> None:
        current_source = self.player.source().toLocalFile()
        if not self._inline_write_in_progress and (not current_source or Path(current_source) != record.path):
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(record.path)))
            self._resume_preview_follow()
        self.track_info.setText(
            f"{record.path.name}\n标题：{record.title or '未知'}    艺术家：{record.artist or '未知'}\n"
            f"唱片集：{record.album or '未知'}    时长：{_format_duration(record.duration)}    实际格式：{record.audio_type}"
        )
        self.current_lyrics_box.setPlainText(record.current_lyrics)
        self.candidate_combo.blockSignals(True)
        self.candidate_combo.clear()
        for candidate in record.candidates:
            self.candidate_combo.addItem(
                f"{candidate.source} · {candidate.match_score:.0f} 分 · {candidate.title} / {candidate.artist}",
            )
        if record.selected_candidate is not None and record.selected_candidate < self.candidate_combo.count():
            self.candidate_combo.setCurrentIndex(record.selected_candidate)
        self.candidate_combo.blockSignals(False)
        self._show_candidate(record)

    def _toggle_playback(self) -> None:
        if self.active_task_kind == "write" or not self._current_record():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._resume_preview_follow()
            self.player.play()

    def _seek_playback(self, position: int) -> None:
        if self.active_task_kind == "write":
            return
        self._resume_preview_follow()
        self.player.setPosition(position)

    def _pause_preview_follow(self) -> None:
        self._preview_follow_playback = False
        self._preview_follow_resume_at = time.monotonic() + 4.0

    def _resume_preview_follow(self) -> None:
        self._preview_follow_playback = True
        self._preview_follow_resume_at = 0.0

    def _preview_should_follow(self) -> bool:
        if not self._preview_follow_playback and time.monotonic() >= self._preview_follow_resume_at:
            self._resume_preview_follow()
        return self._preview_follow_playback

    def _set_playback_volume(self, value: int) -> None:
        self.audio_output.setVolume(max(0, min(100, value)) / 100.0)
        self.volume_label.setText(f"{value}%")
        self.settings.playback_volume = value

    def _playback_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText("暂停" if playing else "播放")
        self.play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        ))

    def _playback_position(self, position: int) -> None:
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(position)
        self.play_time.setText(f"{_format_duration(position / 1000)} / {_format_duration(self.player.duration() / 1000)}")
        from bisect import bisect_right

        index = bisect_right(self._preview_timestamps, position) - 1
        if self._preview_should_follow() and index >= 0:
            item = self.preview_lines.item(index)
            if index != self.preview_lines.currentRow():
                self.preview_lines.setCurrentRow(index)
            item_rect = self.preview_lines.visualItemRect(item)
            if not self.preview_lines.viewport().rect().contains(item_rect):
                self.preview_lines.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _preview_line_activated(self, row: int) -> None:
        if 0 <= row < len(self._preview_timestamps):
            self.preview_lines.setCurrentRow(row)
            self._seek_playback(self._preview_timestamps[row])

    def _refresh_preview_lyrics(self, selected_timestamp: int | None = None) -> None:
        record = self._current_record()
        self.preview_lines.clear()
        self._preview_timestamps = []
        self._preview_line_indices = []
        if not record:
            self._update_preview_edit_actions()
            return
        text = record.current_lyrics if self.preview_source.currentData() == "current" else (
            record.candidate.effective_lrc_text if record.candidate else ""
        )
        indexed_lines = sorted(enumerate(parse_lrc(text).lines), key=lambda value: value[1].timestamp_ms)
        for original_index, line in indexed_lines:
            if line.text.strip():
                self._preview_line_indices.append(original_index)
                self._preview_timestamps.append(line.timestamp_ms)
                self.preview_lines.addItem(f"{_format_duration(line.timestamp_ms / 1000)}   {line.text}")
        if not self._preview_timestamps:
            self.preview_lines.addItem("没有带时间轴的歌词")
        else:
            self._playback_position(self.player.position())
            if selected_timestamp is not None:
                row = min(range(len(self._preview_timestamps)), key=lambda index: abs(self._preview_timestamps[index] - selected_timestamp))
                self.preview_lines.setCurrentRow(row)
                self.preview_lines.scrollToItem(self.preview_lines.item(row))
        self._update_preview_edit_actions()

    def _update_preview_edit_actions(self) -> None:
        editable = bool(
            self.preview_source.currentData() == "candidate"
            and self._current_record()
            and self._current_record().candidate
            and self._preview_timestamps
        )
        for control in (
            self.preview_offset,
            self.shift_with_current,
            self.line_earlier_button,
            self.line_current_button,
            self.line_later_button,
            self.all_earlier_button,
            self.all_later_button,
        ):
            control.setEnabled(editable)
        self.write_current_button.setEnabled(self._can_write_current())

    def _can_write_current(self) -> bool:
        record = self._current_record()
        if not record or not record.candidate or self._inline_write_in_progress:
            return False
        if not self.busy:
            return True
        return self.active_task_kind == "verify" and str(record.path) not in self._verification_pending_paths

    def _preview_offset_ms(self) -> int:
        return max(100, round(self.preview_offset.value() * 1_000))

    def _editable_preview(self):
        record = self._current_record()
        row = self.preview_lines.currentRow()
        if self.preview_source.currentData() != "candidate" or not record or not record.candidate:
            QMessageBox.information(self, "请选择候选歌词", "试听页只编辑所选候选歌词。需要修改当前歌词时，请先点击“载入当前歌词”。")
            return None
        if self._preview_has_unsaved_text(record):
            return None
        if not (0 <= row < len(self._preview_line_indices)):
            QMessageBox.information(self, "请选择歌词行", "请先选择需要调整时间戳的歌词行。")
            return None
        parsed = parse_lrc(record.candidate.effective_lrc_text)
        line_index = self._preview_line_indices[row]
        if not (0 <= line_index < len(parsed.lines)):
            return None
        return record, parsed, line_index

    def _shift_preview_line(self, offset_ms: int) -> None:
        context = self._editable_preview()
        if not context:
            return
        record, parsed, line_index = context
        line = parsed.lines[line_index]
        line.timestamp_ms = max(0, line.timestamp_ms + offset_ms)
        parsed.lines.sort(key=lambda value: value.timestamp_ms)
        self._save_candidate_text(record, serialize_lrc(parsed), "试听页逐句时间戳已保存", line.timestamp_ms)

    def _set_preview_line_to_current_position(self) -> None:
        context = self._editable_preview()
        if not context:
            return
        record, parsed, line_index = context
        timestamp = max(0, round(self.player.position() / 100) * 100)
        if self.shift_with_current.isChecked():
            credit_end = opening_credit_end(parsed)
            if parsed.lines[line_index].timestamp_ms <= credit_end:
                QMessageBox.information(self, "开头署名", "开头署名不参与正文整体平移；可取消勾选后单独调整这句。")
                return
            text = shift_lyric_timeline(record.candidate.effective_lrc_text, timestamp - parsed.lines[line_index].timestamp_ms)
            self._save_candidate_text(record, text, "以当前句为锚点平移了正文歌词", timestamp)
            return
        parsed.lines[line_index].timestamp_ms = timestamp
        parsed.lines.sort(key=lambda value: value.timestamp_ms)
        self._save_candidate_text(record, serialize_lrc(parsed), "本句已设为当前播放位置", timestamp)

    def _shift_preview_all(self, offset_ms: int) -> None:
        record = self._current_record()
        if self.preview_source.currentData() != "candidate" or not record or not record.candidate:
            QMessageBox.information(self, "请选择候选歌词", "整体移动只作用于所选候选歌词。")
            return
        if self._preview_has_unsaved_text(record):
            return
        selected_row = self.preview_lines.currentRow()
        selected_timestamp = None
        if 0 <= selected_row < len(self._preview_timestamps):
            selected_timestamp = max(0, self._preview_timestamps[selected_row] + offset_ms)
        text = shift_lyric_timeline(record.candidate.effective_lrc_text, offset_ms)
        self._save_candidate_text(record, text, f"试听页已整体调整 {offset_ms / 1_000:+.1f} 秒", selected_timestamp)

    def _preview_has_unsaved_text(self, record: TrackRecord) -> bool:
        if self.editor.toPlainText().strip() == record.candidate.effective_lrc_text.strip():
            return False
        QMessageBox.information(self, "先保存编辑", "候选与编辑页有尚未保存的修改；请先保存或撤销，再调整试听页时间戳。")
        return True

    def _show_candidate(self, record: TrackRecord) -> None:
        candidate = record.candidate
        if not candidate:
            self.candidate_info.setText("尚无候选歌词")
            self.editor.clear()
        else:
            correction = f"；{candidate.correction_description}" if candidate.correction_description else ""
            self.candidate_info.setText(
                f"来源：{candidate.source}    匹配：{candidate.match_score:.1f} 分    "
                f"{candidate.structure.summary}{correction}"
            )
            self.editor.setPlainText(candidate.effective_lrc_text)
        if record.verification:
            report = record.verification
            lines = [report.status, report.summary, "", *report.details]
            if report.recognized_text:
                lines.extend(["", "模型识别文字：", report.recognized_text])
            self.report_box.setPlainText("\n".join(lines))
        else:
            self.report_box.clear()
        self._refresh_preview_lyrics()

    def _candidate_changed(self, index: int) -> None:
        record = self._current_record()
        if not record or index < 0:
            return
        record.selected_candidate = index
        record.verification = None
        self._show_candidate(record)
        self.model.refresh(self.table.currentIndex().row())

    def _enabled_providers(self) -> list[str]:
        enabled: list[str] = []
        if self.settings.provider_lrclib:
            enabled.append("lrclib")
        if self.settings.provider_netease:
            enabled.append("netease")
        if self.settings.provider_qq:
            enabled.append("qq")
        if self.settings.provider_kugou:
            enabled.append("kugou")
        return enabled

    def search_checked(self) -> None:
        records = [record for record in self.model.records if record.checked]
        providers = self._enabled_providers()
        if not records:
            QMessageBox.information(self, "没有勾选音乐", "请先勾选需要搜索歌词的音乐。")
            return
        if not providers:
            QMessageBox.warning(self, "没有歌词来源", "请在设置中至少启用一个联网歌词来源。")
            return

        def task(progress, control):
            results: dict[str, SearchOutcome] = {}
            context = SearchContext()
            worker_count = min(4, len(records))
            pool = ThreadPoolExecutor(max_workers=worker_count)
            remaining = iter(records)
            futures = {}
            for _ in range(worker_count):
                record = next(remaining, None)
                if record is not None:
                    futures[pool.submit(search_all, record, providers, context)] = record
            pending = set(futures)
            try:
                while pending:
                    control.checkpoint()
                    completed, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        record = futures[future]
                        try:
                            results[str(record.path)] = future.result()
                        except Exception as exc:
                            results[str(record.path)] = SearchOutcome([], [str(exc)])
                        progress((len(results), len(records), f"联网搜索 {len(results)}/{len(records)}：{record.path.name}"))
                        control.checkpoint()
                        next_record = next(remaining, None)
                        if next_record is not None:
                            next_future = pool.submit(search_all, next_record, providers, context)
                            futures[next_future] = next_record
                            pending.add(next_future)
            except TaskCancelled:
                for future in pending:
                    future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                raise TaskCancelled(results)
            else:
                pool.shutdown(wait=True)
            progress((len(records), len(records), "联网搜索完成"))
            return results

        self._start_task(task, self._search_done, "正在联网搜索并过滤歌词结构", "search")

    def _search_done(self, results: dict[str, SearchOutcome]) -> None:
        by_path = {str(record.path): record for record in self.model.records}
        found = 0
        for path, outcome in results.items():
            record = by_path.get(path)
            if not record:
                continue
            record.candidates = outcome.candidates
            record.selected_candidate = 0 if outcome.candidates else None
            record.verification = None
            if outcome.candidates:
                record.search_status = f"{len(outcome.candidates)} 个有效候选"
                found += 1
            else:
                record.search_status = "无有效候选"
            record.error = "；".join(outcome.errors)
        self.model.refresh()
        current = self._current_record()
        if current:
            self._show_record(current)
        self.status_label.setText(f"联网搜索完成：{found}/{len(results)} 首找到结构有效的同步歌词")

    def verify_checked(self) -> None:
        records = [record for record in self.model.records if record.checked and record.candidate]
        if not records:
            QMessageBox.information(self, "没有可复核歌词", "请先搜索或导入歌词，并勾选相应音乐。")
            return
        invalid = []
        for record in records:
            report = validate_structure(parse_lrc(record.candidate.effective_lrc_text), audio_duration=record.duration, strict_online=False)
            if not report.valid:
                invalid.append(f"{record.path.name}：{report.summary}")
        if invalid:
            QMessageBox.warning(self, "歌词结构无效", "请先修复以下歌词：\n\n" + "\n".join(invalid[:12]))
            return
        model_marker = self.paths.models_dir / f"models--Systran--faster-whisper-{self.settings.model_name}"
        if not model_marker.exists():
            answer = QMessageBox.question(
                self,
                "首次下载识别模型",
                f"将下载 {self.settings.model_name} 多语言音频识别模型到“音乐歌词工具数据\\识别模型”。\n"
                "模型可能需要数百 MB 至数 GB，下载和首次识别需要一些时间。继续吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._verification_pending_paths = {str(record.path) for record in records}

        def task(progress, control):
            results = {}
            try:
                for index, record in enumerate(records, 1):
                    control.checkpoint()
                    progress((index - 1, len(records), f"音频复核 {index}/{len(records)}：{record.path.name}"))
                    try:
                        best_index, candidate_results = verify_audio_candidates(
                            record.path,
                            [candidate.effective_lrc_text for candidate in record.candidates],
                            model_name=self.settings.model_name,
                            model_dir=self.paths.models_dir,
                            audio_duration=record.duration,
                            tolerance_seconds=self.settings.tolerance_seconds,
                            language_mode=self.settings.recognition_language,
                            compute_device=self.settings.compute_device,
                            progress=lambda message, i=index: progress((i - 1, len(records), f"{record.path.name}：{message}")),
                            checkpoint=control.checkpoint,
                        )
                    except TaskCancelled:
                        raise
                    except Exception as exc:
                        LOGGER.exception("audio verification failed: %s", record.path)
                        result = VerificationResult("无法复核", f"识别器运行失败：{exc}；可人工试听，不代表歌词错误", 0, 0, 0)
                        best_index = record.selected_candidate or 0
                        candidate_results = [result for _candidate in record.candidates]
                    payload = (best_index, candidate_results)
                    results[str(record.path)] = payload
                    progress({"kind": "verification_result", "path": str(record.path), "payload": payload})
            except TaskCancelled:
                raise TaskCancelled(results)
            progress((len(records), len(records), "音频复核完成"))
            return results

        self._start_task(task, self._verify_done, "正在使用本地模型复核歌词内容和时间轴", "verify")

    def _verify_done(self, results) -> None:
        passed = 0
        for record in self.model.records:
            payload = results.get(str(record.path))
            if not payload:
                continue
            self._apply_verification_result(record, payload, refresh=False)
            if record.verification and record.verification.passed:
                passed += 1
        self.model.refresh()
        current = self._current_record()
        if current:
            self._show_record(current)
        self.status_label.setText(f"音频复核完成：{passed}/{len(results)} 首通过或已生成可靠校正")

    def _apply_verification_result(self, record: TrackRecord, payload, *, refresh: bool = True) -> None:
        best_index, candidate_results = payload
        if not candidate_results or not (0 <= best_index < len(candidate_results)):
            return
        current_index = record.selected_candidate if record.selected_candidate is not None else 0
        current_index = min(current_index, len(candidate_results) - 1)
        best_result = candidate_results[best_index]
        current_result = candidate_results[current_index]
        should_switch = best_index != current_index and (
            best_result.passed
            or verification_rank(best_result) > verification_rank(current_result)
            and best_result.content_similarity >= current_result.content_similarity + 0.08
        )
        chosen_index = best_index if should_switch else current_index
        chosen_result = candidate_results[chosen_index]
        self._verification_pending_paths.discard(str(record.path))
        for candidate in record.candidates:
            candidate.corrected_lrc_text = None
            candidate.correction_description = ""
        record.selected_candidate = chosen_index
        record.verification = chosen_result
        if chosen_result.corrected_lrc_text and record.candidate:
            record.candidate.corrected_lrc_text = chosen_result.corrected_lrc_text
            record.candidate.correction_description = chosen_result.summary
        if should_switch and record.candidate:
            chosen_result.details.insert(0, f"已比较 {len(candidate_results)} 个候选并切换到：{record.candidate.source}")
        if refresh:
            row = next((index for index, item in enumerate(self.model.records) if item is record), None)
            self.model.refresh(row)
            if self._current_record() is record:
                self._show_record(record)

    def import_lrc(self) -> None:
        record = self._current_record()
        if not record:
            return
        path, _ = QFileDialog.getOpenFileName(self, "导入 LRC", str(record.path.parent), "LRC 歌词 (*.lrc);;文本文件 (*.txt)")
        if not path:
            return
        text = read_text_file(Path(path))
        self._add_manual_candidate(record, text, f"导入：{Path(path).name}")

    def use_current_lyrics(self) -> None:
        record = self._current_record()
        if not record or not record.current_lyrics:
            QMessageBox.information(self, "没有当前歌词", "当前音频和同名 LRC 都没有歌词。")
            return
        self._add_manual_candidate(record, record.current_lyrics, "当前歌词")

    def _add_manual_candidate(self, record: TrackRecord, text: str, source: str) -> None:
        parsed = parse_lrc(text)
        report = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
        candidate = LyricCandidate(
            source=source,
            source_id="manual",
            title=record.title,
            artist=record.artist,
            album=record.album,
            duration=record.duration,
            lrc_text=text,
            parsed=parsed,
            structure=report,
            match_score=100.0,
        )
        record.candidates.append(candidate)
        record.selected_candidate = len(record.candidates) - 1
        record.search_status = "人工歌词" if report.valid else "人工歌词待修复"
        record.verification = None
        self.model.refresh(self.table.currentIndex().row())
        self._show_record(record)
        if not report.valid:
            QMessageBox.warning(self, "歌词需要修复", report.summary + "\n可以在右侧编辑后点击“保存编辑”。")

    def save_editor(self) -> None:
        record = self._current_record()
        if not record or not record.candidate:
            return
        text = self.editor.toPlainText().strip()
        self._save_candidate_text(record, text + ("\n" if text else ""), "歌词编辑已保存；写入前请执行音频复核")

    def _save_candidate_text(
        self,
        record: TrackRecord,
        text: str,
        status_message: str,
        selected_timestamp: int | None = None,
    ) -> None:
        parsed = parse_lrc(text)
        report = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
        record.candidate.lrc_text = text.rstrip() + ("\n" if text.strip() else "")
        record.candidate.corrected_lrc_text = None
        record.candidate.correction_description = ""
        record.candidate.parsed = parsed
        record.candidate.structure = report
        record.verification = None
        record.search_status = "人工编辑完成" if report.valid else "编辑内容待修复"
        self.model.refresh(self.table.currentIndex().row())
        if self.editor.toPlainText().rstrip() != record.candidate.lrc_text.rstrip():
            self._replace_editor_text(record.candidate.lrc_text)
        self.candidate_info.setText(
            f"来源：{record.candidate.source}    匹配：{record.candidate.match_score:.1f} 分    {report.summary}"
        )
        self.report_box.clear()
        self._refresh_preview_lyrics(selected_timestamp)
        if report.valid:
            self.status_label.setText(status_message + "；写入前请重新执行音频复核")
        else:
            QMessageBox.warning(self, "结构检查未通过", report.summary)

    def apply_offset(self, offset_ms: int) -> None:
        if not self.editor.toPlainText().strip():
            return
        self._replace_editor_text(shift_lyric_timeline(self.editor.toPlainText(), offset_ms))
        self.status_label.setText(f"已在编辑区整体调整 {offset_ms / 1_000:+.1f} 秒；请点击“保存编辑”")

    def _replace_editor_text(self, text: str) -> None:
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()

    def _undo_edit(self) -> None:
        self.editor.undo()
        if self.detail_tabs.currentIndex() == 3:
            self._sync_editor_history()

    def _redo_edit(self) -> None:
        self.editor.redo()
        if self.detail_tabs.currentIndex() == 3:
            self._sync_editor_history()

    def _sync_editor_history(self) -> None:
        record = self._current_record()
        if not record or not record.candidate:
            return
        text = self.editor.toPlainText().rstrip() + "\n"
        parsed = parse_lrc(text)
        record.candidate.lrc_text = text
        record.candidate.corrected_lrc_text = None
        record.candidate.correction_description = ""
        record.candidate.parsed = parsed
        record.candidate.structure = validate_structure(parsed, audio_duration=record.duration, strict_online=False)
        record.verification = None
        record.search_status = "人工编辑完成" if record.candidate.structure.valid else "编辑内容待修复"
        self.model.refresh(self.table.currentIndex().row())
        self.candidate_info.setText(f"来源：{record.candidate.source}    {record.candidate.structure.summary}")
        self.report_box.clear()
        self._refresh_preview_lyrics()

    def write_checked(self) -> None:
        records = [record for record in self.model.records if record.checked]
        if not records:
            QMessageBox.information(self, "没有勾选音乐", "请先勾选需要写入歌词的音乐。")
            return
        self._write_records(records)

    def write_current_candidate(self) -> None:
        record = self._current_record()
        if not record or not record.candidate:
            QMessageBox.information(self, "没有候选歌词", "请先选择、搜索或导入一份候选歌词。")
            return
        editor_text = self.editor.toPlainText().strip()
        candidate_text = record.candidate.effective_lrc_text.strip()
        if editor_text and editor_text != candidate_text:
            answer = QMessageBox.question(
                self,
                "保存编辑后写入",
                "编辑区有尚未保存的修改。要先保存这些修改，再写入当前歌曲吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.save_editor()
        self._write_records([record])

    def _write_records(self, records: list[TrackRecord]) -> None:
        if self._inline_write_in_progress:
            QMessageBox.information(self, "歌词正在写入", "请等待当前歌曲写入完成。")
            return
        inline_during_verification = bool(
            self.busy
            and self.active_task_kind == "verify"
            and len(records) == 1
            and str(records[0].path) not in self._verification_pending_paths
        )
        if self.busy and not inline_during_verification:
            message = (
                "当前歌曲尚未完成本轮音频复核，请在列表状态刷新后再写入。"
                if self.active_task_kind == "verify" and len(records) == 1
                else "请等待当前任务完成。"
            )
            QMessageBox.information(self, "任务正在运行", message)
            return
        missing = [record.path.name for record in records if not record.candidate]
        if missing:
            QMessageBox.warning(self, "缺少候选歌词", "以下音乐没有待写入歌词：\n\n" + "\n".join(missing[:12]))
            return
        current = self._current_record()
        if current in records and current.candidate:
            edited = self.editor.toPlainText().strip()
            if edited and edited != current.candidate.effective_lrc_text.strip():
                answer = QMessageBox.question(
                    self, "尚未保存的编辑", "当前歌曲的编辑区有尚未保存的修改。要先保存并使用编辑后的歌词写入吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self.save_editor()
        invalid = []
        for record in records:
            report = validate_structure(parse_lrc(record.candidate.effective_lrc_text), audio_duration=record.duration, strict_online=False)
            if not report.valid:
                invalid.append(f"{record.path.name}：{report.summary}")
        if invalid:
            QMessageBox.warning(self, "结构检查未通过", "不会写入以下音乐：\n\n" + "\n".join(invalid[:12]))
            return
        unverified = [record.path.name for record in records if not record.verification or not record.verification.passed]
        if unverified and not self._confirm_unverified_write(unverified):
            return
        existing = [record.path.name for record in records if record.current_lyrics]
        if existing:
            answer = QMessageBox.question(
                self,
                "替换已有歌词",
                f"{len(existing)} 首音乐已经有内嵌歌词或同名 LRC。写入会替换所选输出位置的歌词；只备份被覆盖的旧 LRC，不长期备份原音频。继续吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        embed = self.embed_checkbox.isChecked()
        create_lrc = self.lrc_checkbox.isChecked()
        # Qt Multimedia keeps the selected FLAC open even after playback stops.
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.setAudioOutput(None)
        QApplication.processEvents()

        if inline_during_verification:
            self._start_inline_write(
                records[0],
                records[0].candidate.effective_lrc_text,
                embed=embed,
                create_lrc=create_lrc,
            )
            return

        def task(progress, control):
            results: list[WriteResult] = []
            try:
                for index, record in enumerate(records, 1):
                    control.checkpoint()
                    progress((index - 1, len(records), f"安全写入 {index}/{len(records)}：{record.path.name}"))
                    results.append(
                        write_outputs(
                            record.path,
                            record.candidate.effective_lrc_text,
                            embed=embed,
                            create_lrc=create_lrc,
                            data_paths=self.paths,
                        )
                    )
            except TaskCancelled:
                raise TaskCancelled(results)
            progress((len(records), len(records), "写入完成"))
            return results

        self._start_task(task, self._write_done, "正在写入临时副本并复读验证", "write")

    def _start_inline_write(
        self,
        record: TrackRecord,
        lrc_text: str,
        *,
        embed: bool,
        create_lrc: bool,
    ) -> None:
        self._inline_write_in_progress = True
        self._inline_write_notice = None
        self._update_actions()

        def task(_progress, _control):
            return write_outputs(
                record.path,
                lrc_text,
                embed=embed,
                create_lrc=create_lrc,
                data_paths=self.paths,
            )

        worker = Worker(task, TaskControl())
        worker.signals.result.connect(self._inline_write_done)
        worker.signals.error.connect(self._inline_write_error)
        worker.signals.finished.connect(self._inline_write_finished)
        self.pool.start(worker)

    def _inline_write_done(self, result: WriteResult) -> None:
        record = next((item for item in self.model.records if item.path == result.path), None)
        if result.success:
            if record:
                refreshed = inspect_track(record.path)
                record.embedded_lyrics = refreshed.embedded_lyrics
                record.external_lyrics = refreshed.external_lyrics
                record.checked = False
            self.model.refresh()
            self._inline_write_notice = ("information", "写入完成", "当前歌曲已写入并复读验证成功。")
            return
        LOGGER.error("lyric write failed for %s: %s", result.path, result.message)
        self._inline_write_notice = ("warning", "写入失败", f"{result.path.name}：{result.message}")

    def _inline_write_error(self, details: str) -> None:
        LOGGER.error("inline lyric write failed\n%s", details)
        summary = details.strip().splitlines()[-1] if details.strip() else "未知错误"
        self._inline_write_notice = ("critical", "写入失败", summary + "\n\n详细信息已写入日志。")

    def _inline_write_finished(self) -> None:
        self._inline_write_in_progress = False
        if self.player.audioOutput() is None:
            self.player.setAudioOutput(self.audio_output)
        current = self._current_record()
        if current:
            self._show_record(current)
        self._update_actions()
        notice = self._inline_write_notice
        self._inline_write_notice = None
        if notice:
            getattr(QMessageBox, notice[0])(self, notice[1], notice[2])

    def _confirm_unverified_write(self, filenames: list[str]) -> bool:
        if self._allow_unverified_writes_this_session:
            return True
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("音频复核尚未通过")
        dialog.setText("以下歌词尚未通过模型复核。为避免内容或时间轴错配，建议取消并先执行“音频复核勾选项”。")
        dialog.setInformativeText(
            "\n".join(filenames[:10])
            + "\n\n确认这些歌词已由你人工核对，仍要强制写入吗？"
        )
        remember = QCheckBox("本次运行始终允许，退出前不再提示")
        remember.setAccessibleName("本次运行不再提示未通过复核的歌词")
        dialog.setCheckBox(remember)
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        result = dialog.exec()
        accepted = result == QMessageBox.StandardButton.Yes.value
        if accepted and remember.isChecked():
            self._allow_unverified_writes_this_session = True
        return accepted

    def _write_done(self, results: list[WriteResult], cancelled: bool = False) -> None:
        success = 0
        failures: list[str] = []
        by_path = {str(record.path): record for record in self.model.records}
        for result in results:
            record = by_path.get(str(result.path))
            if result.success:
                success += 1
                if record:
                    refreshed = inspect_track(record.path)
                    record.embedded_lyrics = refreshed.embedded_lyrics
                    record.external_lyrics = refreshed.external_lyrics
                    record.checked = False
            else:
                failures.append(f"{result.path.name}：{result.message}")
                LOGGER.error("lyric write failed for %s: %s", result.path, result.message)
        self.model.refresh()
        current = self._current_record()
        if current:
            self._show_record(current)
        if cancelled:
            if failures:
                QMessageBox.warning(self, "写入已终止", f"终止前成功 {success} 首，失败 {len(failures)} 首：\n\n" + "\n".join(failures[:10]))
        elif failures:
            QMessageBox.warning(self, "部分写入失败", f"成功 {success} 首，失败 {len(failures)} 首：\n\n" + "\n".join(failures[:10]))
        else:
            QMessageBox.information(self, "写入完成", f"{success} 首音乐已写入并复读验证成功。原音频不会保存在备份目录。")
        self.status_label.setText(f"写入完成：成功 {success}，失败 {len(failures)}")

    def _output_toggled(self, source: QCheckBox, checked: bool) -> None:
        if not checked and not self.embed_checkbox.isChecked() and not self.lrc_checkbox.isChecked():
            source.blockSignals(True)
            source.setChecked(True)
            source.blockSignals(False)
            self.status_label.setText("至少需要保留一种歌词输出方式")
        self.settings.embed_lyrics = self.embed_checkbox.isChecked()
        self.settings.create_lrc = self.lrc_checkbox.isChecked()
        self._update_actions()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.settings, self.paths, self.theme_manager)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply()
            save_settings(self.paths, self.settings)
            self.status_label.setText("设置已保存")

    def show_help(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("使用说明")
        dialog.resize(720, 620)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(HELP_TEXT)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _start_task(self, function, result_handler, initial_status: str, task_kind: str = "generic") -> None:
        if self.busy or self._inline_write_in_progress:
            QMessageBox.information(self, "任务正在运行", "请等待当前任务完成。")
            return
        self.busy = True
        self.active_task_kind = task_kind
        self._task_was_cancelled = False
        self.task_control = TaskControl()
        self.status_label.setText(initial_status)
        self.progress.setRange(0, 0)
        self._update_actions()
        worker = Worker(function, self.task_control)
        worker.signals.progress.connect(self._task_progress)
        worker.signals.result.connect(result_handler)
        worker.signals.cancelled.connect(lambda partial: self._task_cancelled(partial, result_handler))
        worker.signals.error.connect(self._task_error)
        worker.signals.finished.connect(self._task_finished)
        self.pool.start(worker)

    def _toggle_task_pause(self) -> None:
        if not self.busy or not self.task_control:
            return
        paused = not self.task_control.paused
        self.task_control.set_paused(paused)
        self.pause_task_button.setText("继续" if paused else "暂停")
        if paused:
            self.status_label.setText("任务已暂停；当前网络请求或文件写入完成后生效")
        else:
            self.status_label.setText("任务继续运行")

    def _cancel_task(self) -> None:
        if not self.busy or not self.task_control:
            return
        self.task_control.cancel()
        self.pause_task_button.setEnabled(False)
        self.stop_task_button.setEnabled(False)
        self.status_label.setText("正在终止；将保留已经完成的歌曲结果")

    def _task_cancelled(self, partial_result, result_handler) -> None:
        self._task_was_cancelled = True
        if partial_result:
            if getattr(result_handler, "__name__", "") == "_write_done":
                result_handler(partial_result, cancelled=True)
            else:
                result_handler(partial_result)
        completed = len(partial_result) if hasattr(partial_result, "__len__") else 0
        self.status_label.setText(f"任务已终止；已保留 {completed} 首已完成结果")

    def _task_progress(self, payload) -> None:
        if isinstance(payload, dict) and payload.get("kind") == "verification_result":
            record = next((item for item in self.model.records if str(item.path) == payload.get("path")), None)
            if record:
                self._apply_verification_result(record, payload.get("payload"))
            return
        if isinstance(payload, tuple) and len(payload) == 3:
            current, total, message = payload
            self.status_label.setText(str(message))
            if total:
                self.progress.setRange(0, total)
                self.progress.setValue(current)
            else:
                self.progress.setRange(0, 0)
        else:
            self.status_label.setText(str(payload))

    def _task_error(self, details: str) -> None:
        LOGGER.error("background task failed\n%s", details)
        summary = details.strip().splitlines()[-1] if details.strip() else "未知错误"
        QMessageBox.critical(self, "任务失败", summary + "\n\n详细信息已写入日志。")
        self.status_label.setText(f"任务失败：{summary}")

    def _task_finished(self) -> None:
        finished_task_kind = self.active_task_kind
        self.busy = False
        self.task_control = None
        if self.player.audioOutput() is None and not self._inline_write_in_progress:
            self.player.setAudioOutput(self.audio_output)
            current = self._current_record()
            if current:
                self._show_record(current)
        self.active_task_kind = None
        if finished_task_kind == "verify":
            self._verification_pending_paths.clear()
        self.pause_task_button.setText("暂停")
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 100)
        if self.progress.maximum() > 0 and not self._task_was_cancelled:
            self.progress.setValue(self.progress.maximum())
        self._update_actions()
        if self._close_when_task_finishes:
            self._close_when_task_finishes = False
            self.close()

    def _update_actions(self) -> None:
        checked = [record for record in self.model.records if record.checked]
        has_candidate = any(record.candidate for record in checked)
        writing = self.active_task_kind == "write" or self._inline_write_in_progress
        available = not self.busy and not self._inline_write_in_progress
        self.table.setEnabled(not writing)
        self.play_button.setEnabled(bool(self._current_record()) and not writing)
        self.seek_slider.setEnabled(bool(self._current_record()) and not writing)
        self.search_button.setEnabled(bool(checked) and available)
        self.verify_button.setEnabled(has_candidate and available)
        self.write_button.setEnabled(bool(checked) and all(record.candidate for record in checked) and available)
        self.write_current_button.setEnabled(self._can_write_current())
        self.pause_task_button.setEnabled(self.busy)
        self.stop_task_button.setEnabled(self.busy)
        existing = sum(bool(record.current_lyrics) for record in self.model.records)
        missing = len(self.model.records) - existing
        verified = sum(bool(record.verification and record.verification.passed) for record in self.model.records)
        self.summary.setText(
            f"共 {len(self.model.records)} 首，其中已有歌词 {existing} 首，缺少歌词 {missing} 首，音频复核通过 {verified} 首；已勾选 {len(checked)} 首"
        )


def _format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, remainder = divmod(total, 60)
    return f"{minutes}:{remainder:02d}"


HELP_TEXT = """音乐歌词工具使用说明

1. 程序启动后自动扫描 EXE 所在文件夹；也可以选择其他文件夹或多选音乐。新扫描到的音乐默认不勾选。
2. 勾选音乐后点击“① 联网搜索勾选项”。所有候选必须先通过 LRC 结构、时间顺序和音频时长检查，不合格结果不会进入候选列表。
3. 在右侧选择候选，也可以导入 LRC、载入当前歌词并手工编辑。编辑器支持撤销/重做。整体移动只移动正文；开头署名保持原时间，并与正文至少间隔 0.1 秒。
4. 点击“② 音频复核勾选项”。每首音频只识别一次，再比较所有有效候选；中文同音字可模糊匹配。每完成一首立即刷新列表。大幅偏移会被视为漏识别或重复段落错配，不会自动修改。底部按钮可暂停、继续或终止任务。
5. 模型只有在文字相似度、锚点数量、覆盖范围和残差同时达标时，才会判定通过或生成整体偏移/线性漂移校正预览。低置信度、现场版、合唱、强混响等情况不会自动修改。
6. 选择“嵌入音频文件”和/或“生成同名 LRC”，然后点击“③ 写入勾选项”。试听确认单首歌词后，也可直接点击“写入当前歌词”。
7. 写入先在同目录临时副本中完成，复读验证歌词、原标签、封面、格式和时长后才替换原文件。原音频只在写入期间作为临时回滚点，成功后立即删除，不会保存在备份目录；仅被覆盖的旧 LRC 会保留小体积备份。

兼容性说明

- MP3 同时写入 USLT 与 SYLT。
- FLAC/OGG/Opus 写入 LYRICS 与 SYNCEDLYRICS。
- M4A/MP4 写入 ©lyr。不同播放器对内嵌同步歌词的支持不一致，同名 LRC 通常最稳妥。
- 原始 AAC 等不支持可靠标签写入的格式，可以只选择生成同名 LRC。

音频识别限制

模型在普通录音室版本上较可靠，但伴奏、混响、粤语唱腔、现场版、串烧、翻唱和合唱会降低识别率。“低置信度”及“无法复核”不等于歌词一定错误。使用“试听与歌词”切换当前或候选歌词；播放进度可点击或拖动，音量滑块位于音量按钮中。当前真实歌词只读，需要编辑时先载入为候选。

国内网易云、QQ、酷狗并发搜索优先；LRCLIB 仅国内没有有效结果时尝试，无法连接会在本批跳过。酷我与汽水当前没有验证可用的稳定歌词接口，因此不显示为可选来源。
"""
