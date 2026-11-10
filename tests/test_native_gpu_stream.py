"""Bounded application-frame -> native NVENC worker protocol tests.

Use a controlled Python worker to test path/ACK/backpressure behavior.
The test double does NOT encode on NVIDIA hardware and makes no GPU claims.
Windows Rust CI separately compiles the actual D3D11 NVENC worker.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, NativeCanvasFrame
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderError, RenderSettings
from app.renderer.native_gpu_stream import (
    render_native_gpu_playlist, stream_native_canvas_h264,
)
from app.timeline.compiler import compile_playlist
from app.models.playlist import PlaylistTrack


MOCK_NATIVE_SOURCE = '''
import json
import os
from pathlib import Path
import sys

flags = dict(zip(sys.argv[1::2], sys.argv[2::2]))
count = int(flags["--expected-frames"])
output = Path(flags["--video-output"])
report = Path(flags["--output"])
for index, line in enumerate(sys.stdin):
    assert index < count
    manifest = Path(line.strip())
    assert manifest.is_file(), manifest
    data = json.loads(manifest.read_text(encoding="utf8"))
    for layer in data["layers"]:
        assert (manifest.parent / layer["file"]).is_file()
    print(f"MMS_FRAME_ACCEPTED {index}", flush=True)
    # The producer must delete the previous frame before sending the next.
    if index:
        old = manifest.parent / f"native-frame-{index-1:08d}.json"
        assert not old.exists(), old
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
    def _run_mock(self, *, count=135, provided=None, cancel=None):
        if os.name != "nt":
            self.skipTest("The native export transport is Windows-only")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_worker = root / "worker.py"
            mock_worker.write_text(MOCK_NATIVE_SOURCE, encoding="utf8")
            # Replace ONLY executable startup with a Python test double.
            # The real frame-spooling/ACK/cleanup code executes unmodified.
            original_popen = subprocess.Popen

            def launch(arguments, **kwargs):
                return original_popen(
                    [sys.executable, "-u", str(mock_worker), *arguments[1:]],
                    **kwargs,
                )

            with patch("app.renderer.native_gpu_stream.subprocess.Popen", side_effect=launch):
                return stream_native_canvas_h264(
                    generated_frames(count) if provided is None else provided,
                    executable=Path(sys.executable),
                    video_path=root / "video.h264",
                    report_path=root / "report.json",
                    width=64, height=64, fps=30,
                    duration_seconds=count / 30,
                    expected_frames=count, cancel_event=cancel,
                )

    def test_streams_over_120_frames_with_one_staged_frame_per_ack(self):
        result = self._run_mock(count=135)
        self.assertEqual(result["frames_encoded"], 135)

    def test_short_canvas_generator_fails_without_success_report(self):
        with self.assertRaisesRegex(RenderError, "stopped at"):
            self._run_mock(count=6, provided=generated_frames(3))

    def test_extra_canvas_generator_fails_closed(self):
        with self.assertRaisesRegex(RenderError, "more frames"):
            self._run_mock(count=6, provided=generated_frames(7))

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
            with patch("app.renderer.native_gpu_stream.stream_native_canvas_h264") as stream, patch(
                "app.renderer.native_gpu_stream.finalize_native_gpu_mp4"
            ) as finalizer:
                finalizer.return_value = "FINALIZED"
                result = render_native_gpu_playlist(
                    renderer, passed, executable=root / "native.exe",
                    tracks=[track], compiled_plan=plan,
                    prepared_audio_path=root / "prepared.m4a",
                    output_path=root / "result.mp4",
                    settings=RenderSettings(
                        fps=30, video_codec="h264_nvenc",
                        output_width=64, output_height=64,
                    ),
                )
            self.assertEqual(result, "FINALIZED")
            self.assertIs(stream.call_args.args[0], passed)
            self.assertEqual(stream.call_args.kwargs["expected_frames"], 30)
            self.assertEqual(finalizer.call_args.kwargs["tracks"], [track])
            self.assertEqual(
                finalizer.call_args.kwargs["ffprobe"], root / "ffprobe.exe",
            )


if __name__ == "__main__":
    unittest.main()
