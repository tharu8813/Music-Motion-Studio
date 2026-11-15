"""The basic AutoMix analyzer: PCM decoding and librosa BPM/beat tracking.

librosa receives PCM from the shared local decoder, with FFmpeg as a
fallback for unsupported sources.

Downbeats are not a real detector output here: librosa.beat.beat_track
gives BPM and beat timestamps only. Bars are a provisional "every 4th
beat" 4/4 guess, deliberately capped at a low confidence (see
PROVISIONAL_METER_CONFIDENCE) so a planner never mistakes it for a real
downbeat model's output -- see TrackAnalysis.beat_alignment_quality().

Phase 7 additions (key/energy/vocal activity) reuse the same decoded
signal -- no second FFmpeg decode -- and stay model-free like the rest of
this analyzer:

- key: Krumhansl-Schmuckler profile correlation over the track's mean
  chroma vector (app/automix/analysis/key.py). Used only as a score
  modifier later, never a blocker or an automatic pitch-shift trigger.
- energy: RMS relative to a documented reference level, not a loudness
  (LUFS) measurement -- see ENERGY_REFERENCE_RMS.
- vocal_activity: deliberately left empty (unknown). Version 2 guessed it
  from the share of energy in the 300-3400 Hz "voice band"; checked against
  real vocal stems (AutoMix roadmap Phase 02/05) that share was *higher* in
  the songs' instrumental seconds than in their sung ones (median 0.53 vs
  0.25 and 0.68 vs 0.13), recall was 0.06-0.36, and 10-12 % of pure
  instrumentals were flagged -- no threshold fixes an inverted signal. An
  unknown value never triggers or penalizes anything; a real detector (e.g.
  a vocal stem) can fill the field later.
"""

from __future__ import annotations

from app.utils.performance import timed

import logging
import subprocess
import threading
from concurrent.futures import CancelledError
from dataclasses import replace
from pathlib import Path
from typing import Callable

import librosa
import numpy as np

from app.automix.analysis.key import estimate_key
from app.automix.analysis.provider import (
    STEP_BARS, STEP_DECODE, STEP_KEY_ENERGY, STEP_RHYTHM, AnalysisCancelled,
)
from app.automix.beatgrid import fit_beat_grid
from app.automix.models import TrackAnalysis
from app.automix.music import analyze_music
from app.automix.structure.timbre import timbre_curves
from app.models.playlist import PlaylistTrack
from app.services.audio_decode import AudioDecodeError, read_pcm
from app.utils.subprocess_utils import hidden_process_kwargs

LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 22050
"""Mono analysis rate: enough for tempo/onset detection, cheap to decode/hold in memory."""

DEFAULT_TEMPO_RANGE = (70.0, 180.0)
"""Conventional playlist-music tempo octave; see normalize_tempo_octave()."""

PLAUSIBLE_BPM_RANGE = (20.0, 300.0)
"""Outside this, a BPM estimate is treated as noise, not a real tempo."""

MINIMUM_ANALYZABLE_SECONDS = 2.0
SILENCE_PEAK_THRESHOLD = 1e-4
BEAT_DEDUPE_TOLERANCE_SECONDS = 0.05
PROVISIONAL_METER_CONFIDENCE = 0.3

ENERGY_REFERENCE_RMS = 0.3
"""RMS of a loud, modern pop/EDM master, used only as a normalization
reference -- not a loudness standard. energy = min(1.0, rms / this)."""

AUDIBLE_BLOCK_SECONDS = 0.05
AUDIBLE_FLOOR_DB = -40.0
"""Below this, relative to the track's 90th-percentile 50 ms block power, a
block counts as silence. Real masters measured -45..-67 dB in their trailing
silence and stayed above -33 dB through a natural decay (Phase 02 real-music
check), so this trims dead air but never a fade or reverb tail."""


def audible_bounds(signal: np.ndarray, duration_seconds: float) -> tuple[float | None, float | None]:
    """(first, last) audible second of a mono SAMPLE_RATE signal; (None, None) if unmeasurable."""
    block = int(AUDIBLE_BLOCK_SECONDS * SAMPLE_RATE)
    count = len(signal) // block
    if count == 0:
        return None, None
    power = np.mean(np.square(signal[:count * block].reshape(count, block), dtype=np.float64), axis=1)
    reference = float(np.percentile(power, 90))
    audible = np.flatnonzero(power >= reference * 10 ** (AUDIBLE_FLOOR_DB / 10)) if reference > 0.0 else ()
    if len(audible) == 0:
        return None, None
    end = min(duration_seconds, (int(audible[-1]) + 1) * AUDIBLE_BLOCK_SECONDS)
    return min(int(audible[0]) * AUDIBLE_BLOCK_SECONDS, end), end


DECAY_BLOCK_SECONDS = 0.25
DECAY_FLOOR_DB = -15.0
"""Below this, relative to the track's 75th-percentile 0.25 s block power,
the ending has faded or decayed. Transitions placed there faded the next
track in over near-silence: on 31 real songs they dipped -13 to -28 dB."""


def decay_start(signal: np.ndarray, duration_seconds: float) -> float | None:
    """Last second of a mono SAMPLE_RATE signal within DECAY_FLOOR_DB of its body; None if unmeasurable."""
    block = int(DECAY_BLOCK_SECONDS * SAMPLE_RATE)
    count = len(signal) // block
    if count == 0:
        return None
    power = np.mean(np.square(signal[:count * block].reshape(count, block), dtype=np.float64), axis=1)
    body = float(np.percentile(power, 75))
    loud = np.flatnonzero(power >= body * 10 ** (DECAY_FLOOR_DB / 10)) if body > 0.0 else ()
    return min(duration_seconds, (int(loud[-1]) + 1) * DECAY_BLOCK_SECONDS) if len(loud) else None


def normalize_tempo_octave(
    bpm: float, tempo_range: tuple[float, float] = DEFAULT_TEMPO_RANGE,
) -> float:
    """Fold a BPM estimate into ``tempo_range`` by doubling/halving it.

    Beat trackers routinely report the wrong tempo octave (75 vs. 150 BPM,
    87 vs. 174 BPM, ...). This does not decide which octave is "true" --
    it just picks the representative within the conventional playlist-music
    range so two tracks at the same perceived tempo compare equal later
    (Phase 3 compatibility scoring).
    """
    if bpm <= 0.0:
        return bpm
    low, high = tempo_range
    while bpm < low and bpm * 2.0 <= high:
        bpm *= 2.0
    while bpm > high and bpm / 2.0 >= low:
        bpm /= 2.0
    return bpm


def _report(
    progress: Callable[[float, str], None] | None, cancel_event: threading.Event, fraction: float, message: str,
) -> None:
    if progress is not None:
        progress(fraction, message)
    if cancel_event.is_set():
        raise AnalysisCancelled(f"AutoMix analysis cancelled: {message}")


class BasicAnalysisProvider:
    """The always-available default AnalysisProvider (see AnalysisProvider Protocol)."""

    provider_id = "basic"
    version = "8"
    """Bumped from "1": Phase 7 added key/energy/vocal_activity to the
    output, which invalidates any cache entry from before those fields
    existed (see app/automix/cache.py -- analyzer_version is part of the
    cache key). "3": audible start/end bounds. "4": no voice-band vocal guess.
    "5": BPM from a fitted beat grid. "6": decay start. "7": local YAMNet genre/mood tags.
    "8": measured one-second RMS/timbre for acoustic transition search."""

    def __init__(self, ffmpeg_executable: Path | None) -> None:
        self.ffmpeg_executable = Path(ffmpeg_executable) if ffmpeg_executable is not None else None

    @timed("automix.basic_seconds")
    def analyze(
        self,
        track: PlaylistTrack,
        *,
        cancel_event: threading.Event,
        progress: Callable[[float, str], None] | None = None,
    ) -> TrackAnalysis:
        _report(progress, cancel_event, 0.0, STEP_DECODE)
        signal = self._decode_mono_pcm(Path(track.file_path), cancel_event)
        return self.analyze_signal(track, signal, cancel_event=cancel_event, progress=progress)

    def analyze_signal(
        self,
        track: PlaylistTrack,
        signal: np.ndarray,
        *,
        cancel_event: threading.Event,
        progress: Callable[[float, str], None] | None = None,
        rhythm: bool = True,
    ) -> TrackAnalysis:
        """``analyze`` on an already decoded mono SAMPLE_RATE signal (shared with Beat This).

        ``rhythm=False`` leaves bpm/beats/downbeats/meter empty (see
        ``with_rhythm``): Beat This replaces them, and librosa's beat tracker
        (2.2 s of a 4-minute track) only matters when it cannot.
        """
        def report(fraction: float, message: str) -> None:
            _report(progress, cancel_event, fraction, message)

        duration_seconds = max(0.0, float(track.duration_seconds))

        if len(signal) < int(MINIMUM_ANALYZABLE_SECONDS * SAMPLE_RATE) or (
            len(signal) == 0 or float(np.max(np.abs(signal))) < SILENCE_PEAK_THRESHOLD
        ):
            LOGGER.info(
                "AutoMix analysis: %s is silent or too short for rhythm analysis", track.file_path,
            )
            return TrackAnalysis(
                track_id=track.id, source_path=track.file_path, duration_seconds=duration_seconds,
                analyzer_id=self.provider_id, analyzer_version=self.version,
            )

        rhythm_fields = self._rhythm_fields(signal, duration_seconds, report) if rhythm else {}

        report(0.75, STEP_KEY_ENERGY)
        key, key_confidence = self._estimate_key(signal)
        energy = self._estimate_energy(signal)
        audible_start, audible_end = audible_bounds(signal, duration_seconds)
        music_tags = analyze_music(signal, SAMPLE_RATE, duration_seconds, cancel_event)
        report(0.95, STEP_KEY_ENERGY)
        # Reuse the decoded PCM and the existing spectral extractor, including
        # when Sonara is absent. Do not manufacture verse/chorus labels.
        measured = signal[:min(len(signal), round(duration_seconds * SAMPLE_RATE))]
        bass, brightness, percussive = timbre_curves(measured, SAMPLE_RATE)
        rms = tuple(float(np.sqrt(np.mean(np.square(measured[start:start + SAMPLE_RATE], dtype=np.float64))))
                    for start in range(0, len(measured), SAMPLE_RATE))
        report(0.99, STEP_KEY_ENERGY)

        return TrackAnalysis(
            track_id=track.id, source_path=track.file_path, duration_seconds=duration_seconds,
            **rhythm_fields,
            key=key, key_confidence=key_confidence,
            energy=energy,
            audible_start_seconds=audible_start, audible_end_seconds=audible_end,
            decay_start_seconds=decay_start(signal, duration_seconds),
            music_tags=music_tags,
            rms_curve=rms, bass_curve=bass, brightness_curve=brightness, percussive_curve=percussive,
            analyzer_id=self.provider_id, analyzer_version=self.version,
        )

    def with_rhythm(self, analysis: TrackAnalysis, signal: np.ndarray) -> TrackAnalysis:
        """``analysis`` (from ``analyze_signal(rhythm=False)``) with this analyzer's own rhythm fields."""
        if analysis.energy is None:
            return analysis  # silent/too short: analyze_signal never tracks beats there
        return replace(analysis, **self._rhythm_fields(signal, analysis.duration_seconds, lambda *_: None))

    @timed("automix.beat_analysis_seconds")
    def _rhythm_fields(
        self, signal: np.ndarray, duration_seconds: float, report: Callable[[float, str], None],
    ) -> dict[str, object]:
        report(0.2, STEP_RHYTHM)
        tempo, raw_beat_times = librosa.beat.beat_track(y=signal, sr=SAMPLE_RATE, units="time")
        beat_times = self._clean_beats(np.atleast_1d(np.asarray(raw_beat_times, dtype=float)), duration_seconds)

        bpm_value = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
        grid = fit_beat_grid(beat_times)  # librosa's tempo is hop-quantized like the beats
        if grid is not None:
            bpm_value = grid.bpm
        bpm_value = normalize_tempo_octave(bpm_value) if bpm_value > 0.0 else 0.0
        low, high = PLAUSIBLE_BPM_RANGE
        if not (low <= bpm_value <= high):
            bpm_value = 0.0
        bpm_confidence = self._bpm_confidence(beat_times) if bpm_value > 0.0 else 0.0

        report(0.6, STEP_BARS)
        downbeats, meter_confidence = self._infer_downbeats(beat_times)
        return {
            "bpm": bpm_value if bpm_value > 0.0 else None,
            "bpm_confidence": bpm_confidence,
            "beats": tuple(beat_times.tolist()),
            "downbeats": tuple(downbeats.tolist()),
            "meter_numerator": 4 if len(downbeats) else None,
            "meter_denominator": 4 if len(downbeats) else None,
            "meter_confidence": meter_confidence,
        }

    @timed("automix.decode_seconds")
    def _decode_mono_pcm(
        self, path: Path, cancel_event: threading.Event, *,
        sample_rate: int = SAMPLE_RATE, channels: int = 1, start: float = 0.0, duration: float | None = None,
    ) -> np.ndarray:
        """Decode an optional window to interleaved float32 PCM without a temporary file."""
        if not path.is_file():
            raise RuntimeError(f"Audio file is missing: {path}")
        try:
            return read_pcm(
                path, cancel_event, sample_rate=sample_rate, channels=channels,
                start=start, duration=duration,
            ).reshape(-1)
        except CancelledError as error:
            raise AnalysisCancelled("AutoMix analysis cancelled during decode") from error
        except AudioDecodeError as error:
            if cancel_event.is_set():
                raise AnalysisCancelled("AutoMix analysis cancelled during decode") from error
            if self.ffmpeg_executable is None:
                raise RuntimeError(f"{error} FFmpeg is required to try this file again.") from error
        window = (["-ss", f"{start:.3f}"] if start > 0.0 else []) + (["-t", f"{duration:.3f}"] if duration else [])
        command = [
            str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-nostdin",
            *window, "-i", str(path), "-vn", "-sn", "-dn",
            "-ac", str(channels), "-ar", str(sample_rate), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **hidden_process_kwargs(),
            )
        except OSError as error:
            raise RuntimeError(f"Could not start FFmpeg for AutoMix analysis: {error}") from error
        # A track longer than a few seconds produces more PCM than the OS
        # pipe buffer holds; polling process.poll() without also draining
        # stdout would let FFmpeg block on a full pipe forever. Read both
        # streams on background threads while the main thread only polls
        # for cancellation, then join once the process has exited.
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def drain(stream: object, sink: list[bytes]) -> None:
            assert stream is not None
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                sink.append(chunk)

        stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_chunks), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_chunks), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            while process.poll() is None:
                if cancel_event.wait(0.05):
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise AnalysisCancelled("AutoMix analysis cancelled during decode")
        finally:
            stdout_thread.join(timeout=5.0)
            stderr_thread.join(timeout=5.0)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if process.returncode != 0:
            message = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip() or "unknown FFmpeg error"
            raise RuntimeError(f"FFmpeg could not decode audio for analysis: {message}")
        return np.frombuffer(b"".join(stdout_chunks), dtype=np.float32)

    @staticmethod
    def _clean_beats(beat_times: np.ndarray, duration_seconds: float) -> np.ndarray:
        """Sort, clip to duration, and drop near-duplicate beats (decode padding, jitter)."""
        cleaned: list[float] = []
        previous = float("-inf")
        tolerance = duration_seconds + BEAT_DEDUPE_TOLERANCE_SECONDS
        for value in sorted(float(v) for v in beat_times if 0.0 <= v <= tolerance):
            clipped = min(value, duration_seconds)
            if clipped - previous < BEAT_DEDUPE_TOLERANCE_SECONDS:
                continue
            cleaned.append(clipped)
            previous = clipped
        return np.array(cleaned, dtype=float)

    @staticmethod
    def _bpm_confidence(beat_times: np.ndarray) -> float:
        """A steady beat grid (low inter-beat-interval variance) means a trustworthy tempo."""
        if len(beat_times) < 4:
            return 0.0
        intervals = np.diff(beat_times)
        mean_interval = float(np.mean(intervals))
        if mean_interval <= 0.0:
            return 0.0
        coefficient_of_variation = float(np.std(intervals)) / mean_interval
        return max(0.0, min(1.0, 1.0 - coefficient_of_variation * 2.0))

    @staticmethod
    @timed("automix.downbeat_guess_seconds")
    def _infer_downbeats(beat_times: np.ndarray) -> tuple[np.ndarray, float]:
        """Provisional 4/4 bar guess: every 4th beat starting at the first.

        Not a real downbeat detector -- see the module docstring. Returns
        an empty array (and zero confidence) when there are too few beats
        to guess a bar structure from.
        """
        if len(beat_times) < 4:
            return np.array([], dtype=float), 0.0
        return beat_times[0::4], PROVISIONAL_METER_CONFIDENCE

    @staticmethod
    @timed("automix.key_seconds")
    def _estimate_key(signal: np.ndarray) -> tuple[str | None, float]:
        chroma = librosa.feature.chroma_stft(y=signal, sr=SAMPLE_RATE)
        chroma_mean = np.mean(chroma, axis=1)
        key, confidence = estimate_key(chroma_mean)
        return (key, confidence) if confidence > 0.0 else (None, 0.0)

    @staticmethod
    @timed("automix.energy_seconds")
    def _estimate_energy(signal: np.ndarray) -> float:
        """RMS relative to ENERGY_REFERENCE_RMS -- a documented heuristic, not LUFS."""
        rms = float(np.sqrt(np.mean(np.square(signal))))
        return max(0.0, min(1.0, rms / ENERGY_REFERENCE_RMS))
