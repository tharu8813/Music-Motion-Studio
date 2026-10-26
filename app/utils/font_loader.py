"""Safe registration of user-provided application fonts."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase


_REGISTERED_FONTS: dict[str, tuple[str, ...]] = {}


def application_font_paths() -> tuple[str, ...]:
    """Fonts already loaded by this editor, including imported project fonts."""
    return tuple(path for path, families in _REGISTERED_FONTS.items() if families)


def load_application_font(path: str | Path) -> tuple[str, ...]:
    """Register a TTF/OTF font for this app session and return its family names.

    The source still stores its original file path, allowing a project opened in a
    later session to register the same font again before its Canvas is painted.
    """
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return ()
    key = str(resolved)
    if key in _REGISTERED_FONTS:
        return _REGISTERED_FONTS[key]
    if not resolved.is_file() or resolved.suffix.lower() not in {".ttf", ".otf"}:
        return ()
    font_id = QFontDatabase.addApplicationFont(str(resolved))
    families = tuple(QFontDatabase.applicationFontFamilies(font_id)) if font_id >= 0 else ()
    _REGISTERED_FONTS[key] = families
    return families
