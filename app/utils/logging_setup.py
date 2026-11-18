"""Application logging and last-resort GUI crash reporting."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import traceback as traceback_module
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Type

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot, qInstallMessageHandler, QtMsgType
from PySide6.QtWidgets import QApplication, QWidget

from app.dialogs.crash_report_dialog import CrashReportDialog
from app.utils.crash_diagnostics import (
    CrashTracker, build_crash_report, save_crash_report,
)


LOGGER = logging.getLogger(__name__)
LOG_FILE_NAME = "playlist-canvas.log"
RUN_MARKER_NAME = "running.marker"
NATIVE_CRASH_FILE_NAME = "native-crash.log"
_CRASH_BRIDGE: "_CrashBridge | None" = None
_NATIVE_CRASH_FILE = None


class _CrashBridge(QObject):
    """Deliver fatal Python errors to the GUI thread, including from worker threads."""

    report_requested = Signal(str, str, str)

    def __init__(self, application: QApplication) -> None:
        super().__init__(application)
        self._showing_report = False
        self._tracker = CrashTracker()
        self._active_dialog: CrashReportDialog | None = None
        self.report_requested.connect(self._show_report, Qt.ConnectionType.QueuedConnection)

    @Slot(str, str, str)
    def _show_report(self, exception_type: str, message: str, traceback_text: str) -> None:
        # Track *before* the modal guard. Nested Qt event loops can deliver
        # another exception while a report is still open.
        repeat_count = (
            1 if exception_type == "Previous abnormal shutdown"
            else self._tracker.record()
        )
        application = QApplication.instance()
        parent = _find_editor_window(application)
        log_path = getattr(logging.getLogger(), "_playlist_canvas_log_path", None)
        report_text = ""
        report_path: str | None = None
        try:
            report_text = build_crash_report(
                exception_type, message, traceback_text, log_path,
                window=parent, repeat_count=repeat_count,
            )
            report_path = str(save_crash_report(
                report_text, log_directory() / "crash-reports"
            ))
        except Exception:
            LOGGER.exception("Unable to persist detailed crash report")
            report_text = report_text or traceback_text

        if self._showing_report:
            if self._active_dialog is not None:
                self._active_dialog.set_repeat_count(repeat_count)
            return
        self._showing_report = True
        try:
            choice = _show_unexpected_error(
                parent, exception_type, message, traceback_text,
                repeat_count=repeat_count, report_text=report_text,
                report_path=report_path,
                on_dialog_created=lambda dialog: setattr(self, "_active_dialog", dialog),
            )
            self._active_dialog = None
            if choice:
                handle_crash_exit_choice(parent, choice)
        except Exception:
            LOGGER.critical("Unable to show or handle the crash report dialog", exc_info=True)
        finally:
            self._active_dialog = None
            self._showing_report = False


def log_directory() -> Path:
    """Return the per-user folder reserved for recoverable diagnostic logs."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PlaylistCanvas" / "logs"


def _run_marker_path() -> Path:
    return log_directory().parent / RUN_MARKER_NAME


def configure_logging() -> Path | None:
    """Configure UTF-8 rotating file logging once and return its location when available."""
    root = logging.getLogger()
    if getattr(root, "_playlist_canvas_configured", False):
        return getattr(root, "_playlist_canvas_log_path", None)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.setLevel(logging.INFO)
    root.addHandler(stream_handler)
    log_path: Path | None = None
    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / LOG_FILE_NAME
        file_handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        root.warning("Could not create the application log file.", exc_info=True)
    _enable_native_crash_trace()
    logging.captureWarnings(True)
    root._playlist_canvas_configured = True  # type: ignore[attr-defined]
    root._playlist_canvas_log_path = log_path  # type: ignore[attr-defined]
    return log_path


def _enable_native_crash_trace() -> None:
    """Write every thread's Python stack to a file when the process dies natively.

    An access violation inside Qt/PySide never reaches sys.excepthook; without
    this the log simply stops and the faulting Python call is unknown.
    """
    global _NATIVE_CRASH_FILE
    if _NATIVE_CRASH_FILE is not None:
        return
    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        _NATIVE_CRASH_FILE = open(directory / NATIVE_CRASH_FILE_NAME, "a", encoding="utf-8")
        _NATIVE_CRASH_FILE.write(f"\n=== session started {datetime.now(timezone.utc).isoformat()} ===\n")
        _NATIVE_CRASH_FILE.flush()
        faulthandler.enable(_NATIVE_CRASH_FILE, all_threads=True)
    except OSError:
        LOGGER.warning("Could not enable the native crash trace", exc_info=True)


def install_exception_hook() -> None:
    """Send uncaught main-thread and standard-thread exceptions to a detailed crash dialog."""
    global _CRASH_BRIDGE
    sys.excepthook = _exception_hook
    threading.excepthook = _thread_exception_hook
    application = QApplication.instance()
    if application is not None and _CRASH_BRIDGE is None:
        _CRASH_BRIDGE = _CrashBridge(application)
        _install_abnormal_shutdown_marker(application)
    qInstallMessageHandler(_qt_message_handler)


def _install_abnormal_shutdown_marker(application: QApplication) -> None:
    """Report a prior native/forced termination on the next successful startup."""
    try:
        marker = _run_marker_path()
        previous = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"started_at={datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
    except OSError:
        LOGGER.warning("Could not maintain the abnormal-shutdown marker", exc_info=True)
        return

    def clear_marker() -> None:
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove the run marker", exc_info=True)

    application.aboutToQuit.connect(clear_marker)
    if previous and _CRASH_BRIDGE is not None:
        QTimer.singleShot(
            0,
            lambda: _CRASH_BRIDGE.report_requested.emit(
                "Previous abnormal shutdown",
                "The previous app session did not close normally.",
                "A previous application session ended without a normal shutdown.\n\n" + previous,
            ),
        )


def _exception_hook(exc_type: Type[BaseException], exception: BaseException,
                    traceback: TracebackType | None) -> None:
    """Record an unexpected exception before scheduling a safe GUI report."""
    LOGGER.critical("Unhandled application exception", exc_info=(exc_type, exception, traceback))
    application = QApplication.instance()
    if application is None or _CRASH_BRIDGE is None:
        sys.__excepthook__(exc_type, exception, traceback)
        return
    traceback_text = "".join(traceback_module.format_exception(exc_type, exception, traceback))
    _CRASH_BRIDGE.report_requested.emit(exc_type.__name__, str(exception), traceback_text)


def _thread_exception_hook(arguments: threading.ExceptHookArgs) -> None:
    """Route uncaught standard-library thread errors through the same crash UI."""
    _exception_hook(arguments.exc_type, arguments.exc_value, arguments.exc_traceback)


def report_unexpected_error(context: str, exception: BaseException) -> None:
    """Log a caught-but-unexpected failure and show the same crash report UI.

    Use this for defensive ``except Exception`` paths where the application can
    remain open but the operation itself has reached an invalid state.
    """
    exc_type, _value, traceback = sys.exc_info()
    exception_type = exc_type.__name__ if exc_type is not None else type(exception).__name__
    LOGGER.critical("Unexpected error in %s", context, exc_info=(exc_type, exception, traceback))
    traceback_text = f"Context: {context}\n\n" + "".join(
        traceback_module.format_exception(exc_type or type(exception), exception, traceback)
    )
    if _CRASH_BRIDGE is not None:
        _CRASH_BRIDGE.report_requested.emit(exception_type, f"{context}: {exception}", traceback_text)


def _qt_message_handler(message_type: QtMsgType, context: object, message: str) -> None:
    """Persist Qt diagnostics; fatal Qt/native termination cannot reliably show UI."""
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    LOGGER.log(levels.get(message_type, logging.ERROR), "Qt: %s", message)
    if message_type is QtMsgType.QtCriticalMsg:
        trace = "Qt critical message\n\n" + message
        if _CRASH_BRIDGE is not None:
            _CRASH_BRIDGE.report_requested.emit("QtCriticalMessage", message, trace)


def record_crash_occurrence() -> int:
    """Share the same repeat counter with recoverable project-load reports."""
    return _CRASH_BRIDGE._tracker.record() if _CRASH_BRIDGE is not None else 1


def _find_editor_window(application: QApplication | None) -> QWidget | None:
    """Prefer the actual editor over a dialog or a splash screen."""
    if application is None:
        return None
    candidates = [application.activeWindow(), *application.topLevelWidgets()]
    for candidate in candidates:
        current = candidate
        while isinstance(current, QWidget):
            if callable(getattr(current, "_save_project", None)):
                return current
            current = current.parentWidget()
    return application.activeWindow()


def _show_unexpected_error(
    parent: QWidget | None,
    exception_type: str,
    exception_message: str,
    traceback_text: str,
    *,
    repeat_count: int = 1,
    report_text: str | None = None,
    report_path: str | None = None,
    on_dialog_created=None,
) -> str | None:
    """Show a report; only explicit menu actions request an app exit."""
    translator = getattr(parent, "translator", None)
    korean = getattr(getattr(translator, "language", None), "value", "ko") == "ko"
    log_path = getattr(logging.getLogger(), "_playlist_canvas_log_path", None)
    dialog = CrashReportDialog(
        exception_type=exception_type,
        exception_message=exception_message,
        traceback_text=traceback_text,
        log_path=str(log_path) if log_path else None,
        korean=korean,
        parent=parent,
        report_text=report_text,
        report_path=report_path,
        repeat_count=repeat_count,
    )
    if on_dialog_created is not None:
        on_dialog_created(dialog)
    dialog.exec()
    return dialog.exit_choice


def handle_crash_exit_choice(window: QWidget | None, choice: str) -> bool:
    """Execute one user-selected exit mode, never silently discarding edits.

    Safe exits respect the existing MainWindow.closeEvent worker guards.
    Force quit is the only mode that bypasses cleanup, after confirmation.
    """
    from PySide6.QtWidgets import QMessageBox

    korean = getattr(getattr(getattr(window, "translator", None), "language", None),
                     "value", "ko") == "ko"

    if choice == "force":
        answer = QMessageBox.warning(
            window,
            "강제 종료 확인" if korean else "Confirm force quit",
            "프로젝트 저장과 작업 정리를 건너뛰고 즉시 종료합니다.\n"
            "저장되지 않은 변경 사항은 사라집니다. 정말 강제 종료할까요?"
            if korean else
            "Quit immediately without saving or cleaning up background tasks?\n"
            "Unsaved changes will be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        LOGGER.critical("User explicitly requested forced termination after repeated failures")
        logging.shutdown()
        os._exit(1)

    if choice not in {"save", "save_as", "discard"}:
        return False
    if window is None or not callable(getattr(window, "_save_project", None)):
        QMessageBox.warning(
            window,
            "종료 불가" if korean else "Cannot exit",
            "편집기 창을 찾을 수 없습니다." if korean else "The editor window is unavailable.",
        )
        return False

    if choice == "discard":
        response = QMessageBox.warning(
            window,
            "저장하지 않고 종료" if korean else "Exit without saving",
            "저장되지 않은 프로젝트 변경 사항을 버리고 종료할까요?"
            if korean else "Discard unsaved project changes and exit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return False
    else:
        try:
            saved = window._save_project(
                force_choose=(choice == "save_as"), wait_for_completion=True
            )
            session = getattr(window, "project_session", None)
            if not saved or getattr(session, "dirty", True):
                QMessageBox.warning(
                    window,
                    "종료 취소" if korean else "Exit cancelled",
                    "프로젝트가 저장되지 않았습니다. 편집기를 유지합니다."
                    if korean else "The project was not saved. The editor will remain open.",
                )
                return False
        except Exception:
            LOGGER.exception("Crash recovery project save failed")
            QMessageBox.warning(
                window,
                "종료 취소" if korean else "Exit cancelled",
                "저장 중 또 다른 오류가 발생했습니다. 프로젝트를 보존하기 위해 종료하지 않습니다."
                if korean else
                "Another error occurred while saving. The editor will remain open.",
            )
            return False

    # Skip the *second* unsaved-changes question only for the user's explicit
    # discard choice. All worker guards and normal cleanup stay enabled.
    if choice == "discard":
        setattr(window, "_crash_report_discard_on_close", True)
    try:
        closed = bool(window.close())
    except Exception:
        LOGGER.exception("Normal shutdown after repeated crash failed")
        closed = False
    finally:
        if choice == "discard":
            setattr(window, "_crash_report_discard_on_close", False)
    if not closed:
        QMessageBox.warning(
            window,
            "종료 대기" if korean else "Shutdown blocked",
            "작업 중인 내보내기·저장·다운로드가 종료를 막고 있습니다. "
            "작업을 마친 뒤 다시 시도하세요."
            if korean else
            "An export, save, or download is still running. Finish it and retry.",
        )
    return closed
