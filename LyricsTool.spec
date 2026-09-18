# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules


repo_root = Path(SPECPATH)
datas = []
binaries = []
hiddenimports = []

for package in (
    "mutagen",
    "requests",
    "certifi",
    "faster_whisper",
    "ctranslate2",
    "av",
    "tokenizers",
    "huggingface_hub",
    "opencc",
    "pypinyin",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# faster-whisper only uses ONNX Runtime's inference API for Silero VAD.  Pulling
# in the whole package also bundles model-conversion, quantization, transformer,
# and benchmark tools (plus their optional Torch/ONNX imports), none of which are
# used by this application.
binaries += collect_dynamic_libs("onnxruntime")
hiddenimports += collect_submodules("onnxruntime.capi")

a = Analysis(
    [str(repo_root / "app.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "tensorflow",
        "matplotlib",
        "IPython",
        "notebook",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AAA音乐歌词工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
