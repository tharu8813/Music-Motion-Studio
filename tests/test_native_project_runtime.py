"""Mocked runtime contracts: no Rust, audio decoding or GPU execution."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from app.models.source import Source, SourceType
from app.preview.native_canvas_layer_packets import NativeCanvasFrame, NativeCanvasBgraLayer
from app.renderer.ffmpeg_renderer import RenderCancelledError, RenderError, RenderResult
from app.renderer.native_project_frames import GuiProjectFrameSource, produce_complete_project_frames
from app.renderer.native_project_export import export_project_document, resolve_native_executable, NATIVE_EXECUTABLE
from app.renderer.native_project_support import validate_project_for_gpu_capture
from app.renderer.native_project_support import resolve_project_frame
from app.renderer.native_dynamic_layers import build_dynamic_stack
from app.timeline.compiler import compile_playlist
from tests.test_native_canvas_project_probe import project_fixture


class RuntimeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # This module tests contracts with mocked audio/frames/encoder only.
        gate = patch("app.renderer.native_project_export._require_developer_runtime")
        gate.start()
        self.addCleanup(gate.stop)

    def pump(self, worker):
        deadline = monotonic() + 5
        while worker.is_alive() and monotonic() < deadline:
            self.app.processEvents()
            worker.join(.001)
        self.app.processEvents()
        self.assertFalse(worker.is_alive(), "worker did not finish with GUI loop running")

    def test_entire_timeline_and_song_boundary_lazy_and_detached(self):
        project = project_fixture()
        before = project.to_dict()
        calls = []
        scene = MagicMock()
        packet = NativeCanvasFrame(320, 180, (
            NativeCanvasBgraLayer("canvas", 320, 180, b"\0\0\0\xff" * (320 * 180)),
        ))

        @contextmanager
        def overlays(*args, **kwargs):
            yield [], [], "none"

        def capture(_scene, track, bands, **kwargs):
            calls.append((track.title, kwargs["elapsed_seconds"], kwargs["timeline_seconds"]))
            track.title += ""
            return packet

        with patch("app.renderer.native_project_frames.CanvasScene", return_value=scene), \
             patch("app.renderer.native_project_frames.SourceItem"), \
             patch("app.renderer.native_project_frames.capture_native_canvas_track_frame", side_effect=capture), \
             patch("app.renderer.native_project_frames.real_audio_dynamic_frames", side_effect=overlays):
            frames = produce_complete_project_frames(project, fps=60, scale=.25,
                prepared_audio=Path("mock.wav"), ffmpeg="mock")
            self.assertEqual(calls, [])
            self.assertEqual(sum(1 for _ in frames), 60)
        self.assertEqual(calls[29], ("First", 29/60, 29/60))
        self.assertEqual(calls[30], ("Second", 0, .5))
        self.assertEqual(project.to_dict(), before)
        scene.clear.assert_called_once()

    def test_long_playlist_fractional_boundary_and_last_pts_use_compiled_clock(self):
        project = project_fixture()
        project.playlist[0].duration_seconds = 3600.125
        project.playlist[1].duration_seconds = 7199.875
        plan = compile_playlist(project.playlist, enabled_only=True)
        # 3-hour timeline, fractional-frame song boundary, then final CFR PTS.
        for n, index, origin in ((216007, 0, 0), (216008, 1, 3600.125),
                                 (647999, 1, 3600.125)):
            with self.subTest(frame=n):
                state = resolve_project_frame(project, n / 60, plan=plan)
                self.assertIs(state["track"], project.playlist[index])
                self.assertAlmostEqual(state["track_elapsed_seconds"], n / 60 - origin)
                self.assertEqual(state["timeline_seconds"], n / 60)

    def test_dynamic_stream_closed_on_cancel_and_early_iterator_close(self):
        for cancel_instead in (False, True):
            with self.subTest(cancel=cancel_instead):
                project = project_fixture()
                project.sources.append(Source(SourceType.AUDIO_VISUALIZER, "Spectrum", z_index=8))
                cancel = Event()
                cleaned = []

                def stream():
                    try:
                        while True:
                            yield object()
                    finally:
                        cleaned.append(True)

                @contextmanager
                def overlays(*args, **kwargs):
                    self.assertIs(kwargs["cancel_event"], cancel)
                    frames = stream()
                    try:
                        yield [object()], [frames], "mock"
                    finally:
                        frames.close()

                scene = MagicMock()
                with patch("app.renderer.native_project_frames.CanvasScene", return_value=scene), \
                     patch("app.renderer.native_project_frames.SourceItem"), \
                     patch("app.renderer.native_project_frames.capture_native_canvas_track_frame"), \
                     patch("app.renderer.native_project_frames.interleave_dynamic_layers", return_value="frame"), \
                     patch("app.renderer.native_project_frames.real_audio_dynamic_frames", side_effect=overlays):
                    frames = produce_complete_project_frames(project, fps=60, scale=.25,
                        prepared_audio=Path("mock.wav"), ffmpeg="mock", cancel_event=cancel)
                    self.assertEqual(next(frames), "frame")
                    if cancel_instead:
                        cancel.set()
                        with self.assertRaises(RenderCancelledError):
                            next(frames)
                    else:
                        frames.close()
                self.assertEqual(cleaned, [True])
                scene.clear.assert_called_once()

    def test_gui_bridge_produces_only_on_pull_and_cleans_on_gui(self):
        source = GuiProjectFrameSource(project_fixture(), fps=60, scale=.25)
        source.prepare("mock.wav", "mock")
        calls, cleanup, results = [], [], []

        def producer(*args, **kwargs):
            try:
                for i in range(2):
                    calls.append(QThread.currentThread())
                    yield i
            finally:
                cleanup.append(QThread.currentThread())

        def consume():
            try:
                results.append(next(source))
                results.append(next(source))
            finally:
                source.close()

        with patch("app.renderer.native_project_frames.produce_complete_project_frames", side_effect=producer):
            self.app.processEvents()
            self.assertEqual(calls, [])
            worker = Thread(target=consume)
            worker.start()
            self.pump(worker)
        self.assertEqual(results, [0, 1])
        self.assertEqual(calls, [self.app.thread()] * 2)
        self.assertEqual(cleanup, [self.app.thread()])

    def test_cancel_waiting_gui_pull_does_not_deadlock_or_start_scene(self):
        cancel = Event()
        source = GuiProjectFrameSource(project_fixture(), fps=60, scale=.25, cancel_event=cancel)
        source.prepare("mock.wav", "mock")
        errors = []

        def consume():
            try:
                next(source)
            except Exception as error:
                errors.append(error)
            finally:
                source.close()

        with patch("app.renderer.native_project_frames.produce_complete_project_frames") as produce:
            worker = Thread(target=consume)
            worker.start()
            cancel.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.app.processEvents()
            produce.assert_not_called()
        self.assertIsInstance(errors[0], RenderCancelledError)

    def test_unsupported_features_fail_before_audio(self):
        for kind in ("video", "transition", "equal_z", "missing_image"):
            project = project_fixture()
            if kind == "video":
                project.sources.append(Source(SourceType.VIDEO, "Video"))
            elif kind == "transition":
                project.settings.transition_mode = "crossfade"
            elif kind == "missing_image":
                project.sources.append(Source(SourceType.IMAGE, "Missing image"))
            else:
                project.sources.append(Source(SourceType.AUDIO_VISUALIZER, "Spectrum",
                    z_index=3 if kind == "equal_z" else 8,
                    rotation=10 if kind == "rotation" else 0))
            with self.subTest(kind=kind), patch("app.renderer.native_project_export.FFmpegRenderer") as renderer:
                with self.assertRaises(ValueError):
                    export_project_document(project, "new.mp4", native_exe="mock.exe")
                renderer.assert_not_called()

    def test_stalled_gui_timeout_and_worker_thread_capture_rejected(self):
        source = GuiProjectFrameSource(project_fixture(), fps=60, scale=.25, request_timeout=.01)
        errors = []

        def consume():
            try:
                next(source)
            except Exception as error:
                errors.append(error)

        worker = Thread(target=consume)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], RenderError)
        self.assertIn("Timed out", str(errors[0]))
        with patch("app.renderer.native_project_frames.produce_complete_project_frames") as produce:
            self.app.processEvents()
            produce.assert_not_called()
        errors.clear()

        def wrong_thread():
            try:
                next(produce_complete_project_frames(project_fixture(), fps=60, scale=.25,
                    prepared_audio=Path("mock.wav"), ffmpeg="mock"))
            except Exception as error:
                errors.append(error)

        worker = Thread(target=wrong_thread)
        worker.start()
        worker.join(1)
        self.assertIsInstance(errors[0], RenderError)
        self.assertIn("GUI thread", str(errors[0]))

    def test_static_project_supported_and_scene_creation_failure_cleans(self):
        project = project_fixture()
        self.assertEqual(validate_project_for_gpu_capture(project, scale=.25, source_policy="dynamic-audio"), [])
        self.assertEqual(build_dynamic_stack(project, .25).order, (("canvas", 0),))
        with patch("app.renderer.native_project_frames.CanvasScene") as scene, \
             patch("app.renderer.native_project_frames.SourceItem", side_effect=ValueError("asset")):
            with self.assertRaisesRegex(ValueError, "asset"):
                next(produce_complete_project_frames(project, fps=60, scale=.25,
                    prepared_audio=Path("mock.wav"), ffmpeg="mock"))
            scene.return_value.clear.assert_called_once()

    def test_bundle_lookup_needs_no_rust_or_development_tree(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "native" / NATIVE_EXECUTABLE
            executable.parent.mkdir()
            executable.write_bytes(b"mock")
            with patch("sys.frozen", True, create=True), patch("sys._MEIPASS", str(root), create=True):
                self.assertEqual(resolve_native_executable(), executable.resolve())
            with self.assertRaises(RenderError):
                resolve_native_executable(root / "missing.exe")

    def test_gui_bridge_forwards_producer_error_and_releases_scene_on_gui(self):
        source = GuiProjectFrameSource(project_fixture(), fps=60, scale=.25)
        source.prepare("mock.wav", "mock")
        errors, cleanup = [], []

        def failing(*args, **kwargs):
            try:
                raise ValueError("failed capture")
                yield
            finally:
                cleanup.append(QThread.currentThread())

        def consume():
            try:
                next(source)
            except Exception as error:
                errors.append(error)
            finally:
                source.close()

        with patch("app.renderer.native_project_frames.produce_complete_project_frames", side_effect=failing):
            worker = Thread(target=consume)
            worker.start()
            self.pump(worker)
        self.assertEqual(str(errors[0]), "failed capture")
        self.assertEqual(cleanup, [self.app.thread()])

    def test_preflight_failure_closes_bridge_and_preserves_existing_output(self):
        source = MagicMock()
        with TemporaryDirectory() as temp:
            target = Path(temp) / "existing.mp4"
            target.write_bytes(b"USER OUTPUT")
            with self.assertRaisesRegex(ValueError, "will not be overwritten"):
                export_project_document(project_fixture(), target, frame_source=source)
            self.assertEqual(target.read_bytes(), b"USER OUTPUT")
        source.close.assert_called_once()

    def test_service_prepares_audio_and_passes_cancellation_progress_and_closes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "native.exe"
            exe.write_bytes(b"mock")
            project = project_fixture()
            cancel = Event()
            source = MagicMock()
            source.project, source.fps, source.scale = project, 60, .25
            source.cancel_event = cancel
            result = RenderResult(root / "new.mp4", 1)
            progress = MagicMock()
            with patch("app.renderer.native_project_export.FFmpegRenderer") as renderer, \
                 patch("app.renderer.native_project_export.render_native_gpu_playlist", return_value=result) as render:
                renderer.return_value.prepare_playlist_audio.return_value = root / "audio.wav"
                actual = export_project_document(project, result.output_path, native_exe=exe,
                    fps=60, scale=.25, cancel_event=cancel, progress_callback=progress, frame_source=source)
            self.assertEqual(actual.output_path, result.output_path)
            self.assertEqual(actual.track_count, result.track_count)
            self.assertIn("completed_wall_seconds", actual.native_report)
            self.assertIs(render.call_args.kwargs["cancel_event"], cancel)
            self.assertIs(render.call_args.kwargs["progress_callback"], progress)
            self.assertEqual(render.call_args.kwargs["compiled_plan"].duration_seconds, 1)
            source.prepare.assert_called_once()
            source.close.assert_called()


if __name__ == "__main__":
    unittest.main()
