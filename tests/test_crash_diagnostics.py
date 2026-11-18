"""Regression coverage for crash diagnostics and user-controlled safe shutdown."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from app.dialogs.crash_report_dialog import CrashReportDialog
from app.dialogs.project_crash_report_dialog import ProjectCrashReportDialog
from app.utils.crash_diagnostics import (
    CrashTracker, build_crash_report, crash_signature, save_crash_report,
)
from app.utils.logging_setup import handle_crash_exit_choice


class CrashDiagnosticsTests(unittest.TestCase):
    def test_repeat_detection_uses_rolling_90_second_window(self) -> None:
        now = [0.0]
        tracker = CrashTracker(clock=lambda: now[0])
        self.assertEqual(tracker.record(), 1)
        now[0] = 20
        self.assertEqual(tracker.record(), 2)
        now[0] = 100
        self.assertEqual(tracker.record(), 2)
        now[0] = 191
        self.assertEqual(tracker.record(), 1)

    def test_report_carries_environment_traceback_and_bounded_log_excerpt(self) -> None:
        with TemporaryDirectory() as root:
            directory = Path(root)
            app_log = directory / "playlist-canvas.log"
            app_log.write_text("log line one\nlog line two\n", encoding="utf-8")
            report = build_crash_report(
                "AttributeError", "'Canvas' object has no attribute 'foo'",
                "Context: Preparing export frames\nTraceback (most recent call last):\nline 18",
                app_log, repeat_count=2,
            )
            self.assertIn("Music Motion Studio", report)
            self.assertIn("OS:", report)
            self.assertIn("Python:", report)
            self.assertIn("Memory:", report)
            self.assertIn("Repeated reports within 90 seconds: 2", report)
            self.assertIn("Error signature:", report)
            self.assertIn("Preparing export frames", report)
            self.assertIn("log line two", report)
            self.assertIn("PRIVACY NOTICE", report)
            self.assertNotIn("Environment variables:", report)
            report_file = save_crash_report(report, directory / "crash-reports")
            self.assertTrue(report_file.exists())
            self.assertEqual(report_file.read_text(encoding="utf-8"), report)
            another = save_crash_report(report, directory / "crash-reports")
            self.assertNotEqual(another, report_file)

    def test_signature_ignores_exception_message_when_frame_is_same(self) -> None:
        first = '  File "app.py", line 18, in capture\nAttributeError: alpha'
        second = '  File "app.py", line 18, in capture\nAttributeError: beta'
        self.assertEqual(
            crash_signature("AttributeError", first),
            crash_signature("AttributeError", second),
        )


class FakeEditor(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.project_session = SimpleNamespace(dirty=True)
        self.translator = SimpleNamespace(language=SimpleNamespace(value="ko"))
        self.saved = []
        self.closed = 0
        self.discard_flag_on_close = False
        self.save_ok = True

    def _save_project(self, force_choose=False, *, wait_for_completion=False):
        self.saved.append((force_choose, wait_for_completion))
        if self.save_ok:
            self.project_session.dirty = False
        return self.save_ok

    def close(self) -> bool:
        self.closed += 1
        self.discard_flag_on_close = getattr(
            self, "_crash_report_discard_on_close", False
        )
        return True


class CrashDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_general_report_exit_menu_only_appears_after_repeat(self) -> None:
        dialog = CrashReportDialog(
            "AttributeError", "missing field", "full stack", None, True,
            report_text="full expanded report", repeat_count=1,
        )
        self.assertTrue(dialog.exit_button.isHidden())
        self.assertEqual(dialog.details.toPlainText(), "full expanded report")
        self.assertEqual(len(dialog.exit_button.menu().actions()), 4)
        dialog.set_repeat_count(2)
        self.assertFalse(dialog.exit_button.isHidden())
        self.assertIn("2회", dialog.repeat_label.text())
        dialog.exit_button.menu().actions()[1].trigger()
        self.assertEqual(dialog.exit_choice, "save_as")
        dialog.deleteLater()

    def test_project_load_report_has_same_repeat_exit_choices(self) -> None:
        dialog = ProjectCrashReportDialog(
            project_path="broken.pvsproj", stage="read",
            exception_type="ValueError", exception_message="bad data",
            cause_type="ValueError", cause_message="bad data", guidance="retry",
            report_text="diagnostics", log_path="logs",
            korean=True, repeat_count=2,
        )
        self.assertFalse(dialog.exit_button.isHidden())
        self.assertEqual(len(dialog.exit_button.menu().actions()), 4)
        dialog.exit_button.menu().actions()[0].trigger()
        self.assertEqual(dialog.exit_choice, "save")
        dialog.deleteLater()

    def test_save_and_exit_only_closes_after_successful_save(self) -> None:
        window = FakeEditor()
        self.assertTrue(handle_crash_exit_choice(window, "save"))
        self.assertEqual(window.saved, [(False, True)])
        self.assertEqual(window.closed, 1)
        window.deleteLater()

    def test_save_as_exit_uses_existing_project_save_as_workflow(self) -> None:
        window = FakeEditor()
        self.assertTrue(handle_crash_exit_choice(window, "save_as"))
        self.assertEqual(window.saved, [(True, True)])
        self.assertEqual(window.closed, 1)
        window.deleteLater()

    def test_save_as_during_another_save_does_not_silently_exit(self) -> None:
        window = FakeEditor()
        window.project_controller = SimpleNamespace(saving=True)
        with patch("PySide6.QtWidgets.QMessageBox.warning"):
            self.assertFalse(handle_crash_exit_choice(window, "save_as"))
        self.assertEqual(window.saved, [])
        self.assertEqual(window.closed, 0)
        window.deleteLater()

    def test_failed_save_keeps_editor_open(self) -> None:
        window = FakeEditor()
        window.save_ok = False
        with patch("PySide6.QtWidgets.QMessageBox.warning"):
            self.assertFalse(handle_crash_exit_choice(window, "save"))
        self.assertEqual(window.closed, 0)
        self.assertTrue(window.project_session.dirty)
        window.deleteLater()

    def test_explicit_discard_skips_only_unsaved_confirmation(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        window = FakeEditor()
        with patch(
            "PySide6.QtWidgets.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.assertTrue(handle_crash_exit_choice(window, "discard"))
        self.assertEqual(window.closed, 1)
        self.assertTrue(window.discard_flag_on_close)
        self.assertFalse(getattr(window, "_crash_report_discard_on_close"))
        self.assertEqual(window.saved, [])
        window.deleteLater()

    def test_discard_no_keeps_editor_open(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        window = FakeEditor()
        with patch(
            "PySide6.QtWidgets.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.No,
        ):
            self.assertFalse(handle_crash_exit_choice(window, "discard"))
        self.assertEqual(window.closed, 0)
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()
