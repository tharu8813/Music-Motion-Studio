"""Immutable AutoMix analysis result model.

TrackAnalysis is cache data, never primary project document data (roadmap
"Persistence invariant") -- it is derived from an audio file and can always
be recomputed, so it is never stored inside a PlaylistTrack or project JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

# Thresholds for TrackAnalysis.beat_alignment_quality(). A provisional (not
# model-based) downbeat guess should never clear the "reliable" bar on its
# own -- see BasicAnalysisProvider's PROVISIONAL_METER_CONFIDENCE.
RELIABLE_BPM_CONFIDENCE = 0.6
RELIABLE_METER_CONFIDENCE = 0.5
INSUFFICIENT_BPM_CONFIDENCE = 0.35
LOW_CROSSOVER_HZ = 200
HIGH_CROSSOVER_HZ = 2500
BAND_POWER_HOP_SECONDS = 0.1
HANDOFF_LEVEL_BLOCK_SECONDS = 0.5


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def _validate_sorted_timestamps(name: str, values: tuple[float, ...], duration_seconds: float) -> None:
    previous = float("-inf")
    for value in values:
        if not _is_finite_number(value) or value < 0.0:
            raise ValueError(f"{name} must contain finite, non-negative seconds.")
        if value < previous:
            raise ValueError(f"{name} must be sorted in ascending order.")
        if value > duration_seconds:
            raise ValueError(f"{name} must not exceed the track duration.")
        previous = value


def _validate_confidence(name: str, value: float) -> None:
    if not _is_finite_number(value) or not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be a finite number between 0.0 and 1.0.")


@dataclass(frozen=True, slots=True)
class MusicTagRegion:
    """Measured music style/mood scores for a source-audio window, not a single forced label."""

    start_seconds: float
    end_seconds: float
    genres: tuple[tuple[str, float], ...] = ()
    moods: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if not (_is_finite_number(self.start_seconds) and _is_finite_number(self.end_seconds)
                and 0.0 <= self.start_seconds < self.end_seconds):
            raise ValueError("MusicTagRegion requires a finite, positive source window.")
        for tags in (self.genres, self.moods):
            names = set()
            for name, score in tags:
                if not isinstance(name, str) or not name.strip() or name in names:
                    raise ValueError("MusicTagRegion labels must be unique, non-empty strings.")
                names.add(name)
                _validate_confidence("MusicTagRegion score", score)


@dataclass(frozen=True, slots=True)
class TrackAnalysis:
    """One track's AutoMix-relevant audio analysis.

    Every field beyond ``track_id``/``source_path``/``duration_seconds`` is
    optional: a provider that only detects BPM leaves beats, key, energy,
    and vocal_activity at their defaults rather than fabricating values.
    """

    track_id: str
    source_path: str
    duration_seconds: float

    bpm: float | None = None
    bpm_confidence: float = 0.0

    beats: tuple[float, ...] = ()
    downbeats: tuple[float, ...] = ()

    meter_numerator: int | None = None
    meter_denominator: int | None = None
    meter_confidence: float = 0.0

    key: str | None = None
    key_confidence: float = 0.0

    energy: float | None = None
    vocal_activity: tuple[tuple[float, float], ...] = ()
    vocal_coverage: tuple[tuple[float, float], ...] | None = None
    """Where vocal activity was actually measured (source seconds): a detector
    only separates each track's head and tail. Outside these spans singing is
    unknown, never "none"; ``()`` = nothing measured (detection failed).
    ``None`` (results from before this field): a non-empty ``vocal_activity``
    stands for the whole track, an empty one for "unknown"."""
    lyric_vocal_spans: tuple[tuple[float, float], ...] = ()
    """Where the track's synced lyrics say it is being sung (audio seconds).
    A lower bound only -- LRC files routinely omit ad-libs and outro vocals --
    so it may keep a transition from starting mid-line but never proves the
    track is silent. Added at planning time from the playlist; never cached."""

    audible_start_seconds: float | None = None
    audible_end_seconds: float | None = None
    """Where sound starts/stops, excluding digital silence at either end
    (``None``: unknown, treat the whole file as audible). Real masters
    commonly carry 1-4 s of silence after the last decay; a transition
    overlapping only that would be a gap, not a mix."""
    decay_start_seconds: float | None = None
    """The last moment the track is within 15 dB of its body's level; after it
    only the ending's fade or decay is left (``None``: unknown)."""

    analyzer_id: str = ""
    analyzer_version: str = ""
    music_tags: tuple[MusicTagRegion, ...] = ()
    """YAMNet music styles/moods in measured windows; empty means unknown."""
    rms_curve: tuple[float, ...] = ()
    bass_curve: tuple[float, ...] = ()
    brightness_curve: tuple[float, ...] = ()
    percussive_curve: tuple[float, ...] = ()
    """Measured one-second audio features. RMS is linear amplitude; the other
    curves are 0..1 (structure.timbre). Empty means unavailable, never silence."""
    band_power_curve: tuple[tuple[float, float, float], ...] = ()
    """Linear low, mid and high power for each 100 ms source interval."""

    def __post_init__(self) -> None:
        if not isinstance(self.track_id, str) or not self.track_id.strip():
            raise ValueError("TrackAnalysis.track_id must be a non-empty string.")
        if not isinstance(self.source_path, str) or not self.source_path.strip():
            raise ValueError("TrackAnalysis.source_path must be a non-empty string.")
        if not _is_finite_number(self.duration_seconds) or self.duration_seconds < 0.0:
            raise ValueError("TrackAnalysis.duration_seconds must be finite and non-negative.")
        if self.bpm is not None and (not _is_finite_number(self.bpm) or self.bpm <= 0.0):
            raise ValueError("TrackAnalysis.bpm must be a finite positive number when known.")
        _validate_confidence("TrackAnalysis.bpm_confidence", self.bpm_confidence)
        _validate_sorted_timestamps("TrackAnalysis.beats", self.beats, self.duration_seconds)
        _validate_sorted_timestamps("TrackAnalysis.downbeats", self.downbeats, self.duration_seconds)
        if self.meter_numerator is not None and (
            not isinstance(self.meter_numerator, int) or isinstance(self.meter_numerator, bool)
            or self.meter_numerator <= 0
        ):
            raise ValueError("TrackAnalysis.meter_numerator must be a positive integer when known.")
        if self.meter_denominator is not None and (
            not isinstance(self.meter_denominator, int) or isinstance(self.meter_denominator, bool)
            or self.meter_denominator <= 0
        ):
            raise ValueError("TrackAnalysis.meter_denominator must be a positive integer when known.")
        _validate_confidence("TrackAnalysis.meter_confidence", self.meter_confidence)
        if self.key is not None and (not isinstance(self.key, str) or not self.key.strip()):
            raise ValueError("TrackAnalysis.key must be a non-empty string when known.")
        _validate_confidence("TrackAnalysis.key_confidence", self.key_confidence)
        if self.energy is not None and (not _is_finite_number(self.energy) or self.energy < 0.0):
            raise ValueError("TrackAnalysis.energy must be finite and non-negative when known.")
        previous_end = float("-inf")
        for start, end in self.vocal_activity:
            if not _is_finite_number(start) or not _is_finite_number(end) or start < 0.0:
                raise ValueError("TrackAnalysis.vocal_activity spans must be finite and non-negative.")
            if end <= start:
                raise ValueError("TrackAnalysis.vocal_activity spans must end after they start.")
            if end > self.duration_seconds:
                raise ValueError("TrackAnalysis.vocal_activity must not exceed the track duration.")
            if start < previous_end:
                raise ValueError("TrackAnalysis.vocal_activity spans must be sorted and non-overlapping.")
            previous_end = end
        previous_end = float("-inf")
        for start, end in self.vocal_coverage or ():
            if not (_is_finite_number(start) and _is_finite_number(end)
                    and previous_end <= start < end <= self.duration_seconds):
                raise ValueError("TrackAnalysis.vocal_coverage spans must be sorted, disjoint and within the track.")
            previous_end = end
        for name in ("audible_start_seconds", "audible_end_seconds", "decay_start_seconds"):
            value = getattr(self, name)
            if value is not None and (not _is_finite_number(value) or not 0.0 <= value <= self.duration_seconds):
                raise ValueError(f"TrackAnalysis.{name} must lie within the track when known.")
        if (self.audible_start_seconds is not None and self.audible_end_seconds is not None
                and self.audible_end_seconds < self.audible_start_seconds):
            raise ValueError("TrackAnalysis audible end must not precede its start.")
        if not isinstance(self.analyzer_id, str) or not isinstance(self.analyzer_version, str):
            raise ValueError("TrackAnalysis analyzer_id/analyzer_version must be strings.")
        previous_end = 0.0
        for name in ("rms_curve", "bass_curve", "brightness_curve", "percussive_curve"):
            curve = getattr(self, name)
            if len(curve) > int(self.duration_seconds) + 1 or any(
                    not _is_finite_number(v) or v < 0.0 or (name != "rms_curve" and v > 1.0) for v in curve):
                raise ValueError(f"TrackAnalysis.{name} must contain valid one-second measurements within the track.")
        if len(self.band_power_curve) > int(self.duration_seconds / BAND_POWER_HOP_SECONDS + 1e-6) + 1 or any(
                len(block) != 3 or any(not _is_finite_number(power) or power < 0.0 for power in block)
                for block in self.band_power_curve):
            raise ValueError("TrackAnalysis.band_power_curve requires three finite powers per source interval.")
        for region in self.music_tags:
            if not isinstance(region, MusicTagRegion) or not (
                    previous_end <= region.start_seconds < region.end_seconds <= self.duration_seconds):
                raise ValueError("TrackAnalysis.music_tags must be sorted, disjoint and within the track.")
            previous_end = region.end_seconds

    def beat_alignment_quality(self) -> str:
        """Classify how much a transition planner should trust this analysis.

        One of ``"reliable"`` (BPM and bar alignment both trustworthy),
        ``"bpm_only"`` (usable tempo but an uncertain/provisional downbeat),
        or ``"insufficient"`` (BPM itself is not trustworthy) -- the three
        buckets a fallback chain needs (roadmap Phase 2 section 9).
        """
        if self.bpm is None or self.bpm_confidence < INSUFFICIENT_BPM_CONFIDENCE:
            return "insufficient"
        if (
            self.beats
            and self.bpm_confidence >= RELIABLE_BPM_CONFIDENCE
            and self.meter_confidence >= RELIABLE_METER_CONFIDENCE
        ):
            return "reliable"
        return "bpm_only"

    def to_cache_fields(self) -> dict[str, Any]:
        """Serialize every field except identity (track_id/source_path).

        Identity is deliberately excluded: the cache is addressed by file
        fingerprint, not track ID, so two PlaylistTracks pointing at the same
        media reuse one cache entry (roadmap Phase 1 section 6).
        """
        return {
            "duration_seconds": self.duration_seconds,
            "bpm": self.bpm,
            "bpm_confidence": self.bpm_confidence,
            "beats": list(self.beats),
            "downbeats": list(self.downbeats),
            "meter_numerator": self.meter_numerator,
            "meter_denominator": self.meter_denominator,
            "meter_confidence": self.meter_confidence,
            "key": self.key,
            "key_confidence": self.key_confidence,
            "energy": self.energy,
            "vocal_activity": [list(span) for span in self.vocal_activity],
            "vocal_coverage": None if self.vocal_coverage is None else [list(span) for span in self.vocal_coverage],
            "audible_start_seconds": self.audible_start_seconds,
            "audible_end_seconds": self.audible_end_seconds,
            "decay_start_seconds": self.decay_start_seconds,
            "analyzer_id": self.analyzer_id,
            "analyzer_version": self.analyzer_version,
            **{name: list(getattr(self, name)) for name in
               ("rms_curve", "bass_curve", "brightness_curve", "percussive_curve")},
            "band_power_curve": [list(block) for block in self.band_power_curve],
            "music_tags": [
                {"start_seconds": r.start_seconds, "end_seconds": r.end_seconds,
                 "genres": [list(tag) for tag in r.genres], "moods": [list(tag) for tag in r.moods]}
                for r in self.music_tags
            ],
        }

    @classmethod
    def from_cache_fields(cls, track_id: str, source_path: str, fields: dict[str, Any]) -> "TrackAnalysis":
        """Rebuild a TrackAnalysis for ``track_id`` from cached, JSON-safe fields."""
        return cls(
            track_id=track_id,
            source_path=source_path,
            duration_seconds=fields["duration_seconds"],
            bpm=fields.get("bpm"),
            bpm_confidence=fields.get("bpm_confidence", 0.0),
            beats=tuple(fields.get("beats", ())),
            downbeats=tuple(fields.get("downbeats", ())),
            meter_numerator=fields.get("meter_numerator"),
            meter_denominator=fields.get("meter_denominator"),
            meter_confidence=fields.get("meter_confidence", 0.0),
            key=fields.get("key"),
            key_confidence=fields.get("key_confidence", 0.0),
            energy=fields.get("energy"),
            vocal_activity=tuple(tuple(span) for span in fields.get("vocal_activity", ())),
            vocal_coverage=(None if fields.get("vocal_coverage") is None
                            else tuple(tuple(span) for span in fields["vocal_coverage"])),
            audible_start_seconds=fields.get("audible_start_seconds"),
            audible_end_seconds=fields.get("audible_end_seconds"),
            decay_start_seconds=fields.get("decay_start_seconds"),
            analyzer_id=fields.get("analyzer_id", ""),
            analyzer_version=fields.get("analyzer_version", ""),
            **{name: tuple(fields.get(name, ())) for name in
               ("rms_curve", "bass_curve", "brightness_curve", "percussive_curve")},
            band_power_curve=tuple(tuple(block) for block in fields.get("band_power_curve", ())),
            music_tags=tuple(MusicTagRegion(
                start_seconds=r["start_seconds"], end_seconds=r["end_seconds"],
                genres=tuple(tuple(tag) for tag in r.get("genres", ())),
                moods=tuple(tuple(tag) for tag in r.get("moods", ())),
            ) for r in fields.get("music_tags", ())),
        )
