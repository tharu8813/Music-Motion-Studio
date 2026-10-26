"""Shared fixture for the main window test suites."""

from __future__ import annotations

from dataclasses import replace
import os
import shutil
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from app.models.playlist import PlaylistTrack
from app.services.playlist_service import AudioImportCandidate
from app.ui.main_window import MainWindow
from app.utils.i18n import Language


class MainWindowTestCase(unittest.TestCase):
    """One fresh MainWindow per test, Korean UI baseline, isolated user presets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setApplicationName("Playlist Canvas Tests")
        cls.application.setOrganizationName("Playlist Canvas Tests")
        # Isolate user presets from the developer's real preset folder.
        cls._preset_dir = tempfile.mkdtemp(prefix="pc-presets-")
        cls._prev_preset_dir = os.environ.get("PLAYLIST_CANVAS_PRESET_DIR")
        os.environ["PLAYLIST_CANVAS_PRESET_DIR"] = cls._preset_dir

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_preset_dir is None:
            os.environ.pop("PLAYLIST_CANVAS_PRESET_DIR", None)
        else:
            os.environ["PLAYLIST_CANVAS_PRESET_DIR"] = cls._prev_preset_dir
        shutil.rmtree(cls._preset_dir, ignore_errors=True)

    def setUp(self) -> None:
        # Main-window UI assertions use the Korean baseline unless a test opts
        # into another language. Translator changes persist through QSettings,
        # so one English-specific test must not leak into every later test.
        settings = QSettings()
        self._original_language_setting = settings.value("language", None)
        settings.setValue("language", Language.KOREAN.value)
        self.window = MainWindow()
        # Registered first, so it runs after every test's own cleanups: closing
        # deletes the window (WA_DeleteOnClose) and those may still use it.
        self.addCleanup(self._close_window)
        self.application.processEvents()

    def _close_window(self) -> None:
        self.window.project_session.dirty = False
        self.window.close()
        self.application.processEvents()

    def tearDown(self) -> None:
        settings = QSettings()
        if self._original_language_setting is None:
            settings.remove("language")
        else:
            settings.setValue("language", self._original_language_setting)
        self.application.processEvents()

    def _import_with_sidecar(
        self, folder_names: dict[str, str], mode: str, audio_name: str,
    ):
        """Import one audio file from a temp folder and return its playlist track."""
        restore = self.window.settings_service.current
        self.addCleanup(self.window.settings_service.save, restore)
        self.window.settings_service.save(replace(
            restore, lyrics_auto_attach_mode=mode,
        ))
        with TemporaryDirectory(prefix="pc-import-sidecar-") as raw:
            folder = Path(raw)
            for name, content in folder_names.items():
                (folder / name).write_text(content, encoding="utf-8")
            audio = folder / audio_name
            audio.write_bytes(b"ID3 stub")
            track = PlaylistTrack(str(audio.resolve()), Path(audio_name).stem)
            with patch.object(
                self.window.playlist_service, "inspect_files",
                return_value=[AudioImportCandidate(track)],
            ):
                added, _accepted, notes = self.window._import_audio_files(
                    [str(audio)]
                )
        self.assertEqual(added, 1)
        return self.window.playlist_service.tracks[-1], notes

    _LRC = "[00:01.00]First line\n[00:04.00]Second line\n"
