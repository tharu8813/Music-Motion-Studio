from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from time import monotonic, sleep
import unittest
from unittest.mock import Mock, patch
import wave

from PySide6.QtGui import QColor, QImage

from app.models.playlist import PlaylistTrack
from app.renderer.canvas_pipe import (
    CanvasPipeCancelledError,
    CanvasPipeError,
    PipedCanvasStream,
    piped_export_supported,
)
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer,
    PipedStaticOverlayLayer,
    PipedVideoInput,
    RenderCancelledError,
    RenderError,
    RenderSettings,
)


def _solid(width: int, height: int, color: QColor, *, alpha: bool = False) -> QImage:
    image = QImage(
        width, height,
        QImage.Format.Format_ARGB32_Premultiplied if alpha else QImage.Format.Format_RGB32,
    )
    image.fill(color)
    return image


def _read_all(path: str, sink: bytearray, *, limit: int | None = None) -> None:
    with open(path, "rb") as reader:
        while True:
            chunk = reader.read(65536)
            if not chunk:
                return
            sink.extend(chunk)
            if limit is not None and len(sink) >= limit:
                return


# Windows named pipes and POSIX FIFOs alike: a plain open() is the reader.
@unittest.skipUnless(piped_export_supported(), "named pipes unavailable")
class PipedCanvasStreamTests(unittest.TestCase):
    def test_subframe_state_skips_conversion_and_pipe_io(self) -> None:
        with patch("app.renderer.canvas_pipe._create_endpoint") as endpoint:
            stream = PipedCanvasStream(Path("unused"), "base", 60, preserve_alpha=True)
            stream._written_seconds = 0.001
            stream._frame_count = 1
            image = Mock()
            stream._write_frame(SimpleNamespace(image=image, duration_seconds=0.001))
            image.convertToFormat.assert_not_called()
            endpoint.return_value.connect.assert_not_called()
            endpoint.return_value.write.assert_not_called()
            self.assertAlmostEqual(stream._written_seconds, 0.002)
            self.assertEqual(stream.frame_count, 1)

    def test_expands_states_to_cfr_frames_like_the_intermediate_encoder(self) -> None:
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "base", 10, preserve_alpha=False)
            received = bytearray()
            stream.submit(_solid(4, 2, QColor(255, 0, 0)), 0.25)
            reader = threading.Thread(target=_read_all, args=(stream.path, received))
            reader.start()
            stream.submit(_solid(4, 2, QColor(0, 0, 255)), 0.25)
            result = stream.finish()
            reader.join(timeout=5.0)

            self.assertFalse(reader.is_alive())
            # 0.25s -> round(2.5) = 3 frames, then 5 frames in total.
            self.assertEqual(result.frame_count, 5)
            self.assertAlmostEqual(result.duration_seconds, 0.5)
            self.assertEqual(len(received), 5 * 4 * 2 * 4)
            # Format_RGB32 is written as bgr0: blue, green, red, pad.
            self.assertEqual(tuple(received[0:3]), (0, 0, 255))
            self.assertEqual(tuple(received[-4:-1]), (255, 0, 0))
            self.assertFalse(Path(stream.path).exists())

    def test_transparent_stream_is_straight_rgba(self) -> None:
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "layer:0", 10, preserve_alpha=True)
            received = bytearray()
            stream.submit(_solid(2, 2, QColor(255, 0, 0, 128), alpha=True), 0.1)
            arguments = stream.input_arguments()
            reader = threading.Thread(target=_read_all, args=(stream.path, received))
            reader.start()
            stream.finish()
            reader.join(timeout=5.0)

        self.assertIn("rgba", arguments)
        self.assertIn("2x2", arguments)
        self.assertEqual(arguments[-2:], ["-i", stream.path])
        red, green, blue, alpha = received[0:4]
        self.assertEqual((red, green, blue), (255, 0, 0))
        self.assertAlmostEqual(alpha, 128, delta=1)

    def test_resolution_change_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "base", 10, preserve_alpha=False)
            stream.submit(_solid(4, 4, QColor(0, 0, 0)), 0.1)
            with self.assertRaises(CanvasPipeError):
                stream.submit(_solid(8, 4, QColor(0, 0, 0)), 0.1)
            stream.cancel()

    def test_reader_that_stops_early_does_not_fail_the_stream(self) -> None:
        """FFmpeg stops reading at ``-t``; its exit status decides success."""
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "base", 30, preserve_alpha=False)
            frame_bytes = 16 * 16 * 4
            received = bytearray()
            stream.submit(_solid(16, 16, QColor(0, 255, 0)), 0.1)
            reader = threading.Thread(
                target=_read_all, args=(stream.path, received),
                kwargs={"limit": frame_bytes},
            )
            reader.start()
            reader.join(timeout=5.0)
            for _index in range(5):
                stream.submit(_solid(16, 16, QColor(0, 255, 0)), 1.0)
            result = stream.finish()

        self.assertTrue(result.reader_closed_early)
        self.assertAlmostEqual(result.duration_seconds, 5.1)

    def test_cancel_before_ffmpeg_connects_returns_promptly(self) -> None:
        with TemporaryDirectory() as directory:
            cancel = threading.Event()
            stream = PipedCanvasStream(
                Path(directory), "base", 10, preserve_alpha=False,
                producer_cancel_event=cancel,
            )
            stream.submit(_solid(4, 4, QColor(0, 0, 0)), 1.0)
            started = monotonic()
            threading.Timer(0.2, cancel.set).start()
            with self.assertRaises(CanvasPipeCancelledError):
                # The queue fills while no reader exists; cancellation must
                # still release the producer.
                for _index in range(10):
                    stream.submit(_solid(4, 4, QColor(0, 0, 0)), 1.0)
            stream.cancel()

        self.assertLess(monotonic() - started, 5.0)

    def test_disable_switch_turns_the_live_path_off(self) -> None:
        previous = os.environ.get("PLAYLIST_CANVAS_DISABLE_PIPED_EXPORT")
        os.environ["PLAYLIST_CANVAS_DISABLE_PIPED_EXPORT"] = "1"
        try:
            self.assertFalse(piped_export_supported())
        finally:
            if previous is None:
                os.environ.pop("PLAYLIST_CANVAS_DISABLE_PIPED_EXPORT", None)
            else:
                os.environ["PLAYLIST_CANVAS_DISABLE_PIPED_EXPORT"] = previous
        self.assertTrue(piped_export_supported())


def _count_bytes(path: str, counts: list[int], *, delay: float = 0.0,
                 stall: threading.Event | None = None) -> None:
    """A reader that only counts (a long stream would not fit a bytearray)."""
    with open(path, "rb") as reader:
        while chunk := reader.read(65536):
            counts[0] += len(chunk)
            if stall is not None:
                stall.wait()
            if delay:
                sleep(delay)


@unittest.skipUnless(piped_export_supported(), "named pipes unavailable")
class PipedCanvasStreamStressTests(unittest.TestCase):
    """Long, slow, stalled, parallel and repeated streams: no leak, no hang."""

    def _threads(self) -> set[str]:
        return {thread.name for thread in threading.enumerate() if thread.name.startswith("canvas-pipe-")}

    def test_thirty_minute_timeline_streams_in_bounded_memory(self) -> None:
        fps, side, seconds = 30, 8, 1800
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "base", fps, preserve_alpha=False)
            counts = [0]
            stream.submit(_solid(side, side, QColor(0, 0, 0)), 1.0)
            reader = threading.Thread(target=_count_bytes, args=(stream.path, counts))
            reader.start()
            for index in range(1, seconds):
                stream.submit(_solid(side, side, QColor(index % 256, 0, 0)), 1.0)
            result = stream.finish()
            reader.join(timeout=30.0)
            diagnostics = stream.diagnostics()

        self.assertFalse(reader.is_alive())
        self.assertEqual(result.frame_count, fps * seconds)
        self.assertEqual(counts[0], fps * seconds * side * side * 4)
        self.assertLessEqual(result.peak_buffered_frames, stream.queue_capacity)
        self.assertEqual(diagnostics["submitted_states"], seconds)
        self.assertEqual(diagnostics["pending"], 0)
        self.assertIsNotNone(diagnostics["connect_seconds"])
        self.assertEqual(self._threads(), set())

    def test_slow_reader_applies_backpressure_instead_of_buffering(self) -> None:
        with TemporaryDirectory() as directory:
            stream = PipedCanvasStream(Path(directory), "base", 10, preserve_alpha=False)
            counts = [0]
            stream.submit(_solid(64, 64, QColor(0, 0, 0)), 0.1)
            reader = threading.Thread(
                target=_count_bytes, args=(stream.path, counts), kwargs={"delay": 0.005},
            )
            reader.start()
            for _index in range(59):
                stream.submit(_solid(64, 64, QColor(0, 0, 0)), 0.1)
                self.assertLessEqual(stream.pending_frames, stream.queue_capacity)
            result = stream.finish()
            reader.join(timeout=30.0)
        self.assertEqual(counts[0], 60 * 64 * 64 * 4)
        self.assertLessEqual(result.peak_buffered_frames, stream.queue_capacity)

    def test_cancel_while_the_reader_is_stalled_releases_everything(self) -> None:
        with TemporaryDirectory() as directory:
            cancel = threading.Event()
            stall = threading.Event()  # never set: FFmpeg stops reading mid-stream
            stream = PipedCanvasStream(
                Path(directory), "base", 30, preserve_alpha=False, producer_cancel_event=cancel,
            )
            stream.submit(_solid(256, 256, QColor(0, 0, 0)), 1.0)
            reader = threading.Thread(
                target=_count_bytes, args=(stream.path, [0]), kwargs={"stall": stall}, daemon=True,
            )
            reader.start()
            threading.Timer(0.3, cancel.set).start()
            started = monotonic()
            with self.assertRaises(CanvasPipeCancelledError):
                for _index in range(100):
                    stream.submit(_solid(256, 256, QColor(0, 0, 0)), 1.0)
            stream.cancel()
            elapsed = monotonic() - started
            stall.set()
            reader.join(timeout=5.0)
        self.assertLess(elapsed, 8.0)
        self.assertEqual(self._threads(), set())

    def test_twenty_parallel_z_bands_all_complete(self) -> None:
        with TemporaryDirectory() as directory:
            streams = [
                PipedCanvasStream(Path(directory), f"layer:{index}", 10, preserve_alpha=True)
                for index in range(20)
            ]
            counts = [[0] for _stream in streams]
            readers = []
            for stream, count in zip(streams, counts):
                stream.submit(_solid(16, 16, QColor(255, 0, 0, 128), alpha=True), 0.1)
                reader = threading.Thread(target=_count_bytes, args=(stream.path, count))
                reader.start()
                readers.append(reader)
            for _index in range(29):  # the capture loop feeds every band in timeline order
                for stream in streams:
                    stream.submit(_solid(16, 16, QColor(255, 0, 0, 128), alpha=True), 0.1)
            results = [stream.finish() for stream in streams]
            for reader in readers:
                reader.join(timeout=10.0)
        self.assertTrue(all(result.frame_count == 30 for result in results))
        self.assertEqual({count[0] for count in counts}, {30 * 16 * 16 * 4})
        self.assertEqual(self._threads(), set())

    def test_twenty_back_to_back_exports_leave_no_threads_or_pipes(self) -> None:
        with TemporaryDirectory() as directory:
            for attempt in range(20):
                stream = PipedCanvasStream(Path(directory), "base", 10, preserve_alpha=False)
                stream.submit(_solid(8, 8, QColor(0, 0, 0)), 0.1)
                if attempt % 2:  # alternate a finished export and an abandoned one
                    reader = threading.Thread(target=_count_bytes, args=(stream.path, [0]))
                    reader.start()
                    stream.finish()
                    reader.join(timeout=5.0)
                else:
                    stream.cancel()
            leftovers = list(Path(directory).iterdir())
        self.assertEqual(self._threads(), set())
        self.assertEqual(leftovers, [])


def _configured_ffmpeg(test: unittest.TestCase) -> Path:
    configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
    if not configured:
        test.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
    executable = Path(configured)
    if not executable.is_file():
        test.fail(f"Configured FFmpeg does not exist: {executable}")
    return executable


def _silence(path: Path, duration: float) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48_000)
        audio.writeframes(b"\0\0\0\0" * round(48_000 * duration))


@unittest.skipUnless(piped_export_supported(), "named pipes unavailable")
class PipedFinalRenderIntegrationTests(unittest.TestCase):
    """Real FFmpeg reads two live pipes while frames are still being produced."""

    fps = 10
    size = 32

    def _render(
        self, executable: Path, directory: Path, duration: float, *,
        settings: RenderSettings | None = None,
        cancel_event: threading.Event | None = None,
        frames_per_second_delay: float = 0.0,
    ) -> tuple[Path, list[BaseException], dict[str, PipedCanvasStream]]:
        audio_path = directory / "silence.wav"
        _silence(audio_path, duration)
        track = PlaylistTrack(str(audio_path), "Silence", duration_seconds=duration)
        settings = settings or RenderSettings(
            fps=self.fps, video_codec="libx264", crf=0, preset="ultrafast",
            output_width=self.size, output_height=self.size,
        )
        base = PipedCanvasStream(directory, "base", self.fps, preserve_alpha=False)
        layer = PipedCanvasStream(directory, "layer:0", self.fps, preserve_alpha=True)
        step = 1.0 / self.fps
        base.submit(_solid(self.size, self.size, QColor(0, 0, 255)), step)
        layer.submit(
            _solid(self.size // 2, self.size // 2, QColor(255, 0, 0, 128), alpha=True),
            step,
        )
        errors: list[BaseException] = []
        output_path = directory / "result.mp4"

        def render() -> None:
            try:
                FFmpegRenderer(executable).render(
                    PipedVideoInput(
                        tuple(base.input_arguments()), duration,
                        self.size, self.size, self.fps,
                    ),
                    [track], output_path, settings,
                    cancel_event=cancel_event,
                    static_layers=[PipedStaticOverlayLayer(
                        2.0,
                        PipedVideoInput(
                            tuple(layer.input_arguments()), duration,
                            self.size // 2, self.size // 2, self.fps,
                        ),
                        4, 4,
                    )],
                )
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
                base.cancel()
                layer.cancel()

        worker = threading.Thread(target=render)
        worker.start()
        remaining = round(duration * self.fps) - 1
        try:
            for index in range(remaining):
                if frames_per_second_delay:
                    sleep(frames_per_second_delay)
                shade = 255 if index % 2 else 200
                base.submit(_solid(self.size, self.size, QColor(0, 0, shade)), step)
                layer.submit(
                    _solid(
                        self.size // 2, self.size // 2,
                        QColor(255, 0, 0, 128), alpha=True,
                    ),
                    step,
                )
            base.finish()
            layer.finish()
        except CanvasPipeError as error:
            errors.append(error)
        worker.join(timeout=60.0)
        self.assertFalse(worker.is_alive())
        return output_path, errors, {"base": base, "layer": layer}

    def test_piped_base_and_alpha_layer_composite_into_the_final_video(self) -> None:
        executable = _configured_ffmpeg(self)
        with TemporaryDirectory(prefix="playlist-pipe-render-") as raw_directory:
            directory = Path(raw_directory)
            output_path, errors, _streams = self._render(executable, directory, 1.0)
            self.assertEqual(errors, [])
            decoded = subprocess.run(
                [
                    str(executable), "-hide_banner", "-loglevel", "error",
                    "-i", str(output_path), "-pix_fmt", "rgb24",
                    "-f", "rawvideo", "pipe:1",
                ],
                capture_output=True, check=True,
            ).stdout
            self.assertEqual(sorted(p.name for p in directory.iterdir()),
                             ["result.mp4", "silence.wav"])

        frame_bytes = self.size * self.size * 3
        self.assertEqual(len(decoded), frame_bytes * self.fps)
        centre = ((self.size // 2) * self.size + self.size // 2) * 3
        corner = (1 * self.size + 1) * 3
        red, _green, blue = decoded[centre:centre + 3]
        self.assertGreater(red, 100)  # the half-transparent layer at (4, 4)
        self.assertGreater(blue, 60)
        red, _green, blue = decoded[corner:corner + 3]
        self.assertLess(red, 40)  # the base outside the layer
        self.assertGreater(blue, 150)

    def test_cancel_during_live_encode_leaves_no_output(self) -> None:
        executable = _configured_ffmpeg(self)
        with TemporaryDirectory(prefix="playlist-pipe-cancel-") as raw_directory:
            directory = Path(raw_directory)
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            started = monotonic()
            output_path, errors, _streams = self._render(
                executable, directory, 3.0, cancel_event=cancel,
                frames_per_second_delay=0.05,
            )
            self.assertTrue(any(isinstance(e, RenderCancelledError) for e in errors))
            self.assertFalse(output_path.exists())
            self.assertEqual(
                [p.name for p in directory.iterdir() if "rendering" in p.name], [],
            )
        self.assertLess(monotonic() - started, 20.0)

    def test_ffmpeg_failure_releases_the_capture_side(self) -> None:
        executable = _configured_ffmpeg(self)
        with TemporaryDirectory(prefix="playlist-pipe-crash-") as raw_directory:
            directory = Path(raw_directory)
            settings = RenderSettings(
                fps=self.fps, video_codec="libx264", crf=0,
                preset="not-a-real-preset",
                output_width=self.size, output_height=self.size,
            )
            started = monotonic()
            output_path, errors, _streams = self._render(
                executable, directory, 2.0, settings=settings,
            )
            self.assertTrue(any(isinstance(e, RenderError) for e in errors))
            self.assertFalse(output_path.exists())
        self.assertLess(monotonic() - started, 20.0)


if __name__ == "__main__":
    unittest.main()
