"""Keep the interface repainting while files are copied for export."""

from collections.abc import Callable

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QProgressDialog, QWidget

from app.utils.qt_worker_lifecycle import wait_for_qthread


class _FileExportThread[Result](QThread):
    def __init__(self, export: Callable[[], Result]) -> None:
        super().__init__()
        self.export = export
        self.result: Result
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            self.result = self.export()
        except Exception as error:
            self.error = error


def run_file_export[Result](parent: QWidget, label: str, export: Callable[[], Result]) -> Result:
    dialog = QProgressDialog(label, "", 0, 0, parent)
    dialog.setCancelButton(None)
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    worker = _FileExportThread(export)
    dialog.show()
    try:
        worker.start()
        wait_for_qthread(worker)
        if worker.error is not None:
            raise worker.error
        return worker.result
    finally:
        dialog.hide()
        dialog.deleteLater()
        worker.deleteLater()
