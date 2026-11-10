"""Production native GPU full-length H264 -> MP4 mux boundary tests.

These tests exercise the actual FFmpeg export finalizer with synthetic *test*
bitstreams (no GPU claims). The user-facing native producer is a separate
milestone. A six-frame diagnostic report MUST NOT pass this production gate.
"""
from __future__ import annotations

from pathlib import Path
from concurrent.futures import CancelledError
import json
from fractions import Fraction
import math
import os
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import wave

from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import (
    ExportMetadata, FFmpegRenderer, PreparedVideoInput,
    RenderCancelledError, RenderError, RenderSettings,
    RenderResult,
)
from app.renderer.native_gpu_export import (
    PRODUCTION_NATIVE_SCOPE, finalize_native_gpu_mp4,
)
from app.timeline.compiler import compile_playlist


def fixture(root: Path, *, duration: float = 1.0, fps: int = 30):
    audio = root / "original.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * round(8000 * duration))
    track = PlaylistTrack(str(audio), "Native mux", duration_seconds=duration)
    plan = compile_playlist([track])
    video = root / "native.h264"
    video.write_bytes(b"TEST-BITSTREAM")
    prepared = root / "prepared.m4a"
    prepared.write_bytes(b"TEST-AAC")
    settings = RenderSettings(
        fps=fps, output_width=64, output_height=64,
        video_codec="h264_nvenc",
    )
    report = {
        "production_export_scope": PRODUCTION_NATIVE_SCOPE,
        "production_export_changed": True,
        "source": "real_qt_canvas_snapshot_cpu_raster_packets",
        "status": "measured",
        "d3d11_alpha_composition_on_gpu": True,
        "nvenc_output_synchronously_completed": True,
        "gpu_nv12_to_cpu_readbacks": 0,
        "resolution": [64, 64],
        "fps": fps,
        "frames_encoded": math.ceil(plan.duration_seconds * fps - 1e-9),
        "expected_frames": math.ceil(plan.duration_seconds * fps - 1e-9),
        "duration_seconds": plan.duration_seconds,
        "pts_monotonic": True,
        "h264_file": str(video),
    }
    report_path = root / "production-gpu-report.json"
    report_path.write_text(json.dumps(report), encoding="utf8")
    return video, prepared, report_path, report, track, plan, settings


def fake_ffprobe(fps: int = 30, frames: int = 30):
    return SimpleNamespace(returncode=0, stdout=json.dumps({
        "streams": [{
            "codec_name": "h264", "width": 64, "height": 64,
            "has_b_frames": 0, "nb_read_frames": str(frames),
        }],
    }).encode(), stderr=b"")


def fake_mp4_probe():
    return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 64, "height": 64,
         "nb_frames": "30", "avg_frame_rate": "30/1", "duration": "1.0"},
        {"codec_type": "audio", "codec_name": "aac", "duration": "1.0"},
    ]}).encode(), stderr=b"")


class NativeGpuProductionFinalizerTests(unittest.TestCase):
    def _call(self, root, *, report_update=None, probe=None, mux_probe=None):
        params = fixture(root)
        video, prepared, report_file, report, track, plan, settings = params
        if report_update:
            report.update(report_update)
            report_file.write_text(json.dumps(report), encoding="utf8")
        renderer = MagicMock(spec=FFmpegRenderer)
        def mux(video, tracks, output, settings, **kwargs):
            output.write_bytes(b"TEST-MP4")
            return RenderResult(output, 1)
        renderer.render.side_effect = mux
        with patch("app.renderer.native_gpu_export.run_probe", side_effect=[
            probe if probe is not None else fake_ffprobe(),
            fake_mp4_probe() if mux_probe is None else mux_probe,
        ]):
            result = finalize_native_gpu_mp4(
                renderer, h264_path=video, gpu_report_path=report_file,
                prepared_audio_path=prepared, tracks=[track],
                compiled_plan=plan, output_path=root / "final.mp4",
                settings=settings, metadata=ExportMetadata(title="My Playlist"),
            )
        return result, renderer, params

    def test_full_native_stream_is_muxed_using_actual_production_renderer(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            result, renderer, params = self._call(root)
            self.assertEqual(result.output_path, root / "final.mp4")
            args, kwargs = renderer.render.call_args
            self.assertIsInstance(args[0], PreparedVideoInput)
            self.assertTrue(args[0].ready_for_mux)
            self.assertEqual(args[0].encoded_codec, "h264_nvenc")
            self.assertEqual(args[0].duration_seconds, params[5].duration_seconds)
            self.assertEqual(args[2].name, "complete.mp4")
            self.assertEqual((root / "final.mp4").read_bytes(), b"TEST-MP4")
            self.assertEqual(kwargs["metadata"].title, "My Playlist")
            self.assertEqual(kwargs["prepared_audio_path"], params[1])
            self.assertEqual(kwargs["visualizers"], [])
            self.assertEqual(kwargs["static_layers"], [])
            self.assertEqual(kwargs["video_clips"], [])

    def test_diagnostic_report_is_strictly_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for diff in (
                {"production_export_scope": "dynamic_overlay_audio_diagnostic"},
                {"production_export_changed": False},
                {"status": "nvenc_library_unavailable"},
                {"frames_encoded": 6},
                {"pts_monotonic": False},
                {"gpu_nv12_to_cpu_readbacks": 1},
            ):
                with self.subTest(diff=diff), self.assertRaises(RenderError):
                    self._call(root, report_update=diff)
                # _call rewrites the report with a clean fixture each time.

    def test_valid_mux_never_overwrites_a_destination_created_during_export(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "final.mp4"
            target.write_bytes(b"KEEP")
            with self.assertRaises(RenderError):
                self._call(root)
            self.assertEqual(target.read_bytes(), b"KEEP")
            self.assertFalse(list(root.glob(".mms-native-mux-*")))

    def test_missing_or_short_h264_is_rejected_before_final_mux(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(RenderError, "complete playlist"):
                self._call(root, probe=fake_ffprobe(frames=6))
            result, renderer, data = self._call(root)
            data[0].unlink()
            with self.assertRaisesRegex(RenderError, "complete .h264"):
                finalize_native_gpu_mp4(
                    renderer, h264_path=data[0], gpu_report_path=data[2],
                    prepared_audio_path=data[1], tracks=[data[4]],
                    compiled_plan=data[5], output_path=root / "end.mp4",
                    settings=data[6],
                )

    def test_b_frame_reordering_is_rejected_before_native_mp4_mux(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            contaminated = fake_ffprobe()
            stream = json.loads(contaminated.stdout)
            stream["streams"][0]["has_b_frames"] = 2
            contaminated.stdout = json.dumps(stream).encode()
            with self.assertRaisesRegex(RenderError, "complete playlist"):
                self._call(root, probe=contaminated)

    def test_cancelled_export_never_launches_mux(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            video, audio, report_file, _, track, plan, settings = fixture(root)
            renderer = MagicMock(spec=FFmpegRenderer)
            cancelled = Event()
            cancelled.set()
            with self.assertRaises(RenderCancelledError):
                finalize_native_gpu_mp4(
                    renderer, h264_path=video, gpu_report_path=report_file,
                    prepared_audio_path=audio, tracks=[track], compiled_plan=plan,
                    output_path=root / "cancelled.mp4", settings=settings,
                    cancel_event=cancelled,
                )
            renderer.render.assert_not_called()

    def test_bad_mux_or_cancel_keeps_existing_destination_and_removes_staging(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "final.mp4"
            target.write_bytes(b"KEEP-EXISTING")
            for field, value in (("nb_frames", "29"), ("avg_frame_rate", "60/1"),
                                 ("duration", "2.0")):
                probe = fake_mp4_probe()
                payload = json.loads(probe.stdout)
                payload["streams"][0][field] = value
                probe.stdout = json.dumps(payload).encode()
                with self.subTest(field=field), self.assertRaises(RenderError):
                    self._call(root, mux_probe=probe)
                self.assertEqual(target.read_bytes(), b"KEEP-EXISTING")
                self.assertFalse(list(root.glob(".mms-native-mux-*")))
            probe = fake_mp4_probe()
            payload = json.loads(probe.stdout)
            payload["streams"][1]["duration"] = "0.5"
            probe.stdout = json.dumps(payload).encode()
            with self.assertRaises(RenderError):
                self._call(root, mux_probe=probe)
            with self.assertRaises(RenderCancelledError):
                self._call(root, mux_probe=CancelledError())
            self.assertEqual(target.read_bytes(), b"KEEP-EXISTING")
            self.assertFalse(list(root.glob(".mms-native-mux-*")))

    def test_real_ffmpeg_produces_final_mp4_with_audio_without_video_reencode(self):
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not configured or not Path(configured).is_file():
            self.skipTest("Verified FFmpeg required for production mux integration")
        ffmpeg = Path(configured)
        ffprobe = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if not ffprobe.is_file():
            self.fail("Verified FFmpeg installation must also ship FFprobe")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            video, prepared, report_file, report, track, plan, settings = fixture(root)
            video.unlink()
            prepared.unlink()
            subprocess.run([
                str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=64x64:r=30", "-frames:v", "30",
                "-c:v", "libx264", "-bf", "0", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-f", "h264",
                str(video),
            ], check=True, timeout=40)
            subprocess.run([
                str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", "1", "-c:a", "aac", "-b:a", "192k", str(prepared),
            ], check=True, timeout=40)
            renderer = FFmpegRenderer(ffmpeg)
            output = root / "complete.mp4"
            result = finalize_native_gpu_mp4(
                renderer, h264_path=video, gpu_report_path=report_file,
                prepared_audio_path=prepared, tracks=[track],
                compiled_plan=plan, output_path=output, settings=settings,
                ffprobe=ffprobe,
                metadata=ExportMetadata(title="GPU completed video"),
            )
            self.assertEqual(result.output_path, output)
            self.assertTrue(output.is_file())
            probe = subprocess.run([
                str(ffprobe), "-v", "error",
                "-show_entries", "stream=codec_name,nb_frames,avg_frame_rate",
                "-of", "json", str(output),
            ], capture_output=True, check=True, timeout=40)
            streams = json.loads(probe.stdout)["streams"]
            self.assertIn("h264", [x["codec_name"] for x in streams])
            self.assertIn("aac", [x["codec_name"] for x in streams])
            video_stream = next(x for x in streams if x["codec_name"] == "h264")
            self.assertEqual(video_stream["nb_frames"], "30")
            self.assertEqual(Fraction(video_stream["avg_frame_rate"]), 30)


if __name__ == "__main__":
    unittest.main()
