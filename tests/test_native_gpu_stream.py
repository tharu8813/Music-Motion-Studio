"""Bounded application-frame -> native NVENC worker protocol tests.

Use a controlled Python worker to test path/ACK/backpressure behavior.
The test double does NOT encode on NVIDIA hardware and makes no GPU claims.
Windows Rust CI separately compiles the actual D3D11 NVENC worker.
"""
from __future__ import annotations

from pathlib import Path
import os
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Timer, enumerate as active_threads
import unittest
from unittest.mock import MagicMock, patch

from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, NativeCanvasFrame
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderError, RenderSettings, RenderResult
from app.renderer.native_gpu_stream import (
    render_native_gpu_playlist, stream_native_canvas_h264,
)
from app.timeline.compiler import compile_playlist
from app.models.playlist import PlaylistTrack


MOCK_NATIVE_SOURCE = '''
import json
import struct
import os
from pathlib import Path
import sys

flags = dict(zip(sys.argv[1::2], sys.argv[2::2]))
count = int(flags["--expected-frames"])
output = Path(flags["--video-output"])
report = Path(flags["--output"])
def exact(n):
    result = bytearray()
    while len(result) < n:
        part = sys.stdin.buffer.read(min(n-len(result), 997))
        assert part, "truncated"
        result.extend(part)
    return result
assert flags["--input-format"] == "binary-v1"
for index in range(count):
    magic, version, pts, metadata, payload = struct.unpack("<4sIQIQ", exact(28))
    assert (magic, version, pts) == (b"MMSB", 1, index)
    data = json.loads(exact(metadata))
    total = 0
    for layer in data["layers"]:
        pixels = exact(layer["bytes"])
        total += len(pixels)
    assert total == payload
    print(f"MMS_FRAME_ACCEPTED {index}", flush=True)
assert not sys.stdin.buffer.read(1)
assert index+1 == count, (index, count)
output.write_bytes(b"MOCK: NOT NVENC VIDEO")
payload = {
    "status": "measured",
    "production_export_scope": "production_complete_playlist",
    "frames_encoded": count,
}
report.write_text(json.dumps(payload), encoding="utf8")
'''


def generated_frames(count: int, width=64, height=64):
    pixels = bytes((20, 60, 120, 255)) * (width * height)
    for _ in range(count):
        yield NativeCanvasFrame(width, height, (
            NativeCanvasBgraLayer("base", width, height, pixels),
        ))


class NativeGpuStreamTransportTests(unittest.TestCase):
    def tearDown(self):
        self.assertEqual([thread.name for thread in active_threads()
                          if thread.name.startswith("mms-native-gpu-")], [])

    def _run_mock(self, *, count=135, provided=None, cancel=None, worker_source=MOCK_NATIVE_SOURCE,
                  timeout=90):
        if os.name != "nt":
            self.skipTest("The native export transport is Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_worker = root / "worker.py"
            mock_worker.write_text(worker_source, encoding="utf8")
            # Replace ONLY executable startup with a Python test double.
            # The real frame-spooling/ACK/cleanup code executes unmodified.
            original_popen = subprocess.Popen

            def launch(arguments, **kwargs):
                return original_popen(
                    [sys.executable, "-u", str(mock_worker), *arguments[1:]],
                    **kwargs,
                )

            with patch("app.renderer.native_gpu_stream.subprocess.Popen", side_effect=launch):
                result = stream_native_canvas_h264(
                    generated_frames(count) if provided is None else provided,
                    executable=Path(sys.executable),
                    video_path=root / "video.h264",
                    report_path=root / "report.json",
                    width=64, height=64, fps=30,
                    duration_seconds=count / 30,
                    expected_frames=count, cancel_event=cancel,
                    acknowledgement_timeout=timeout,
                )
                self.assertEqual(json.loads((root / "report.json").read_text())
                                 ["frame_transport_bytes"], result["frame_transport_bytes"])
                return result

    def test_fractional_duration_roundtrips_across_native_process_boundary(self):
        # Rounding this duration to 12 significant digits loses one CFR frame.
        duration = 10.000000000009
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.renderer.native_gpu_stream.subprocess.Popen", side_effect=OSError("startup probe")) as launch:
                with self.assertRaises(RenderError):
                    stream_native_canvas_h264([], executable=Path(sys.executable),
                        video_path=root / "video.h264", report_path=root / "report.json",
                        width=64, height=64, fps=120, duration_seconds=duration, expected_frames=1201)
            args = launch.call_args.args[0]
            self.assertEqual(float(args[args.index("--duration-seconds") + 1]), duration)

    def test_streams_over_120_frames_with_one_staged_frame_per_ack(self):
        result = self._run_mock(count=135)
        self.assertEqual(result["frames_encoded"], 135)
        self.assertEqual(result["frame_transport"], "binary-v1")
        self.assertEqual(result["bgra_disk_bytes"], 0)
        self.assertGreater(result["frame_transport_bytes"], 135*64*64*4)
        self.assertEqual(result["timing_scope"],
                         "serial_qt_frame_production_pipe_write_and_native_ack")
        self.assertGreaterEqual(result["frame_production_seconds"], 0)
        self.assertGreaterEqual(result["frame_production_max_seconds"], 0)
        self.assertGreater(result["frame_pipe_write_seconds"], 0)
        self.assertGreaterEqual(result["frame_ack_wait_seconds"], 0)
        self.assertGreater(result["native_stream_elapsed_seconds"], 0)
        self.assertGreater(result["native_stream_wall_fps"], 0)
        # The pipe+ACK timings retain the old frame_transport_seconds counter.
        self.assertLessEqual(
            result["frame_pipe_write_seconds"] + result["frame_ack_wait_seconds"],
            result["frame_transport_seconds"] + 0.01,
        )

    def test_deliberately_slow_frame_producer_is_visible_in_native_timings(self):
        if os.name != "nt":
            self.skipTest("The native export transport is Windows-only")
        from time import sleep
        def slowly_generated():
            for frame in generated_frames(6):
                sleep(0.025)
                yield frame
        result = self._run_mock(count=6, provided=slowly_generated())
        self.assertGreaterEqual(result["frame_production_seconds"], 0.12)
        self.assertGreater(result["frame_production_max_seconds"], 0.02)
        self.assertGreater(
            result["native_stream_elapsed_seconds"],
            result["frame_production_seconds"],
        )

    def test_opt_in_one_frame_lookahead_preserves_order_and_caller_thread(self):
        if os.name != "nt":
            self.skipTest("The native export transport is Windows-only")
        from threading import get_ident
        from app.renderer import native_gpu_stream
        main_thread = get_ident()
        acked = Event()
        painted_threads = []
        observed_overlap = []
        original_ack = native_gpu_stream._frame_acknowledgement

        def marked_ack(*args, **kwargs):
            original_ack(*args, **kwargs)
            acked.set()

        def on_owner_thread():
            for frame in generated_frames(6):
                painted_threads.append(get_ident())
                if len(painted_threads) == 2:
                    observed_overlap.append(not acked.is_set())
                yield frame

        # The controlled worker delays the first ACK so that a speculative
        # second Qt frame must be produced while the previous GPU IO waits.
        slow_worker = MOCK_NATIVE_SOURCE.replace(
            '    print(f"MMS_FRAME_ACCEPTED {index}", flush=True)',
            '    import time; time.sleep(0.15)\\n'
            '    print(f"MMS_FRAME_ACCEPTED {index}", flush=True)',
        )
        with patch.dict(os.environ, {"PLAYLIST_CANVAS_NATIVE_PIPELINED": "1"}), \\
             patch.object(native_gpu_stream, "_frame_acknowledgement", side_effect=marked_ack):
            result = self._run_mock(
                count=6, provided=on_owner_thread(), worker_source=slow_worker,
            )
        self.assertEqual(result["frames_encoded"], 6)
        self.assertTrue(result["frame_pipeline_enabled"])
        self.assertEqual(result["max_outstanding_frame_packets"], 2)
        self.assertEqual(result["timing_scope"],
                         "one_frame_qt_production_overlaps_native_write_and_ack")
        self.assertEqual(painted_threads, [main_thread] * 6)
        self.assertEqual(observed_overlap, [True])
        self.assertGreater(result["frame_pipe_write_seconds"], 0)
        self.assertGreater(result["frame_ack_wait_seconds"], 0)

    def test_opt_in_pipeline_short_and_extra_sources_fail_closed(self):
        with patch.dict(os.environ, {"PLAYLIST_CANVAS_NATIVE_PIPELINED": "1"}):
            with self.assertRaisesRegex(RenderError, "stopped at"):
                self._run_mock(count=6, provided=generated_frames(3))
            with self.assertRaisesRegex(RenderError, "more frames"):
                self._run_mock(count=6, provided=generated_frames(7))

    def test_opt_in_pipeline_bad_ack_and_cancellation_clean_up(self):
        with patch.dict(os.environ, {"PLAYLIST_CANVAS_NATIVE_PIPELINED": "1"}):
            closed = []

            def frames():
                try:
                    yield from generated_frames(6)
                finally:
                    closed.append(True)

            with self.assertRaises(RenderError):
                self._run_mock(
                    count=6, provided=frames(),
                    worker_source='print("MMS_FRAME_ACCEPTED 999", flush=True)',
                )
            self.assertEqual(closed, [True])
            flag = Event()
            timer = Timer(0.2, flag.set)
            timer.start()
            try:
                with self.assertRaises(RenderCancelledError):
                    self._run_mock(
                        count=6, cancel=flag,
                        worker_source='import time; time.sleep(30)',
                    )
            finally:
                timer.cancel()

    def test_short_canvas_generator_fails_without_success_report(self):
        with self.assertRaisesRegex(RenderError, "stopped at"):
            self._run_mock(count=6, provided=generated_frames(3))

    def test_extra_canvas_generator_fails_closed(self):
        with self.assertRaisesRegex(RenderError, "more frames"):
            self._run_mock(count=6, provided=generated_frames(7))

    def test_bad_ack_eof_and_encoder_crash_close_iterator(self):
        for source in (
            'print("MMS_FRAME_ACCEPTED 9", flush=True)',
            'import sys; sys.stdout.close()',
            'raise SystemExit(7)',
        ):
            closed = []
            def frames():
                try:
                    yield from generated_frames(6)
                finally:
                    closed.append(True)
            with self.subTest(source=source), self.assertRaises(RenderError):
                self._run_mock(count=6, provided=frames(), worker_source=source)
            self.assertEqual(closed, [True])

    def test_ack_timeout_closes_iterator(self):
        closed = []
        def frames():
            try:
                yield from generated_frames(6)
            finally:
                closed.append(True)
        with self.assertRaisesRegex(RenderError, "Timed out"):
            self._run_mock(count=6, provided=frames(), timeout=0.2,
                           worker_source='import time; time.sleep(30)')
        self.assertEqual(closed, [True])

    def test_cancel_while_waiting_for_ack(self):
        flag = Event()
        timer = Timer(0.2, flag.set)
        timer.start()
        try:
            with self.assertRaises(RenderCancelledError):
                self._run_mock(count=6, cancel=flag,
                               worker_source='import time; time.sleep(30)')
        finally:
            timer.cancel()

    def test_cancel_when_worker_never_reads_large_frame(self):
        flag = Event()
        timer = Timer(0.2, flag.set)
        timer.start()
        try:
            with self.assertRaises(RenderCancelledError):
                self._run_mock(count=6, cancel=flag,
                    provided=generated_frames(6, 64, 64),
                    worker_source='import time; time.sleep(30)')
        finally:
            timer.cancel()

    def test_large_stderr_is_drained_without_a_disk_log(self):
        source = MOCK_NATIVE_SOURCE.replace('flags = dict',
                    'sys.stderr.write("x" * 200000)\nsys.stderr.flush()\nflags = dict')
        self.assertEqual(self._run_mock(count=6, worker_source=source)["frames_encoded"], 6)

    def test_cancelled_request_fails_without_starting_native_process(self):
        flag = Event()
        flag.set()
        if os.name != "nt":
            self.skipTest("The native export transport is Windows-only")
        with self.assertRaises(RenderCancelledError):
            self._run_mock(count=6, cancel=flag)

    def test_product_pipeline_calls_production_mux_after_native_stream(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            track = PlaylistTrack(str(root / "song.wav"), "Sine", duration_seconds=1.0)
            plan = compile_playlist([track])
            renderer = MagicMock(spec=FFmpegRenderer)
            renderer.executable = root / "ffmpeg.exe"
            passed = generated_frames(30)
            mux_progress = MagicMock()
            with patch("app.renderer.native_gpu_stream.stream_native_canvas_h264") as stream, patch(
                "app.renderer.native_gpu_stream.finalize_native_gpu_mp4"
            ) as finalizer:
                finalizer.return_value = RenderResult(root / "result.mp4", 1)
                result = render_native_gpu_playlist(
                    renderer, passed, executable=root / "native.exe",
                    tracks=[track], compiled_plan=plan,
                    prepared_audio_path=root / "prepared.m4a",
                    output_path=root / "result.mp4",
                    mux_progress_callback=mux_progress,
                    settings=RenderSettings(
                        fps=30, video_codec="h264_nvenc",
                        output_width=64, output_height=64,
                    ),
                )
            self.assertEqual(result.output_path, root / "result.mp4")
            self.assertIs(result.native_report, stream.return_value)
            self.assertIs(stream.call_args.args[0], passed)
            self.assertEqual(stream.call_args.kwargs["expected_frames"], 30)
            self.assertEqual(finalizer.call_args.kwargs["tracks"], [track])
            self.assertIs(finalizer.call_args.kwargs["progress_callback"], mux_progress)
            self.assertEqual(
                finalizer.call_args.kwargs["ffprobe"], root / "ffprobe.exe",
            )


if __name__ == "__main__":
    unittest.main()
