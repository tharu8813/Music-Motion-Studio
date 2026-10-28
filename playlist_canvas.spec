# -*- mode: python ; coding: utf-8 -*-
"""One-folder Windows build specification for Playlist Canvas."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH)
# Dependency test suites can import compiler probes and start child processes;
# they are not runtime dependencies of the installed editor.
def runtime_submodule(name):
    return "tests" not in name.split(".")


numpy_datas, numpy_binaries, numpy_hiddenimports = collect_all("numpy", filter_submodules=runtime_submodule)
# AutoMix's default analyzer (app/automix/analysis/basic.py) needs librosa
# and the native/JIT pieces of its dependency chain: numba's LLVM JIT
# (llvmlite), scipy's compiled extensions, scikit-learn, and soundfile's
# bundled libsndfile. collect_all mirrors the existing numpy approach above
# so PyInstaller finds their binaries/data even where a hooks-contrib entry
# does not already cover them.
#
# Sonara (song structure) is a ~2 MB Rust extension and ships too, as does the
# default analyzer: Beat This! (beats, ~23 MB) and Open-Unmix (vocals, ~9 MB)
# exported to int8 ONNX and run by onnxruntime (its PyInstaller hook collects
# the native runtime; see app/automix/analysis/beat_this_onnx.py). The PyTorch-based analyzers
# (Beat This!, Demucs) do NOT: they made the installer ~1 GB and pinned the
# CPU during first analysis. They are excluded below even when installed in
# the build venv, because PyInstaller would otherwise follow their
# function-level imports.
#
# collect_all only *warns* for a package that is not installed, which is how
# 1.2.0.6 shipped without librosa and silently lost AutoMix. A release build
# must fail instead.
import importlib.util

AUTOMIX_PACKAGES = ("librosa", "numba", "llvmlite", "scipy", "sklearn", "soundfile", "sonara")
HEAVY_ANALYZER_PACKAGES = (
    "torch", "torchaudio", "beat_this", "rotary_embedding_torch", "demucs", "julius",
)
AUTOMIX_MODEL_DIRECTORY = project_root / "app" / "automix" / "analysis" / "models"
AUTOMIX_MODEL_FILES = (
    "beat_this_final0_int8.onnx", "BEAT_THIS_LICENSE.txt", "umxhq_vocals_int8.onnx", "OPEN_UNMIX_LICENSE.txt",
    "yamnet.onnx", "yamnet_class_map.csv", "YAMNET_LICENSE.txt", "YAMNET_NOTICE.md",
)
_missing = [name for name in (*AUTOMIX_PACKAGES, "onnxruntime") if importlib.util.find_spec(name) is None]
_missing += [name for name in AUTOMIX_MODEL_FILES if not (AUTOMIX_MODEL_DIRECTORY / name).is_file()]
if _missing:
    raise SystemExit(
        "Release build environment is missing AutoMix packages or models: " + ", ".join(_missing)
        + ". Install requirements-lock.txt and requirements-packaging.txt (see PACKAGING.md)."
    )

automix_datas: list = []
automix_binaries: list = []
automix_hiddenimports: list = []
for _package in AUTOMIX_PACKAGES:
    _datas, _binaries, _hiddenimports = collect_all(_package, filter_submodules=runtime_submodule)
    automix_datas += _datas
    automix_binaries += _binaries
    automix_hiddenimports += _hiddenimports

analysis = Analysis(
    [str(project_root / "main.py")],
    pathex=[str(project_root)],
    binaries=numpy_binaries + automix_binaries,
    # FFmpeg is deliberately not bundled. The installed application downloads
    # and checksum-verifies its own per-user copy on first use.
    datas=numpy_datas + automix_datas + [
        (str(project_root / "app" / "ui" / "studio.qss"), "app/ui"),
        (str(project_root / "app" / "assets" / "icons" / "check.svg"), "assets/icons"),
        (str(project_root / "app" / "resources" / "app_icon.ico"), "app/resources"),
        (str(project_root / "app" / "resources" / "language-pack-template.json"), "app/resources"),
        (str(project_root / "app" / "resources" / "ko.json"), "app/resources"),
        (str(project_root / "app" / "resources" / "en.json"), "app/resources"),
        (str(project_root / "app" / "assets" / "icons" / "spin_down.svg"), "assets/icons"),
        (str(project_root / "app" / "assets" / "icons" / "spin_up.svg"), "assets/icons"),
        (str(project_root / "app" / "assets" / "icons" / "spin_down_disabled.svg"), "assets/icons"),
        (str(project_root / "app" / "assets" / "icons" / "spin_up_disabled.svg"), "assets/icons"),
        (str(project_root / "LICENSE.txt"), "."),
    ] + [(str(AUTOMIX_MODEL_DIRECTORY / name), "app/automix/analysis/models") for name in AUTOMIX_MODEL_FILES]
    # User Guide screenshots (tools/capture_help_images.py), one folder per language.
    + [(str(path), f"app/resources/help/{path.parent.name}")
       for path in sorted((project_root / "app" / "resources" / "help").glob("*/*.png"))],
    hiddenimports=numpy_hiddenimports + automix_hiddenimports + [
        "PySide6.QtSvg", "PySide6.QtMultimedia", "PySide6.QtOpenGLWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Build-host customization must never change the installed Python runtime.
    excludes=[*HEAVY_ANALYZER_PACKAGES, "sitecustomize", "usercustomize"],
    noarchive=False,
)
# Qt uses Windows' system ICU. An unrelated Poppler installation on the build
# host can shadow it with a same-name DLL whose exports are incompatible.
analysis.binaries = [
    entry for entry in analysis.binaries
    if not (Path(entry[0]).name.lower() == "icuuc.dll" and "poppler" in entry[1].lower())
]
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Playlist Canvas",
    icon=str(project_root / "app" / "resources" / "app_icon.ico"),
    version=str(project_root / "windows_version_info.txt"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
COLLECT(
    executable,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="Playlist Canvas",
)

# Settings → Maintenance → Integrity check compares the installed files with
# this list (app/services/maintenance_service.py). Skip what setup.iss excludes.
import hashlib
import json

_dist = Path(DISTPATH) / "Playlist Canvas"
_integrity = {}
for _path in sorted(_dist.rglob("*")):
    if _path.is_file() and _path.suffix.lower() not in {".pdb", ".log", ".tmp"} and _path.name != "integrity.json":
        with _path.open("rb") as _handle:
            _integrity[_path.relative_to(_dist).as_posix()] = hashlib.file_digest(_handle, "sha256").hexdigest()
(_dist / "integrity.json").write_text(json.dumps({"files": _integrity}, indent=1), encoding="utf-8")
