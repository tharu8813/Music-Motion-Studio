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
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.services.playlist_service import AudioImportCandidate
from app.ui.main_window import MainWindow
from app.utils.i18n import Language



def accept_ffmpeg_export(dialog) -> object:
    """Accept a dialog using FFmpeg, independent of the production default engine."""
    index = dialog.engine_combo.findData("ffmpeg")
    if index < 0:
        raise AssertionError("FFmpeg export engine is missing")
    dialog.engine_combo.setCurrentIndex(index)
    return dialog.DialogCode.Accepted


def accept_native_gpu_export(dialog) -> object:
    """Accept a dialog selecting native GPU without relying on stored user preferences."""
    index = dialog.engine_combo.findData("native_gpu")
    if index < 0:
        raise AssertionError("Rust GPU export engine is missing")
    dialog.engine_combo.setCurrentIndex(index)
    return dialog.DialogCode.Accepted


def reject_unmocked_export_dialog(_dialog) -> object:
    """A real modal settings dialog would hang a headless CI test indefinitely."""
    raise AssertionError(
        "ExportSettingsDialog.exec() is unmocked in a headless test. "
        "Patch it with accept_ffmpeg_export or accept_native_gpu_export "
        "(or explicitly return QDialog.DialogCode.Rejected)."
    )


class MainWindowTestCase(unittest.TestCase):
    """One fresh MainWindow per test, Korean UI baseline, isolated user presets."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setApplicationName("Music Motion Studio Tests")
        cls.application.setOrganizationName("Music Motion Studio Tests")
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
        # A forgotten real export settings exec() blocks an offscreen runner
        # until the entire Windows CI job times out. Tests must explicitly
        # choose the FFmpeg or Rust GPU code path (or model cancellation).
        # Register this cleanup before window close: cleanups run in LIFO order,
        # so the guard remains active while _close_window() is called.
        modal_guard = patch.object(
            ExportSettingsDialog, "exec", new=reject_unmocked_export_dialog,
        )
        modal_guard.start()
        self.addCleanup(modal_guard.stop)
        # Registered last, so it runs before the modal guard is removed.
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
