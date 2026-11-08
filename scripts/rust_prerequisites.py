"""Rust prerequisites for source debugging and release builds.

Only developer workflows call these checks. PyInstaller-frozen applications
do not call this module or require Rust on end-user computers.
"""

from __future__ import annotations

import importlib
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_MODULE = "music_motion_native"
NATIVE_DISTRIBUTION = "music-motion-native"
NATIVE_MANIFEST = PROJECT_ROOT / "native" / "export_analysis" / "Cargo.toml"
REQUIRED_NATIVE_FUNCTIONS = ("reduce_spectra", "pcm_rms", "pcm_waveform", "pack_bgr24")


class RustPrerequisiteError(RuntimeError):
    """Actionable prerequisite error, before opening a development GUI."""


def require_rust_toolchain() -> dict[str, str]:
    """Require a usable rustc and cargo on PATH; never auto-install Rust."""
    installed: dict[str, str] = {}
    for name in ("rustc", "cargo"):
        executable = shutil.which(name)
        if not executable:
            raise RustPrerequisiteError(
                f"{name} was not found on PATH. Install the stable Rust MSVC "
                "toolchain (https://rustup.rs/), close and reopen the terminal, "
                "then verify 'rustc --version' and 'cargo --version'. "
                "Windows Rust builds also need Visual Studio C++ Build Tools."
            )
        try:
            completed = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RustPrerequisiteError(
                f"Unable to execute {name} --version: {error}. "
                "Check the Rust installation and terminal PATH."
            ) from error
        if completed.returncode != 0:
            raise RustPrerequisiteError(
                f"{name} --version failed: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        installed[name] = completed.stdout.strip()
    return installed


def required_native_version() -> str:
    """The extension version must match this checkout, not an older wheel."""
    try:
        return str(tomllib.loads(NATIVE_MANIFEST.read_text(encoding="utf-8"))["package"]["version"])
    except (OSError, KeyError, ValueError) as error:
        raise RustPrerequisiteError(
            f"Could not read Rust package metadata from {NATIVE_MANIFEST}: {error}"
        ) from error


def require_native_extension() -> None:
    """Reject a stale or missing Python extension in the development venv."""
    expected = required_native_version()
    command = "python scripts/setup_rust_native.py"
    try:
        actual = metadata.version(NATIVE_DISTRIBUTION)
    except metadata.PackageNotFoundError as error:
        raise RustPrerequisiteError(
            f"Rust extension {NATIVE_DISTRIBUTION} {expected} is not installed "
            f"in this Python environment. Run: {command}"
        ) from error
    if actual != expected:
        raise RustPrerequisiteError(
            f"Installed Rust extension version {actual} does not match the "
            f"checkout version {expected}. Run: {command}"
        )
    try:
        module = importlib.import_module(NATIVE_MODULE)
    except (ImportError, OSError) as error:
        raise RustPrerequisiteError(
            f"Rust extension cannot be imported: {error}. Run: {command}"
        ) from error
    missing = [name for name in REQUIRED_NATIVE_FUNCTIONS if not callable(getattr(module, name, None))]
    if missing:
        raise RustPrerequisiteError(
            f"Rust extension is missing APIs {', '.join(missing)}. Run: {command}"
        )


def require_developer_environment(*, require_native: bool = True) -> None:
    """Gate source launches/tests/builds, never packaged end-user execution."""
    require_rust_toolchain()
    if require_native:
        require_native_extension()


if __name__ == "__main__":
    try:
        require_developer_environment()
    except RustPrerequisiteError as error:
        raise SystemExit(f"[Rust prerequisite] {error}") from error
    print("Rust toolchain and project native extension are ready.")
