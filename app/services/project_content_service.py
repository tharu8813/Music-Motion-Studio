"""Per-project reusable content library."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.models.project import ProjectContent, ProjectDocument
from app.services.lyrics_service import LYRICS_EXTENSIONS


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".svg"}
FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
CONTENT_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class ProjectContentService(QObject):
    """Own library items and keep referenced project media discoverable."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._items: list[ProjectContent] = []
        self._listed: set[str] = set()  # raw paths known to be in the library

    @property
    def items(self) -> list[ProjectContent]:
        return list(self._items)

    def replace(self, items: Iterable[ProjectContent]) -> None:
        self._items = list(items)
        self._listed.clear()
        self._deduplicate()
        self.changed.emit()

    def add_paths(self, paths: Iterable[str | Path]) -> int:
        existing: set[str] | None = None
        added = 0
        for raw_path in paths:
            # Every source edit re-synchronizes: skip paths already in the library
            # without touching the disk again.
            if str(raw_path) in self._listed:
                continue
            if existing is None:
                existing = {self._key(item.path) for item in self._items}
            path = Path(raw_path).expanduser()
            media_type = self.classify(path)
            if not path.is_file() or media_type is None:
                continue
            resolved = str(path.resolve())
            key = self._key(resolved)
            self._listed.add(str(raw_path))
            if key in existing:
                continue
            self._items.append(ProjectContent(resolved, media_type, path.stem))
            existing.add(key)
            added += 1
        if added:
            self.changed.emit()
        return added

    def remove(self, content_id: str) -> None:
        remaining = [item for item in self._items if item.id != content_id]
        if len(remaining) != len(self._items):
            self._items = remaining
            self._listed.clear()
            self.changed.emit()

    def synchronize(self, document: ProjectDocument) -> None:
        self.add_paths(self._document_paths(document))

    def referenced_keys(self, document: ProjectDocument) -> set[str]:
        """Return the dedup keys of every media path the project currently uses.

        Used by the Project Content panel to mark items already placed on the
        Canvas, cover art, or the playlist as "Added".
        """
        return {self._key(path) for path in self._document_paths(document)}

    @staticmethod
    def _document_paths(document: ProjectDocument) -> list[str]:
        paths: list[str] = []
        for source in document.sources:
            paths.extend(path for path in (source.content_path, source.font_path) if path)
            paths.extend(path for path in source.video_paths if path)
        for track in document.playlist:
            paths.extend(
                path for path in (
                    track.file_path, track.lyrics_path, track.cover_path,
                ) if path
            )
            paths.extend(path for path in track.video_paths if path)
        return paths

    @staticmethod
    def classify(path: Path) -> str | None:
        extension = path.suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            return "image"
        if extension in CONTENT_AUDIO_EXTENSIONS:
            return "audio"
        if extension in VIDEO_EXTENSIONS:
            return "video"
        if extension in FONT_EXTENSIONS:
            return "font"
        if extension in LYRICS_EXTENSIONS:
            return "lyrics"
        return None

    def _deduplicate(self) -> None:
        unique: list[ProjectContent] = []
        seen: set[str] = set()
        for item in self._items:
            key = self._key(item.path)
            if key not in seen:
                unique.append(item)
                seen.add(key)
        self._items = unique

    @staticmethod
    def _key(path: str) -> str:
        try:
            return str(Path(path).expanduser().resolve()).casefold()
        except OSError:
            return str(path).casefold()
