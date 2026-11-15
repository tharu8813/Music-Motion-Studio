from __future__ import annotations

import struct
import threading
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from app.automix.analysis.basic import (
    SAMPLE_RATE,
    BasicAnalysisProvider,
    audible_bounds,
    decay_start,
    normalize_tempo_octave,
)
from app.automix.analysis.provider import AnalysisCancelled
from app.models.playlist import PlaylistTrack


def _tone_signal(
    frequency: float, duration: float, sample_rate: int = SAMPLE_RATE, amplitude: float = 0.5,
) -> np.ndarray:
    """A pure sine tone, for key/energy/vocal-activity unit tests."""
    t = np.linspace(0.0, duration, int(duration * sample_rate), endpoint=False)
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def _click_signal(bpm: float, duration: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """A synthetic metronome click track at ``bpm``, for analyzer unit tests."""
    interval = 60.0 / bpm
    signal = np.zeros(int(duration * sample_rate), dtype=np.float32)
    click_samples = int(0.01 * sample_rate)
    time = 0.0
    while time < duration:
        start = int(time * sample_rate)
        end = min(len(signal), start + click_samples)
        signal[start:end] = 1.0
        time += interval
    return signal


def _write_click_wav(path: Path, bpm: float, duration: float, sample_rate: int = SAMPLE_RATE) -> None:
    signal = _click_signal(bpm, duration, sample_rate)
    pcm16 = (signal * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(pcm16)}h", *pcm16.tolist()))


def _track(path: Path, duration_seconds: float) -> PlaylistTrack:
    return PlaylistTrack(file_path=str(path), title=path.stem, duration_seconds=duration_seconds)


class NormalizeTempoOctaveTests(unittest.TestCase):
    def test_low_tempo_is_doubled_into_range(self) -> None:
        self.assertAlmostEqual(normalize_tempo_octave(37.5), 75.0)
        self.assertAlmostEqual(normalize_tempo_octave(43.5), 87.0)

    def test_high_tempo_is_halved_into_range(self) -> None:
        self.assertAlmostEqual(normalize_tempo_octave(300.0), 150.0)

    def test_values_already_in_range_are_unchanged_even_if_a_half_double_sibling_also_fits(self) -> None:
        # Both 75 and 150 lie inside DEFAULT_TEMPO_RANGE; normalize_tempo_octave
        # only folds a value that is actually outside the range, so it never
        # relabels one in-range tempo as its in-range sibling.
        self.assertAlmostEqual(normalize_tempo_octave(75.0), 75.0)
        self.assertAlmostEqual(normalize_tempo_octave(150.0), 150.0)

    def test_in_range_tempo_is_unchanged(self) -> None:
        self.assertAlmostEqual(normalize_tempo_octave(128.0), 128.0)

    def test_zero_or_negative_bpm_passes_through(self) -> None:
        self.assertEqual(normalize_tempo_octave(0.0), 0.0)
        self.assertEqual(normalize_tempo_octave(-5.0), -5.0)


class BasicAnalysisProviderTests(unittest.TestCase):
    """Exercises the rhythm-analysis logic directly, bypassing real FFmpeg decode."""

    def _analyze_signal(self, provider: BasicAnalysisProvider, signal: np.ndarray, duration: float):
        track = _track(Path("stub.wav"), duration)
        with patch.object(provider, "_decode_mono_pcm", return_value=signal):
            return provider.analyze(track, cancel_event=threading.Event())

    def test_bpm_detected_for_synthetic_clicks(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        for bpm in (120.0, 128.0, 150.0):
            with self.subTest(bpm=bpm):
                signal = _click_signal(bpm, duration=8.0)
                result = self._analyze_signal(provider, signal, duration=8.0)
                self.assertIsNotNone(result.bpm)
                # Beat trackers can land on a tempo octave; a click track's
                # detected BPM should be within a documented tolerance of the
                # true value OR exactly half/double of it.
                candidates = (bpm, bpm / 2.0, bpm * 2.0)
                self.assertTrue(
                    any(abs(result.bpm - candidate) <= candidate * 0.06 for candidate in candidates),
                    f"bpm={result.bpm} not close to any of {candidates}",
                )
                self.assertGreater(result.bpm_confidence, 0.5)
                self.assertTrue(result.beats)

    def test_rhythm_added_later_is_the_rhythm_computed_up_front(self) -> None:
        """Beat This skips librosa's beats and adds them only on fallback: same result either way."""
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        signal = _click_signal(128.0, duration=8.0)
        track = _track(Path("stub.wav"), 8.0)
        full = provider.analyze_signal(track, signal, cancel_event=threading.Event())
        light = provider.analyze_signal(track, signal, cancel_event=threading.Event(), rhythm=False)
        self.assertIsNone(light.bpm)
        self.assertEqual(light.beats, ())
        self.assertEqual(provider.with_rhythm(light, signal), full)

    def test_silence_returns_no_bpm_not_false_confidence(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        signal = np.zeros(int(8.0 * SAMPLE_RATE), dtype=np.float32)
        result = self._analyze_signal(provider, signal, duration=8.0)
        self.assertIsNone(result.bpm)
        self.assertEqual(result.bpm_confidence, 0.0)
        self.assertEqual(result.beats, ())

    def test_very_short_signal_returns_no_bpm(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        signal = _click_signal(120.0, duration=0.5)
        result = self._analyze_signal(provider, signal, duration=0.5)
        self.assertIsNone(result.bpm)
        self.assertEqual(result.beats, ())

    def test_empty_signal_is_handled_without_crashing(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        result = self._analyze_signal(provider, np.array([], dtype=np.float32), duration=3.0)
        self.assertIsNone(result.bpm)

    def test_downbeats_are_provisional_and_bpm_only_quality(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        result = self._analyze_signal(provider, _click_signal(128.0, duration=8.0), duration=8.0)
        self.assertTrue(result.downbeats)
        self.assertEqual(result.meter_numerator, 4)
        self.assertLess(result.meter_confidence, 0.5)
        # A provisional downbeat guess must never look "reliable" on its own.
        self.assertEqual(result.beat_alignment_quality(), "bpm_only")

    def test_cancellation_before_decode_raises_without_decoding(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        track = _track(Path("stub.wav"), 8.0)
        cancel_event = threading.Event()
        cancel_event.set()
        with patch.object(provider, "_decode_mono_pcm") as decode:
            with self.assertRaises(AnalysisCancelled):
                provider.analyze(track, cancel_event=cancel_event)
            decode.assert_not_called()

    def test_missing_ffmpeg_executable_raises_a_controlled_error(self) -> None:
        with TemporaryDirectory(prefix="automix-basic-") as directory:
            source = Path(directory) / "a.wav"
            source.write_bytes(b"not really audio")
            provider = BasicAnalysisProvider(Path(directory) / "does-not-exist.exe")
            with self.assertRaises(RuntimeError):
                provider.analyze(_track(source, 5.0), cancel_event=threading.Event())

    def test_missing_source_file_raises_a_controlled_error(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        track = _track(Path("does-not-exist.mp3"), 5.0)
        with self.assertRaises(RuntimeError):
            provider.analyze(track, cancel_event=threading.Event())

    def test_key_and_energy_are_populated_for_a_tonal_signal(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        signal = _tone_signal(440.0, 10.0)
        result = self._analyze_signal(provider, signal, duration=10.0)
        self.assertIsNotNone(result.key)
        self.assertGreater(result.key_confidence, 0.0)
        self.assertGreater(result.energy, 0.0)

    def test_louder_signal_has_higher_energy_than_quieter_one(self) -> None:
        quiet = BasicAnalysisProvider._estimate_energy(_tone_signal(440.0, 5.0, amplitude=0.05))
        loud = BasicAnalysisProvider._estimate_energy(_tone_signal(440.0, 5.0, amplitude=0.5))
        self.assertLess(quiet, loud)

    def test_silence_has_zero_energy_and_no_key(self) -> None:
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        result = self._analyze_signal(provider, np.zeros(int(8.0 * SAMPLE_RATE), dtype=np.float32), duration=8.0)
        self.assertIsNone(result.key)
        self.assertEqual(result.key_confidence, 0.0)
        self.assertIsNone(result.energy)
        self.assertEqual(result.vocal_activity, ())

    def test_no_vocal_activity_is_guessed_from_the_voice_band(self) -> None:
        # A 1 kHz tone sits squarely in the old 300-3400 Hz "voice band" heuristic,
        # which real vocal stems showed to be inverted; vocals stay unknown.
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        result = self._analyze_signal(provider, _tone_signal(1000.0, 8.0), duration=8.0)
        self.assertEqual(result.vocal_activity, ())

class AudibleBoundsTests(unittest.TestCase):
    def test_trailing_and_leading_silence_are_excluded_but_a_decay_is_not(self) -> None:
        rate = SAMPLE_RATE
        body = 0.5 * np.sin(2 * np.pi * 220 * np.arange(10 * rate) / rate)
        decay = body[:2 * rate] * 10 ** (-30 / 20)  # a -30 dB reverb tail is still sound
        signal = np.concatenate([np.zeros(rate), body, decay, np.zeros(3 * rate)]).astype(np.float32)
        start, end = audible_bounds(signal, len(signal) / rate)
        self.assertAlmostEqual(start, 1.0, delta=0.06)
        self.assertAlmostEqual(end, 13.0, delta=0.06)

    def test_silence_has_no_bounds(self) -> None:
        self.assertEqual(audible_bounds(np.zeros(SAMPLE_RATE, dtype=np.float32), 1.0), (None, None))


class DecayStartTests(unittest.TestCase):
    def test_decay_starts_where_a_linear_fade_crosses_fifteen_db_down(self) -> None:
        rate = SAMPLE_RATE
        body = 0.5 * np.sin(2 * np.pi * 220 * np.arange(20 * rate) / rate)
        fade = body[:10 * rate] * np.linspace(1.0, 0.0, 10 * rate)  # -15 dB at 1 - 10**(-15/20) of the way
        signal = np.concatenate([body, fade, np.zeros(2 * rate)]).astype(np.float32)
        expected = 20.0 + 10.0 * (1 - 10 ** (-15 / 20))
        self.assertAlmostEqual(decay_start(signal, len(signal) / rate), expected, delta=0.3)

    def test_a_track_that_stops_at_full_level_decays_at_its_end(self) -> None:
        signal = (0.5 * np.sin(np.arange(5 * SAMPLE_RATE) * 0.1)).astype(np.float32)
        self.assertAlmostEqual(decay_start(signal, 5.0), 5.0, delta=0.01)

    def test_silence_has_no_decay_start(self) -> None:
        self.assertIsNone(decay_start(np.zeros(SAMPLE_RATE, dtype=np.float32), 1.0))


class RealAudioBasicAnalysisProviderTests(unittest.TestCase):
    def test_real_decode_and_analysis_of_a_click_track(self) -> None:
        with TemporaryDirectory(prefix="automix-basic-real-") as directory:
            source = Path(directory) / "click.wav"
            _write_click_wav(source, bpm=128.0, duration=10.0)
            provider = BasicAnalysisProvider(None)
            result = provider.analyze(_track(source, 10.0), cancel_event=threading.Event())
            self.assertIsNotNone(result.bpm)
            candidates = (128.0, 64.0, 256.0)
            self.assertTrue(any(abs(result.bpm - candidate) <= candidate * 0.06 for candidate in candidates))


if __name__ == "__main__":
    unittest.main()
