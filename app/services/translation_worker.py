"""Shared cancellable worker for translation and account/model management."""
from threading import Event
from PySide6.QtCore import QThread, Signal
from app.services.lyrics_translation_service import TranslationCancelled, TranslationError


class TranslationWorker(QThread):
    progress = Signal(int, int)
    activity = Signal(object)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work, self.cancel = work, Event()
        self.value, self.error = None, ""

    def run(self):
        try:
            self.value = self.work(self.cancel, self.progress.emit)
        except TranslationCancelled:
            pass
        except TranslationError as error:
            self.error = str(error)
        except Exception:
            self.error = "처리하지 못했습니다. 설정·연결 상태를 확인하세요. / Request failed; check settings and connection."
