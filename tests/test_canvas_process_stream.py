from __future__ import annotations

import os
from queue import Empty, Full
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QTransform

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_process_stream import CanvasProcessStream, parallel_canvas_supported
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.canvas_pipe import CanvasPipeCancelledError, CanvasPipeError, piped_export_supported
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.ui.design_system import apply_studio_style
from app.utils.subprocess_utils import hidden_process_kwargs


class CanvasProcessStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        apply_studio_style(cls.application)

    def setUp(self):
        self.scene = CanvasScene()
        self.scene.set_artboard_size(96, 80)
        self.scene.addItem(SourceItem(Source(
            SourceType.SHAPE, 'Background', width=96, height=80,
            fill_color='#24383c', z_index=0,
        )))
        self.scene.addItem(SourceItem(Source(
            SourceType.TEXT, 'Text', text='IVY 한글', font_size=13,
            width=86, height=50, x=4.25, y=5.5, rotation=4, z_index=1,
            fill_color='#dfd8cc', animation_in='fade',
        )))
        self.track = PlaylistTrack('unused.wav', 'Track', duration_seconds=6)
        self.cancel = threading.Event()
        self.captured = []
        self.capturer = ExportCanvasCapturer(
            self.scene, [self.track], 6, set(), [(None, 0), (1, None)],
            self.stage, lambda: None, lambda *_: None,
            retain_static_frames=False, output_fps=24,
        )

    def tearDown(self):
        self.scene.clear()

    def stage(self, image, seconds, _key):
        self.captured.append(QImage(image))
        return RenderFrame(image, seconds)

    def sample(self, index=0):
        return ExportFrameSample(
            self.track, 1, 0, 1 / 24, index / 24, index / 24,
            animation_phase='in', animation_progress=index / 6,
            animation_phase_duration=.25,
        )

    def stream(self, root, key='base', wait=None):
        return CanvasProcessStream(
            Path(root), key, 24, capturer=self.capturer,
            producer_cancel_event=self.cancel,
            producer_wait_callback=wait or self.application.processEvents,
        )

    def test_parallel_selection_is_bounded(self):
        with patch('app.preview.canvas_process_stream.os.cpu_count', return_value=8):
            self.assertTrue(parallel_canvas_supported(self.capturer, 2))
            self.assertFalse(parallel_canvas_supported(self.capturer, 1))
            self.assertFalse(parallel_canvas_supported(self.capturer, 5))
            self.capturer.playlist_duration = 1
            self.assertFalse(parallel_canvas_supported(self.capturer, 2))
            with patch('app.preview.canvas_process_stream.os.cpu_count', return_value=2):
                self.assertFalse(parallel_canvas_supported(self.capturer, 2))

    def test_final_reply_is_read_when_worker_exits_during_poll(self):
        stream = CanvasProcessStream.__new__(CanvasProcessStream)
        stream.stream_key = 'base'
        stream._result = None
        stream._process = Mock()
        type(stream._process).exitcode = PropertyMock(side_effect=[None, 0, 0])
        result = object()
        stream._replies = Mock()
        stream._replies.get.side_effect = [Empty, ('done', (result, {})), Empty]
        stream._poll()
        self.assertIs(stream._result, result)

    def test_worker_eof_request_is_idempotent_and_finish_does_not_repeat_it(self):
        stream = CanvasProcessStream.__new__(CanvasProcessStream)
        stream._finish_requested = False
        stream._result = object()
        stream._process = Mock()
        with (
            patch.object(stream, "submit_sample") as submit,
            patch.object(stream, "cancel") as cancel,
        ):
            stream.request_finish()
            stream.request_finish()
            expected = stream._result
            self.assertIs(stream.finish(), expected)
        submit.assert_called_once_with(None)
        cancel.assert_called_once()
        self.assertTrue(stream._finish_requested)

    def test_worker_eof_request_can_be_retried_after_queue_failure(self):
        stream = CanvasProcessStream.__new__(CanvasProcessStream)
        stream._finish_requested = False
        with patch.object(stream, "submit_sample", side_effect=[
            CanvasPipeError("queue full"), None,
        ]) as submit:
            with self.assertRaises(CanvasPipeError):
                stream.request_finish()
            self.assertFalse(stream._finish_requested)
            stream.request_finish()
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(stream._finish_requested)

    def test_parent_queue_backpressure_counts_full_waits_separately(self):
        stream = CanvasProcessStream.__new__(CanvasProcessStream)
        stream._arguments = ["-i", "mock-pipe"]
        stream._queue_put_wall_seconds = 0.0
        stream._queue_full_events = 0
        stream._queue_submit_count = 0
        stream._jobs = Mock()
        stream._jobs.put.side_effect = [Full(), None]
        stream._track_indexes = {}
        with (
            patch("app.preview.canvas_process_stream.PROFILE_ENABLED", True),
            patch.object(stream, "_check"),
            patch.object(stream, "_wait") as waited,
        ):
            stream.submit_sample(None)
        waited.assert_called_once()
        self.assertEqual(stream._jobs.put.call_count, 2)
        metrics = stream.job_queue_diagnostics()
        self.assertEqual(metrics["full_events"], 1)
        self.assertEqual(metrics["submit_count"], 1)
        self.assertGreaterEqual(metrics["put_wall_seconds"], 0.0)

    def test_equal_z_or_transient_qt_pose_keeps_existing_capture_path(self):
        with patch('app.preview.canvas_process_stream.os.cpu_count', return_value=8):
            self.assertTrue(parallel_canvas_supported(self.capturer, 2))
            overlay = SourceItem(Source(SourceType.SHAPE, 'Same Z', z_index=1))
            self.scene.addItem(overlay)
            self.assertFalse(parallel_canvas_supported(self.capturer, 2))
            self.scene.removeItem(overlay)
            item = self.scene.items()[0]
            item.setOpacity(.7)
            self.assertFalse(parallel_canvas_supported(self.capturer, 2))
            item.setOpacity(1)
            item.setTransform(QTransform().shear(.1, .05))
            self.assertFalse(parallel_canvas_supported(self.capturer, 2))

    def test_cancel_interrupts_startup_or_full_queue_and_reaps_child(self):
        import numpy as np

        self.capturer.bass_envelopes = {self.track.id: np.zeros(1_000_000, dtype=np.float32)}
        waits = 0
        def wait():
            nonlocal waits
            waits += 1
            if waits >= 3:
                self.cancel.set()
        with TemporaryDirectory() as root:
            start = monotonic()
            stream = self.stream(root, wait=wait)
            try:
                with self.assertRaises(CanvasPipeCancelledError):
                    for index in range(30):
                        stream.submit_sample(self.sample(index))
            finally:
                stream.cancel()
            self.assertFalse(stream._process.is_alive())
            feeder = stream._jobs._thread
            if feeder is not None:
                feeder.join(1)
                self.assertFalse(feeder.is_alive())
            self.assertLess(monotonic() - start, 8)

    def test_worker_crash_is_reported_instead_of_blocking(self):
        with TemporaryDirectory() as root:
            stream = self.stream(root)
            try:
                stream._process.terminate()
                stream._process.join(3)
                with self.assertRaises(CanvasPipeError):
                    stream.submit_sample(self.sample())
            finally:
                stream.cancel()
            self.assertFalse(stream._process.is_alive())

    @unittest.skipUnless(piped_export_supported(), 'Video pipes are unavailable')
    def test_session_reaps_all_workers_on_cancel_or_encoder_failure(self):
        from app.preview import export_session as session_module
        from app.preview.export_plan import ExportPlan
        from app.preview.export_session import ExportSession, FinalRenderStoppedError, PngStaging
        from app.renderer.export_timeline import ExportTimelinePlanner
        from app.renderer.ffmpeg_renderer import RenderCancelledError, RenderSettings

        for item in self.scene.items():
            item.source.loop_motion = 'float'
        sources = [item.source for item in self.scene.items()]
        bands = [(None, 0), (1, None)]
        plan = ExportPlan(
            bands, set(), False, True,
            ExportTimelinePlanner.build_by_z_band([self.track], sources, set(), bands, 6, lazy=True),
            6, 6, use_piped_visuals=True,
        )
        for cancel in (True, False):
            with self.subTest(cancel=cancel), TemporaryDirectory() as root:
                self.cancel.clear()
                workers = []
                class RecordingStream(CanvasProcessStream):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        workers.append(self)
                session = ExportSession(
                    scene=self.scene, renderer=SimpleNamespace(executable=Path('unused')),
                    plan=plan, render_settings=RenderSettings(fps=6), active_tracks=[self.track],
                    stream_root=Path(root), preparation_cancel=self.cancel, korean=False,
                    staging=PngStaging(self.stage, lambda *_: None, lambda: None,
                                       lambda: None, lambda: 0, 2),
                    layer_worker_count=lambda _: 2, report_progress=lambda *_: None,
                    pump_ui=self.application.processEvents,
                )
                started = False
                def start(_artifacts):
                    nonlocal started
                    started = True
                    if cancel:
                        self.cancel.set()
                with patch.object(session_module, 'CanvasProcessStream', RecordingStream), patch(
                    'app.preview.canvas_process_stream.os.cpu_count', return_value=8,
                ):
                    with self.assertRaises(RenderCancelledError if cancel else FinalRenderStoppedError):
                        session.run_piped(start, lambda: 'Encoder failed' if started and not cancel else None)
                self.assertTrue(started)
                self.assertEqual(len(workers), 2)
                self.assertTrue(all(not worker._process.is_alive() for worker in workers))
                self.assertEqual(session._pipes, {})

    @unittest.skipUnless(os.environ.get('PLAYLIST_CANVAS_TEST_FFMPEG') and piped_export_supported(),
                         'Real FFmpeg pipe test is opt-in')
    def test_live_three_input_low_fps_ffmpeg_eof_and_exact_final_frame_count(self):
        """Exercise the actual spawn workers, raw pipe inputs and FFmpeg EOF.

        A 12-FPS overlay feeds a 60-FPS composite together with two full-rate
        inputs. No parent waits for a worker to finish until all EOF packets
        have been sent, including the terminal low-FPS alpha frame.
        """
        from app.renderer.ffmpeg.filter_graph import layered_filter_graph
        from app.preview.export_session import ExportSession

        fps, count, step = 60, 360, 5
        duration = count / fps
        self.scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Floating alpha band",
            x=20, y=22, width=31, height=22, z_index=2,
            fill_color="#55AACC",
        )))
        bands = [(None, 0), (0, 1), (1, None)]
        capturer = ExportCanvasCapturer(
            self.scene, [self.track], duration, set(), bands,
            self.stage, lambda: None, lambda *_: None,
            retain_static_frames=False, output_fps=fps,
        )
        streams: dict[str, CanvasProcessStream] = {}
        encoder = None
        started_at = monotonic()
        deadline = started_at + 35.0

        def pump() -> None:
            self.application.processEvents()
            if monotonic() > deadline:
                raise CanvasPipeError("Timed out waiting for FFmpeg stream EOF.")

        def sample(index: int, rate: int, step_size: int) -> ExportFrameSample:
            return ExportFrameSample(
                self.track, 1, 0.0, 1.0 / rate,
                index / rate, index / rate,
                cfr_target_frame_count=(index + 1) * step_size,
            )

        with TemporaryDirectory(prefix="canvas-ffmpeg-eof-") as root:
            try:
                for key, multiplier in (("base", 1), ("layer:0", 1), ("layer:1", step)):
                    stream = CanvasProcessStream(
                        Path(root), key, fps, capturer=capturer,
                        producer_cancel_event=self.cancel, producer_wait_callback=pump,
                        pipe_frame_step=multiplier,
                    )
                    streams[key] = stream
                    stream.submit_sample(sample(0, fps // multiplier, multiplier))

                graph = layered_filter_graph(
                    [], [(1.0, Path("layer0"), "pipe", 0, 0),
                         (2.0, Path("layer1"), "pipe", 0, 0)],
                    fps, 96, 80, base_already_sized=True, base_is_cfr=True,
                    optimize_constant_overlays=True,
                    pipe_input_fps={1: fps // step},
                )
                output = Path(root) / "eof-output.framemd5"
                command = [
                    os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"],
                    "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-filter_complex_threads", "1",
                    *streams["base"].input_arguments(),
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    *streams["layer:0"].input_arguments(),
                    *streams["layer:1"].input_arguments(),
                    "-filter_complex", graph,
                    "-map", "[vout]", "-frames:v", str(count),
                    "-fps_mode", "cfr", "-r", str(fps), "-pix_fmt", "yuv420p",
                    "-f", "framemd5", str(output),
                ]
                encoder = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    **hidden_process_kwargs(),
                )
                for index in range(1, count):
                    streams["base"].submit_sample(sample(index, fps, 1))
                    streams["layer:0"].submit_sample(sample(index, fps, 1))
                    if index % step == 0:
                        low_index = index // step
                        streams["layer:1"].submit_sample(
                            sample(low_index, fps // step, step),
                        )
                # Exercise the real ExportSession fan-out, not a duplicate
                # ordering rule in the test. All calls stay on the Qt thread.
                session = ExportSession.__new__(ExportSession)
                session._pipes = streams
                session._request_worker_eofs(["base", "layer:0", "layer:1"])
                results = {
                    key: streams[key].finish()
                    for key in ("base", "layer:0", "layer:1")
                }
                self.assertEqual(results["base"].frame_count, count)
                self.assertEqual(results["layer:0"].frame_count, count)
                self.assertEqual(results["layer:1"].frame_count, count // step)
                for result in results.values():
                    self.assertAlmostEqual(result.duration_seconds, duration, places=4)
                _stdout, stderr = encoder.communicate(timeout=15)
                self.assertEqual(encoder.returncode, 0, stderr.decode("utf-8", "replace"))
                md5_frames = [
                    line for line in output.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")
                ]
                self.assertEqual(len(md5_frames), count)
                self.assertLess(monotonic() - started_at, 35)
            finally:
                for stream in streams.values():
                    stream.cancel()
                if encoder is not None:
                    if encoder.poll() is None:
                        encoder.kill()
                    encoder.communicate()

    @unittest.skipUnless(os.environ.get('PLAYLIST_CANVAS_TEST_FFMPEG') and piped_export_supported(),
                         'Real FFmpeg pipe test is opt-in')
    def test_opaque_and_alpha_streams_match_serial_pixels_including_fonts(self):
        from app.animation.keyframes import Keyframe
        self.track.lyrics = [
            {"start": index / 24, "end": (index + 1) / 24,
             "text": f"{index}: 가사 / lyrics"} for index in range(12)
        ]
        self.scene.addItem(SourceItem(Source(
            SourceType.LYRICS, 'Transported lyrics', x=2, y=45, width=90, height=30,
            font_size=10, z_index=2, subtitle_animation='none',
            subtitle_context_lines=0, subtitle_next_lines=0, fill_color='#00000000',
        )))
        # Exercise serialization into the independent Qt renderer, including
        # custom handles and nested properties, on opaque and alpha bands.
        for item in self.scene.items():
            if not isinstance(item, SourceItem):
                continue
            source = item.source
            source.shadow.enabled = True
            endpoints = {"width": (source.width, source.width * .7),
                         "border_radius": (0, 20), "shadow.offset_x": (-3, 8),
                         "shadow.opacity": (.15, .8)}
            if source.source_type is SourceType.TEXT:
                endpoints["font_size"] = (13, 26)
            else:
                endpoints["outline_width"] = (0, 6)
            source.keyframes.global_curves = {
                name: [Keyframe(0, start, "bezier", (.2, -.4, .7, 1.5)), Keyframe(.25, end)]
                for name, (start, end) in endpoints.items()
            }
        # Prepare invariant/crop decisions after the animated project exists,
        # as ExportSession does before it starts either rendering path.
        self.capturer = ExportCanvasCapturer(
            self.scene, [self.track], 6, set(), [(None, 0), (1, None)],
            self.stage, lambda: None, lambda *_: None,
            retain_static_frames=False, output_fps=24,
        )
        for key in ('base', 'layer:0'):
            with self.subTest(key=key), TemporaryDirectory() as root:
                self.captured.clear()
                for index in range(6):
                    self.capturer.capture_stream(self.sample(index), key)
                image_format = (QImage.Format.Format_RGB32 if key == 'base'
                                else QImage.Format.Format_RGBA8888)
                expected_frames = []
                for image in self.captured:
                    prepared = image.convertToFormat(image_format)
                    expected_frames.append(bytes(prepared.constBits()))
                expected = b''.join(expected_frames)
                stream = self.stream(root, key)
                reader = None
                try:
                    stream.submit_sample(self.sample())
                    reader = subprocess.Popen([
                        os.environ['PLAYLIST_CANVAS_TEST_FFMPEG'], '-v', 'error',
                        *stream.input_arguments(), '-frames:v', '6', '-an',
                        '-c:v', 'copy', '-f', 'rawvideo', 'pipe:1',
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_process_kwargs())
                    for index in range(1, 6):
                        stream.submit_sample(self.sample(index))
                    result = stream.finish()
                    actual, error = reader.communicate(timeout=15)
                    self.assertEqual(reader.returncode, 0, error)
                    self.assertEqual(result.frame_count, 6)
                    self.assertAlmostEqual(result.duration_seconds, .25)
                    self.assertEqual(actual, expected)
                    self.assertFalse(stream._process.is_alive())
                finally:
                    stream.cancel()
                    if reader is not None:
                        if reader.poll() is None:
                            reader.kill()
                        reader.communicate()


if __name__ == '__main__':
    unittest.main()
