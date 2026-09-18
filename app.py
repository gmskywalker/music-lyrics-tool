from __future__ import annotations

import argparse
import ctypes
import json
import logging
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from lyrictool.gui import MainWindow
from lyrictool.metadata import inspect_track, read_text_file
from lyrictool.settings import DataPaths, load_settings
from lyrictool.theme import FluentThemeManager
from lyrictool.verifier import verify_audio


def configure_logging(paths: DataPaths) -> None:
    paths.ensure()
    log_file = paths.logs_dir / "音乐歌词工具.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--folder")
    parser.add_argument("--screenshot")
    parser.add_argument("--no-auto-scan", action="store_true")
    parser.add_argument("--diagnose-audio")
    parser.add_argument("--diagnose-lrc")
    parser.add_argument("--diagnose-model-dir")
    parser.add_argument("--diagnose-output")
    parser.add_argument("--diagnose-language", choices=("auto", "zh", "yue", "en"), default="auto")
    parser.add_argument("--diagnose-model", choices=("tiny", "base", "small", "medium", "large-v3"), default="small")
    parser.add_argument("--diagnose-device", choices=("auto", "cpu"), default="auto")
    return parser.parse_known_args()[0]


def run_model_diagnostic(args: argparse.Namespace) -> int:
    output = Path(args.diagnose_output).resolve()
    messages: list[str] = []
    try:
        audio_path = Path(args.diagnose_audio).resolve()
        lrc_path = Path(args.diagnose_lrc).resolve()
        model_dir = Path(args.diagnose_model_dir).resolve()
        track = inspect_track(audio_path)
        result = verify_audio(
            audio_path,
            read_text_file(lrc_path),
            model_name=args.diagnose_model,
            model_dir=model_dir,
            audio_duration=track.duration,
            tolerance_seconds=1.5,
            language_mode=args.diagnose_language,
            progress=messages.append,
            compute_device=args.diagnose_device,
        )
        payload = {
            "ok": True,
            "status": result.status,
            "summary": result.summary,
            "details": result.details,
            "progress": messages,
        }
        exit_code = 0
    except Exception:
        payload = {"ok": False, "error": traceback.format_exc(), "progress": messages}
        exit_code = 2
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return exit_code


def main() -> int:
    args = parse_args()
    diagnostic_values = (args.diagnose_audio, args.diagnose_lrc, args.diagnose_model_dir, args.diagnose_output)
    if any(diagnostic_values):
        if not all(diagnostic_values):
            return 2
        return run_model_diagnostic(args)
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("gmskywalker.music-lyrics-tool.1")
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("AAA音乐歌词工具")
    app.setOrganizationName("gmskywalker")
    paths = DataPaths()
    configure_logging(paths)
    settings = load_settings(paths)
    if args.folder:
        settings.last_folder = str(Path(args.folder).resolve())
    theme_manager = FluentThemeManager(app, settings.theme)
    theme_manager.apply()
    window = MainWindow(paths, settings, theme_manager)
    window.show()

    if not args.no_auto_scan and Path(window.folder_edit.text()).is_dir():
        QTimer.singleShot(100, window.scan_current_folder)
    if args.screenshot:
        target = Path(args.screenshot).resolve()

        def capture() -> None:
            record = window._current_record()
            if record and record.current_lyrics and not record.candidate:
                window.use_current_lyrics()
            window.detail_tabs.setCurrentIndex(3)
            window.preview_source.setCurrentIndex(window.preview_source.findData("candidate"))
            target.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(target))
            app.quit()

        QTimer.singleShot(7_000, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
