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

    def test_accelerated_profile_is_explicit_and_scoped_to_one_export(self):
        self.options.gpu_speed_combo.currentData.return_value = "accelerated"
        worker = self.start()
        fast = NativeGpuOptimizations.for_profile("accelerated")
        self.assertEqual(worker.native_optimizations, fast)
        self.assertEqual(self.bridge_kwargs[-1]["optimizations"], fast)
        self.assertTrue(fast.pipelined and fast.partial_bands
                        and fast.compact_bands and fast.reuse_layers)

    def test_standard_gui_profile_cannot_be_overridden_by_cli_environment(self):
        self.options.gpu_speed_combo.currentData.return_value = "standard"
        with patch.dict(os.environ, {
            "PLAYLIST_CANVAS_NATIVE_LAYER_REUSE": "1",
            "PLAYLIST_CANVAS_NATIVE_PIPELINED": "1",
            "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1",
            "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "1",
        }):
            worker = self.start()
        safe = NativeGpuOptimizations.for_profile("standard")
        self.assertEqual(worker.native_optimizations, safe)
        self.assertEqual(self.bridge_kwargs[-1]["optimizations"], safe)
        self.assertFalse(any((safe.pipelined, safe.partial_bands,
                              safe.compact_bands, safe.reuse_layers)))

    def test_success_waits_for_worker_and_gui_close(self):
        worker = self.start()
        result = object()
        worker.succeeded.emit(result)
        worker.finished.emit()
        self.controller.finished.assert_not_called()
        self.close_bridge()
        self.controller.succeeded.assert_called_once_with(result)
        self.controller.finished.assert_called_once()

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

    def test_worker_routes_gpu_and_preserves_ffmpeg(self):
        renderer = MagicMock()
        worker = RenderWorker(renderer, QImage(), [], self.output, RenderSettings())
        worker.run()
        renderer.render.assert_called_once()
        cancel = Event()
        source = object()
        worker = RenderWorker(renderer, QImage(), [], self.output, RenderSettings(),
                              native_project=self.project, native_frame_source=source,
                              cancel_event=cancel)
        with patch("app.renderer.native_project_export.export_project_document") as export:
            worker.run()
        self.assertIs(export.call_args.kwargs["cancel_event"], cancel)
        self.assertIs(export.call_args.kwargs["frame_source"], source)
        self.assertIsNone(export.call_args.kwargs["optimizations"])
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
