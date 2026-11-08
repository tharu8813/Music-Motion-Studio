"""Pruning states with no CFR output must preserve every encoded pixel.

These tests intentionally use the pipe's own integer rounding contract,
including the floating-point half-frame boundary where simply merging
duration values would produce a different frame (e.g. 6.95s @ 10fps).
"""
from __future__ import annotations

from dataclasses import replace
from math import floor
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage

from app.models.playlist import PlaylistTrack
from app.renderer.cfr_sample_pruning import iter_cfr_emitting_samples
from app.renderer.canvas_pipe import PipedCanvasStream, _QueuedCanvasFrame
from app.renderer.export_timeline import ExportFrameSample
from app.preview.canvas_process_stream import _pack_sample, _unpack_sample


def _samples(durations: list[float]) -> list[ExportFrameSample]:
    track = PlaylistTrack("sample.wav", "Test", duration_seconds=sum(durations))
    elapsed = 0.0
    output: list[ExportFrameSample] = []
    for index, duration in enumerate(durations):
        output.append(ExportFrameSample(
            track=track, track_number=1,
            track_start_seconds=0.0, duration_seconds=duration,
            elapsed_seconds=elapsed, timeline_seconds=elapsed,
        ))
        elapsed += duration
    return output


def _expand(samples: list[ExportFrameSample], fps: int) -> list[float]:
    """Model the exact production writer, including explicit frame targets."""
    elapsed = 0.0
    emitted = 0
    frames: list[float] = []
    for sample in samples:
        elapsed += sample.duration_seconds
        target = (
            sample.cfr_target_frame_count
            if sample.cfr_target_frame_count is not None
            else max(1, floor(elapsed * fps + 0.5))
        )
        if target < emitted:
            raise AssertionError("Frame target moved backwards")
        frames.extend([sample.elapsed_seconds] * (target - emitted))
        emitted = target
    return frames


class CfrSamplePruningTests(unittest.TestCase):
    def test_random_subframe_and_half_frame_boundaries_preserve_every_frame(self) -> None:
        random = Random(73)
        choices = (
            1 / 240, 1 / 120, 1 / 100, 1 / 90,
            1 / 60, 1 / 30, 1 / 15, .001, .003, .027, .011, .05,
        )
        for fps in (10, 24, 29, 30, 60, 120):
            for _ in range(75):
                count = random.randrange(1, 1000)
                samples = _samples([random.choice(choices) for _ in range(count)])
                pruned = list(iter_cfr_emitting_samples(iter(samples), fps))
                self.assertEqual(_expand(samples, fps), _expand(pruned, fps))
                self.assertAlmostEqual(
                    sum(x.duration_seconds for x in samples),
                    sum(x.duration_seconds for x in pruned),
                    places=10,
                )
                self.assertLessEqual(len(pruned), len(samples))
                self.assertTrue(all(x.cfr_target_frame_count is not None for x in pruned))

    def test_pruning_reduces_queue_packets_without_dropping_output_frames(self) -> None:
        samples = _samples([1 / 180] * 1800)
        pruned = list(iter_cfr_emitting_samples(samples, 60))
        self.assertLess(len(pruned), len(samples) * 0.4)
        self.assertEqual(len(_expand(samples, 60)), 600)
        self.assertEqual(_expand(samples, 60), _expand(pruned, 60))
        self.assertEqual(pruned[0].cfr_target_frame_count, 1)
        self.assertEqual(pruned[-1].cfr_target_frame_count, 600)

    def test_empty_schedule_and_invalid_fps(self) -> None:
        self.assertEqual(list(iter_cfr_emitting_samples([], 60)), [])
        with self.assertRaises(ValueError):
            list(iter_cfr_emitting_samples(_samples([0.01]), 0))
        with self.assertRaises(ValueError):
            list(iter_cfr_emitting_samples(_samples([0.01, 0.0]), 60))

    def test_worker_packets_preserve_target_frame_count(self) -> None:
        track = PlaylistTrack("sample.wav", "Test", duration_seconds=1.0)
        sample = replace(
            ExportFrameSample(track, 1, 0.0, 0.02, 0.0, 0.0),
            cfr_target_frame_count=2,
        )
        packet = _pack_sample(sample, {id(track): 0})
        restored = _unpack_sample(packet, [track])
        self.assertEqual(restored, sample)
        self.assertEqual(restored.cfr_target_frame_count, 2)

    def test_actual_pipe_raw_frame_bytes_are_identical(self) -> None:
        # Use real QImages and the real pipe writer, mocking only the OS reader.
        # The exact 10-FPS half-frame boundary is part of this regression.
        rng = Random(73)
        durations = [
            rng.choice([1 / 120, 1 / 90, 1 / 60, .001, .003, .027, .05])
            for _ in range(830)
        ]
        samples = _samples(durations)
        pruned = list(iter_cfr_emitting_samples(samples, 10))

        def pipe_bytes(sequence: list[ExportFrameSample]) -> tuple[bytes, int]:
            emitted = bytearray()
            with patch("app.renderer.canvas_pipe._create_endpoint") as endpoint_factory:
                endpoint = endpoint_factory.return_value
                endpoint.write.side_effect = (
                    lambda data, _cancel: emitted.extend(bytes(data))
                )
                stream = PipedCanvasStream(
                    Path("unused"), "layer:0", 10, preserve_alpha=True,
                )
                for index, sample in enumerate(sequence):
                    # The image is identified by the original sample timestamp.
                    # QImage is a 2×2 RGBA image, enough to distinguish states.
                    image = QImage(2, 2, QImage.Format.Format_RGBA8888)
                    value = round(sample.elapsed_seconds * 1000) % 256
                    image.fill(QColor(value, value // 2, 255 - value, 255))
                    stream._write_frame(_QueuedCanvasFrame(
                        image, sample.duration_seconds, sample.cfr_target_frame_count,
                    ))
                return bytes(emitted), stream.frame_count

        original, old_count = pipe_bytes(samples)
        optimized, new_count = pipe_bytes(pruned)
        self.assertEqual(original, optimized)
        self.assertEqual(old_count, new_count)
        self.assertEqual(old_count * 2 * 2 * 4, len(original))


if __name__ == "__main__":
    unittest.main()
