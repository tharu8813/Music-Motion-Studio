"""Reuse AutoMix's per-track rhythm analysis for Canvas loop motion."""

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from math import isfinite
from pathlib import Path
from statistics import median
from threading import Event

from app.animation.curves import TrackTempo
from app.automix.models import TrackAnalysis
from app.models.playlist import PlaylistTrack
from app.models.source import Source
from app.utils.subprocess_utils import lower_thread_if_background


def track_tempo(analysis: TrackAnalysis) -> TrackTempo | None:
    if analysis.bpm is None or analysis.beat_alignment_quality() == "insufficient":
        return None
    # Existing analysis files permit equal timestamps. Drop duplicates before
    # interpolating so every beat interval has positive length.
    beats = tuple(dict.fromkeys(analysis.beats))
    period = median(b - a for a, b in zip(beats, beats[1:])) if len(beats) > 1 else 60.0 / analysis.bpm
    return TrackTempo(period, beats) if isfinite(period) and 0.2 <= period <= 3.0 else None


def analyze_track_tempos(
    tracks: Sequence[PlaylistTrack], ffmpeg: Path, cancel_event: Event,
) -> dict[str, TrackTempo]:
    from app.automix.analysis.registry import create_analysis_provider
    from app.automix.analysis.service import AnalysisService

    eligible = [track for track in tracks if track.enabled and track.file_path and track.duration_seconds > 0]
    if not eligible or cancel_event.is_set():
        return {}
    result = AnalysisService(create_analysis_provider("auto", ffmpeg)).analyze_tracks(
        eligible, cancel_event=cancel_event,
    )
    return {
        track_id: tempo for track_id, analysis in result.analyses.items()
        if (tempo := track_tempo(analysis)) is not None
    }


def prepare_track_tempos(
    tracks: Sequence[PlaylistTrack], sources: Sequence[Source], ffmpeg: str | Path,
    cancel_event: Event, *, pump_ui=None, on_start=None,
) -> dict[str, TrackTempo]:
    """Freeze tempo data before capturing export frames."""
    from app.renderer.ffmpeg_renderer import RenderCancelledError

    if cancel_event.is_set():
        raise RenderCancelledError("Tempo analysis was cancelled.")
    if not any(source.visible and source.uses_tempo_sync and source.loop_motion_amount > 0 for source in sources):
        return {}
    if on_start is not None:
        on_start()
    if pump_ui is None:
        tempos = analyze_track_tempos(tracks, Path(ffmpeg), cancel_event)
    else:
        with ThreadPoolExecutor(max_workers=1, initializer=lower_thread_if_background) as worker:
            future = worker.submit(analyze_track_tempos, tracks, Path(ffmpeg), cancel_event)
            try:
                while not future.done():
                    pump_ui()
                    wait((future,), timeout=0.05)
            finally:
                if not future.done():
                    cancel_event.set()
            tempos = future.result()
    if cancel_event.is_set():
        raise RenderCancelledError("Tempo analysis was cancelled.")
    return tempos
