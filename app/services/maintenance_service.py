"""Settings → Maintenance: installed-file integrity and resetting the program's saved state.

The release build writes ``integrity.json`` (every shipped file's SHA-256) next to
the executable; see the end of ``playlist_canvas.spec``. A source checkout has no
manifest, so only an installed build can be verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Callable

from PySide6.QtCore import QSettings, QStandardPaths

MANIFEST_NAME = "integrity.json"
KEPT_SETTINGS = (
    "export/ffmpeg_path", "migration/playlist_canvas_brand",
    "migration/music_motion_studio_brand",
)
"""Survive a reset: the managed FFmpeg is only found through its saved path, and
dropping the migration flag would copy the legacy brand's settings back in."""


@dataclass
class IntegrityReport:
    checked: int
    missing: list[str] = field(default_factory=list)
    damaged: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.damaged


def installation_root() -> Path | None:
    """The installed program folder, or None when running from source."""
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None


def verify_installation(
    root: Path, progress: Callable[[float], bool] | None = None,
) -> IntegrityReport | None:
    """Hash every file the manifest lists; None if ``progress`` returned False (cancelled).

    Raises OSError/ValueError when the manifest itself is missing or unreadable.
    """
    files = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))["files"]
    if not isinstance(files, dict):
        raise ValueError("Invalid integrity manifest")
    report = IntegrityReport(len(files))
    for index, (name, expected) in enumerate(files.items()):
        if progress is not None and not progress(index / max(1, len(files))):
            return None
        try:
            with (root / name).open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
        except FileNotFoundError:
            report.missing.append(name)
            continue
        except OSError:
            actual = ""
        if actual != expected:
            report.damaged.append(name)
    return report


def reset_program_data() -> None:
    """Forget every preference and cache; projects, presets, language packs and FFmpeg stay."""
    from app.automix.cache import clear_caches

    settings = QSettings()
    kept = {key: settings.value(key) for key in KEPT_SETTINGS if settings.contains(key)}
    settings.clear()
    for key, value in kept.items():
        settings.setValue(key, value)
    settings.sync()
    clear_caches()
    data_root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if data_root:
        for name in ("preview-proxies", "updates"):
            shutil.rmtree(Path(data_root) / name, ignore_errors=True)


def restart_arguments() -> tuple[str, list[str]]:
    """Program and arguments that start a fresh copy of this app (no project argument)."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, [str(Path(sys.argv[0]).resolve())]
