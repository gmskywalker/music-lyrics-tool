from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


APP_DATA_NAME = "音乐歌词工具数据"


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class AppSettings:
    last_folder: str = ""
    recursive: bool = False
    embed_lyrics: bool = True
    create_lrc: bool = True
    provider_lrclib: bool = True
    provider_netease: bool = True
    provider_qq: bool = True
    provider_kugou: bool = True
    model_name: str = "small"
    compute_device: str = "auto"
    recognition_language: str = "auto"
    tolerance_seconds: float = 1.5
    playback_volume: int = 80
    theme: str = "system"
    geometry: str = ""
    splitter_state: str = ""


class DataPaths:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or executable_dir()) / APP_DATA_NAME
        self.settings_file = self.base_dir / "设置.json"
        self.cache_dir = self.base_dir / "缓存"
        self.logs_dir = self.base_dir / "日志"
        self.models_dir = self.base_dir / "识别模型"
        self.gpu_runtime_dir = self.base_dir / "GPU运行库"
        self.backups_dir = self.base_dir / "备份"

    def ensure(self) -> None:
        for path in (self.base_dir, self.cache_dir, self.logs_dir, self.models_dir, self.backups_dir):
            path.mkdir(parents=True, exist_ok=True)

    def clear_logs_and_cache(self) -> None:
        self.ensure()
        for target in (self.cache_dir, self.logs_dir):
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)


def load_settings(paths: DataPaths) -> AppSettings:
    paths.ensure()
    try:
        payload = json.loads(paths.settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return AppSettings()
    allowed = AppSettings.__dataclass_fields__
    return AppSettings(**{key: value for key, value in payload.items() if key in allowed})


def save_settings(paths: DataPaths, settings: AppSettings) -> None:
    paths.ensure()
    temporary = paths.settings_file.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(paths.settings_file)
