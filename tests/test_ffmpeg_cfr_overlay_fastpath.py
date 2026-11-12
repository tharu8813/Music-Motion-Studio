"""Pixel-stable FFmpeg CFR and fixed overlay composition fast paths."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.renderer.ffmpeg import filter_graph
from app.renderer.ffmpeg_renderer import FFmpegRenderer, VisualizerOverlay
from app.utils.subprocess_utils import hidden_process_kwargs


class CfrOverlayFastpathTests(unittest.TestCase):
    @staticmethod
    def raw_pipe(fps: int = 60) -> tuple[str, ...]:
        return (
            "-thread_queue_size", "4",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo",
            "-pix_fmt", "bgr0",
            "-video_size", "64x48",
            "-framerate", str(fps),
            "-i", r"\\.\pipe\canvas-test",
        )

    def test_only_verified_raw_canvas_inputs_skip_fps(self) -> None:
        args = self.raw_pipe()
        self.assertTrue(filter_graph.is_raw_canvas_cfr_input(args, 60, 64, 48))
        self.assertFalse(filter_graph.is_raw_canvas_cfr_input(args, 30, 64, 48))
        self.assertFalse(filter_graph.is_raw_canvas_cfr_input(args, 60, 128, 48))
        self.assertFalse(filter_graph.is_raw_canvas_cfr_input(
            ("-i", "prepared.mp4"), 60, 64, 48,
        ))
        self.assertFalse(filter_graph.is_raw_canvas_cfr_input(
            (*args[:args.index("-f")], "-f", "concat", *args[args.index("-f") + 2:]),
            60, 64, 48,
        ))
        self.assertTrue(filter_graph.is_raw_canvas_cfr_input(
            [*args[:args.index("-pix_fmt")], "-pix_fmt", "rgba",
             *args[args.index("-pix_fmt") + 2:]],
            60, 64, 48,
        ))

    def test_raw_cfr_scaling_preserves_resize_but_skips_only_fps(self) -> None:
        before = filter_graph.output_scaling_filter(60, 64, 48, True, True)
        after = filter_graph.output_scaling_filter(60, 64, 48, True, True, True)
        self.assertEqual(before, "fps=60")
        self.assertEqual(after, "null")
        resized = filter_graph.output_scaling_filter(60, 128, 72, True, False, True)
        self.assertIn("scale=128:72", resized)
        self.assertNotIn("fps=", resized)
        self.assertEqual(
            FFmpegRenderer._output_scaling_filter(60, 64, 48, base_is_cfr=True),
            "scale=64:48:force_original_aspect_ratio=decrease,"
            "pad=64:48:(ow-iw)/2:(oh-ih)/2:color=black",
        )

    def test_graph_preserves_legacy_paths_and_overlay_order(self) -> None:
        overlay = VisualizerOverlay(8, 4, 16, 16, "bars", "#FFFFFF", z_index=0.0)
        static = [(2.0, Path("static.mp4"), "concat", 1, 3)]
        original = filter_graph.layered_filter_graph(
            [overlay], static, 60, 64, 48, base_already_sized=True,
        )
        faster = filter_graph.layered_filter_graph(
            [overlay], static, 60, 64, 48, base_already_sized=True,
            base_is_cfr=True, optimize_constant_overlays=True,
        )
        self.assertIn("[0:v]fps=60:start_time=0", original)
        self.assertIn("[0:v]settb=1/60,setpts=N[base]", faster)
        self.assertNotIn("eval=init", original)
        self.assertIn("[base][2:v]overlay=8:4:eof_action=pass:eval=init", faster)
        self.assertIn("[zlayer0][3:v]overlay=1:3:eof_action=pass:eval=init", faster)
        self.assertEqual(faster.count("eval=init"), 2)
        self.assertEqual(faster.rsplit(";", 1)[-1], "[composited]null[vout]")

    def test_time_based_keyframe_overlay_keeps_per_frame_evaluation(self) -> None:
        overlay = VisualizerOverlay(
            8, 4, 16, 16, "bars", "#FFFFFF", keyframe_binding=object(),
        )
        with patch.object(
            filter_graph, "ffmpeg_transform",
            return_value=("[moving]", "10+T*2", "4"),
        ):
            graph = filter_graph.layered_filter_graph(
                [overlay], [], 60, 64, 48, base_is_cfr=True,
                optimize_constant_overlays=True,
            )
        self.assertIn("overlay='10+T*2':'4':eof_action=pass", graph)
        self.assertNotIn("eval=init", graph)

    def test_framesync_preserves_every_frame_of_staged_and_raw_overlays_at_30_fps(self):
        ffmpeg = FFmpegRenderer.find_executable(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"))
        with TemporaryDirectory() as raw:
            root = Path(raw)
            rgba = root / "sequence.rgba"
            rgba.write_bytes(b"".join(bytes((gray, gray, gray, 255)) * 256
                                      for gray in (20, 50, 80, 110, 140, 170)))
            raw_input = ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", "16x16",
                         "-framerate", "30", "-i", str(rgba)]
            alpha_pair = root / "prepared.mkv"
            subprocess.run([
                ffmpeg, "-v", "error", *raw_input, "-filter_complex_threads", "1",
                "-filter_complex", "[0:v]split[c0][a0];[c0]format=bgr0[c];[a0]alphaextract[a]",
                "-map", "[c]", "-map", "[a]", "-c:v", "ffv1", str(alpha_pair),
            ], check=True, capture_output=True, timeout=20, **hidden_process_kwargs())
            graph = filter_graph.layered_filter_graph(
                [VisualizerOverlay(0, 0, 16, 16, "bars", "#FFFFFF")],
                [(1.0, alpha_pair, "alpha_pair", 16, 0)], 30, 32, 16,
                base_already_sized=True, base_is_cfr=True,
            )
            video = subprocess.run([
                ffmpeg, "-v", "error", "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", "color=black:size=32x16:rate=30",
                "-f", "lavfi", "-i", "anullsrc", *raw_input, "-i", str(alpha_pair),
                "-filter_complex", graph, "-map", "[vout]", "-frames:v", "6",
                "-pix_fmt", "yuv420p", "-f", "rawvideo", "-",
            ], check=True, capture_output=True, timeout=20, **hidden_process_kwargs()).stdout
            frame_size = 32 * 16 * 3 // 2
            self.assertEqual(len(video), 6 * frame_size)
            left = list(video[8 * 32 + 8::frame_size])
            right = list(video[8 * 32 + 24::frame_size])
            self.assertEqual(left, right)
            self.assertEqual(left, sorted(set(left)))

    def test_ffmpeg_real_frames_are_identical_before_and_after(self) -> None:
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not ffmpeg:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG for actual pixel verification.")
        with TemporaryDirectory(prefix="canvas-cfr-fastpath-") as raw:
            directory = Path(raw)
            base_file = directory / "base.bgr0"
            # Each frame is a distinct colour to detect frame duplication.
            base_file.write_bytes(b"".join(
                bytes(((i * 17) % 256, 50, 180, 0)) * (64 * 48)
                for i in range(24)
            ))
            overlay = VisualizerOverlay(8, 4, 16, 16, "bars", "#FFFFFF", z_index=0.0)
            static = [(2.0, Path("static.mp4"), "concat", 1, 3)]

            def render(fast: bool) -> bytes:
                graph = filter_graph.layered_filter_graph(
                    [overlay], static, 60, 64, 48,
                    base_already_sized=True, base_is_cfr=fast,
                    optimize_constant_overlays=fast,
                )
                return subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-filter_complex_threads", "1",
                    "-f", "rawvideo", "-pix_fmt", "bgr0",
                    "-video_size", "64x48", "-framerate", "60",
                    "-i", str(base_file),
                    "-f", "lavfi", "-i", "anullsrc",
                    "-f", "lavfi", "-i",
                    "color=red@0.6:size=16x16:rate=60,format=rgba",
                    "-f", "lavfi", "-i",
                    "color=green@0.4:size=12x12:rate=60,format=rgba",
                    "-filter_complex", graph, "-map", "[vout]",
                    "-frames:v", "24", "-pix_fmt", "yuv420p",
                    "-f", "rawvideo", "pipe:1",
                ], capture_output=True, check=True, timeout=25,
                    **hidden_process_kwargs()).stdout

            before = render(False)
            after = render(True)
            self.assertEqual(len(before), 24 * 64 * 48 * 3 // 2)
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
