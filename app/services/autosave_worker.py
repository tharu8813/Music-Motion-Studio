"""Background writer for crash-recovery snapshots."""

from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime

from PySide6.QtCore import QThread, Signal

from app.services.autosave_service import AutosaveService
from app.services.project_service import ProjectError


class AutosaveWorker(QThread):
    """Encode and write one recovery snapshot outside the GUI thread.

    The GUI thread still builds ``document_data`` with ``ProjectDocument
    .to_dict()`` (it reads the live models); this worker only owns the JSON
    encoding and the atomic file replace, which dominate the cost on large
    projects.
    """

    succeeded = Signal(object)  # Path
    failed = Signal(str)

    def __init__(
        self,
        service: AutosaveService,
        document_data: dict[str, object],
        project_path: Path | None,
    ) -> None:
        super().__init__()
        self._service = service
        self._document_data = document_data
        self._project_path = project_path
        self._saved_at = datetime.now(UTC)
        self._generation = service.generation

    def run(self) -> None:
        try:
            path = self._service.write_document_data(
                self._document_data, self._project_path, self._saved_at,
                expected_generation=self._generation,
            )
        except ProjectError as error:
            self.failed.emit(str(error))
        except Exception as error:  # a worker thread must never crash the app
            self.failed.emit(f"Could not save recovery snapshot: {error}")
        else:
            self.succeeded.emit(path)
