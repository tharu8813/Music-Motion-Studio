"""Background project loading so .pvsproj extraction never blocks the GUI thread."""

from __future__ import annotations

from pathlib import Path
import threading
from time import monotonic

from PySide6.QtCore import QThread, Signal

from app.models.project import ProjectDocument
from app.preview.album_art import prewarm_cover_thumbnail
from app.services.project_service import ProjectService


class ProjectLoadWorker(QThread):
    """Read, extract and validate one project file outside the GUI thread.

    The original exception is kept (not stringified) so the caller can re-raise
    it and the crash report/guidance still see the real type and traceback.
    """

    # (extracted bytes, total bytes); object because packages exceed 32-bit ints.
    progress = Signal(object, object)
    preparing_covers = Signal()

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.cancel_event = threading.Event()
        self.document: ProjectDocument | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        last_report = 0.0

        def report_progress(done: int, total: int) -> None:
            nonlocal last_report
            now = monotonic()
            if done in (0, total) or now - last_report >= 0.08:
                self.progress.emit(done, total)
                last_report = now

        try:
            self.document = ProjectService.load(
                self.path, progress=report_progress, cancel_event=self.cancel_event,
            )
        except Exception as error:
            self.error = error
            return
        # Decode the playlist's cover thumbnails here, not while the GUI builds its rows.
        if self.document.playlist and not self.cancel_event.is_set():
            self.preparing_covers.emit()
        for track in self.document.playlist:
            if self.cancel_event.is_set():
                break
            prewarm_cover_thumbnail(track.file_path, track.cover_path)
