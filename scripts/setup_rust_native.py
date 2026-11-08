"""One-time Rust extension bootstrap for the active development Python.

From the repository root:
    .\\.venv\\Scripts\\python.exe scripts/setup_rust_native.py

Requires the Rust MSVC toolchain and C++ Build Tools on Windows.
This script installs maturin and the native wheel only into the interpreter
used to run it. It never modifies system-wide Rust or other Python installs.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from rust_prerequisites import (
    NATIVE_MANIFEST, RustPrerequisiteError, require_native_extension,
    require_rust_toolchain,
)


def _run(command: list[str]) -> None:
    print("Running:", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    require_rust_toolchain()
    _run([sys.executable, "-m", "pip", "install", "maturin>=1.9.4,<2"])
    executable = Path(sys.executable).parent / (
        "maturin.exe" if sys.platform == "win32" else "maturin"
    )
    maturin = str(executable) if executable.is_file() else shutil.which("maturin")
    if not maturin:
        raise RustPrerequisiteError(
            "maturin was installed but its command was not found. "
            "Check this Python environment's Scripts directory."
        )
    with TemporaryDirectory(prefix="music-motion-native-") as directory:
        _run([
            maturin, "build", "--release", "--manifest-path",
            str(NATIVE_MANIFEST), "--interpreter", sys.executable,
            "--out", directory,
        ])
        wheels = list(Path(directory).glob("music_motion_native-*.whl"))
        if len(wheels) != 1:
            raise RustPrerequisiteError(
                f"Expected exactly one native wheel in {directory}; found {len(wheels)}"
            )
        _run([
            sys.executable, "-m", "pip", "install",
            "--upgrade", "--force-reinstall", "--no-deps", str(wheels[0]),
        ])
    require_native_extension()
    print("Rust export extension is ready for source debugging and packaging.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RustPrerequisiteError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"[Rust setup] {error}") from error
