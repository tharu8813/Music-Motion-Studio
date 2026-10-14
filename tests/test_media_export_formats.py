"""Export destination UI, audio routing, and real container/codec coverage."""

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.dialogs.export_complete_dialog import ExportCompleteDialog
from app.dialogs.export_progress_dialog import ExportProgressDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderError, RenderSettings
from app.services.app_settings_service import AppSettings
from app.services.export_formats import VIDEO_FORMATS, AUDIO_FORMATS
from app.timeline.compiler import compile_playlist
from app.preview.canvas_snapshot import CanvasSnapshot
from app.utils.i18n import Translator
from app.utils.subprocess_utils import hidden_process_kwargs
from tests.main_window_base import MainWindowTestCase


class ExportDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.dialog = ExportSettingsDialog(
            AppSettings(), 2, 120, Translator(), Path(self.directory.name) / "playlist.mp4",
        )

    def tearDown(self):
        self.dialog.close()
        # Unparented dialogs must be deleted on the GUI thread before encoding.
        for widget in QApplication.topLevelWidgets():
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.directory.cleanup()

    def test_filename_folder_and_extension_are_independent(self):
        self.dialog.filename_edit.setText("여름.mix.mp4")
        for extension in VIDEO_FORMATS:
            self.dialog.format_combo.setCurrentIndex(self.dialog.format_combo.findData(extension))
            self.assertEqual(self.dialog.output_path, Path(self.directory.name).resolve() / f"여름.mix.{extension}")
        self.dialog._accept_if_valid()
        self.assertEqual(self.dialog.result(), QDialog.DialogCode.Accepted)

    def test_audio_mode_limits_formats_and_hides_video_controls(self):
        self.dialog.export_type_combo.setCurrentIndex(1)
        self.assertTrue(self.dialog.audio_only)
        self.assertTrue(self.dialog.render_group.isHidden())
        self.assertTrue(self.dialog.quality_group.isHidden())
        self.assertTrue(self.dialog.codec_combo.isHidden())
        self.assertFalse(self.dialog.audio_bitrate_combo.isHidden())
        for extension in AUDIO_FORMATS:
            self.dialog.format_combo.setCurrentIndex(self.dialog.format_combo.findData(extension))
            self.assertEqual(self.dialog.output_path.suffix, f".{extension}")
            self.assertEqual(self.dialog.audio_bitrate_combo.isEnabled(), extension not in {"wav", "flac"})
        self.dialog.export_type_combo.setCurrentIndex(0)
        self.assertFalse(self.dialog.render_group.isHidden())
        self.assertTrue(self.dialog.audio_bitrate_combo.isEnabled())

    def test_invalid_names_do_not_start_export(self):
        for name in ("", "../escape", "a/b", "CON", "LPT1.mp3", "a:b", "bad."):
            self.dialog.filename_edit.setText(name)
            with patch.object(QMessageBox, "warning") as warning:
                self.dialog._accept_if_valid()
            warning.assert_called_once()
            self.assertEqual(self.dialog.result(), QDialog.DialogCode.Rejected)

    def test_overwrite_confirmation_uses_selected_audio_extension(self):
        self.dialog.export_type_combo.setCurrentIndex(1)
        output = Path(self.directory.name) / "playlist.mp3"
        output.write_bytes(b"existing")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            self.dialog._accept_if_valid()
        self.assertIn("playlist.mp3", question.call_args.args[2])
        self.assertEqual(output.read_bytes(), b"existing")

    def test_audio_progress_and_completion_offer_audio_actions(self):
        progress = ExportProgressDialog()
        complete = ExportCompleteDialog(Path(self.directory.name) / "mix.mp3", Translator())
        try:
            progress.set_audio_only(True)
            self.assertEqual(progress.step_progress("Encoding audio"), [("Audio", 1.0), ("Save audio", None)])
            self.assertTrue(progress.step_labels[0].isHidden())
            self.assertIn(complete.play_button.text(), ("Play audio", "오디오 재생하기"))
        finally:
            progress.complete(False)
            progress.close()
            complete.close()


class AudioExportRoutingTests(MainWindowTestCase):
    def test_audio_export_skips_gpu_selection_and_canvas_capture(self):
        track = PlaylistTrack("source.wav", "Track", duration_seconds=1)
        self.window.playlist_service.replace([track])
        worker = MagicMock()
        try:
            with patch("app.controllers.export_controller.FFmpegRenderer") as renderer, \
                 patch.object(ExportSettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                 patch.object(ExportSettingsDialog, "output_path", new_callable=PropertyMock,
                              return_value=Path("mix.mp3").resolve()), \
                 patch("app.controllers.export_controller.RenderWorker", return_value=worker) as worker_class, \
                 patch("app.controllers.export_controller.ExportOrchestrator.start_storage_monitor"), \
                 patch("app.controllers.export_controller.VideoEncoderAdvisor.automatic_encoder") as gpu, \
                 patch.object(CanvasSnapshot, "capture_track") as capture:
                self.window._export_video()
            gpu.assert_not_called()
            capture.assert_not_called()
            renderer.return_value.preflight_export.assert_called_once()
            worker.start.assert_called_once()
            self.assertEqual(worker_class.call_args.kwargs["transition_mode"], self.window.project_settings.transition_mode)
            self.assertEqual(worker_class.call_args.args[3], str(Path("mix.mp3").resolve()))
        finally:
            self.window._render_worker = None
            if self.window._export_dialog:
                self.window._export_dialog.complete(False)
                self.window._export_dialog = None
            self.window._unlock_main_form_after_export()
            self.window.activity_progress.finish("export")
            self.window._active_export_output_path = None


class AudioExportSafetyTests(unittest.TestCase):
    def test_failure_or_cancellation_preserves_existing_audio(self):
        for outcome in (RenderError("encoding failed"), RenderCancelledError("cancelled")):
            with self.subTest(outcome=type(outcome).__name__), TemporaryDirectory() as directory:
                source = Path(directory) / "source.wav"
                source.touch()
                target = Path(directory) / "mix.mp3"
                target.write_bytes(b"original")
                tracks = [PlaylistTrack(str(source), "Track", duration_seconds=1)]
                renderer = object.__new__(FFmpegRenderer)

                def prepare(active_tracks, temporary, settings, **kwargs):
                    kwargs["plan_callback"](compile_playlist(active_tracks))
                    prepared = temporary / "prepared.flac"
                    prepared.touch()
                    self.assertEqual(kwargs["audio_codec"], "flac")
                    return prepared

                with patch.object(renderer, "ensure_encoder_available"), \
                     patch.object(renderer, "ensure_encoder_usable") as video_probe, \
                     patch.object(renderer, "prepare_playlist_audio", side_effect=prepare), \
                     patch.object(renderer, "_run", side_effect=outcome):
                    with self.assertRaises(type(outcome)):
                        renderer.render(QImage(), tracks, target, RenderSettings())
                video_probe.assert_not_called()
                self.assertEqual(target.read_bytes(), b"original")
                self.assertEqual(list(Path(directory).glob("*.rendering.mp3")), [])


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"), "Set PLAYLIST_CANVAS_TEST_FFMPEG for real encoding")
class MediaFormatIntegrationTests(unittest.TestCase):
    def test_audio_crossfade_uses_the_resolved_mix_duration(self):
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            source = Path(directory) / "source.wav"
            subprocess.run([str(executable), "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=2", "-y", str(source)], check=True,
                           **hidden_process_kwargs())
            tracks = [PlaylistTrack(str(source), f"Track {index}", duration_seconds=2) for index in range(2)]
            result = FFmpegRenderer(executable).render(
                QImage(), tracks, Path(directory) / "mix.flac", RenderSettings(),
                transition_mode="crossfade", crossfade_seconds=0.5,
            )
            self.assertTrue(result.validation.passed, result.validation)
            self.assertAlmostEqual(result.validation.duration_seconds, 3.5, places=2)

    def test_all_formats_produce_the_requested_streams_and_extensions(self):
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        probe = executable.with_name("ffprobe.exe" if executable.suffix == ".exe" else "ffprobe")
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            source = root / "source.wav"
            subprocess.run([str(executable), "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", "-y", str(source)], check=True,
                           **hidden_process_kwargs())
            tracks = [PlaylistTrack(str(source), "Track", duration_seconds=1)]
            image = QImage(64, 64, QImage.Format.Format_RGB32)
            image.fill(0xff336699)
            renderer = FFmpegRenderer(executable)
            expected_audio = {"mp3": "mp3", "m4a": "aac", "wav": "pcm_s16le", "flac": "flac", "ogg": "vorbis"}
            for extension in VIDEO_FORMATS + AUDIO_FORMATS:
                with self.subTest(extension=extension):
                    target = root / f"mix.{extension}"
                    result = renderer.render(image if extension in VIDEO_FORMATS else QImage(), tracks,
                                             target, RenderSettings(output_width=64, output_height=64))
                    self.assertEqual(result.output_path, target)
                    self.assertTrue(result.validation.passed, result.validation)
                    completed = subprocess.run([str(probe), "-v", "error", "-show_streams", "-of", "json", str(target)],
                                               check=True, capture_output=True, text=True, **hidden_process_kwargs())
                    streams = json.loads(completed.stdout)["streams"]
                    types = [item["codec_type"] for item in streams]
                    self.assertIn("audio", types)
                    self.assertEqual("video" in types, extension in VIDEO_FORMATS)
                    if extension in AUDIO_FORMATS:
                        self.assertEqual(streams[0]["codec_name"], expected_audio[extension])


if __name__ == "__main__":
    unittest.main()
