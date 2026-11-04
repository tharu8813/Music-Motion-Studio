"""The ONNX analyzer: Beat This! chunking/peak picking and Open-Unmix vocal regions, plus the shipped models.

The model tests run whenever onnxruntime and the committed model files are
present (no network, no PyTorch). Parity with the PyTorch models was measured
separately on real music (see app/automix/analysis/beat_this_onnx.py and
vocals.py); these check the wiring on synthetic signals.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.automix.analysis.basic import SAMPLE_RATE
from app.automix.analysis.beat_this_onnx import (
    FPS,
    BeatThisOnnxAnalysisProvider,
    beats_from_logits,
    chunk_starts,
    model_path,
    onnx_beats_available,
    peak_times,
)
from app.automix.analysis.vocals import ONNX_MODEL_NAME, OnnxVocalDetector
from app.models.playlist import PlaylistTrack


def accented_clicks(bpm: float, seconds: float) -> np.ndarray:
    """Beat 1 of each 4/4 bar a loud low thump, the others quiet high clicks, over a quiet bed."""
    signal = 0.04 * np.sin(2 * np.pi * 220.0 * np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE)
    click = np.arange(int(0.09 * SAMPLE_RATE))
    envelope = np.exp(-30.0 * click / SAMPLE_RATE)
    for index, time in enumerate(np.arange(0.0, seconds - 0.1, 60.0 / bpm)):
        downbeat = index % 4 == 0
        start = int(time * SAMPLE_RATE)
        tone = np.sin(2 * np.pi * (130.0 if downbeat else 1400.0) * click / SAMPLE_RATE)
        signal[start:start + len(click)] += (0.9 if downbeat else 0.3) * envelope * tone
    return np.clip(signal, -1.0, 1.0).astype(np.float32)


class ChunkingAndPeakTests(unittest.TestCase):
    def test_chunks_overlap_by_two_borders_and_the_last_ends_the_piece(self) -> None:
        self.assertEqual(chunk_starts(1000), [-6])
        self.assertEqual(chunk_starts(3000), [-6, 1482, 1506])

    def test_adjacent_peak_frames_merge_and_downbeats_snap_to_beats(self) -> None:
        logits = np.full(200, -5.0)
        logits[[50, 51, 120]] = 3.0
        self.assertTrue(np.allclose(peak_times(logits), [50.5 / FPS, 120 / FPS]))
        _, downbeats = beats_from_logits(logits, np.where(np.arange(200) == 119, 2.0, -5.0))
        self.assertTrue(np.allclose(downbeats, [120 / FPS]))

    def test_no_peak_above_half_probability_means_no_beats(self) -> None:
        beats, downbeats = beats_from_logits(np.full(100, -1.0), np.full(100, -1.0))
        self.assertEqual((len(beats), len(downbeats)), (0, 0))

    def test_downbeat_snapping_matches_distance_matrix_including_ties_and_edges(self) -> None:
        beats_logits = np.full(120, -5.0)
        downbeats_logits = beats_logits.copy()
        beats_logits[[25, 75]] = 4.0
        downbeats_logits[[0, 30, 50, 90, 110]] = 2.0
        beats, downbeats = beats_from_logits(beats_logits, downbeats_logits)
        unsnapped = peak_times(downbeats_logits)
        expected = np.unique(beats[np.abs(beats[None, :] - unsnapped[:, None]).argmin(axis=1)])
        np.testing.assert_array_equal(downbeats, expected)


@unittest.skipUnless(onnx_beats_available(), "onnxruntime or the Beat This ONNX model is not installed")
class ShippedModelTests(unittest.TestCase):
    def analyze(self, bpm: float, seconds: float):
        with patch("app.automix.analysis.beat_this.vocal_detection_available", return_value=False):
            provider = BeatThisOnnxAnalysisProvider(Path("ffmpeg"))
        provider._vocals = None  # rhythm only: VocalDetectorTests cover the vocals
        track = PlaylistTrack("clicks.wav", "Clicks", duration_seconds=seconds)
        with patch.object(provider._basic, "_decode_mono_pcm", return_value=accented_clicks(bpm, seconds)):
            return provider.analyze(track, cancel_event=threading.Event())

    def test_accented_track_longer_than_one_chunk_is_a_reliable_four_four(self) -> None:
        result = self.analyze(128.0, 70.0)  # 3500 frames: three overlapping chunks
        self.assertEqual(result.analyzer_id, "beat_this_onnx")
        self.assertAlmostEqual(result.bpm, 128.0, delta=1.0)
        self.assertEqual(result.meter_numerator, 4)
        self.assertEqual(result.beat_alignment_quality(), "reliable")
        # Downbeats on the loud thumps, not on a quiet click.
        bar = 4 * 60.0 / 128.0
        phase = np.remainder(np.asarray(result.downbeats) + bar / 8, bar) - bar / 8
        self.assertLess(float(np.max(np.abs(phase))), 0.05)

    def test_short_track_fits_one_padded_chunk(self) -> None:
        result = self.analyze(100.0, 20.0)
        self.assertAlmostEqual(result.bpm, 100.0, delta=1.0)

    def test_cache_identity_names_the_models_and_runtime(self) -> None:
        version = BeatThisOnnxAnalysisProvider(Path("ffmpeg")).version
        self.assertIn("beat_this_final0_int8", version)
        self.assertIn("+ort", version)
        self.assertTrue(version.endswith("+vox-umxhq"), version)


class VocalDetectorTests(unittest.TestCase):
    """OnnxVocalDetector: which regions it separates, and the stem-vs-mix level decision."""

    def detector(self, stem_gain: float | None):
        """``stem_gain``: the fake network's output as a fraction of the mix magnitude; None: the real model."""
        rng = np.random.default_rng(0)
        calls = []

        def decode(path, cancel_event, *, sample_rate, channels, start, duration):
            calls.append((start, duration))
            return (0.2 * rng.standard_normal(int(duration * sample_rate) * channels)).astype(np.float32)

        detector = OnnxVocalDetector(decode, model_path(ONNX_MODEL_NAME))
        if stem_gain is not None:
            class Session:
                def run(self, _outputs, feeds):
                    return [feeds["magnitude"] * stem_gain]
            detector._session = Session()
        return detector, calls

    def test_separates_only_the_head_and_tail_regions(self) -> None:
        detector, calls = self.detector(1.0)
        spans = detector.detect(Path("song.wav"), 200.0, threading.Event())
        self.assertEqual(calls, [(0.0, 45.0), (155.0, 45.0)])
        # The whole stem is the mix: sung throughout both regions, silent between.
        self.assertEqual(len(spans), 2)
        self.assertLess(spans[0][0], 0.2)
        self.assertGreater(spans[0][1], 44.5)
        self.assertGreaterEqual(spans[1][0], 155.0)

    def test_a_stem_far_below_the_mix_is_not_singing(self) -> None:
        detector, _ = self.detector(0.05)  # -26 dB, below VOCAL_RELATIVE_DB
        self.assertEqual(detector.detect(Path("song.wav"), 60.0, threading.Event()), ())

    @unittest.skipUnless(onnx_beats_available() and model_path(ONNX_MODEL_NAME).is_file(),
                         "onnxruntime or the Open-Unmix ONNX model is not installed")
    def test_shipped_model_runs_on_a_short_track(self) -> None:
        detector, calls = self.detector(None)
        spans = detector.detect(Path("song.wav"), 12.0, threading.Event())
        self.assertEqual(calls, [(0.0, 12.0)])
        self.assertTrue(all(0.0 <= start < end <= 12.0 for start, end in spans))


if __name__ == "__main__":
    unittest.main()
