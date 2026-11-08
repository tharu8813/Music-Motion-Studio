"""Use a lower raw pipe FPS only when every Canvas image holds uniformly."""
from __future__ import annotations

from dataclasses import replace
from math import floor
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtGui import QColor, QImage

from app.models.playlist import PlaylistTrack
from app.renderer.canvas_pipe import PipedCanvasStream, _QueuedCanvasFrame
from app.renderer.cfr_sample_pruning import (
    iter_cfr_emitting_samples, uniform_cfr_frame_step,
    low_fps_overlay_pipe_enabled,
)
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg.filter_graph import layered_filter_graph
from app.utils.subprocess_utils import hidden_process_kwargs


def samples(durations: list[float]) -> list[ExportFrameSample]:
    track = PlaylistTrack("uniform.wav", "Uniform", duration_seconds=sum(durations))
    elapsed = 0.0
    output: list[ExportFrameSample] = []
    for duration in durations:
        output.append(ExportFrameSample(
            track, 1, 0.0, duration, elapsed, elapsed,
        ))
        elapsed += duration
    return output


class LowFpsCanvasOverlayTests(unittest.TestCase):
    def test_low_fps_transport_is_opt_in_after_real_world_stall(self):
        self.assertFalse(low_fps_overlay_pipe_enabled(True, {}))
        self.assertFalse(low_fps_overlay_pipe_enabled(False, {
            "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE": "1",
        }))
        self.assertTrue(low_fps_overlay_pipe_enabled(True, {
            "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE": "1",
        }))
        self.assertTrue(low_fps_overlay_pipe_enabled(True, {
            "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE": "yes",
        }))
        self.assertFalse(low_fps_overlay_pipe_enabled(True, {
            "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE": "1",
            "PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE": "1",
        }))
        self.assertFalse(low_fps_overlay_pipe_enabled(True, {
            "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE": "0",
        }))

    def test_whole_timeline_must_have_one_identical_integer_frame_step(self):
        normal = samples([1 / 12] * 2679)
        self.assertEqual(uniform_cfr_frame_step(iter(normal), 60), 5)
        self.assertEqual(uniform_cfr_frame_step(iter(samples([1 / 30] * 60)), 60), 2)
        self.assertEqual(uniform_cfr_frame_step(iter(samples([1 / 60] * 60)), 60), 1)
        self.assertEqual(uniform_cfr_frame_step([], 60), 1)
        self.assertEqual(
            uniform_cfr_frame_step(iter(samples([1 / 12] * 30)), 60, min_pipe_fps=30),
            1,
        )

    def test_mixed_animation_boundary_or_nondivisor_falls_back(self):
        for durations in (
            [1 / 12] * 25 + [1 / 60] + [1 / 12] * 25,
            [1 / 12] * 30 + [1 / 24] + [1 / 12] * 30,
            [1 / 11] * 50,
            [1 / 120] * 120,
        ):
            with self.subTest(duration_count=len(durations)):
                self.assertEqual(uniform_cfr_frame_step(samples(durations), 60), 1)

    def test_stamped_output_targets_map_to_exact_pipe_frame_targets(self):
        sequence = list(iter_cfr_emitting_samples(samples([1 / 12] * 150), 60))
        self.assertTrue(all(s.cfr_target_frame_count % 5 == 0 for s in sequence))
        self.assertEqual(len(sequence), 150)
        self.assertEqual(sequence[-1].cfr_target_frame_count, 750)
        self.assertEqual(
            [s.cfr_target_frame_count // 5 for s in sequence],
            list(range(1, 151)),
        )

    def test_pipe_receives_five_times_fewer_exact_rgba_bytes(self):
        sequence = list(iter_cfr_emitting_samples(samples([1 / 12] * 20), 60))
        def encode(fps: int, frame_step: int) -> tuple[bytes, int]:
            pixels = bytearray()
            with patch("app.renderer.canvas_pipe._create_endpoint") as factory:
                endpoint = factory.return_value
                endpoint.write.side_effect = lambda b, _cancel: pixels.extend(bytes(b))
                pipe = PipedCanvasStream(Path("unused"), "layer:0", fps, preserve_alpha=True)
                for index, sample in enumerate(sequence):
                    image = QImage(2, 2, QImage.Format.Format_RGBA8888)
                    image.fill(QColor(index * 7, 128, 255 - index * 7, 170))
                    pipe._write_frame(_QueuedCanvasFrame(
                        image, sample.duration_seconds,
                        sample.cfr_target_frame_count // frame_step,
                    ))
                return bytes(pixels), pipe.frame_count
        full, full_frames = encode(60, 1)
        reduced, reduced_frames = encode(12, 5)
        self.assertEqual(full_frames, 100)
        self.assertEqual(reduced_frames, 20)
        self.assertEqual(len(full), len(reduced) * 5)
        self.assertEqual(
            full,
            b"".join(
                reduced[i * 16:(i + 1) * 16] * 5
                for i in range(reduced_frames)
            ),
        )

    def test_ffmpeg_static_graph_expands_only_reduced_raw_layer(self):
        layer = [(1.0, Path("canvas-pipe"), "pipe", 4, 4)]
        normal = layered_filter_graph(
            [], layer, 60, 24, 16,
            base_already_sized=True, base_is_cfr=True,
            optimize_constant_overlays=True,
        )
        reduced = layered_filter_graph(
            [], layer, 60, 24, 16,
            base_already_sized=True, base_is_cfr=True,
            optimize_constant_overlays=True, pipe_input_fps={0: 12},
        )
        self.assertNotIn("staticcfr", normal)
        self.assertIn("[2:v]fps=60:start_time=0[staticcfr0]", reduced)
        self.assertIn("[base][staticcfr0]overlay=4:4", reduced)
        with self.assertRaises(ValueError):
            layered_filter_graph(
                [], layer, 60, 24, 16, pipe_input_fps={0: 13},
            )

    def test_real_ffmpeg_lossless_alpha_composite_matches_full_rate_pixels(self):
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not executable:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG for actual FFmpeg parity test.")
        width, height, fps, step = 24, 16, 60, 5
        with TemporaryDirectory(prefix="lowfps-canvas-overlay-") as dirname:
            directory = Path(dirname)
            for frame_count in (10, 25, 60, 125):
                with self.subTest(frame_count=frame_count):
                    colors = [
                        bytes((i * 17 % 255, i * 29 % 255, 255 - i * 13 % 255, 200))
                        * (8 * 8)
                        for i in range(frame_count // step)
                    ]
                    baseline_file = directory / "baseline.rgba"
                    optimized_file = directory / "optimized.rgba"
                    baseline_file.write_bytes(b"".join(block * step for block in colors))
                    optimized_file.write_bytes(b"".join(colors))

                    def composite(path: Path, rate: int, reduced: bool) -> bytes:
                        graph = layered_filter_graph(
                            [], [(1, Path("canvas-pipe"), "pipe", 4, 4)],
                            fps, width, height,
                            base_already_sized=True, base_is_cfr=True,
                            optimize_constant_overlays=True,
                            pipe_input_fps={0: rate} if reduced else None,
                        )
                        command = [
                            executable, "-nostdin", "-hide_banner",
                            "-loglevel", "error", "-filter_complex_threads", "1",
                            "-f", "lavfi", "-i",
                            f"color=blue:size={width}x{height}:rate=60:"
                            f"duration={frame_count / fps:.12f}",
                            "-f", "lavfi", "-i", "anullsrc",
                            "-f", "rawvideo", "-pix_fmt", "rgba",
                            "-video_size", "8x8", "-framerate", str(rate),
                            "-i", str(path), "-filter_complex", graph,
                            "-map", "[vout]", "-frames:v", str(frame_count),
                            "-fps_mode", "cfr", "-r", str(fps),
                            "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
                        ]
                        result = subprocess.run(
                            command, capture_output=True, timeout=30,
                            **hidden_process_kwargs(),
                        )
                        self.assertEqual(result.returncode, 0, result.stderr.decode(
                            "utf-8", errors="replace",
                        )[-1400:])
                        return result.stdout
                    expected = composite(baseline_file, fps, False)
                    actual = composite(optimized_file, fps // step, True)
                    self.assertEqual(len(expected), frame_count * width * height * 3 // 2)
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
