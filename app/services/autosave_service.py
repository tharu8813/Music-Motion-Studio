"""Project-scoped automatic backup and crash-recovery management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading
from uuid import uuid4

from app.models.project import ProjectDocument
from app.services.project_service import ProjectError


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """One durable recovery document plus the project it belongs to."""

    path: Path
    document: ProjectDocument
    project_path: Path | None
    saved_at: datetime
    dirty: bool = True


class AutosaveService:
    """Maintain separate, atomic crash-recovery snapshots for each project."""

    schema_version = 1

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve() / "recoveries"
        self._session_id = uuid4().hex
        self.generation = 0
        self._write_lock = threading.Lock()

    def save(self, document: ProjectDocument, project_path: Path | None, *, dirty: bool = True) -> RecoverySnapshot:
        """Atomically store a snapshot without changing the user's project file.

        ``document.to_dict()`` reads the live models, so this call must stay on
        the thread that owns them; :meth:`write_document_data` does the rest and
        is safe to hand to a worker.
        """
        saved_at = datetime.now(UTC)
        target = self.write_document_data(
            document.to_dict(), project_path, saved_at, dirty=dirty,
        )
        return RecoverySnapshot(target, document, project_path, saved_at, dirty)

    def write_document_data(
        self,
        document_data: dict[str, object],
        project_path: Path | None,
        saved_at: datetime | None = None,
        *, dirty: bool = True, expected_generation: int | None = None,
    ) -> Path:
        """Atomically write a recovery snapshot from an already-serialized dict.

        ``document_data`` is plain ``ProjectDocument.to_dict()`` output, fully
        detached from the Qt models, so the JSON encoding and disk write here can
        run on a background thread while the user keeps editing.
        """
        saved_at = saved_at or datetime.now(UTC)
        target = self._path_for(project_path)
        payload = {
            "schema_version": self.schema_version,
            "saved_at": saved_at.isoformat(),
            "project_path": str(project_path.resolve()) if project_path else None,
            "document": document_data,
            "dirty": dirty,
        }
        temporary_path: Path | None = None
        try:
            ProjectDocument.from_dict(document_data)
            self.directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".tmp", dir=self.directory,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
            with self._write_lock:
                if expected_generation is None or expected_generation == self.generation:
                    temporary_path.replace(target)
            return target
        except (OSError, TypeError, ValueError) as error:
            raise ProjectError(f"Could not save recovery snapshot: {error}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def latest_recovery(self) -> RecoverySnapshot | None:
        """Return the newest valid recovery snapshot, leaving it intact for retry."""
        return next(iter(self.recoveries()), None)

    def recoveries(self) -> list[RecoverySnapshot]:
        """Return every valid recovery snapshot from newest to oldest."""
        if not self.directory.is_dir():
            return []
        snapshots: list[RecoverySnapshot] = []
        for path in self.directory.glob("*.recovery.json"):
            try:
                snapshots.append(self.load_snapshot(path))
            except ProjectError:
                # A damaged recovery must not block valid snapshots from other projects.
                continue
        return sorted(snapshots, key=lambda snapshot: snapshot.saved_at, reverse=True)

    def clear(self, project_path: Path | None) -> None:
        """Discard only the snapshot belonging to the specified workspace."""
        target = self._path_for(project_path)
        try:
            with self._write_lock:
                self.generation += 1
                target.unlink(missing_ok=True)
        except OSError as error:
            raise ProjectError(f"Could not remove recovery snapshot: {error}") from error

    def clear_snapshot(self, snapshot: RecoverySnapshot) -> None:
        """Discard an explicitly selected snapshot after the user confirms it."""
        try:
            if snapshot.path.is_file():
                snapshot.path.unlink()
        except OSError as error:
            raise ProjectError(f"Could not remove recovery snapshot: {error}") from error

    def _path_for(self, project_path: Path | None) -> Path:
        identity = str(project_path.resolve()) if project_path else "untitled-workspace"
        digest = sha256(identity.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{digest}-{self._session_id}.recovery.json"

    @staticmethod
    def load_snapshot(path: Path) -> RecoverySnapshot:
        """Read a specific validated snapshot, including an update handoff."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Recovery root must be an object")
            if data.get("schema_version") != AutosaveService.schema_version:
                raise ValueError("Unsupported recovery schema")
            saved_at = datetime.fromisoformat(data["saved_at"])
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=UTC)
            project_value = data.get("project_path")
            project_path = Path(project_value) if project_value else None
            dirty = data.get("dirty", True)
            if not isinstance(dirty, bool):
                raise ValueError("Invalid recovery dirty flag")
            return RecoverySnapshot(
                path=path,
                document=ProjectDocument.from_dict(data["document"]),
                project_path=project_path,
                saved_at=saved_at,
                dirty=dirty,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProjectError(f"Could not open recovery snapshot: {error}") from error
