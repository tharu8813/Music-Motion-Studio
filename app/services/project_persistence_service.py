"""Small, UI-independent helpers for project persistence decisions."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path


_MAX_PROJECT_STEM = 120
_WINDOWS_RESERVED_STEMS = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def default_project_path(title: str, directory: Path | None = None) -> Path:
    """Build a bounded, Windows-safe project filename without depending on Qt."""
    safe_title = "".join(
        character if character.isalnum() or character in " _-" else "_"
        for character in str(title)
    ).strip() or "playlist"
    if len(safe_title) > _MAX_PROJECT_STEM:
        # Retain an identifying digest so long titles with the same prefix
        # cannot silently map to the same default project filename.
        digest = sha256(safe_title.encode("utf-8")).hexdigest()[:8]
        prefix = safe_title[:_MAX_PROJECT_STEM - len(digest) - 1].rstrip(" _-")
        safe_title = f"{prefix or 'playlist'}-{digest}"
    if safe_title.upper() in _WINDOWS_RESERVED_STEMS:
        safe_title = f"_{safe_title}"
    return (directory or Path.cwd()) / f"{safe_title}.pvsproj"


def is_legacy_project_path(path: Path | None) -> bool:
    """Return whether a path uses the legacy JSON project format."""
    return path is not None and path.suffix.lower() == ".json"
