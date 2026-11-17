"""Track-aware Text Source template expansion for video export."""

from __future__ import annotations

import re

from app.models.playlist import PlaylistTrack
from app.utils.time_format import format_clock


# Keep the editor's completion list and the renderer's replacement contract in
# one place.  Unknown tokens are intentionally preserved by the renderer.
TEXT_TEMPLATE_TOKEN_NAMES = (
    "title",
    "artist",
    "album",
    "track",
    "track_total",
    "filename",
    "current_time",
    "total_time",
    "track_current_time",
    "track_total_time",
    "video_current_time",
    "video_total_time",
)


def expand_track_template(template: str, track: PlaylistTrack, track_number: int,
                          track_total: int, start_seconds: float,
                          track_elapsed_seconds: float = 0.0,
                          playlist_duration_seconds: float | None = None, *,
                          time_format: str = "HH:mm") -> str:
    """Replace track and whole-video time tokens while preserving unknown tokens."""
    video_current_seconds = max(0.0, start_seconds + track_elapsed_seconds)
    video_total_seconds = (
        max(video_current_seconds, playlist_duration_seconds)
        if playlist_duration_seconds is not None else video_current_seconds
    )
    def clock(seconds: float) -> str:
        return format_timestamp(seconds, time_format=time_format)

    values = {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "track": str(track_number),
        "track_total": str(track_total),
        "filename": track.filename,
        # Legacy aliases intentionally now follow the current song, which is
        # the expected meaning beside the current song's total duration.
        "current_time": clock(track_elapsed_seconds),
        "total_time": clock(track.duration_seconds),
        "track_current_time": clock(track_elapsed_seconds),
        "track_total_time": clock(track.duration_seconds),
        "video_current_time": clock(video_current_seconds),
        "video_total_time": clock(video_total_seconds),
    }
    return re.sub(
        r"%([a-z_]+)%",
        lambda match: values.get(match.group(1).lower(), match.group(0)),
        template,
    )


_PLACEHOLDER_LABELS = {
    "en": {
        "title": "Title", "artist": "Artist", "album": "Album",
        "track": "Track #", "track_total": "Track count", "filename": "File name",
        "current_time": "Elapsed", "total_time": "Duration",
        "track_current_time": "Elapsed", "track_total_time": "Duration",
        "video_current_time": "Video elapsed", "video_total_time": "Video duration",
    },
    "ko": {
        "title": "제목", "artist": "아티스트", "album": "앨범",
        "track": "트랙 번호", "track_total": "전체 곡 수", "filename": "파일명",
        "current_time": "현재 시간", "total_time": "전체 시간",
        "track_current_time": "현재 시간", "track_total_time": "전체 시간",
        "video_current_time": "영상 현재 시간", "video_total_time": "영상 전체 시간",
    },
}


def expand_placeholder_labels(template: str, korean: bool = False) -> str:
    """Show known ``%token%`` names as parenthesised labels on the editing canvas.

    Surrounding literal text is preserved: ``%title%The Album`` becomes
    ``(제목)The Album``.  Unknown tokens are left as-is.  The real values are
    substituted only in preview and export, against the actual playlist track.
    """
    labels = _PLACEHOLDER_LABELS["ko" if korean else "en"]
    return re.sub(
        r"%([a-z_]+)%",
        lambda match: (
            f"({labels[match.group(1).lower()]})"
            if match.group(1).lower() in labels else match.group(0)
        ),
        template,
    )


def format_timestamp(seconds: float, *, time_format: str = "HH:mm") -> str:
    """Render a time token using the time source\'s chosen duration format.

    ``HH:mm`` is the historical default and retains automatic MM:SS / HH:MM:SS
    formatting for existing projects and ordinary text template sources.
    """
    total = max(0, int(seconds))
    if time_format == "mm:ss":
        return format_clock(total, hours=False)
    if time_format == "HH:mm:ss":
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return format_clock(total)
