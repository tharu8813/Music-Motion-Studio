"""Validate the files required by a PyInstaller one-folder distribution."""

from __future__ import annotations

import argparse
from pathlib import Path


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
    Path("app/automix/analysis/models/beat_this_final0_int8.onnx"),
    Path("app/automix/analysis/models/umxhq_vocals_int8.onnx"),
    Path("app/automix/analysis/models/BEAT_THIS_LICENSE.txt"),
    Path("app/automix/analysis/models/OPEN_UNMIX_LICENSE.txt"),
    Path("app/automix/analysis/models/yamnet.onnx"),
    Path("app/automix/analysis/models/yamnet_class_map.csv"),
    Path("app/automix/analysis/models/YAMNET_LICENSE.txt"),
    Path("app/automix/analysis/models/YAMNET_NOTICE.md"),
)


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
    if missing:
        print("Missing distribution files:")
        print("\n".join(f"- {path}" for path in missing))
        return 1
    print(f"Distribution OK: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
