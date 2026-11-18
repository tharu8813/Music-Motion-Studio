"""An interrupted prior session is a safe-start choice, not a new crash."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main import choose_startup_mode
from app.utils.logging_setup import _install_abnormal_shutdown_marker


class FakeApplication:
    def __init__(self, *, previous=False):
        self.properties = {"previous_abnormal_shutdown": previous}
        self.aboutToQuit = MagicMock()

    def property(self, key):
        return self.properties.get(key)

    def setProperty(self, key, value):
        self.properties[key] = value


class SafeStartupTests(TestCase):
    def test_old_run_marker_prompts_only_and_does_not_generate_crash_report(self):
        with TemporaryDirectory() as root:
            marker = Path(root) / "running.marker"
            marker.write_text("started_at=previous session\\n", encoding="utf-8")
            app = FakeApplication()
            with (patch("app.utils.logging_setup._run_marker_path", return_value=marker),
                  patch("app.utils.logging_setup._CRASH_BRIDGE") as bridge):
                _install_abnormal_shutdown_marker(app)
                self.assertTrue(app.property("previous_abnormal_shutdown"))
                self.assertIn("started_at=", marker.read_text(encoding="utf-8"))
                bridge.report_requested.emit.assert_not_called()
                callback = app.aboutToQuit.connect.call_args.args[0]
                callback()
                self.assertFalse(marker.exists())

    def test_new_install_without_old_marker_does_not_prompt(self):
        with TemporaryDirectory() as root:
            marker = Path(root) / "running.marker"
            app = FakeApplication(previous=True)
            with patch("app.utils.logging_setup._run_marker_path", return_value=marker):
                _install_abnormal_shutdown_marker(app)
            self.assertFalse(app.property("previous_abnormal_shutdown"))
            self.assertTrue(marker.exists())

    def test_safe_start_choice_is_session_only(self):
        app = FakeApplication(previous=True)
        with patch("main.QMessageBox") as message_cls:
            dialog = message_cls.return_value
            safe_button, normal_button = object(), object()
            dialog.addButton.side_effect = [safe_button, normal_button]
            dialog.clickedButton.return_value = safe_button
            self.assertTrue(choose_startup_mode(app))
            self.assertTrue(app.property("safe_mode"))
            self.assertEqual(dialog.addButton.call_count, 2)
            dialog.setDefaultButton.assert_called_once_with(safe_button)

    def test_normal_mode_choice_preserves_existing_settings(self):
        app = FakeApplication(previous=True)
        with patch("main.QMessageBox") as message_cls:
            dialog = message_cls.return_value
            safe_button, normal_button = object(), object()
            dialog.addButton.side_effect = [safe_button, normal_button]
            dialog.clickedButton.return_value = normal_button
            self.assertFalse(choose_startup_mode(app))
            self.assertFalse(app.property("safe_mode"))

    def test_smoke_and_explicit_safe_mode_do_not_block_on_dialog(self):
        app = FakeApplication(previous=True)
        with patch("main.QMessageBox") as message_cls:
            self.assertFalse(choose_startup_mode(app, smoke_test=True))
            self.assertFalse(app.property("safe_mode"))
            self.assertTrue(choose_startup_mode(app, force_safe_mode=True))
            self.assertTrue(app.property("safe_mode"))
            message_cls.assert_not_called()

    def test_clean_restart_is_normal_even_after_previous_safe_mode(self):
        app = FakeApplication(previous=False)
        app.setProperty("safe_mode", True)
        with patch("main.QMessageBox") as message_cls:
            self.assertFalse(choose_startup_mode(app))
            message_cls.assert_not_called()
        self.assertFalse(app.property("safe_mode"))

    def test_dismissing_choice_defaults_to_safe_mode(self):
        app = FakeApplication(previous=True)
        with patch("main.QMessageBox") as message_cls:
            dialog = message_cls.return_value
            dialog.addButton.side_effect = [object(), object()]
            dialog.clickedButton.return_value = None
            self.assertTrue(choose_startup_mode(app))
            self.assertTrue(app.property("safe_mode"))


if __name__ == "__main__":
    import unittest
    unittest.main()
