"""Regression tests for the zero-resize Canvas → FFmpeg fast path."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from app.renderer.ffmpeg.filter_graph import layered_filter_graph, output_scaling_filter
from app.renderer.ffmpeg_renderer import FFmpegRenderer, VisualizerOverlay
from app.utils.subprocess_utils import hidden_process_kwargs


class IdentityCanvasScaleTests(unittest.TestCase):
    def test_original_filter_stays_default_for_unknown_or_mismatched_inputs(self) -> None:
        for include_fps in (True, False):
            with self.subTest(include_fps=include_fps):
                result = output_scaling_filter(60, 1920, 1080, include_fps=include_fps)
                self.assertIn("scale=1920:1080", result)
                self.assertIn("pad=1920:1080", result)
        self.assertIn(
            "pad=1920:1080",
            FFmpegRenderer._output_scaling_filter(60, 1920, 1080),
        )

    def test_identity_scaling_keeps_cfr_but_skips_scale_pad(self) -> None:
        self.assertEqual(
            output_scaling_filter(60, 1920, 1080, already_sized=True),
            "fps=60",
        )
        self.assertEqual(
            output_scaling_filter(60, 1920, 1080, include_fps=False, already_sized=True),
            "null",
        )
        self.assertEqual(
            FFmpegRenderer._output_scaling_filter(
                60, 1920, 1080, already_sized=True,
            ),
            "fps=60",
        )

    def test_layered_graph_skips_only_last_scale_pad_when_explicitly_enabled(self) -> None:
        visualizer = VisualizerOverlay(8, 4, 16, 16, "bars", "#FFFFFF")
        before = layered_filter_graph([visualizer], [], 10, 64, 48)
        after = layered_filter_graph(
            [visualizer], [], 10, 64, 48, base_already_sized=True,
        )
        self.assertTrue(before.endswith(
            "scale=64:48:force_original_aspect_ratio=decrease,"
            "pad=64:48:(ow-iw)/2:(oh-ih)/2:color=black[vout]"
        ))
        self.assertTrue(after.endswith("null[vout]"))
        self.assertEqual(
            before[:before.rfind(";")] + ";",
            after[:after.rfind(";")] + ";",
        )
        self.assertEqual(
            FFmpegRenderer._layered_filter_graph(
                [visualizer], [], 10, 64, 48, base_already_sized=True,
            ),
            after,
        )

    def test_real_ffmpeg_overlay_pixels_identical_with_and_without_last_scale(self) -> None:
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not executable:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG for FFmpeg pixel parity.")
        visualizer = VisualizerOverlay(8, 4, 16, 16, "bars", "#FFFFFF")

        def render(use_identity: bool) -> bytes:
            graph = layered_filter_graph(
                [visualizer], [], 10, 64, 48, base_already_sized=use_identity,
            )
            return subprocess.run([
                executable, "-hide_banner", "-loglevel", "error",
                "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10",
                "-f", "lavfi", "-i", "anullsrc",
                "-f", "lavfi", "-i",
                "color=red@0.6:size=16x16:rate=10,format=rgba",
                "-filter_complex", graph, "-map", "[vout]",
                "-frames:v", "8", "-pix_fmt", "yuv420p",
                "-f", "rawvideo", "pipe:1",
            ], capture_output=True, check=True, timeout=15,
                **hidden_process_kwargs()).stdout

        original = render(False)
        optimized = render(True)
        self.assertEqual(len(original), 8 * 64 * 48 * 3 // 2)
        self.assertEqual(original, optimized)


if __name__ == "__main__":
    unittest.main()
