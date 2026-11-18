"""Settings crash test actions never terminate the app except the opted-in native mode."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from app.dialogs.settings_dialog import SettingsDialog
from app.services.app_settings_service import AppSettings
from app.services.theme_service import Theme
from app.utils.crash_test_service import (
    CRASH_TEST_MODES,
    IntentionalCrashTestError,
    run_crash_test,
)
from app.utils.i18n import Language, Translator


class CrashTestServiceTests(unittest.TestCase):
    def test_all_six_modes_available(self) -> None:
        self.assertEqual(set(CRASH_TEST_MODES), {
            "main_exception", "worker_exception", "caught_exception",
            "qt_critical", "repeated_exception", "native_abort",
        })

    def test_unknown_test_cannot_run(self) -> None:
        with self.assertRaises(ValueError):
            run_crash_test("accidental_typo")

    def test_caught_operation_routes_through_normal_diagnostics(self) -> None:
        with patch("app.utils.crash_test_service.report_unexpected_error") as report:
            run_crash_test("caught_exception")
        report.assert_called_once()
        args = report.call_args.args
        self.assertIn("Maintenance crash test", args[0])
        self.assertIsInstance(args[1], IntentionalCrashTestError)

    def test_main_exception_uses_installed_exception_hook(self) -> None:
        calls = []
        def fake_exception_hook(exc_type, exception, traceback):
            calls.append((exc_type, exception, traceback))
        with patch.object(sys, "excepthook", fake_exception_hook):
            run_crash_test("main_exception")
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], IntentionalCrashTestError)
        self.assertIsInstance(calls[0][1], IntentionalCrashTestError)
        self.assertIsNotNone(calls[0][2])

    def test_worker_exception_uses_thread_exception_hook(self) -> None:
        recorded = []
        ready = threading.Event()
        def fake_thread_hook(args):
            recorded.append(args)
            ready.set()
        with patch.object(threading, "excepthook", fake_thread_hook):
            run_crash_test("worker_exception")
            self.assertTrue(ready.wait(5))
        self.assertEqual(len(recorded), 1)
        self.assertIs(recorded[0].exc_type, IntentionalCrashTestError)
        self.assertEqual(recorded[0].thread.name, "MMS-CrashTest-Worker")

    def test_qt_critical_uses_qt_logging_path(self) -> None:
        with patch("app.utils.crash_test_service.qCritical") as qt_critical:
            run_crash_test("qt_critical")
        qt_critical.assert_called_once()
        self.assertIn("Intentional crash test", qt_critical.call_args.args[0])

    def test_repeat_test_emits_two_real_report_requests(self) -> None:
        with patch("app.utils.crash_test_service.report_unexpected_error") as report:
            run_crash_test("repeated_exception")
        self.assertEqual(report.call_count, 2)
        self.assertIn("1/2", report.call_args_list[0].args[0])
        self.assertIn("2/2", report.call_args_list[1].args[0])

    def test_native_termination_is_never_implicit(self) -> None:
        with patch("app.utils.crash_test_service.os.abort") as abort:
            with patch("app.utils.crash_test_service.report_unexpected_error"):
                run_crash_test("caught_exception")
                run_crash_test("repeated_exception")
        abort.assert_not_called()


class CrashTestSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, language: Language) -> SettingsDialog:
        translator = Translator()
        translator.set_language(language)
        return SettingsDialog(AppSettings(), language, Theme.DARK, translator)

    def test_menu_is_inside_program_maintenance_in_both_languages(self) -> None:
        for lang in (Language.KOREAN, Language.ENGLISH):
            dialog = self._dialog(lang)
            try:
                self.assertEqual(dialog.tabs.indexOf(dialog.maintenance_page), 5)
                self.assertIn(dialog.crash_test_button, [
                    widget for widget in dialog.maintenance_group.findChildren(type(dialog.crash_test_button))
                ])
                actions = dialog.crash_test_button.menu().actions()
                self.assertEqual(len(actions), 6)
                self.assertTrue(all(action.text() for action in actions))
                self.assertEqual(len(dialog.crash_test_actions), 6)
                self.assertEqual(
                    {action.data() for action in actions}, set(CRASH_TEST_MODES)
                )
                dialog.retranslate()
                self.assertTrue(all(action.text() for action in actions))
            finally:
                dialog.deleteLater()

    def test_menu_trigger_dispatches_mode_without_a_capturing_lambda(self) -> None:
        dialog = self._dialog(Language.KOREAN)
        try:
            with patch(
                "PySide6.QtWidgets.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Yes,
            ) as confirm:
                dialog.crash_test_actions["repeated_exception"].trigger()
            confirm.assert_called_once()
            self.assertEqual(dialog.maintenance_action, "crash-test:repeated_exception")
            self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)
        finally:
            dialog.close()

    def test_repeated_mode_requires_confirmation_and_defers_action(self) -> None:
        dialog = self._dialog(Language.KOREAN)
        try:
            with patch("PySide6.QtWidgets.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
                dialog._confirm_crash_test("repeated_exception")
            self.assertEqual(dialog.maintenance_action, "crash-test:repeated_exception")
            self.assertEqual(dialog.result(), dialog.DialogCode.Rejected)
        finally:
            dialog.deleteLater()

    def test_native_abort_is_cancelled_by_default(self) -> None:
        dialog = self._dialog(Language.ENGLISH)
        try:
            with patch("PySide6.QtWidgets.QMessageBox.warning", return_value=QMessageBox.StandardButton.No) as warning:
                dialog._confirm_crash_test("native_abort")
            self.assertEqual(dialog.maintenance_action, "")
            self.assertIn("terminate", warning.call_args.args[2])
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
