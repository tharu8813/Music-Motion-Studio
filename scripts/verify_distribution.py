"""Validate the files required by a PyInstaller one-folder distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


REQUIRED_FILES = (
    Path("app/resources/app_icon.ico"),
    Path("app/resources/project_file_icon.ico"),
    Path("app/resources/ko.json"),
    Path("app/resources/en.json"),
    Path("app/resources/language-pack-template.json"),
    Path("app/resources/help/ko/canvas_workspace.png"),
    Path("app/resources/help/en/canvas_workspace.png"),
    Path("app/ui/studio.qss"),
    Path("assets/icons/check.svg"),
    Path("assets/icons/spin_down_disabled.svg"),
    Path("assets/icons/spin_up_disabled.svg"),
    Path("LICENSE.txt"),
    Path("python314.dll"),
    # The installer must ship the GPU encoder binary, not just its Python
    # Rust extension. Otherwise the GPU engine fails on the end user's PC.
    Path("native/d3d11_qt_canvas_stream_export.exe"),
    Path("app/automix/analysis/models/beat_this_final0_int8.onnx"),
    Path("app/automix/analysis/models/umxhq_vocals_int8.onnx"),
    Path("app/automix/analysis/models/BEAT_THIS_LICENSE.txt"),
    Path("app/automix/analysis/models/OPEN_UNMIX_LICENSE.txt"),
    Path("app/automix/analysis/models/yamnet.onnx"),
    Path("app/automix/analysis/models/yamnet_class_map.csv"),
    Path("app/automix/analysis/models/YAMNET_LICENSE.txt"),
    Path("app/automix/analysis/models/YAMNET_NOTICE.md"),
)


# PyInstaller emits this manifest after COLLECT. Inno Setup ships it at
# distribution root; the application also uses it for Maintenance -> Integrity.
# Verify required paths are represented there, then actually hash critical
# launch/runtime executables, so a broken build cannot pass as releasable.
CRITICAL_HASH_PATHS = (
    Path("Music Motion Studio.exe"),
    Path("_internal/python314.dll"),
    Path("_internal/native/d3d11_qt_canvas_stream_export.exe"),
)


def _verify_integrity_manifest(
    distribution: Path, required_payload: set[Path],
) -> list[str]:
    manifest = distribution / "integrity.json"
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ["integrity.json (missing or unreadable)"]
    if not isinstance(document, dict) or not isinstance(document.get("files"), dict):
        return ["integrity.json (invalid files map)"]

    records = document["files"]
    errors: list[str] = []
    required = set(CRITICAL_HASH_PATHS)
    required.update(Path("_internal") / name for name in required_payload)
    # Binaries found by glob below also need manifest coverage. The
    # manifest never requires paths outside the known distribution root.
    for name in sorted(required):
        record = name.as_posix()
        expected = records.get(record)
        if not isinstance(expected, str) or re.fullmatch(r"[a-fA-F0-9]{64}", expected) is None:
            errors.append(f"integrity.json missing valid SHA-256: {record}")

    for name in CRITICAL_HASH_PATHS:
        expected = records.get(name.as_posix())
        file = distribution / name
        if not file.is_file() or not isinstance(expected, str) or re.fullmatch(r"[a-fA-F0-9]{64}", expected) is None:
            continue  # The required-file checks above already report this.
        try:
            with file.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError:
            errors.append(f"integrity check could not read: {name.as_posix()}")
        else:
            if actual.lower() != expected.lower():
                errors.append(f"integrity SHA-256 mismatch: {name.as_posix()}")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path, nargs="?", default=root / "dist" / "Music Motion Studio")
    distribution = parser.parse_args().distribution.resolve()
    executable = distribution / "Music Motion Studio.exe"
    internal = distribution / "_internal"
    missing = [str(path) for path in REQUIRED_FILES if not (internal / path).is_file()]
    # New help topics must ship with their pictures in both built-in languages.
    missing += [
        str(path.relative_to(root))
        for path in sorted((root / "app/resources/help").glob("*/*.png"))
        if not (internal / path.relative_to(root)).is_file()
    ]
    if not executable.is_file():
        missing.append(str(executable))
    numpy_core = internal / "numpy" / "_core"
    if not numpy_core.is_dir() or not list(numpy_core.glob("_multiarray_umath*.pyd")):
        missing.append("_internal/numpy/_core/_multiarray_umath*.pyd")
    if not list((internal / "onnxruntime" / "capi").glob("onnxruntime_pybind11_state*.pyd")):
        missing.append("_internal/onnxruntime/capi/onnxruntime_pybind11_state*.pyd")
    native_files: set[Path] = set()
    for package, pattern in (("ctranslate2", "_ext*.pyd"), ("sentencepiece", "_sentencepiece*.pyd")):
        found = list((internal / package).glob(pattern))
        if not found:
            missing.append(f"_internal/{package}/{pattern}")
        native_files.update(path.relative_to(internal) for path in found)
    # The Rust extension is installed on the build host and bundled in the
    # package. It is not compiled or installed on the end user's computer.
    found_rust = list(internal.rglob("music_motion_native*.pyd"))
    if not found_rust:
        missing.append("_internal/**/music_motion_native*.pyd")
    native_files.update(path.relative_to(internal) for path in found_rust)
    required_payload = set(REQUIRED_FILES)
    required_payload.update(path.relative_to(root) for path in (root / "app/resources/help").glob("*/*.png"))
    required_payload.update(native_files)
    # CI must reject a distribution that omitted its release integrity
    # manifest or does not record the essential binaries and assets.
    missing.extend(_verify_integrity_manifest(distribution, required_payload))
    if missing:
        print("Missing distribution files:")
        print("\n".join(f"- {path}" for path in missing))
        return 1
    print(f"Distribution OK: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
