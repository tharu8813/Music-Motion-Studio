"""Background extraction for an explicitly requested partial project recovery."""

from pathlib import Path
import threading

from PySide6.QtCore import QThread

from app.services.project_salvage_service import ProjectSalvageResult, ProjectSalvageService


class ProjectSalvageWorker(QThread):
    """Own one salvage operation; no live editor objects cross threads."""

    def __init__(self, source: Path, directory: Path) -> None:
        super().__init__()
        self.source = source
        self.directory = directory
        self.cancel_event = threading.Event()
        self.result: ProjectSalvageResult | None = None
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.result = ProjectSalvageService.salvage(
                self.source, self.directory, cancel_event=self.cancel_event,
            )
        except Exception as error:
            self.error = error
