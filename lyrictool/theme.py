from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QWidget


LIGHT = {
    "window_background": "#f5f5f5",
    "canvas_background": "#ffffff",
    "surface_background": "#ffffff",
    "surface_secondary": "#f0f0f0",
    "surface_tertiary": "#e8e8e8",
    "text_primary": "#242424",
    "text_secondary": "#616161",
    "text_disabled": "#a0a0a0",
    "border_primary": "#d1d1d1",
    "border_subtle": "#e0e0e0",
    "focus_border": "#0f6cbd",
    "brand_background": "#0f6cbd",
    "brand_hover": "#115ea3",
    "brand_pressed": "#0c3b5e",
    "on_brand": "#ffffff",
    "selection": "#e5f1fb",
    "success": "#107c10",
    "warning": "#8a5700",
    "danger": "#c50f1f",
    "danger_background": "#fdf3f4",
}

DARK = {
    "window_background": "#202020",
    "canvas_background": "#1b1b1b",
    "surface_background": "#292929",
    "surface_secondary": "#252525",
    "surface_tertiary": "#333333",
    "text_primary": "#ffffff",
    "text_secondary": "#d6d6d6",
    "text_disabled": "#777777",
    "border_primary": "#666666",
    "border_subtle": "#3d3d3d",
    "focus_border": "#479ef5",
    "brand_background": "#115ea3",
    "brand_hover": "#0f6cbd",
    "brand_pressed": "#0c3b5e",
    "on_brand": "#ffffff",
    "selection": "#14395b",
    "success": "#54b054",
    "warning": "#fce100",
    "danger": "#f1707b",
    "danger_background": "#3b1e21",
}


def set_fluent_property(widget: QWidget, name: str, value: str | bool) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class FluentThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, app: QApplication, mode: str = "system") -> None:
        super().__init__()
        self.app = app
        self.mode = mode
        hints = self.app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._system_scheme_changed)

    def _system_scheme_changed(self, _scheme) -> None:
        if self.mode == "system":
            self.apply()

    def _dark(self) -> bool:
        if self.mode == "dark":
            return True
        if self.mode == "light":
            return False
        try:
            from PySide6.QtCore import Qt

            return self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except (AttributeError, RuntimeError):
            return self.app.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in {"system", "light", "dark"} else "system"
        self.apply()

    def apply(self) -> None:
        tokens = DARK if self._dark() else LIGHT
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(tokens["window_background"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["text_primary"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(tokens["canvas_background"]))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["surface_secondary"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(tokens["text_primary"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(tokens["surface_background"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["text_primary"]))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens["brand_background"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens["on_brand"]))
        palette.setColor(QPalette.ColorRole.Link, QColor(tokens["focus_border"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(tokens["text_disabled"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(tokens["text_disabled"]))
        self.app.setPalette(palette)
        self.app.setStyleSheet(self._qss(tokens))
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        if font.pointSizeF() < 9.0:
            font.setPointSizeF(9.0)
        self.app.setFont(font)
        self.theme_changed.emit("dark" if self._dark() else "light")

    @staticmethod
    def _qss(t: dict[str, str]) -> str:
        return f"""
            QMainWindow, QDialog {{ background: {t['window_background']}; color: {t['text_primary']}; }}
            QWidget {{ color: {t['text_primary']}; }}
            QLabel[role="secondary"] {{ color: {t['text_secondary']}; }}
            QFrame[fluentRole="toolbar"], QFrame[fluentRole="panel"] {{
                background: {t['surface_background']};
                border: 1px solid {t['border_subtle']};
                border-radius: 6px;
            }}
            QPushButton, QToolButton {{
                min-height: 30px; padding: 3px 12px;
                background: {t['surface_background']}; color: {t['text_primary']};
                border: 2px solid {t['border_primary']}; border-radius: 4px;
            }}
            QPushButton:hover, QToolButton:hover {{ background: {t['surface_secondary']}; }}
            QPushButton:pressed, QToolButton:pressed {{ background: {t['surface_tertiary']}; }}
            QPushButton:focus, QToolButton:focus {{ border-color: {t['focus_border']}; }}
            QPushButton:disabled, QToolButton:disabled {{ color: {t['text_disabled']}; background: {t['surface_secondary']}; }}
            QPushButton[fluentAppearance="primary"] {{
                min-height: 36px; font-weight: 600; background: {t['brand_background']};
                color: {t['on_brand']}; border-color: {t['brand_background']};
            }}
            QPushButton[fluentAppearance="primary"]:hover {{ background: {t['brand_hover']}; border-color: {t['brand_hover']}; }}
            QPushButton[fluentAppearance="primary"]:pressed {{ background: {t['brand_pressed']}; border-color: {t['brand_pressed']}; }}
            QPushButton[fluentAppearance="primary"]:focus {{ border-color: {t['focus_border']}; }}
            QPushButton[fluentAppearance="danger"] {{ color: {t['danger']}; }}
            QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox {{
                background: {t['canvas_background']}; color: {t['text_primary']};
                border: 2px solid {t['border_primary']}; border-radius: 4px; padding: 5px 7px;
                selection-background-color: {t['brand_background']}; selection-color: {t['on_brand']};
            }}
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border-color: {t['focus_border']}; }}
            QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ color: {t['text_disabled']}; background: {t['surface_secondary']}; }}
            QTableView {{
                background: {t['canvas_background']}; alternate-background-color: {t['surface_secondary']};
                border: 1px solid {t['border_primary']}; border-radius: 4px;
                selection-background-color: {t['selection']}; selection-color: {t['text_primary']};
                gridline-color: {t['border_subtle']};
            }}
            QTableView::item {{ padding: 5px 6px; border-bottom: 1px solid {t['border_subtle']}; }}
            QTableView::item:hover {{ background: {t['surface_secondary']}; }}
            QTableView::item:selected {{ background: {t['selection']}; color: {t['text_primary']}; }}
            QTableView::item:focus {{ border: 1px solid {t['focus_border']}; }}
            QHeaderView::section {{
                background: {t['surface_secondary']}; color: {t['text_primary']};
                padding: 7px 6px; border: none; border-right: 1px solid {t['border_subtle']};
                border-bottom: 1px solid {t['border_primary']}; font-weight: 600;
            }}
            QTabWidget::pane {{ border: 1px solid {t['border_primary']}; background: {t['surface_background']}; }}
            QTabBar::tab {{ padding: 7px 14px; background: {t['surface_secondary']}; border: 1px solid {t['border_subtle']}; }}
            QTabBar::tab:selected {{ background: {t['surface_background']}; border-bottom: 2px solid {t['brand_background']}; }}
            QProgressBar {{ border: 1px solid {t['border_primary']}; border-radius: 3px; background: {t['surface_secondary']}; text-align: center; }}
            QProgressBar::chunk {{ background: {t['brand_background']}; border-radius: 2px; }}
            QStatusBar {{ background: {t['surface_secondary']}; color: {t['text_secondary']}; }}
            QCheckBox {{ spacing: 7px; }}
            QCheckBox:focus {{ color: {t['focus_border']}; }}
            QMenu {{ background: {t['surface_background']}; border: 1px solid {t['border_primary']}; }}
            QMenu::item:selected {{ background: {t['selection']}; }}
            QToolTip {{ background: {t['surface_background']}; color: {t['text_primary']}; border: 1px solid {t['border_primary']}; }}
        """
