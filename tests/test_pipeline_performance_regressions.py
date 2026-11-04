"""Numerical, transport and cache-budget contracts for pipeline optimizations."""
from dataclasses import fields
import math
import os
from pathlib import Path
import pickle
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import numpy as np
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication
from app.models.playlist import PlaylistTrack
from app.preview.frame_state import MixJunction
from app.preview import canvas_snapshot as snapshot
from app.preview.canvas_process_stream import _pack_sample, _unpack_sample
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.python_visualizer import PythonVisualizerError, PythonVisualizerRenderer


class PipelinePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_compact_samples_preserve_every_field_and_worker_track_identity(self):
        tracks = [PlaylistTrack("same.wav", f"Track {i}", id=str(i),
                                lyrics=[dict(start=0, end=2, text=f"Lyric {i}")]) for i in range(2)]
        indexes = {id(track): index for index, track in enumerate(tracks)}
        for index in (0, 1, 0):
            sample = ExportFrameSample(tracks[index], index + 1, 7.5, .013, .25, 7.75,
                                       "out", .4, .8, MixJunction(.5, .2, 3, 2), False)
            packet = pickle.loads(pickle.dumps(_pack_sample(sample, indexes)))
            restored = _unpack_sample(packet, tracks)
            self.assertIs(restored.track, tracks[index])
            for field in fields(sample):
                self.assertEqual(getattr(sample, field.name), getattr(restored, field.name))
        self.assertIsNone(_pack_sample(None, indexes))
        self.assertIsNone(_unpack_sample(None, tracks))
        tracks[0].lyrics *= 2000
        self.assertLess(len(pickle.dumps(_pack_sample(sample, indexes))), 512)

    def test_compact_sample_rejects_unregistered_track_instead_of_wrong_metadata(self):
        track = PlaylistTrack("x.wav", "Track")
        sample = ExportFrameSample(track, 1, 0, .1, 0, 0)
        with self.assertRaisesRegex(ValueError, "track"):
            _pack_sample(sample, {})

    def test_fft_batch_matches_frozen_per_frame_reducer_exactly(self):
        renderer = PythonVisualizerRenderer(Path("unused"))
        rng = np.random.default_rng(748)
        for sample_count, fps, bands in ((0, 30, 24), (1, 60, 4), (8197, 30, 48),
                                         (35000, 60, 96), (12345, 24, 32)):
            with self.subTest(samples=sample_count, fps=fps, bands=bands):
                samples = rng.uniform(-.8, .8, sample_count).astype(np.float32)
                count = max(1, math.ceil(len(samples) * fps / renderer.sample_rate))
                window = np.hanning(renderer.fft_size).astype(np.float32)
                frequencies = np.fft.rfftfreq(renderer.fft_size, 1 / renderer.sample_rate)
                bins, probes = renderer._frequency_band_layout(
                    frequencies, np.geomspace(35, renderer.sample_rate / 2, bands + 1))
                expected = np.zeros((count, bands), dtype=np.float32)
                offsets = np.arange(renderer.fft_size) - renderer.fft_size // 2
                for index in range(count):
                    positions = index * renderer.sample_rate // fps + offsets
                    valid = (positions >= 0) & (positions < len(samples))
                    segment = np.zeros(renderer.fft_size, dtype=np.float32)
                    segment[valid] = samples[positions[valid]]
                    spectrum = np.abs(np.fft.rfft(segment * window)) / (renderer.fft_size / 2)
                    expected[index] = np.clip(renderer._frequency_band_values(
                        spectrum, frequencies, bins, probes), 0, 2)
                actual = renderer._analyze_levels(samples, fps, bands, threading.Event())
                np.testing.assert_array_equal(actual, expected)

    def test_fft_cancel_stops_before_batch(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(PythonVisualizerError):
            PythonVisualizerRenderer(Path("unused"))._analyze_levels(np.ones(1000), 60, 48, cancelled)

    def test_filtered_pixmaps_evict_lru_with_byte_and_entry_limits(self):
        cache = snapshot._FILTERED_TRACK_PIXMAP_CACHE
        cache.clear()
        self.addCleanup(cache.clear)
        item = SimpleNamespace(_apply_image_filters=Mock(side_effect=lambda pixmap, **_: pixmap))
        def image(value):
            pixmap = QPixmap(32, 32)
            pixmap.fill(value)
            return pixmap
        with patch.object(snapshot, "_FILTERED_TRACK_PIXMAP_BUDGET", 2 * 32 * 32 * 4):
            first = snapshot._filtered_track_pixmap(item, lambda: image(1), (1,))
            snapshot._filtered_track_pixmap(item, lambda: image(2), (2,))
            self.assertEqual(snapshot._filtered_track_pixmap(item, lambda: self.fail("cache miss"), (1,)), first)
            snapshot._filtered_track_pixmap(item, lambda: image(3), (3,))
            self.assertIn((1,), cache)
            self.assertNotIn((2,), cache)
            self.assertLessEqual(sum(p.width() * p.height() * 4 for p in cache.values()), 8192)
            large = QPixmap(64, 64)
            large.fill(10)
            returned = snapshot._filtered_track_pixmap(item, large, (4,))
            self.assertEqual(returned.toImage(), large.toImage())
            self.assertNotIn((4,), cache)
            self.assertEqual(len(cache), 2)
        cache.clear()
        for index in range(120):
            snapshot._filtered_track_pixmap(item, image(index), (index,))
        self.assertLessEqual(len(cache), 96)


if __name__ == "__main__":
    unittest.main()
