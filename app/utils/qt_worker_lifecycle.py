"""The one verified way to stop and delete a cancelled QThread on shutdown.

Every controller that owns a worker thread must end it through
``stop_qthread_now``. The order below is what fixed real Windows 0xC0000409
fail-fast crashes; keeping a single copy means a fix here reaches all of them.
"""

from __future__ import annotations

from PySide6.QtCore import SIGNAL, QCoreApplication, QEvent, QEventLoop, QThread, QTimer


def wait_for_qthread(worker: QThread | None) -> None:
    """Wait with paints/completion signals flowing, including an already-finished thread."""
    if worker is None:
        return
    try:
        running = worker.isRunning()
    except RuntimeError:
        return  # the C++ QThread is already gone
    if running:
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        # QThread emits finished *before* it stops reporting isRunning(), so a
        # finish landing between the check above and connect() would never
        # quit the loop. Polling isFinished() closes that window.
        poll = QTimer()

        def quit_if_finished() -> None:
            try:
                if worker.isFinished():
                    loop.quit()
            except RuntimeError:
                loop.quit()  # An owner's normal finished handler can delete it.

        poll.timeout.connect(quit_if_finished)
        poll.start(50)
        if not worker.isFinished():
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        poll.stop()
        try:
            worker.finished.disconnect(loop.quit)
        except (RuntimeError, TypeError):
            pass
    try:
        worker.wait()
    except RuntimeError:
        pass


def stop_qthread_now(worker: QThread | None) -> None:
    """Disconnect the owner's finished handler, join, then force deletion now.

    Preserve this order: deferred deletion from an owner's nested shutdown loop
    previously raced a live native thread and caused Windows 0xC0000409 crashes.
    """
    if worker is None:
        return
    try:
        if worker.receivers(SIGNAL("finished()")) > 0:
            worker.finished.disconnect()
    except RuntimeError:
        return
    wait_for_qthread(worker)
    try:
        worker.deleteLater()
    except RuntimeError:
        return
    QCoreApplication.sendPostedEvents(worker, QEvent.Type.DeferredDelete)
