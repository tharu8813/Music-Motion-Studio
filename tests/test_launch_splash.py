"""Startup progress must repaint, localize, and close when the editor is ready."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from app.ui.launch_splash import LaunchSplash


class LaunchSplashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_progress_localization_and_completion(self):
        for korean in (True, False):
            with self.subTest(korean=korean), patch("app.ui.launch_splash.launch_is_korean", return_value=korean):
                splash = LaunchSplash("Music Motion Studio", "1.3.1.1")
                window = QWidget()
                try:
                    splash.show()
                    for progress, expected in ((-1, 0), (75, 75), (101, 100)):
                        splash.set_status("작업 공간 준비", "Preparing workspace", progress)
                        self.assertEqual(splash.progress_bar.value(), expected)
                        self.assertEqual(splash.message(), "작업 공간 준비" if korean else "Preparing workspace")
                        self.assertFalse(splash.grab().isNull())
                    self.assertTrue(splash.progress_bar.isVisible())
                    window.show()
                    splash.finish(window)
                    self.assertFalse(splash.isVisible())
                finally:
                    splash.close()
                    window.close()


if __name__ == "__main__":
    unittest.main()
