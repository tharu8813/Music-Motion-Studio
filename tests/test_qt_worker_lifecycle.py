from __future__ import annotations

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.utils.qt_worker_lifecycle import stop_qthread_now, wait_for_qthread  # noqa: E402


class _Worker(QThread):
    def __init__(self, release: threading.Event) -> None:
        super().__init__()
        self.release = release

    def run(self) -> None:
        self.release.wait(5)


class StopQThreadNowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_running_worker_is_waited_for_and_deleted_immediately(self) -> None:
        release = threading.Event()
        worker = _Worker(release)
        owner_handler_calls: list[bool] = []
        worker.finished.connect(lambda: owner_handler_calls.append(True))
        worker.start()
        threading.Timer(0.1, release.set).start()

        stop_qthread_now(worker)

        self.assertFalse(shiboken6.isValid(worker))
        # The owner's own finished handler must not also run (it would deleteLater a second time).
        self.app.processEvents()
        self.assertEqual(owner_handler_calls, [])

    def test_worker_finishing_before_the_wait_does_not_hang(self) -> None:
        release = threading.Event()
        release.set()
        worker = _Worker(release)
        worker.start()
        while not worker.isFinished():  # finished already emitted, never seen by a loop
            pass
        wait_for_qthread(worker)
        self.assertTrue(shiboken6.isValid(worker))
        stop_qthread_now(worker)
        self.assertFalse(shiboken6.isValid(worker))

    def test_none_and_already_deleted_workers_are_ignored(self) -> None:
        wait_for_qthread(None)
        stop_qthread_now(None)
        worker = _Worker(threading.Event())
        shiboken6.delete(worker)
        wait_for_qthread(worker)
        stop_qthread_now(worker)  # must not raise

    def test_wait_keeps_gui_timers_and_owner_completion_working(self) -> None:
        release = threading.Event()
        worker = _Worker(release)
        finished = []
        pulses = []
        worker.finished.connect(lambda: finished.append(True))
        timer = QTimer()
        timer.timeout.connect(lambda: pulses.append(True))
        timer.start(10)
        QTimer.singleShot(80, release.set)
        worker.start()
        try:
            wait_for_qthread(worker)
            self.app.processEvents()
            self.assertTrue(pulses)
            self.assertEqual(finished, [True])
            self.assertFalse(worker.isRunning())
            self.assertTrue(shiboken6.isValid(worker))
        finally:
            release.set()
            timer.stop()
            stop_qthread_now(worker)

    def test_fast_owner_deleted_workers_can_be_waited_repeatedly(self) -> None:
        for _ in range(40):
            release = threading.Event()
            release.set()
            worker = _Worker(release)
            worker.finished.connect(worker.deleteLater)
            worker.start()
            wait_for_qthread(worker)
            stop_qthread_now(worker)
            self.assertFalse(shiboken6.isValid(worker))


if __name__ == "__main__":
    unittest.main()
