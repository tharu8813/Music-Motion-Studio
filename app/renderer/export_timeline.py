"""Build the ordered visual sample plan used by preview-frame export."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil
from typing import Sequence

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.album_art import AMBIENT_FLOW_HZ
from app.preview.frame_state import MixJunction, mix_phase_durations, segment_junction, lyric_instrumental_windows
from app.timeline.compiler import compile_playlist
from app.timeline.render_plan import CompiledRenderPlan, visual_segments


@dataclass(frozen=True, slots=True)
class ExportFrameSample:
    """One Canvas state and the exact duration it occupies on the timeline."""

    track: PlaylistTrack
    track_number: int
    track_start_seconds: float
    duration_seconds: float
    elapsed_seconds: float
    timeline_seconds: float
    animation_phase: str | None = None
    animation_progress: float = 1.0
    animation_phase_duration: float = 0.0
    junction: MixJunction | None = None


class ExportTimelinePlanner:
    """Reproduce the existing export sampling schedule without rendering pixels."""

    @staticmethod
    def build_by_z_band(
        tracks: Sequence[PlaylistTrack],
        sources: Sequence[Source],
        dynamic_source_ids: set[str],
        z_bands: Sequence[tuple[float | None, float | None]],
        animation_fps: int,
        compiled_plan: CompiledRenderPlan | None = None,
    ) -> dict[str, list[ExportFrameSample]]:
        """Build an independent sample schedule for each Canvas Z band.

        A lyric or clock in one band must not force unrelated bands through the
        same sample points. Dynamic FFmpeg overlays are omitted because Canvas
        capture hides them and the renderer schedules them independently.
        """
        canvas_sources = [
            source for source in sources
            if source.visible and source.id not in dynamic_source_ids
        ]
        timelines: dict[str, list[ExportFrameSample]] = {}
        for index, (z_min, z_max) in enumerate(z_bands):
            stream_key = "base" if index == 0 else f"layer:{index - 1}"
            band_sources = [
                source for source in canvas_sources
                if (z_min is None or source.z_index >= z_min)
                and (z_max is None or source.z_index <= z_max)
            ]
            timelines[stream_key] = ExportTimelinePlanner.build(
                tracks, band_sources, animation_fps, compiled_plan,
            )
        return timelines

    @staticmethod
    def build(
        tracks: Sequence[PlaylistTrack],
        sources: Sequence[Source],
        animation_fps: int,
        compiled_plan: CompiledRenderPlan | None = None,
        *, track_number_offset: int = 0,
    ) -> list[ExportFrameSample]:
        sources = [source.resolved_lyrics() for source in sources]
        if compiled_plan is not None and compiled_plan != compile_playlist(tracks):
            return ExportTimelinePlanner._build_compiled(tracks, sources, animation_fps, compiled_plan)
        samples: list[ExportFrameSample] = []
        previous_track: PlaylistTrack | None = None
        previous_start = 0.0
        previous_number = 1

        # Presentation windows carry track_id, not the PlaylistTrack object the
        # animation math below needs, so resolve it back by id; ids are unique
        # and stable per timeline_from_playlist().
        track_by_id = {track.id: track for track in tracks}
        windows = compile_playlist(tracks).presentation.windows
        cursor = 0.0
        for number, window in enumerate(windows, start=1 + track_number_offset):
            track = track_by_id[window.track_id]
            start = window.timeline_start
            intro, outro = ExportTimelinePlanner._animation_durations(track, sources)
            gap = max(0.0, start - cursor)
            if gap > 0.0:
                gap_track = previous_track or track
                gap_elapsed = (
                    gap_track.duration_seconds if previous_track is not None else 0.0
                )
                gap_points = {cursor, start}
                for source in sources:
                    for boundary in (
                        source.timeline_start,
                        source.timeline_start + source.timeline_duration,
                    ):
                        if cursor < boundary < start and (
                            boundary == source.timeline_start
                            or source.timeline_duration > 0.0
                        ):
                            gap_points.add(boundary)
                if any(
                    source.source_type is SourceType.BACKGROUND
                    and source.background_mode == "album_art"
                    and source.background_ambient
                    for source in sources
                ):
                    # The ambient-blur background keeps flowing between tracks;
                    # without full-rate points here it would freeze during the
                    # silent gap.  Matches the stable-region schedule.
                    flow_steps = max(1, round(gap * AMBIENT_FLOW_HZ))
                    gap_points.update(
                        cursor + gap * step / flow_steps
                        for step in range(flow_steps + 1)
                    )
                gap_phase = "out" if previous_track is not None else "in"
                if previous_track is not None:
                    previous_intro, gap_phase_duration = (
                        ExportTimelinePlanner._animation_durations(
                            previous_track, sources,
                        )
                    )
                    # Keep the old formula explicit: the previous intro bounds
                    # how much of that track remains available to its outro.
                    gap_phase_duration = min(
                        (previous_track.duration_seconds - previous_intro) / 2,
                        gap_phase_duration,
                    )
                else:
                    gap_phase_duration = intro
                ordered_gap_points = sorted(gap_points)
                for point, next_point in zip(
                    ordered_gap_points, ordered_gap_points[1:]
                ):
                    samples.append(ExportFrameSample(
                        track=gap_track,
                        track_number=(previous_number if previous_track else number),
                        track_start_seconds=(
                            previous_start if previous_track else start
                        ),
                        duration_seconds=next_point - point,
                        elapsed_seconds=gap_elapsed,
                        timeline_seconds=min(
                            next_point - 0.0005, point + 0.0005,
                        ),
                        animation_phase=(
                            gap_phase if gap_phase_duration > 0.0 else None
                        ),
                        animation_progress=(
                            1.0 if gap_phase == "out" else 0.0
                        ),
                        animation_phase_duration=max(0.0, gap_phase_duration),
                    ))

            samples.extend(ExportTimelinePlanner._animation_samples(
                track, number, start, intro, outro, "in", intro, animation_fps,
            ))

            stable = max(0.0, track.duration_seconds - intro - outro)
            sample_points = ExportTimelinePlanner._stable_sample_points(
                track, start, intro, stable, sources, animation_fps, number,
            )
            ordered_points = sorted(sample_points)
            for point, next_point in zip(ordered_points, ordered_points[1:]):
                elapsed = min(next_point - 0.0005, point + 0.0005)
                samples.append(ExportFrameSample(
                    track=track,
                    track_number=number,
                    track_start_seconds=start,
                    duration_seconds=next_point - point,
                    elapsed_seconds=elapsed,
                    timeline_seconds=start + elapsed,
                ))

            samples.extend(ExportTimelinePlanner._animation_samples(
                track, number, start, intro, outro, "out", outro, animation_fps,
            ))
            cursor = start + track.duration_seconds
            previous_track = track
            previous_start = start
            previous_number = number
        return samples

    @staticmethod
    def _build_compiled(tracks, sources, animation_fps, plan) -> list[ExportFrameSample]:
        """Reuse the source-time sample schedule, clipped to the span each track is drawn.

        Across a crossfade/AutoMix overlap the Canvas changes hands in the overlap's
        middle (render_plan.visual_segments). Frames there carry a MixJunction, and
        the mix entrance/exit plus the screen-clock visuals after the handover (Now
        Playing exit, album-art fade) are sampled densely on the timeline.
        """
        track_by_id = {track.id: track for track in tracks}
        windows = plan.presentation.windows
        result: list[ExportFrameSample] = []
        cursor = 0.0
        for segment in visual_segments(plan):
            number = segment.window_index + 1
            window = windows[segment.window_index]
            track = track_by_id[window.track_id]
            window_start = window.timeline_start
            start = max(segment.start, window_start)
            end = min(segment.end, window.timeline_end)
            rate, source_in = window.playback_rate, window.source_time_at_start
            if start > cursor:
                # Gaps keep the last owner visible while global clocks/animations advance.
                # ponytail: dense gap samples; use sparse boundaries if long gaps dominate.
                steps = max(1, ceil((start - cursor) * animation_fps)) if sources else 1
                for step in range(steps):
                    point = cursor + (start - cursor) * step / steps
                    next_point = cursor + (start - cursor) * (step + 1) / steps
                    previous = result[-1] if result else ExportFrameSample(
                        track, number, window_start, 0.0, source_in, point,
                    )
                    result.append(replace(previous, timeline_seconds=point,
                                          elapsed_seconds=plan.presentation.local_time(point),
                                          duration_seconds=next_point - point))

            def source_time(point: float) -> float:
                return source_in + (point - window_start) * rate

            # Timeline ranges sampled densely instead of by the source-time schedule.
            mix_intro, mix_outro = mix_phase_durations(
                sources, segment_junction(segment, segment.start),
            )
            dense: list[tuple[float, float]] = []
            if segment.mixed_in:
                dense.append((start, start + mix_intro))
                dense.extend(ExportTimelinePlanner._screen_clock_ranges(sources, start))
            if segment.mixed_out:
                dense.append((end - mix_outro, end))
            dense = sorted((max(start, a), min(end, b)) for a, b in dense)
            cuts = sorted({point for pair in dense for point in pair})

            timed: list[tuple[float, ExportFrameSample]] = []
            translated_sources = [replace(
                source,
                timeline_start=(source.timeline_start - window_start) * rate + source_in,
                timeline_duration=source.timeline_duration * rate,
            ) for source in sources]
            local_samples = ExportTimelinePlanner.build(
                [replace(track, start_time_seconds=None)], translated_sources,
                max(1, ceil(animation_fps / rate)),
                track_number_offset=number - 1,
            )
            source_cursor = 0.0
            source_start, source_end = source_time(start), source_time(end)
            for sample in local_samples:
                left = max(source_start, source_cursor)
                source_cursor += sample.duration_seconds
                right = min(source_end, source_cursor)
                if right <= left:
                    continue
                a = window_start + (left - source_in) / rate
                b = window_start + (right - source_in) / rate
                edges = [a, *(cut for cut in cuts if a < cut < b), b]
                for piece_a, piece_b in zip(edges, edges[1:]):
                    if any(lo <= piece_a and piece_b <= hi for lo, hi in dense):
                        continue
                    lo, hi = source_time(piece_a), source_time(piece_b)
                    # A piece cut at a dense-range edge keeps its state just inside it.
                    nudge = min(0.0005, (hi - lo) / 2)
                    elapsed = min(hi - (nudge if piece_b < b else 0.0),
                                  max(lo + (nudge if piece_a > a else 0.0),
                                      sample.elapsed_seconds))
                    timeline = window_start + (elapsed - source_in) / rate
                    timed.append((piece_a, replace(
                        sample, track=track, track_number=number,
                        track_start_seconds=window_start,
                        duration_seconds=piece_b - piece_a, elapsed_seconds=elapsed,
                        timeline_seconds=timeline,
                        junction=segment_junction(segment, timeline),
                    )))
            covered = start
            for lo, hi in dense:
                lo = max(lo, covered)
                if hi <= lo:
                    continue
                covered = hi
                steps = max(2, round((hi - lo) * animation_fps))
                for step in range(steps):
                    # Both endpoints, like _animation_samples: an exit ends fully
                    # played at the handover rather than one frame short of it.
                    point = lo + (hi - lo) * step / (steps - 1)
                    timed.append((lo + (hi - lo) * step / steps, ExportFrameSample(
                        track, number, window_start, (hi - lo) / steps,
                        min(source_end, source_time(point)), point,
                        junction=segment_junction(segment, point),
                    )))
            result.extend(sample for _key, sample in sorted(timed, key=lambda item: item[0]))
            cursor = end
        return result

    @staticmethod
    def _screen_clock_ranges(
        sources: Sequence[Source], handover: float,
    ) -> list[tuple[float, float]]:
        """Timeline ranges after a mix handover whose visuals follow the screen
        clock (seconds since the handover): Now Playing's exit and the album-art
        background cross-fade."""
        ranges = []
        for source in sources:
            if source.source_type is SourceType.NOW_PLAYING:
                exit_start = max(0.0, source.now_playing_duration - source.now_playing_exit_duration)
                ranges.append((handover + exit_start, handover + source.now_playing_duration))
            elif (source.source_type is SourceType.BACKGROUND
                  and source.background_mode == "album_art"
                  and source.background_track_transition):
                ranges.append((handover, handover + max(0.05, source.background_track_transition_seconds)))
        return ranges

    @staticmethod
    def _animation_durations(
        track: PlaylistTrack, sources: Sequence[Source],
    ) -> tuple[float, float]:
        intro = min(
            track.duration_seconds / 2,
            max(
                (source.animation_in_duration for source in sources
                 if source.animation_in != "none"),
                default=0.0,
            ),
        )
        outro = min(
            (track.duration_seconds - intro) / 2,
            max(
                (source.animation_out_duration for source in sources
                 if source.animation_out != "none"),
                default=0.0,
            ),
        )
        return intro, outro

    @staticmethod
    def _animation_samples(
        track: PlaylistTrack,
        track_number: int,
        start: float,
        intro: float,
        outro: float,
        phase: str,
        duration: float,
        animation_fps: int,
    ) -> list[ExportFrameSample]:
        if duration <= 0:
            return []
        steps = max(2, round(duration * animation_fps))
        samples: list[ExportFrameSample] = []
        for step in range(steps):
            # Include both animation endpoints in the state *and* its elapsed
            # timestamp.  CanvasSnapshot derives each source's bounded phase
            # from elapsed time, so keeping the former ``step / steps`` time
            # here silently ignored animation_progress=1.0.  The last exit
            # frame then retained opacity before disappearing at the cut.
            progress = step / (steps - 1)
            phase_start = (
                0.0
                if phase == "in"
                else intro + max(
                    0.0, track.duration_seconds - intro - outro,
                )
            )
            elapsed = phase_start + duration * progress
            samples.append(ExportFrameSample(
                track=track,
                track_number=track_number,
                track_start_seconds=start,
                duration_seconds=duration / steps,
                elapsed_seconds=elapsed,
                timeline_seconds=start + elapsed,
                animation_phase=phase,
                animation_progress=progress,
                animation_phase_duration=duration,
            ))
        return samples

    @staticmethod
    def _stable_sample_points(
        track: PlaylistTrack,
        start: float,
        intro: float,
        stable: float,
        sources: Sequence[Source],
        animation_fps: int,
        track_number: int = 1,
    ) -> set[float]:
        sample_points = {intro, intro + stable}
        for source in sources:
            boundaries = [source.timeline_start]
            if source.timeline_duration > 0.0:
                boundaries.append(source.timeline_start + source.timeline_duration)
            for boundary in boundaries:
                local_point = boundary - start
                if intro < local_point < intro + stable:
                    sample_points.add(local_point)

        progress_bars = [
            source for source in sources
            if source.source_type is SourceType.PROGRESS_BAR
        ]
        if progress_bars and stable > 0.0:
            # Progress is continuous motion, but the rendered fill edge can never
            # move faster than the output pixel grid.  Sampling finer than one
            # bar-pixel per frame only produces Canvas frames that differ by
            # sub-pixel anti-aliasing, so they never coalesce downstream and the
            # base stream re-rasterises the ambient background for nothing.  Cap
            # the rate at the widest bar's pixel travel, floored at the ambient
            # flow rate (a short clip still needs to look smooth) and ceilinged
            # at animation_fps.  Only the Z stream containing the progress
            # element gets this schedule; static streams stay capture-invariant.
            widest = max(
                (bar.width * max(1.0, bar.scale) for bar in progress_bars),
                default=1.0,
            )
            pixel_rate = max(1.0, widest) / stable
            progress_hz = max(
                float(AMBIENT_FLOW_HZ), min(float(animation_fps), pixel_rate),
            )
            progress_steps = max(1, round(stable * progress_hz))
            sample_points.update(
                intro + stable * step / progress_steps
                for step in range(progress_steps + 1)
            )

        if any(
            source.source_type is SourceType.BACKGROUND
            and source.background_mode == "album_art"
            and source.background_ambient
            for source in sources
        ):
            # The ambient-blur background flows continuously (Apple Music
            # style).  Like the progress bar it needs a full-rate schedule or
            # export would freeze it on one capture-invariant frame.
            # AMBIENT_FLOW_HZ matches the phase quantisation used to dedupe
            # frames downstream, so a finer rate would just coalesce.
            motion = max(
                (source.background_ambient_motion for source in sources
                 if source.source_type is SourceType.BACKGROUND
                 and source.background_mode == "album_art"
                 and source.background_ambient),
                default=1.0,
            )
            flow_steps = max(1, round(
                stable * min(float(animation_fps), AMBIENT_FLOW_HZ * motion)
            ))
            sample_points.update(
                intro + stable * step / flow_steps
                for step in range(flow_steps + 1)
            )

        if any(
            source.uses_bass_reaction
            for source in sources
        ):
            bass_steps = max(1, round(stable * animation_fps))
            sample_points.update(
                intro + stable * step / bass_steps
                for step in range(bass_steps + 1)
            )

        if any(source.loop_motion != "none" for source in sources):
            # Looping idle motion never rests, so sample it at the output rate.
            loop_steps = max(1, round(stable * animation_fps))
            sample_points.update(
                intro + stable * step / loop_steps
                for step in range(loop_steps + 1)
            )

        if track_number >= 2:
            fade_seconds = max(
                (source.background_track_transition_seconds for source in sources
                 if source.source_type is SourceType.BACKGROUND
                 and source.background_mode == "album_art"
                 and source.background_track_transition),
                default=0.0,
            )
            # The album-art background cross-fades from the previous track over
            # this window at track start.  Points before ``intro`` are already
            # dense from the in-animation schedule; cover the remainder here.
            fade_end = min(fade_seconds, intro + stable)
            if fade_end > intro:
                fade_steps = max(1, round((fade_end - intro) * animation_fps))
                sample_points.update(
                    intro + (fade_end - intro) * step / fade_steps
                    for step in range(fade_steps + 1)
                )

        has_time_text = any(
            source.source_type is SourceType.TIME
            or (
                source.source_type is SourceType.TEXT
                and any(
                    token in source.text.lower()
                    for token in (
                        "%current_time%", "%track_current_time%",
                        "%video_current_time%",
                    )
                )
            )
            for source in sources
        )
        if has_time_text:
            first_local_second = max(1, int(intro) + 1)
            last_local_second = int(intro + stable)
            sample_points.update(
                float(second)
                for second in range(first_local_second, last_local_second + 1)
                if intro < second < intro + stable
            )
            first_global_second = max(1, int(start + intro) + 1)
            last_global_second = int(start + intro + stable)
            sample_points.update(
                float(second) - start
                for second in range(first_global_second, last_global_second + 1)
                if intro < float(second) - start < intro + stable
            )

        lyric_sources = [
            source for source in sources
            if source.source_type is SourceType.LYRICS
        ]
        if lyric_sources:
            track_offset = track.lyrics_timing_offset_seconds
            for source in lyric_sources:
                intervals = list(lyric_instrumental_windows(track, source)) if source.subtitle_intro_midtrack else []
                cues = [cue for cue in track.lyrics if str(cue.get("text", "")).strip()]
                if source.subtitle_karaoke_enabled:
                    offset = track_offset + source.subtitle_timing_offset
                    intervals.extend((float(cue["start"]) - offset, float(cue["end"]) - offset)
                                     for cue in cues)
                if source.subtitle_intro_enabled and cues:
                    intervals.append((0.0, float(cues[0]["start"]) - track_offset - source.subtitle_timing_offset))
                for a, b in intervals:
                    if source.subtitle_animation != "none":
                        b += source.subtitle_animation_duration
                    a, b = max(intro, a), min(intro + stable, b)
                    if b <= a:
                        continue
                    steps = max(1, ceil((b - a) * animation_fps))
                    sample_points.update(a + (b - a) * step / steps for step in range(steps + 1))
            for cue in track.lyrics:
                for point in (
                    float(cue.get("start", 0.0)),
                    float(cue.get("end", 0.0)),
                ):
                    for lyric_source in lyric_sources:
                        adjusted_point = (
                            point - track_offset
                            - lyric_source.subtitle_timing_offset
                        )
                        if intro < adjusted_point < intro + stable:
                            sample_points.add(adjusted_point)
                # A cue animates in at its start and, when a gap follows,
                # releases its highlight after its end.
                for edge in (
                    float(cue.get("start", 0.0)), float(cue.get("end", 0.0)),
                ):
                    for lyric_source in lyric_sources:
                        if lyric_source.subtitle_animation == "none":
                            continue
                        steps = max(
                            1,
                            round(
                                lyric_source.subtitle_animation_duration
                                * animation_fps
                            ),
                        )
                        for step in range(steps + 1):
                            point = (
                                edge - track_offset
                                - lyric_source.subtitle_timing_offset
                                + lyric_source.subtitle_animation_duration
                                * step / steps
                            )
                            if intro < point < intro + stable:
                                sample_points.add(point)

        for source in (
            item for item in sources
            if item.source_type is SourceType.NOW_PLAYING
        ):
            exit_start = max(
                0.0,
                source.now_playing_duration - source.now_playing_exit_duration,
            )
            steps = max(
                1, round(source.now_playing_exit_duration * animation_fps),
            )
            for step in range(steps + 1):
                point = (
                    exit_start
                    + source.now_playing_exit_duration * step / steps
                )
                if intro < point < intro + stable:
                    sample_points.add(point)
        return sample_points
