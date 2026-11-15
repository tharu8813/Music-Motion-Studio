"""Qt preview worker shutdown ownership.

A worker may be running a non-interruptible probe even after cancellation.
Keep it alive under QApplication until its finished signal can dispose of it,
without blocking the closing preview UI.
"""

from __future__ import annotations

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication


def finish_or_detach_worker(worker: QThread) -> None:
    """Detach from the dialog and delete when finished, never wait synchronously."""
    application = QApplication.instance()
    if application is not None:
        worker.setParent(application)
    worker.finished.connect(worker.deleteLater)
