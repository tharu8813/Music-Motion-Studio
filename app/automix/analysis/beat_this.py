"""Optional advanced AutoMix analyzer: Beat This! neural beat/downbeat tracking.

Beat This! (CPJKU/beat_this on GitHub and PyPI as ``beat-this``; MIT license
for both code and the pretrained weights -- confirmed from the upstream
README/LICENSE/pyproject.toml at integration time) gives genuinely
model-based downbeats instead of BasicAnalysisProvider's provisional "every
4th beat" 4/4 guess (see app/automix/analysis/basic.py's module docstring).
That provisional guess is exactly what BasicAnalysisProvider deliberately
caps at PROVISIONAL_METER_CONFIDENCE=0.3, below
TrackAnalysis.RELIABLE_METER_CONFIDENCE=0.5 -- so a real downbeat model is
what actually unlocks BEAT_MATCH-quality transitions in
app/automix/candidates.py, not a planner change.

``torch``/``beat_this`` are heavyweight, optional runtime dependencies (not
in requirements.txt -- see docs/automix-history/automix-phase1-dependency-evaluation.md's
"Beat This!" section for the original size/license evaluation) and are
imported lazily, inside _load_model(), only once analysis is actually
attempted -- never at module import time, matching the same principle
app/controllers/automix_analysis_controller.py already documents for
librosa. A missing/broken install, a failed/corrupted model download, or an
inference error for one track all degrade to BasicAnalysisProvider's own
result for that track: AutoMix, and the app as a whole, must keep working
with this engine completely absent.

This is a hybrid provider (key/energy/vocal-activity/validation/silence
handling are delegated to an owned BasicAnalysisProvider instance, not
reimplemented) -- Beat This! only ever replaces the rhythm fields (bpm,
bpm_confidence, beats, downbeats, meter_*).

Beat This! predicts beat and downbeat *timestamps*, not a time signature --
whether those downbeats actually imply a plausible, internally consistent
bar structure (e.g. not "every beat is also a downbeat," a degenerate
prediction a fully unaccented/homogeneous audio signal can produce) is
sanity-checked here (_estimate_meter_numerator/_meter_confidence) before
being reported as a usable meter. Model output itself is never
postprocessed/truncated to look more plausible (no "just take every 4th
downbeat"): an implausible or inconsistent bar structure is reported as
low confidence and/or an unknown meter_numerator, and the existing
degrade chain (candidates.py) is what falls back from there -- never a
hardcoded meter forced on the data.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np

from app.automix.analysis.basic import SAMPLE_RATE as BASIC_SAMPLE_RATE
from app.automix.analysis.basic import BasicAnalysisProvider, normalize_tempo_octave
from app.automix.analysis.provider import STEP_BEAT_MODEL, STEP_DECODE, STEP_VOCALS, AnalysisCancelled
from app.automix.analysis.vocals import ENGINE_ID as VOCAL_ENGINE_ID
from app.automix.analysis.vocals import DemucsVocalDetector, measured_regions, vocal_detection_available
from app.automix.beatgrid import fit_beat_grid
from app.automix.models import TrackAnalysis
from app.models.playlist import PlaylistTrack

LOGGER = logging.getLogger(__name__)

DEFAULT_CHECKPOINT = "final0"
"""Beat This!'s default pretrained checkpoint (~78 MB). The Windows installer
ships it (see bundled_checkpoint); elsewhere it is downloaded and
cached by the ``beat_this`` package itself on first use, in its own cache
directory -- unlike app/ffmpeg/managed_installer.py, this provider does not
implement its own download/checksum/staging pipeline for the model, since
one already exists upstream. A corrupted or interrupted download surfaces
as an exception from beat_this's own inference call, which analyze() below
catches like any other inference failure and degrades to the basic
analyzer for that track."""

MINIMUM_BEATS_FOR_TEMPO = 4
"""Below this, an inter-beat-interval statistic is not meaningful (matches
BasicAnalysisProvider._bpm_confidence's own minimum)."""
MINIMUM_DOWNBEATS_FOR_METER = 2
"""At least one full bar interval is needed to say anything about meter regularity."""

_TIMESTAMP_DEDUPE_TOLERANCE_SECONDS = 1e-3


def _installed_beat_this_version() -> str:
    """The installed ``beat-this`` distribution's version, or "unavailable".

    Uses importlib.metadata, which reads installed-package metadata
    (dist-info) without importing the package itself -- safe to call
    unconditionally, even when torch/beat_this are not installed at all,
    without triggering the heavy import this whole module otherwise avoids
    until analyze() actually needs it.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("beat-this")
    except PackageNotFoundError:
        return "unavailable"
    except Exception:  # noqa: BLE001 - a version string must never block construction
        return "unknown"


class BeatThisAnalysisProvider:
    """Hybrid AnalysisProvider: Beat This! for rhythm, BasicAnalysisProvider for the rest.

    One instance loads its model at most once (see _load_model, guarded by
    a lock for AnalysisService's concurrent per-track worker threads) and
    reuses it for every track passed to analyze() -- construct one instance
    per analysis batch/session, never one per track.
    """

    provider_id = "beat_this"
    version = "9"
    """This *implementation's* version: bump it if the confidence
    calibration or output mapping in this module changes in a way that
    should invalidate previously cached results, independent of the
    upstream model/package version. Accessed on the class (not an
    instance) this stays "1" -- see __init__, which combines it with the
    checkpoint name and the installed ``beat-this`` package version into
    the actual per-instance cache identity (self.version), so a checkpoint
    change or a package upgrade also invalidates old cache entries without
    a manual version bump here. "2": the model reads the FFmpeg-decoded
    signal (any format FFmpeg reads, e.g. AAC/M4A) instead of loading the file
    itself, plus BasicAnalysisProvider "3"'s audible bounds. "3": basic "4" (no
    voice-band vocal guess). "4": BPM from a fitted beat grid, not the
    frame-quantized median interval. "5": basic "6" (decay start). "6": vocal_coverage.
    "7": basic "7" (YAMNet genre/mood tags). "8": basic "8" (acoustic curves).
    "9": basic "9" (100 ms band power)."""

    def __init__(
        self, ffmpeg_executable: Path | None, *,
        device: str | None = None, checkpoint: str = DEFAULT_CHECKPOINT,
    ) -> None:
        self._basic = BasicAnalysisProvider(ffmpeg_executable)
        self._requested_device = device
        self._checkpoint = checkpoint
        self._model = None
        self._model_lock = threading.Lock()
        # Shadows the class attribute above with the full cache identity for
        # this instance (app/automix/cache.py keys entries by
        # provider_id+version). importlib.metadata reads installed-package
        # metadata without importing/executing the package, so this never
        # eagerly loads torch just to compute a version string.
        self.version = f"{BeatThisAnalysisProvider.version}+{checkpoint}+pkg{_installed_beat_this_version()}"
        # Optional time-resolved vocal activity (app/automix/analysis/vocals.py).
        # Part of the cache identity: installing the engine later re-analyzes.
        self._vocals = None
        if vocal_detection_available():
            self._vocals = DemucsVocalDetector(self._basic._decode_mono_pcm)
            self.version += f"+vox-{VOCAL_ENGINE_ID}"

    def analyze(
        self, track: PlaylistTrack, *,
        cancel_event: threading.Event,
        progress: Callable[[float, str], None] | None = None,
    ) -> TrackAnalysis:
        # Reuses every bit of BasicAnalysisProvider's decode, silence/
        # too-short handling, key/energy/vocal-activity estimation, and
        # TrackAnalysis validation -- only the rhythm fields below are ever
        # replaced (see module docstring).
        # One shared decode feeds both models, with FFmpeg available for AAC/M4A.
        if progress is not None:
            progress(0.0, STEP_DECODE)
        signal = self._basic._decode_mono_pcm(Path(track.file_path), cancel_event)
        # The basic steps take the first half; the beat model and vocals follow.
        basic_progress = (lambda fraction, step: progress(fraction * 0.5, step)) if progress is not None else None
        # Without librosa's beats: they are only needed where the model fails,
        # via fallback() -- the result is identical either way.
        basic_result = self._basic.analyze_signal(
            track, signal, cancel_event=cancel_event, progress=basic_progress, rhythm=False)

        def fallback() -> TrackAnalysis:
            return self._basic.with_rhythm(basic_result, signal)

        if cancel_event.is_set():
            raise AnalysisCancelled("AutoMix analysis cancelled before Beat This inference.")
        if basic_result.energy is None:
            # `energy` is only ever left at its default None by
            # BasicAnalysisProvider's own early return for a silent/too-short
            # track (see basic.py's MINIMUM_ANALYZABLE_SECONDS/
            # SILENCE_PEAK_THRESHOLD check) -- every other path through
            # analyze() always computes a real energy value, even when
            # librosa's own BPM estimate is rejected as implausible. That
            # distinction matters: "librosa could not find a usable BPM" is
            # not the same thing as "there is no audio signal to analyze",
            # and Beat This! can succeed on tracks librosa's beat tracker
            # fails on (that is the whole point of adding it). Skipping
            # inference here is therefore only a true silent/too-short
            # short-circuit, never a proxy for "basic found no BPM".
            return basic_result
        try:
            raw_beats, raw_downbeats = self._run_inference(signal, cancel_event, progress)
        except AnalysisCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - the ML engine must never break AutoMix
            LOGGER.warning(
                "Beat This analysis unavailable for %s (%s); using the basic beat/downbeat estimate instead.",
                track.file_path, error,
            )
            return fallback()
        if cancel_event.is_set():
            raise AnalysisCancelled("AutoMix analysis cancelled after Beat This inference.")

        beats = _sanitize_timestamps(raw_beats, basic_result.duration_seconds)
        if not beats:
            return fallback()
        bpm, bpm_confidence = _bpm_from_beats(np.array(beats))
        if bpm is None:
            return fallback()

        downbeats = _sanitize_timestamps(raw_downbeats, basic_result.duration_seconds)
        if downbeats:
            # `downbeats` stays the model's own, unmodified output even when
            # the bar structure it implies is implausible (e.g. every beat
            # also reported as a downbeat) -- meter_numerator=None and a low
            # meter_confidence are how that is communicated to the planner,
            # never by silently discarding/truncating what the model
            # actually predicted (see module docstring, "never postprocess
            # model output to look plausible").
            beats_array, downbeats_array = np.array(beats), np.array(downbeats)
            meter_confidence = _meter_confidence(beats_array, downbeats_array)
            meter_numerator, _consistency = _estimate_meter_numerator(beats_array, downbeats_array)
            meter_denominator = 4 if meter_numerator is not None else None
        else:
            # No real downbeats from the model for this track: keep the
            # basic analyzer's own provisional bar guess (already
            # deliberately low-confidence, see PROVISIONAL_METER_CONFIDENCE)
            # rather than reporting an empty meter.
            provisional = fallback()
            downbeats = provisional.downbeats
            meter_confidence = provisional.meter_confidence
            meter_numerator = provisional.meter_numerator
            meter_denominator = provisional.meter_denominator

        if progress is not None and self._vocals is not None:
            progress(0.8, STEP_VOCALS)
        vocal_activity, vocal_coverage, analyzer_id = self._detect_vocals(
            track, basic_result.duration_seconds, cancel_event)
        return replace(
            basic_result,
            bpm=bpm,
            bpm_confidence=bpm_confidence,
            beats=beats,
            downbeats=downbeats,
            meter_numerator=meter_numerator,
            meter_denominator=meter_denominator,
            meter_confidence=meter_confidence,
            vocal_activity=vocal_activity,
            vocal_coverage=vocal_coverage,
            analyzer_id=analyzer_id,
            analyzer_version=self.version,
        )

    def _detect_vocals(
        self, track: PlaylistTrack, duration: float, cancel_event: threading.Event,
    ) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...] | None, str]:
        """(vocal spans, measured spans, analyzer_id). A failed detection measured
        nothing (so "no spans" there means unknown, not "no vocals") and is stamped
        with another analyzer_id, so AnalysisService treats it as a cache miss and
        retries it later (e.g. once the model download succeeds)."""
        if self._vocals is None:
            return (), None, self.provider_id
        try:
            spans = self._vocals.detect(Path(track.file_path), duration, cancel_event)
        except AnalysisCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - vocal detection must never break AutoMix
            LOGGER.warning("AutoMix vocal detection unavailable for %s (%s); vocals stay unknown.",
                           track.file_path, error)
            return (), (), f"{self.provider_id}-novocals"
        if cancel_event.is_set():
            raise AnalysisCancelled("AutoMix analysis cancelled during vocal detection.")
        return spans, measured_regions(duration), self.provider_id

    def _run_inference(
        self, signal: np.ndarray, cancel_event: threading.Event,
        progress: Callable[[float, str], None] | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if progress is not None:
            progress(0.55, STEP_BEAT_MODEL)
        audio2beats = self._load_model()
        if cancel_event.is_set():
            # PyTorch inference itself cannot be interrupted mid-forward-pass;
            # this is the latest point cancellation can still be honored
            # before committing to the (uninterruptible) inference call.
            raise AnalysisCancelled("AutoMix analysis cancelled before Beat This inference.")
        with self._model_lock:
            # Serialize actual inference: AnalysisService analyzes several
            # tracks concurrently on a thread pool, but running several
            # forward passes through the same model at once wastes CPU/GPU
            # rather than speeding anything up, and risks CUDA OOM on
            # constrained GPUs. Decoding/Basic analysis above stays
            # parallel; only the model call itself is serialized.
            beats, downbeats = audio2beats(signal, BASIC_SAMPLE_RATE)
        return np.asarray(beats, dtype=float), np.asarray(downbeats, dtype=float)

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:  # re-check: lost a race to load
                return self._model
            import torch
            from beat_this.inference import Audio2Beats

            device = self._requested_device or ("cuda" if torch.cuda.is_available() else "cpu")
            LOGGER.info("Loading Beat This model %r on %s", self._checkpoint, device)
            self._model = Audio2Beats(
                checkpoint_path=bundled_checkpoint(self._checkpoint) or self._checkpoint, device=device, dbn=False,
            )
            return self._model


BUNDLED_CHECKPOINT_DIRECTORY = Path("beat_this") / "checkpoints"
"""Where the installer ships checkpoints (``<name>.ckpt``), relative to the
frozen app's bundle folder, so the installed app analyzes offline."""


def bundled_checkpoint(name: str) -> str | None:
    """Path of ``name``'s checkpoint shipped inside a frozen build, else None.

    Only the loading path changes: the cache identity stays the checkpoint
    *name*, so bundled and downloaded copies of one model share cache entries.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is None:
        return None
    candidate = Path(bundle) / BUNDLED_CHECKPOINT_DIRECTORY / f"{name}.ckpt"
    return str(candidate) if candidate.is_file() else None


def _sanitize_timestamps(values: np.ndarray, duration_seconds: float) -> tuple[float, ...]:
    """Sort, clip to duration, and drop near-duplicates -- same discipline as
    BasicAnalysisProvider._clean_beats, applied here so a TrackAnalysis built
    from Beat This! output always satisfies TrackAnalysis.__post_init__
    regardless of the model's own timestamp precision/rounding."""
    cleaned: list[float] = []
    previous = float("-inf")
    for value in sorted(float(v) for v in values if np.isfinite(v) and v >= 0.0):
        clipped = min(value, duration_seconds)
        if clipped - previous < _TIMESTAMP_DEDUPE_TOLERANCE_SECONDS:
            continue
        cleaned.append(clipped)
        previous = clipped
    return tuple(cleaned)


def _bpm_from_beats(beats: np.ndarray) -> tuple[float | None, float]:
    """Median inter-beat interval, robust to a handful of misdetected beats.

    Confidence calibration (documented per roadmap "clearly documented"
    requirement):

    - 0.0            = unusable (fewer than MINIMUM_BEATS_FOR_TEMPO beats,
                        or a degenerate/zero interval)
    - ~0.35 and up    = minimum usable (TrackAnalysis.INSUFFICIENT_BPM_CONFIDENCE)
    - ~0.6 and up     = reliable (TrackAnalysis.RELIABLE_BPM_CONFIDENCE)
    - 1.0             = a near-metronomic beat grid

    Built from the median absolute deviation of inter-beat intervals around
    their median (less outlier-sensitive than BasicAnalysisProvider's own
    mean/stddev coefficient-of-variation, appropriate here since a handful
    of misdetected beats should not tank confidence for an otherwise steady
    track): ``confidence = clamp(1 - 3 * (MAD / median_interval), 0, 1)``.
    A track with a genuinely steady beat -- exactly the case Beat This! is
    meant to unlock over the basic analyzer's provisional guess -- lands
    close to 1.0.
    """
    if len(beats) < MINIMUM_BEATS_FOR_TEMPO:
        return None, 0.0
    intervals = np.diff(np.sort(beats))
    intervals = intervals[intervals > 0.0]
    if len(intervals) < MINIMUM_BEATS_FOR_TEMPO - 1:
        return None, 0.0
    median_interval = float(np.median(intervals))
    if median_interval <= 0.0:
        return None, 0.0
    # The median of 50 fps timestamps is quantized (128 BPM reads 130.4); the fitted grid is not.
    grid = fit_beat_grid(beats)
    bpm = normalize_tempo_octave(grid.bpm if grid is not None else 60.0 / median_interval)
    deviation = float(np.median(np.abs(intervals - median_interval)))
    confidence = max(0.0, min(1.0, 1.0 - (deviation / median_interval) * 3.0))
    return bpm, confidence


MINIMUM_PLAUSIBLE_BEATS_PER_BAR = 2
MAXIMUM_PLAUSIBLE_BEATS_PER_BAR = 7
"""A bar this short or this long is not a meter AutoMix's bar-length
candidates (app/automix/candidates.py's BAR_LENGTHS, built on 4-beat bars)
can make sense of -- treated as an implausible/degenerate meter estimate,
same tier as the 1:1 "every beat is also a downbeat" case."""

MINIMUM_BAR_COUNT_CONSISTENCY = 0.6
"""At least this fraction of inter-downbeat spans must agree on the same
beat count for a meter estimate to be trusted at all (see
_estimate_meter_numerator) -- below this, the downbeat predictions are
too inconsistent to imply any single meter, plausible-looking or not."""

_DOWNBEAT_ALIGNMENT_TOLERANCE_SECONDS = 0.08
"""How far a downbeat may sit from its nearest reported beat and still
count as "on the beat grid" -- generous relative to typical beat spacing
(roughly 0.4-0.6s at 100-150 BPM) and Beat This!'s own frame resolution,
so this only flags a downbeat that isn't actually near any detected beat,
not one merely off by ordinary timestamp jitter."""


def _beats_per_bar_counts(beats: np.ndarray, downbeats: np.ndarray) -> np.ndarray:
    """How many beats fall within each inter-downbeat span.

    The same integer-counting idea Beat This! 1.1's own
    ``beat_this.utils.infer_beat_numbers`` uses to number beats within a
    bar (counting beats between consecutive downbeats) -- reimplemented
    narrowly here to get per-bar *counts* directly, and so a malformed
    prediction (e.g. downbeats that are not a subset of beats) degrades to
    an empty/low-confidence result instead of that function's
    print()-based warnings or a raised ValueError.
    """
    if len(downbeats) < 2:
        return np.array([], dtype=int)
    sorted_beats = np.sort(beats)
    sorted_downbeats = np.sort(downbeats)
    indices = np.searchsorted(sorted_beats, sorted_downbeats)
    return np.diff(indices)


def _estimate_meter_numerator(beats: np.ndarray, downbeats: np.ndarray) -> tuple[int | None, float]:
    """Estimate beats-per-bar from per-bar beat counts, plus how consistent that count is.

    Returns ``(numerator, consistency)``. ``numerator`` is ``None`` when
    the per-bar beat counts disagree too much
    (``< MINIMUM_BAR_COUNT_CONSISTENCY``) or the most common count falls
    outside a plausible bar length -- most notably the degenerate
    "every beat is also reported as a downbeat" case, where every span is
    exactly 1 beat wide and MINIMUM_PLAUSIBLE_BEATS_PER_BAR=2 rejects it
    outright. A confidently wrong 4/4 is worse than an honest "unknown", so
    this never guesses when it cannot clear both bars.
    """
    counts = _beats_per_bar_counts(beats, downbeats)
    counts = counts[counts > 0]
    if len(counts) == 0:
        return None, 0.0
    values, frequencies = np.unique(counts, return_counts=True)
    mode_index = int(np.argmax(frequencies))
    mode = int(values[mode_index])
    consistency = float(frequencies[mode_index]) / len(counts)
    if not (MINIMUM_PLAUSIBLE_BEATS_PER_BAR <= mode <= MAXIMUM_PLAUSIBLE_BEATS_PER_BAR):
        return None, consistency
    if consistency < MINIMUM_BAR_COUNT_CONSISTENCY:
        return None, consistency
    return mode, consistency


def _downbeat_alignment_score(beats: np.ndarray, downbeats: np.ndarray) -> float:
    """Fraction of downbeats that sit within tolerance of some reported beat.

    Beat This!'s own postprocessing already snaps downbeats onto detected
    beats, so this is a defensive sanity check (a provider-level one, not a
    trust-the-model assumption) rather than something expected to matter
    for real model output -- see module docstring on not silently
    "fixing" model output.
    """
    if len(downbeats) == 0 or len(beats) == 0:
        return 0.0
    sorted_beats = np.sort(beats)
    aligned = 0
    for downbeat in downbeats:
        index = np.searchsorted(sorted_beats, downbeat)
        candidates = sorted_beats[max(0, index - 1):index + 1]
        if len(candidates) and np.min(np.abs(candidates - downbeat)) <= _DOWNBEAT_ALIGNMENT_TOLERANCE_SECONDS:
            aligned += 1
    return aligned / len(downbeats)


def _meter_confidence(beats: np.ndarray, downbeats: np.ndarray) -> float:
    """Combine bar-length plausibility, bar-duration regularity, downbeat/beat
    grid alignment, and coverage into one 0.0-1.0 confidence.

    Unlike a plain "are the downbeats evenly spaced in time" check (which a
    degenerate "every beat is also a downbeat" prediction can pass
    trivially, since a perfectly regular beat grid is also a perfectly
    regular "bar" grid of length 1), _estimate_meter_numerator's plausible-
    bar-length gate runs first and is a hard requirement: an implausible or
    inconsistent beats-per-bar count forces confidence to 0.0 regardless of
    how evenly spaced the downbeats otherwise look.
    """
    if len(downbeats) < MINIMUM_DOWNBEATS_FOR_METER:
        return 0.0
    numerator, count_consistency = _estimate_meter_numerator(beats, downbeats)
    if numerator is None:
        return 0.0
    bar_intervals = np.diff(np.sort(downbeats))
    bar_intervals = bar_intervals[bar_intervals > 0.0]
    if len(bar_intervals) == 0:
        return 0.0
    median_bar = float(np.median(bar_intervals))
    if median_bar <= 0.0:
        return 0.0
    deviation = float(np.median(np.abs(bar_intervals - median_bar)))
    time_consistency = max(0.0, min(1.0, 1.0 - (deviation / median_bar) * 3.0))
    alignment = _downbeat_alignment_score(beats, downbeats)
    expected_bars = max(1.0, len(beats) / numerator)
    coverage = max(0.0, min(1.0, len(downbeats) / expected_bars))
    return max(0.0, min(1.0, count_consistency * time_consistency * alignment * (0.7 + 0.3 * coverage)))
