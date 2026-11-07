"""Filename helpers shared by the export dialog and controller."""

from __future__ import annotations

from pathlib import Path
import re


_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def project_export_stem(title: str) -> str:
    """Preserve the project title while making it safe as a Windows filename."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title)).strip(" .")
    if not value or value.casefold() in {"untitled project", "제목 없는 프로젝트"}:
        value = "playlist"
    value = value[:160].rstrip(" .") or "playlist"
    if value.split(".", 1)[0].upper() in _RESERVED:
        value = "_" + value
    return value


def available_export_path(target: str | Path) -> Path:
    """Find the first unused filename without overwriting an existing export.

    A sibling such as 'Project (2).mp4' is used when 'Project.mp4' exists.
    The extension stays unchanged for video, audio, and subtitle exports.
    """
    original = Path(target)
    candidate = original
    if not candidate.exists():
        return candidate
    for number in range(2, 100_000):
        candidate = original.with_name(f"{original.stem} ({number}){original.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("Unable to find an unused export filename.")
