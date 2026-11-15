"""Persistent most-recently-used project paths."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from app.services.project_persistence_service import is_project_path


class RecentProjectsService(QObject):
    """Store a bounded, existing-only list of recently used projects."""

    changed = Signal()
    _settings_key = "projects/recent"
    _maximum_count = 10

    def projects(self) -> list[Path]:
        """Return valid recent projects in most-recent-first order."""
        saved = QSettings().value(self._settings_key, [])
        if isinstance(saved, str):
            saved = [saved]
        paths: list[Path] = []
        seen: set[str] = set()
        for value in saved if isinstance(saved, list) else []:
            path = Path(str(value)).expanduser()
            if not is_project_path(path):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = str(resolved).casefold()
            if key in seen or not is_project_path(resolved) or not resolved.is_file():
                continue
            seen.add(key)
            paths.append(resolved)
            if len(paths) >= self._maximum_count:
                break
        normalized = [str(path) for path in paths]
        if normalized != [str(value) for value in saved]:
            QSettings().setValue(self._settings_key, normalized)
        return paths

    def add(self, project_path: str | Path) -> None:
        """Move a project to the front of the recent list."""
        candidate = Path(project_path).expanduser()
        if not is_project_path(candidate):
            return
        path = candidate.resolve()
        if not is_project_path(path) or not path.is_file():
            return
        key = str(path).casefold()
        projects = [entry for entry in self.projects() if str(entry).casefold() != key]
        projects.insert(0, path)
        QSettings().setValue(
            self._settings_key,
            [str(entry) for entry in projects[:self._maximum_count]],
        )
        self.changed.emit()

    def remove(self, project_path: str | Path) -> None:
        """Forget one recent entry without touching the project file."""
        key = str(Path(project_path).expanduser().resolve()).casefold()
        projects = [entry for entry in self.projects() if str(entry).casefold() != key]
        QSettings().setValue(self._settings_key, [str(entry) for entry in projects])
        self.changed.emit()

    def clear(self) -> None:
        """Forget all recent entries without deleting any files."""
        QSettings().remove(self._settings_key)
        self.changed.emit()

