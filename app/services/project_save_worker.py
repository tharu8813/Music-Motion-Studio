"""Background project persistence that keeps the Qt event loop responsive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from app.models.project import ProjectDocument
from app.services.project_service import ProjectError, ProjectRevision, ProjectSaveConflict, ProjectService


class ProjectSaveWorker(QThread):
    """Write one immutable project snapshot outside the GUI thread."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        target: str | Path,
        document_data: dict[str, Any],
        thumbnail: QImage | None = None, *,
        expected_revision: ProjectRevision | None = None,
    ) -> None:
        super().__init__()
        self.target = Path(target)
        self.expected_revision = expected_revision
        self.saved_revision: ProjectRevision | None = None
        self.conflict = False
        # Plain dictionaries are already detached from the live Qt models. Model
        # validation/construction also happens in run(), away from the GUI thread.
        self.document_data = document_data
        self.thumbnail = QImage(thumbnail) if thumbnail is not None else None

    def run(self) -> None:
        try:
            document = ProjectDocument.from_dict(self.document_data)
            saved, self.saved_revision = ProjectService.save_with_revision(
                self.target, document, self.thumbnail, expected_revision=self.expected_revision,
            )
            if self.saved_revision is None:
                raise ProjectSaveConflict(
                    "The published project revision could not be verified. "
                    "Your edits and recovery snapshot are retained. Save under a different name."
                )
        except ProjectSaveConflict as error:
            self.conflict = True
            self.failed.emit(str(error))
        except ProjectError as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(f"Could not save project: {error}")
        else:
            self.succeeded.emit(saved)
