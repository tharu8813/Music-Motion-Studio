from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from app.renderer.static_video_stream import (
    DirectVideoEncodingProfile,
    StaticVideoStreamEncoder,
    StaticVideoStreamError,
)


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, value: object) -> int:
        payload = bytes(value)
        self.data.extend(payload)
        return len(payload)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, command: list[str], return_code: int = 0) -> None:
        self.command = command
        self.stdin = _FakeStdin()
        self.stderr = BytesIO()
        self.returncode: int | None = None
        self.return_code = return_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = self.return_code
        if self.return_code == 0 and not self.terminated:
            Path(self.command[-1]).touch()
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


class _SlowFlushProcess(_FakeProcess):
    """Pretend FFmpeg needs several polling cycles to flush its encoder."""

    def __init__(self, command: list[str], polls: int = 3) -> None:
        super().__init__(command)
        self.remaining_polls = polls

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and self.remaining_polls > 0:
            self.remaining_polls -= 1
            raise subprocess.TimeoutExpired(self.command, timeout)
        return super().wait(timeout)


class StaticVideoStreamEncoderTests(unittest.TestCase):
    def test_subframe_state_skips_conversion_and_encoder_io(self) -> None:
        stream = StaticVideoStreamEncoder(Path("ffmpeg"), Path("unused.mkv"), 60)
        stream._duration_seconds = 0.001
        stream._frame_count = 1
        stream._process = Mock()
        image = Mock()
        stream._write_frame(SimpleNamespace(image=image, duration_seconds=0.001))
        image.convertToFormat.assert_not_called()
        stream._process.stdin.write.assert_not_called()
        self.assertAlmostEqual(stream._duration_seconds, 0.002)
        self.assertEqual(stream._frame_count, 1)

    def test_finish_pumps_ui_while_ffmpeg_flushes(self) -> None:
        callbacks: list[int] = []

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            return _SlowFlushProcess(command)

        image = QImage(2, 2, QImage.Format.Format_RGB32)
        with TemporaryDirectory(prefix="static-stream-responsive-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), Path(raw_directory) / "stream.mkv", 30,
                producer_wait_callback=lambda: callbacks.append(1),
            )
            encoder.submit(image, 0.1)
            result = encoder.finish()

        self.assertEqual(result.frame_count, 3)
        self.assertGreaterEqual(len(callbacks), 3)

    def test_finish_honors_cancel_while_ffmpeg_flushes(self) -> None:
        cancel_event = threading.Event()
        process: _SlowFlushProcess | None = None

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            nonlocal process
            process = _SlowFlushProcess(command, polls=100)
            return process

        image = QImage(2, 2, QImage.Format.Format_RGB32)
        with TemporaryDirectory(prefix="static-stream-finish-cancel-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), Path(raw_directory) / "stream.mkv", 30,
                producer_cancel_event=cancel_event,
                producer_wait_callback=cancel_event.set,
            )
            encoder.submit(image, 0.1)
            with self.assertRaisesRegex(StaticVideoStreamError, "cancelled"):
                encoder.finish()

        self.assertIsNotNone(process)
        assert process is not None
        self.assertTrue(process.terminated)

    def test_cancel_stops_expanding_one_coalesced_state_immediately(self) -> None:
        """A cancel mid-expansion must not keep feeding FFmpeg thousands of frames."""
        cancel_event = threading.Event()

        class _TripStdin(_FakeStdin):
            def write(self, value: object) -> int:
                if len(self.data) // max(1, len(bytes(value))) >= 6:
                    cancel_event.set()
                return super().write(value)

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command)
            process.stdin = _TripStdin()
            return process

        image = QImage(2, 2, QImage.Format.Format_RGB32)
        with TemporaryDirectory(prefix="static-stream-cancel-expand-") as raw, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), Path(raw) / "stream.mkv", 30,
                producer_cancel_event=cancel_event,
            )
            encoder.submit(image, 60.0)  # 1800 output frames without a cancel
            deadline = time.monotonic() + 3.0
            while not cancel_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)
            written = encoder.frame_count
            encoder.cancel()

        self.assertTrue(cancel_event.is_set())
        self.assertLess(written, 30)  # nowhere near the 1800-frame expansion

    def test_variable_durations_stream_as_lossless_cfr_with_bounded_queue(self) -> None:
        processes: list[_FakeProcess] = []

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command)
            processes.append(process)
            return process

        first = QImage(2, 2, QImage.Format.Format_RGB32)
        first.fill(QColor("#112233"))
        second = QImage(2, 2, QImage.Format.Format_RGB32)
        second.fill(QColor("#445566"))
        with TemporaryDirectory(prefix="static-stream-test-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            output = Path(raw_directory) / "canvas-stream.mkv"
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), output, 10, queue_capacity=2,
            )
            encoder.submit(first, 0.15)
            encoder.submit(second, 0.15)
            result = encoder.finish()

            self.assertTrue(output.is_file())

        self.assertEqual(len(processes), 1)
        process = processes[0]
        self.assertIn("libx264rgb", process.command)
        self.assertIn("bgr0", process.command)
        self.assertIn("-crf", process.command)
        self.assertEqual(result.duration_seconds, 0.3)
        self.assertEqual(result.frame_count, 3)
        self.assertFalse(result.has_alpha_stream)
        self.assertLessEqual(result.peak_buffered_frames, 2)
        bytes_per_frame = first.width() * first.height() * 4
        self.assertEqual(len(process.stdin.data), bytes_per_frame * 3)
        self.assertEqual(
            process.stdin.data[:bytes_per_frame * 2],
            bytes(first.constBits()) * 2,
        )
        self.assertEqual(
            process.stdin.data[bytes_per_frame * 2:],
            bytes(second.constBits()),
        )
        self.assertTrue(process.stdin.closed)

    def test_transparent_stream_encodes_lossless_color_and_alpha_tracks(self) -> None:
        processes: list[_FakeProcess] = []

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command)
            processes.append(process)
            return process

        image = QImage(2, 2, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(20, 40, 60, 128))
        with TemporaryDirectory(prefix="static-alpha-stream-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"),
                Path(raw_directory) / "alpha-stream.mkv",
                10,
                preserve_alpha=True,
            )
            encoder.submit(image, 0.1)
            result = encoder.finish()

        command = processes[0].command
        self.assertTrue(result.has_alpha_stream)
        self.assertTrue(any("alphaextract,format=gray" in value for value in command))
        self.assertIn("-c:v:0", command)
        self.assertIn("libx264rgb", command)
        self.assertIn("-c:v:1", command)
        self.assertIn("ffv1", command)
        expected = image.convertToFormat(QImage.Format.Format_RGBA8888)
        self.assertEqual(processes[0].stdin.data, bytes(expected.constBits()))

    def test_opaque_stream_can_encode_selected_final_codec_during_capture(self) -> None:
        processes: list[_FakeProcess] = []

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command)
            processes.append(process)
            return process

        image = QImage(4, 2, QImage.Format.Format_RGB32)
        image.fill(QColor("#123456"))
        with TemporaryDirectory(prefix="direct-final-stream-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), Path(raw_directory) / "direct.mkv", 60,
                direct_profile=DirectVideoEncodingProfile(
                    1920, 1080, "h264_amf",
                    ("-quality", "speed", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"),
                ),
            )
            encoder.submit(image, 0.1)
            result = encoder.finish()

        command = processes[0].command
        self.assertIn("h264_amf", command)
        self.assertTrue(any(
            "fps=60,scale=1920:1080" in argument for argument in command
        ))
        self.assertIn("-quality", command)
        self.assertNotIn("libx264rgb", command)
        self.assertEqual((result.width, result.height), (1920, 1080))
        self.assertEqual(result.frame_count, 6)

    def test_mismatched_frame_size_is_rejected_before_writing_it(self) -> None:
        process: _FakeProcess | None = None

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            nonlocal process
            process = _FakeProcess(command)
            return process

        first = QImage(2, 2, QImage.Format.Format_RGB32)
        second = QImage(3, 2, QImage.Format.Format_RGB32)
        with TemporaryDirectory(prefix="static-stream-size-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"), Path(raw_directory) / "stream.mkv", 30,
            )
            encoder.submit(first, 0.1)
            with self.assertRaises(StaticVideoStreamError):
                encoder.submit(second, 0.1)
            encoder.cancel()

        self.assertIsNotNone(process)
        assert process is not None
        self.assertTrue(process.terminated)

    def test_external_cancel_event_stops_submission_and_encoder(self) -> None:
        cancel_event = threading.Event()
        process: _FakeProcess | None = None

        def create_process(command: list[str], **_kwargs: object) -> _FakeProcess:
            nonlocal process
            process = _FakeProcess(command)
            return process

        image = QImage(2, 2, QImage.Format.Format_RGB32)
        with TemporaryDirectory(prefix="static-stream-cancel-") as raw_directory, patch(
            "app.renderer.static_video_stream.subprocess.Popen",
            side_effect=create_process,
        ):
            encoder = StaticVideoStreamEncoder(
                Path("ffmpeg.exe"),
                Path(raw_directory) / "stream.mkv",
                30,
                producer_cancel_event=cancel_event,
            )
            encoder.submit(image, 0.1)
            cancel_event.set()
            with self.assertRaises(StaticVideoStreamError):
                encoder.submit(image, 0.1)
            pipeline = encoder._pipeline
            self.assertIsNotNone(pipeline)
            assert pipeline is not None
            self.assertFalse(pipeline._thread.is_alive())
            encoder.cancel()

        self.assertIsNotNone(process)
        assert process is not None
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
