"""Actual controller/worker routing with mocked GPU and FFmpeg hardware."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from app.controllers.export_controller import ExportOrchestrator
from app.renderer.render_worker import RenderWorker
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.services.app_settings_service import AppSettings
from tests.test_native_canvas_project_probe import project_fixture


class FakeWorker(QObject):
    progress = Signal(str, float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()
    storage_path_changed = Signal(str, object)

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.cancel = kwargs["cancel_event"].set
        self.native_optimizations = kwargs.get("native_optimizations")
        self.start = MagicMock()


class Bridge(QObject):
    closed = Signal()
    def __init__(self, *args, **kwargs):
        super().__init__()


class GpuUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = str(Path(self.directory.name) / "new.mp4")
        self.window = MagicMock()
        self.project = project_fixture()
        self.window._project_document.return_value = self.project
        self.controller = ExportOrchestrator(self.window)
        self.options = MagicMock()
        self.options.app_settings = AppSettings(render_width=self.project.canvas.width,
                                                render_height=self.project.canvas.height)
        self.options.save_as_default = False
        self.options.output_path = Path(self.output)
        self.controller.finished = MagicMock()
        self.controller.succeeded = MagicMock()
        self.controller.failed = MagicMock()
        self.controller.cancelled = MagicMock()
        self.controller.start_storage_monitor = MagicMock()
        self.bridges = []
        self.bridge_kwargs = []

        def make_bridge(*args, **kwargs):
            source = Bridge()
            self.bridges.append(source)
            self.bridge_kwargs.append(kwargs)
            return source

        for target, value in (
            ("app.renderer.native_project_export.resolve_native_executable", MagicMock()),
            ("app.controllers.export_controller.ExportProgressDialog", MagicMock()),
            ("app.controllers.export_controller.RenderWorker", FakeWorker),
            ("app.renderer.native_project_frames.GuiProjectFrameSource", make_bridge),
        ):
            mocked = patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def start(self):
        self.assertTrue(self.controller.start_gpu_export(MagicMock(), [], self.output, self.options))
        return self.window._render_worker

    def close_bridge(self):
        self.bridges[-1].closed.emit()

    def test_rust_gpu_always_uses_single_automatic_profile_scoped_to_export(self):
        worker = self.start()
        auto = NativeGpuOptimizations.for_profile("automatic")
        self.assertEqual(worker.native_optimizations, auto)
        self.assertEqual(self.bridge_kwargs[-1]["optimizations"], auto)
        self.assertTrue(all((
            auto.pipelined, auto.partial_bands, auto.compact_bands,
            auto.reuse_layers, auto.parallel_bgra, auto.adaptive_bgra,
        )))

    def test_gpu_export_summary_localizes_the_active_encoding_settings(self):
        self.window.translator.is_korean = True
        self.start()
        label = self.window._export_dialog.set_export_details.call_args.args[2]
        self.assertIn("화질 자동 · 인코딩 속도 P3", label)
        self.assertNotIn("Encoding speed", label)

        self.window.translator.is_korean = False
        self.options.app_settings = AppSettings(native_nvenc_cq=19, native_nvenc_preset="p5")
        self.start()
        label = self.window._export_dialog.set_export_details.call_args.args[2]
        self.assertIn("Quality CQ 19 · Encoding speed P5", label)
        self.assertNotIn("인코딩 속도", label)

    def test_old_speed_choice_and_global_cli_flags_cannot_change_gui_gpu_mode(self):
        # Historical settings or a third-party caller may still have an old
        # speed control. It must be ignored; engine alone chooses the policy.
        self.options.gpu_speed_combo.currentData.return_value = "standard"
        with patch.dict(os.environ, {
            "PLAYLIST_CANVAS_NATIVE_LAYER_REUSE": "0",
            "PLAYLIST_CANVAS_NATIVE_PIPELINED": "0",
            "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "0",
            "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "0",
            "PLAYLIST_CANVAS_NATIVE_PARALLEL_BGRA": "0",
        }):
            worker = self.start()
        auto = NativeGpuOptimizations.for_profile("automatic")
        self.assertEqual(worker.native_optimizations, auto)
        self.assertEqual(self.bridge_kwargs[-1]["optimizations"], auto)

    def test_success_waits_for_worker_and_gui_close(self):
        worker = self.start()
        result = object()
        worker.succeeded.emit(result)
        worker.finished.emit()
        self.controller.finished.assert_not_called()
        self.close_bridge()
        self.controller.succeeded.assert_called_once_with(result)
        self.controller.finished.assert_called_once()

    def test_1080p_output_from_720p_project_starts_gpu_without_fallback(self):
        # Export UI resolution may be higher than the Canvas size.
        self.options.app_settings = AppSettings(render_width=1920, render_height=1080)
        with patch.object(QMessageBox, "information") as notice:
            worker = self.start()
        self.assertIsNotNone(worker)
        notice.assert_not_called()
        self.assertEqual(self.bridge_kwargs[-1]["scale"], 1.5)

    def test_gpu_preflight_fallback_is_in_selected_language_and_explains_sizes(self):
        self.options.app_settings = AppSettings(render_width=4000, render_height=2250)
        self.window.translator.is_korean = True
        with patch.object(QMessageBox, "information") as notice:
            self.assertFalse(
                self.controller.start_gpu_export(MagicMock(), [], self.output, self.options)
            )
        title, message = notice.call_args.args[1:3]
        self.assertIn("GPU 내보내기", title)
        self.assertIn("해상도", message)
        self.assertIn("1280×720", message)
        self.assertIn("4000×2250", message)
        self.assertNotIn("GPU export unavailable", message)
        self.assertNotIn("Continuing with FFmpeg", message)

        self.window.translator.is_korean = False
        with patch.object(QMessageBox, "information") as notice:
            self.assertFalse(
                self.controller.start_gpu_export(MagicMock(), [], self.output, self.options)
            )
        title, message = notice.call_args.args[1:3]
        self.assertEqual(title, "GPU export unavailable")
        self.assertIn("3840×2160", message)
        self.assertNotIn("기존 FFmpeg", message)

    def test_unsupported_preflight_uses_ffmpeg_before_worker(self):
        self.project.settings.transition_mode = "crossfade"
        with patch.object(QMessageBox, "information"):
            self.assertFalse(self.controller.start_gpu_export(MagicMock(), [], self.output, self.options))
        self.controller.start_storage_monitor.assert_not_called()

    def test_error_offers_fallback_only_after_close(self):
        worker = self.start()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question, \
             patch.object(self.controller, "export_video") as retry:
            worker.failed.emit("device lost")
            worker.finished.emit()
            question.assert_not_called()
            self.close_bridge()
            self.app.processEvents()
            retry.assert_called_once_with(self.options)
        self.options.engine_combo.setCurrentIndex.assert_called_once_with(0)

    def test_cancel_never_offers_fallback(self):
        worker = self.start()
        worker.cancel()
        with patch.object(QMessageBox, "question") as question:
            worker.failed.emit("cancelled during device initialization")
            worker.finished.emit()
            self.close_bridge()
        question.assert_not_called()
        self.controller.cancelled.assert_called_once()

    def test_cancelled_signal_with_gui_close_first_keeps_lifetime(self):
        worker = self.start()
        with patch.object(QMessageBox, "question") as question:
            worker.cancel()
            worker.cancelled.emit()
            self.close_bridge()
            self.controller.finished.assert_not_called()
            worker.finished.emit()
        question.assert_not_called()
        self.controller.cancelled.assert_called_once()
        self.controller.finished.assert_called_once()

    def test_existing_output_blocks_midstream_retry(self):
        worker = self.start()
        Path(self.output).write_bytes(b"existing")
        with patch.object(QMessageBox, "question") as question:
            worker.failed.emit("device lost")
            worker.finished.emit()
            self.close_bridge()
        question.assert_not_called()
        self.assertEqual(Path(self.output).read_bytes(), b"existing")

    def test_native_worker_throttles_gui_progress_but_reports_first_and_last(self):
        renderer = MagicMock()
        worker = RenderWorker(renderer, QImage(), [], self.output, RenderSettings())
        seen = []
        worker.progress.connect(lambda stage, fraction, message: seen.append(
            (stage, fraction, message),
        ))
        with patch("app.renderer.render_worker.monotonic",
                   side_effect=(100.0, 100.05, 100.12, 100.30, 100.32)):
            for index in (1, 2, 3, 10, 100):
                worker._report_native_frame(index, 100)
        self.assertEqual([row[2] for row in seen], [
            "Rust GPU: 1/100 frames",
            "Rust GPU: 10/100 frames",
            "Rust GPU: 100/100 frames",
        ])
        self.assertAlmostEqual(seen[-1][1], .85)
        self.assertEqual({row[0] for row in seen}, {"Encoding video"})

    def test_worker_routes_gpu_and_preserves_ffmpeg(self):
        renderer = MagicMock()
        worker = RenderWorker(renderer, QImage(), [], self.output, RenderSettings())
        worker.run()
        renderer.render.assert_called_once()
        cancel = Event()
        source = object()
        worker = RenderWorker(renderer, QImage(), [], self.output,
                              RenderSettings(native_nvenc_preset="p5", native_nvenc_cq=19),
                              native_project=self.project, native_frame_source=source,
                              cancel_event=cancel)
        with patch("app.renderer.native_project_export.export_project_document") as export:
            worker.run()
        self.assertIs(export.call_args.kwargs["cancel_event"], cancel)
        self.assertIs(export.call_args.kwargs["frame_source"], source)
        self.assertIsNone(export.call_args.kwargs["optimizations"])
        self.assertEqual(export.call_args.kwargs["native_nvenc_preset"], "p5")
        self.assertEqual(export.call_args.kwargs["native_nvenc_cq"], 19)
        self.assertEqual(renderer.render.call_count, 1)

    def test_export_video_ui_dispatches_gpu_and_keeps_ffmpeg_default(self):
        self.window._render_worker = None
        self.window._export_preparation_cancel = None
        self.window.playlist_service.tracks = self.project.playlist
        self.window.settings_service.current = AppSettings()
        self.window.settings_service.default_output_directory.return_value = Path(self.directory.name)
        with patch("app.controllers.export_controller.FFmpegRenderer"), \
             patch.object(self.controller, "start_gpu_export", return_value=True) as gpu, \
             patch.object(self.controller, "resolve_render_settings", return_value=None) as ffmpeg:
            self.options.engine_combo.currentData.return_value = "native_gpu"
            self.controller.export_video(self.options)
            gpu.assert_called_once()
            ffmpeg.assert_not_called()
            self.options.engine_combo.currentData.return_value = "ffmpeg"
            self.controller.export_video(self.options)
            self.assertEqual(gpu.call_count, 1)
            ffmpeg.assert_called_once()

    def test_duplicate_export_is_blocked_before_options(self):
        self.window._render_worker = object()
        self.controller.export_video(self.options)
        self.window._project_document.assert_not_called()

    def test_ffmpeg_fallback_publication_refuses_output_race(self):
        renderer = FFmpegRenderer.__new__(FFmpegRenderer)
        renderer.prevent_output_overwrite = True
        target = Path(self.output)
        staged = target.with_suffix(".part")
        staged.write_bytes(b"new")
        target.write_bytes(b"keep")
        with self.assertRaises(OSError):
            renderer._publish_output(staged, target)
        self.assertEqual(target.read_bytes(), b"keep")
        target.unlink()
        renderer._publish_output(staged, target)
        self.assertEqual(target.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
