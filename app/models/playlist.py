"""Serializable playlist track model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from math import isfinite
from typing import Any
from uuid import uuid4

from app.utils.time_format import format_clock

EQ_BANDS_HZ = (60, 250, 1000, 4000, 12000)
"""Per-track graphic EQ: a low shelf, three peaks, a high shelf."""
EQ_LIMIT_DB = 12.0
VOLUME_RANGE_DB = (-24.0, 12.0)


def track_audio_filter(volume_db: float, eq_db: list[float]) -> str:
    """FFmpeg filters for a volume and EQ_BANDS_HZ gains; "" when neutral."""
    parts = []
    last = len(EQ_BANDS_HZ) - 1
    for index, (frequency, gain) in enumerate(zip(EQ_BANDS_HZ, eq_db)):
        if abs(gain) < 0.05:
            continue
        if index == 0:
            parts.append(f"lowshelf=f={frequency}:g={gain:.2f}")
        elif index == last:
            parts.append(f"highshelf=f={frequency}:g={gain:.2f}")
        else:
            parts.append(f"equalizer=f={frequency}:t=o:w=2:g={gain:.2f}")
    if abs(volume_db) >= 0.05:
        parts.append(f"volume={volume_db:.2f}dB")
    return ",".join(parts)


@dataclass(slots=True)
class PlaylistTrack:
    """Audio metadata and inclusion state for a single playlist entry."""

    file_path: str
    title: str
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    duration_seconds: float = 0.0
    start_time_seconds: float | None = None
    enabled: bool = True
    lyrics_path: str = ""
    lyrics: list[dict[str, Any]] = field(default_factory=list)
    lyrics_timing_offset_seconds: float = 0.0
    id: str = field(default_factory=lambda: str(uuid4()))
    cover_path: str = ""
    video_paths: list[str] = field(default_factory=list)
    volume_db: float = 0.0
    eq_db: list[float] = field(default_factory=list)
    """Gain per EQ_BANDS_HZ band; empty is flat."""

    @property
    def audio_filter(self) -> str:
        """FFmpeg filters for this track's volume/EQ, applied before any mixing; "" when neutral."""
        return track_audio_filter(self.volume_db, self.eq_db)

    @property
    def filename(self) -> str:
        """Return only the user-facing source file name."""
        return Path(self.file_path).name

    @property
    def duration_label(self) -> str:
        """Format duration as minutes and seconds."""
        return format_clock(round(self.duration_seconds), hours=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert the model to JSON-compatible data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlaylistTrack":
        """Restore a playlist track from JSON-compatible data."""
        if not isinstance(data, dict):
            raise ValueError("Project playlist tracks must be objects.")
        track = cls(**data)
        if not isinstance(track.id, str) or not track.id.strip():
            raise ValueError("Every playlist track must have a non-empty string ID.")
        if not isinstance(track.file_path, str) or not isinstance(track.title, str):
            raise ValueError("Playlist file paths and titles must be strings.")
        if not isinstance(track.cover_path, str):
            raise ValueError("Playlist cover paths must be strings.")
        if not isinstance(track.video_paths, list) or not all(
            isinstance(path, str) for path in track.video_paths
        ):
            raise ValueError(f"Track '{track.title}' has invalid video paths.")
        duration = track.duration_seconds
        if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not isfinite(float(duration)) or duration < 0):
            raise ValueError(f"Track '{track.title}' has an invalid duration.")
        if track.start_time_seconds is not None:
            start = track.start_time_seconds
            if (not isinstance(start, (int, float)) or isinstance(start, bool)
                    or not isfinite(float(start)) or start < 0):
                raise ValueError(f"Track '{track.title}' has an invalid start time.")
        if not isinstance(track.enabled, bool) or not isinstance(track.lyrics, list):
            raise ValueError(f"Track '{track.title}' has invalid enabled or lyrics data.")
        offset = track.lyrics_timing_offset_seconds
        if (not isinstance(offset, (int, float)) or isinstance(offset, bool)
                or not isfinite(float(offset)) or abs(float(offset)) > 3_600):
            raise ValueError(f"Track '{track.title}' has an invalid lyric timing offset.")
        low, high = VOLUME_RANGE_DB
        if (not isinstance(track.volume_db, (int, float)) or isinstance(track.volume_db, bool)
                or not low <= track.volume_db <= high):
            raise ValueError(f"Track '{track.title}' has an invalid volume.")
        if (not isinstance(track.eq_db, list) or len(track.eq_db) > len(EQ_BANDS_HZ)
                or not all(isinstance(gain, (int, float)) and not isinstance(gain, bool)
                           and abs(gain) <= EQ_LIMIT_DB for gain in track.eq_db)):
            raise ValueError(f"Track '{track.title}' has invalid EQ settings.")
        for cue in track.lyrics:
            if not isinstance(cue, dict) or not isinstance(cue.get("text", ""), str):
                raise ValueError(f"Track '{track.title}' contains an invalid lyric cue.")
            for key in ("start", "end"):
                value = cue.get(key)
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not isfinite(float(value)) or value < 0):
                    raise ValueError(
                        f"Track '{track.title}' lyric cue has an invalid '{key}' value."
                    )
            if float(cue["end"]) < float(cue["start"]):
                raise ValueError(f"Track '{track.title}' lyric cue ends before it starts.")
            cue["text"] = str(cue.get("text", "")).replace("\\n", "\n")
            words = cue.get("words", [])
            if not isinstance(words, list):
                raise ValueError(f"Track '{track.title}' has invalid word timing data.")
            previous_end, previous_offset = float(cue["start"]), 0
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]:
                    raise ValueError(f"Track '{track.title}' contains an invalid lyric word.")
                for key in ("start", "end"):
                    value = word.get(key)
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not isfinite(float(value))):
                        raise ValueError(f"Track '{track.title}' has an invalid word timestamp.")
                offset = word.get("offset")
                if isinstance(offset, int) and "\n" in cue["text"][previous_offset:offset]:
                    previous_end = float(cue["start"])
                # Independently rounded source timestamps may overlap by 1 ms.
                # Keep the source values; this tolerance must not admit words
                # outside the cue, negative times, or reversed intervals.
                if (not float(cue["start"]) <= word["start"] <= word["end"] <= float(cue["end"])
                        or word["start"] + .001 + 1e-9 < previous_end
                        or not isinstance(offset, int) or isinstance(offset, bool) or offset < previous_offset
                        or cue["text"][offset:offset + len(word["text"])] != word["text"]):
                    raise ValueError(f"Track '{track.title}' has invalid word timing or text offsets.")
                previous_end, previous_offset = word["end"], offset + len(word["text"])
        track.lyrics.sort(key=lambda cue: (float(cue["start"]), float(cue["end"])))
        return track
