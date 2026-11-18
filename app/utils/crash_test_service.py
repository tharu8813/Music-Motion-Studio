"""Explicit user-triggered crash-report test cases for Settings > Maintenance.

Test modes are intentionally opt-in. Most exercise the existing report pipeline
without terminating the editor; only the distinctly labeled native-abort mode
intentionally kills the entire process.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

from PySide6.QtCore import qCritical

from app.utils.logging_setup import report_unexpected_error

LOGGER = logging.getLogger(__name__)

CRASH_TEST_MODES = (
    "main_exception",
    "worker_exception",
    "caught_exception",
    "qt_critical",
    "repeated_exception",
    "native_abort",
)


class IntentionalCrashTestError(RuntimeError):
    """Clearly distinguish opt-in tests from product bugs in crash reports."""


def _caught_test_error(label: str) -> None:
    try:
        raise IntentionalCrashTestError(f"[Intentional crash test] {label}")
    except IntentionalCrashTestError as error:
        report_unexpected_error(f"Maintenance crash test: {label}", error)


def run_crash_test(mode: str) -> None:
    """Trigger exactly one selected test pathway from the GUI thread.

    Never invoke native_abort in automated tests: os.abort() is deliberately
    irreversible and produces an abnormal-shutdown marker next time.
    """
    if mode not in CRASH_TEST_MODES:
        raise ValueError(f"Unsupported crash test mode: {mode}")

    LOGGER.warning("User intentionally started crash diagnostic test: %s", mode)

    if mode == "caught_exception":
        _caught_test_error("caught operation exception")
    elif mode == "main_exception":
        # Calls the registered main-thread hook with a real exception and
        # traceback, without risking Qt event-loop internals on platforms
        # where a raised Python slot exception can terminate the process.
        try:
            raise IntentionalCrashTestError(
                "[Intentional crash test] main-thread unhandled exception hook"
            )
        except IntentionalCrashTestError:
            sys.excepthook(*sys.exc_info())
    elif mode == "worker_exception":
        def fail_in_worker() -> None:
            raise IntentionalCrashTestError(
                "[Intentional crash test] unhandled background thread exception"
            )

        threading.Thread(
            target=fail_in_worker,
            name="MMS-CrashTest-Worker",
            daemon=True,
        ).start()
    elif mode == "qt_critical":
        # The installed qInstallMessageHandler logs and routes QtCritical.
        # Unlike qFatal, qCritical does not terminate the process.
        qCritical("[Intentional crash test] Qt critical message")
    elif mode == "repeated_exception":
        # Both reports are queued into the Qt GUI loop. The second arrives
        # even if the first report dialog has opened its own modal event loop.
        _caught_test_error("repeat test 1/2")
        _caught_test_error("repeat test 2/2")
    elif mode == "native_abort":
        LOGGER.critical(
            "User confirmed intentional native process abort in maintenance"
        )
        logging.shutdown()
        os.abort()
