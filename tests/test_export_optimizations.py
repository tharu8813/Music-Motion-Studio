from __future__ import annotations

from dataclasses import replace
from itertools import product
import os
from pathlib import Path
import subprocess
import unittest

from app.renderer.ffmpeg.filter_graph import layered_filter_graph, video_fade_expression
from app.renderer.ffmpeg_renderer import VideoClipOverlay
from app.utils.subprocess_utils import hidden_process_kwargs


class VideoFilterOptimizationTests(unittest.TestCase):
    clip = VideoClipOverlay(Path("video.mp4"), 0, 0.8, 0, 8, 8, 32, 32, 0)

    def test_neutral_controls_skip_work_and_non_neutral_controls_remain(self):
        graph = layered_filter_graph([], [], 10, 64, 48, [self.clip])
        self.assertNotIn("eq=", graph)
        self.assertNotIn("colorchannelmixer=", graph)
        self.assertNotIn("geq=", graph)
        for changes, expected in (
            ({"brightness": 50}, "eq=brightness=0.5000"),
            ({"contrast": 50}, "contrast=1.5000"),
            ({"saturation": 0.5}, "saturation=0.5000"),
            ({"opacity": 0.5}, "colorchannelmixer=aa=0.5000"),
        ):
            with self.subTest(changes=changes):
                graph = layered_filter_graph([], [], 10, 64, 48, [replace(self.clip, **changes)])
                self.assertIn(expected, graph)

    def test_alpha_masks_match_the_previous_rgba_filter_pixels(self):
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not executable:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG for actual pixel comparisons.")

        def render(graph):
            return subprocess.run([
                executable, "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", "color=blue:size=64x48:rate=10",
                "-f", "lavfi", "-i", "anullsrc",
                "-f", "lavfi", "-i", "testsrc2=size=32x32:rate=10,format=rgba,colorchannelmixer=aa=0.6",
                "-filter_complex", graph, "-map", "[vout]", "-frames:v", "8",
                "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
            ], capture_output=True, check=True, timeout=15, **hidden_process_kwargs()).stdout

        for (rounded, faded), opacity in product(((True, False), (False, True), (True, True)), (1.0, 0.65)):
            with self.subTest(rounded=rounded, faded=faded, opacity=opacity):
                clip = replace(self.clip, border_radius=6 if rounded else 0, opacity=opacity,
                               animation_in="fade" if faded else "none", animation_windows=((0, 0.8, 0.8, 0),))
                # Frozen reference: evaluate all RGBA planes as the original exporter did.
                filters = ["[2:v]trim=duration=0.8,settb=AVTB,setpts=PTS-STARTPTS,scale=32:32",
                           "format=rgba"]
                if rounded:
                    filters.append("geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                                   "a='if(lte(pow(max(max(6-X,X-25),0),2)+"
                                   "pow(max(max(6-Y,Y-25),0),2),36),alpha(X,Y),0)'")
                filters.append(f"colorchannelmixer=aa={opacity}")
                if faded:
                    filters.append("geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
                                   f"a='alpha(X,Y)*({video_fade_expression(clip)})'")
                reference = ("[0:v]fps=10:start_time=0,settb=AVTB,setpts=N/(10*TB)[base];"
                             + ",".join(filters) + "[clip];[base][clip]overlay=8:8:eof_action=pass:shortest=0,"
                             "scale=64:48:force_original_aspect_ratio=decrease,"
                             "pad=64:48:(ow-iw)/2:(oh-ih)/2:color=black[vout]")
                output = render(layered_filter_graph([], [], 10, 64, 48, [clip]))
                self.assertEqual(len(output), 8 * 64 * 48 * 3 // 2)
                self.assertEqual(output, render(reference))


if __name__ == "__main__":
    unittest.main()
