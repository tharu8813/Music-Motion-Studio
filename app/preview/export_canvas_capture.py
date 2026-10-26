"""Capture a prepared export timeline through the existing Qt Canvas renderer."""

from __future__ import annotations

from app.utils.performance import timed

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from math import floor

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage

from app.animation.curves import ease_in_out_cubic
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.album_art import AMBIENT_FLOW_HZ
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.frame_state import (
    resolve_edge_animation, resolve_lyrics_cue_state, resolve_lyrics_intro_state, resolve_now_playing_exit_state,
    resolve_lyrics_word_reveal, resolve_timeline_window_phase,
)
from app.preview.text_template import expand_track_template
from app.services.lyrics_intro_service import instrumental_spans
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.renderer.ffmpeg_renderer import RenderFrame, StaticOverlayLayer


class ExportCanvasCapturer:
    """Turn planned Canvas states into the existing disk-backed frame streams."""

    def __init__(
        self,
        scene: CanvasScene,
        tracks: Sequence[PlaylistTrack],
        playlist_duration: float,
        dynamic_source_ids: set[str],
        z_bands: Sequence[tuple[float | None, float | None]],
        stage_frame: Callable[[QImage, float, str], RenderFrame],
        ensure_not_cancelled: Callable[[], None],
        after_capture: Callable[[int, str], None],
        *,
        retain_static_frames: bool = True,
        output_scale: float = 1.0,
        bass_envelopes: dict[object, object] | None = None,
        bass_fps: int = 30,
        output_fps: int | None = None,
        instrumental_spans_by_track: dict[str, dict[str, tuple]] | None = None,
    ) -> None:
        self.scene = scene
        self.tracks = list(tracks)
        self.playlist_duration = playlist_duration
        self.dynamic_source_ids = set(dynamic_source_ids)
        self.z_bands = list(z_bands)
        # 1.0 keeps the historic artboard-resolution capture; export passes the
        # output/artboard ratio so the vector scene is rasterised at final size.
        self.output_scale = max(1.0, float(output_scale))
        self.stage_frame = stage_frame
        self.ensure_not_cancelled = ensure_not_cancelled
        self.after_capture = after_capture
        self.retain_static_frames = retain_static_frames
        self.bass_envelopes = bass_envelopes or {}
        self.bass_fps = max(1, int(bass_fps))
        self.output_fps = output_fps
        self.instrumental_spans_by_track = instrumental_spans_by_track
        self._stream_seconds: dict[str, float] = {}
        self._last_stream_images: dict[str, QImage] = {}
        self.skipped_cfr_captures = 0
        self._static_band_frames: list[list[RenderFrame]] = [
            [] for _band in self.z_bands[1:]
        ]
        self.invariant_stream_keys = CanvasSnapshot.invariant_stream_keys(
            self.scene,
            self.dynamic_source_ids,
            self.z_bands,
            self.playlist_duration,
        )
        self._invariant_images: dict[str, QImage] = {}
        visible_items = [
            item for item in self.scene.items()
            if isinstance(item, SourceItem)
            and item.isVisible()
            and item.source.visible
        ]
        self._stream_source_items: dict[str, list[SourceItem]] = {}
        for index, (z_min, z_max) in enumerate(self.z_bands):
            stream_key = "base" if index == 0 else f"layer:{index - 1}"
            self._stream_source_items[stream_key] = [
                item for item in visible_items
                if (z_min is None or item.source.z_index >= z_min)
                and (z_max is None or item.source.z_index <= z_max)
            ]
        self._stream_capture_rects: dict[str, QRectF] = {}
        for index, (z_min, z_max) in enumerate(self.z_bands[1:]):
            capture_rect = CanvasSnapshot.band_capture_envelope(
                self.scene, z_min, z_max,
            )
            if capture_rect is not None:
                self._stream_capture_rects[f"layer:{index}"] = capture_rect
        self.partial_render_capture_count = 0
        self.scene_render_source_pixels = 0
        self.full_frame_source_pixels = 0

    def coalesce_samples(
        self,
        samples: Sequence[ExportFrameSample],
        stream_key: str,
    ) -> list[ExportFrameSample]:
        return list(self.iter_coalesced_samples(samples, stream_key))

    def iter_coalesced_samples(
        self, samples: Iterable[ExportFrameSample], stream_key: str,
        wait_callback: Callable[[], None] | None = None,
    ) -> Iterator[ExportFrameSample]:
        """Merge adjacent samples whose complete Canvas state is identical.

        Only consecutive samples are combined.  A ``None`` state key is a
        conservative opt-out: an element whose state cannot be proven equal
        continues through the established one-capture-per-sample path.
        """
        previous = None
        previous_key: tuple[object, ...] | None = None
        for index, sample in enumerate(samples):
            if wait_callback is not None and index % 256 == 0:
                wait_callback()
                self.ensure_not_cancelled()
            state_key = self._stream_state_key(sample, stream_key)
            if (
                previous is not None
                and state_key is not None
                and previous_key is not None
                and state_key == previous_key
            ):
                previous = replace(
                    previous,
                    duration_seconds=(
                        previous.duration_seconds + sample.duration_seconds
                    ),
                )
            else:
                if previous is not None:
                    yield previous
                previous = sample
            previous_key = state_key
        if previous is not None:
            yield previous

    def _stream_state_key(
        self, sample: ExportFrameSample, stream_key: str,
    ) -> tuple[object, ...] | None:
        """Return the resolved visual state of every element in one Z stream."""
        _z_min, _z_max, _transparent, _retained = self._stream_parameters(stream_key)
        source_keys: list[tuple[object, ...]] = []
        for item in self._stream_source_items.get(stream_key, []):
            source = item.source
            if (
                not item.isVisible()
                or not source.visible
                or source.id in self.dynamic_source_ids
            ):
                continue
            source_key = self._source_state_key(source, sample)
            if source_key is None:
                return None
            source_keys.append(source_key)
        return tuple(source_keys)

    def _source_state_key(
        self, source: Source, sample: ExportFrameSample,
    ) -> tuple[object, ...] | None:
        """Resolve the sample-dependent pixels of one Canvas source."""
        source = source.resolved_lyrics()
        global_seconds = max(0.0, sample.timeline_seconds)
        timing_end = source.timeline_start + source.timeline_duration
        visible = not (
            global_seconds < source.timeline_start
            or (
                source.timeline_duration > 0.0
                and global_seconds >= timing_end
            )
        )
        if not visible:
            return (source.id, "hidden")

        # Same screen clock as CanvasSnapshot.capture_track: after a mix handover the
        # track-start visuals (Now Playing, background fade) run from the handover.
        screen_elapsed = (
            sample.junction.visual_elapsed
            if sample.junction is not None and sample.junction.visual_elapsed is not None
            else sample.elapsed_seconds
        )
        content_state: tuple[object, ...]
        if source.source_type in {SourceType.TEXT, SourceType.TIME}:
            template = (
                source.text
                if "%" in source.text
                else "%current_time%"
                if source.source_type is SourceType.TIME
                else source.text
            )
            content_state = (
                "text",
                expand_track_template(
                    template,
                    sample.track,
                    sample.track_number,
                    len(self.tracks),
                    global_seconds - sample.elapsed_seconds,
                    sample.elapsed_seconds,
                    self.playlist_duration,
                ),
            )
        elif source.source_type is SourceType.LYRICS:
            cue_state = resolve_lyrics_cue_state(
                sample.track, sample.elapsed_seconds,
                sample.track.lyrics_timing_offset_seconds, source.subtitle_timing_offset,
                source.subtitle_animation, source.subtitle_animation_duration,
            )
            transition = cue_state.transition_progress if cue_state.transitioning else None
            intro_state = resolve_lyrics_intro_state(sample.track, source, sample.elapsed_seconds,
                instrumental_spans(sample.track, source) if self.instrumental_spans_by_track is None
                else self.instrumental_spans_by_track[sample.track.id][source.id])
            content_state = (
                "lyrics", id(sample.track), cue_state.cue_index, cue_state.active_cue_index,
                transition, cue_state.release_progress,
                intro_state,
                resolve_lyrics_word_reveal(sample.track, source, sample.elapsed_seconds, cue_state, intro_state),
            )
        elif source.source_type is SourceType.TRACK_LIST:
            content_state = ("track_list", sample.track_number)
        elif source.source_type is SourceType.NOW_PLAYING:
            exit_state = resolve_now_playing_exit_state(
                screen_elapsed, source.now_playing_duration, source.now_playing_exit_duration,
            )
            if not exit_state.visible:
                content_state = ("now_playing", "hidden")
            else:
                content_state = (
                    "now_playing",
                    sample.track.file_path,
                    sample.track.title,
                    sample.track.artist,
                    sample.track.album,
                    exit_state.exit_progress,
                )
        elif source.source_type is SourceType.PROGRESS_BAR:
            progress = (
                global_seconds / max(0.01, self.playlist_duration)
                if source.progress_mode == "video"
                else sample.elapsed_seconds
                / max(0.01, sample.track.duration_seconds)
            )
            content_state = ("progress", max(0.0, min(1.0, progress)))
        elif source.source_type is SourceType.ALBUM_COVER:
            content_state = (
                "cover",
                source.content_path
                or (sample.track.file_path, sample.track.cover_path),
            )
        elif source.source_type is SourceType.BACKGROUND:
            if source.background_mode == "album_art":
                background_state: object = (
                    sample.track.file_path,
                    sample.track.cover_path,
                    source.background_ambient,
                    source.background_ambient_blur,
                    source.background_ambient_motion,
                    source.background_bass_reactive,
                    source.background_bass_strength,
                )
                if source.background_ambient:
                    # The ambient colour field drifts, so a new frame is needed
                    # at the flow rate even while the track stays the same.
                    background_state = (
                        *background_state,
                        round(
                            global_seconds * source.background_ambient_motion
                            * AMBIENT_FLOW_HZ
                        ),
                    )
                previous_track = (
                    self.tracks[sample.track_number - 2]
                    if (source.background_track_transition
                        and 2 <= sample.track_number <= len(self.tracks))
                    else None
                )
                fade_seconds = max(
                    0.05, source.background_track_transition_seconds,
                )
                if (previous_track is not None
                        and 0.0 <= screen_elapsed < fade_seconds):
                    # Mid cross-fade the pixels depend on the blend fraction and
                    # the previous track's artwork, so those must enter the key.
                    background_state = (
                        *background_state,
                        "fade",
                        ease_in_out_cubic(max(0.0, min(
                            1.0, screen_elapsed / fade_seconds,
                        ))),
                        previous_track.file_path,
                        previous_track.cover_path,
                    )
            else:
                background_state = source.background_mode
            content_state = ("background", background_state)
        elif source.source_type in {
            SourceType.IMAGE,
            SourceType.SHAPE,
            SourceType.LOGO,
            SourceType.WATERMARK,
        }:
            content_state = ("static",)
        elif source.source_type in {
            SourceType.VIDEO,
            SourceType.AUDIO_VISUALIZER,
            SourceType.AUDIO_WAVEFORM,
            SourceType.AUDIO_LEVEL_METER,
            SourceType.PARTICLE_OVERLAY,
        }:
            # These should normally be hidden and rendered by FFmpeg.  Opt out
            # if an unexpected caller includes one in a Canvas stream.
            return None
        else:
            return None

        animation_state: tuple[object, ...] = ("stable",)
        source_has_window = source.timeline_start > 0.0 or source.timeline_duration > 0.0
        if source_has_window:
            window_phase, window_progress = resolve_timeline_window_phase(
                source, global_seconds,
            )
            style = (
                source.animation_in if window_phase == "in" else source.animation_out
            )
            if window_phase is not None and style != "none":
                animation_state = (
                    window_phase, style, max(0.0, min(1.0, window_progress)),
                )
        else:
            edge = resolve_edge_animation(
                source, sample.animation_phase,
                elapsed_seconds=sample.elapsed_seconds,
                track_duration=sample.track.duration_seconds,
                phase_progress=sample.animation_progress,
                phase_duration=sample.animation_phase_duration,
                junction=sample.junction,
            )
            if edge is not None:
                animation_state = edge
        if source.loop_motion != "none":
            # A looping motion changes every frame; key on the exact time.
            animation_state = (*animation_state, "loop", round(global_seconds, 4))
        if source.uses_bass_reaction:
            animation_state = (*animation_state, "bass", round(global_seconds, 4))
        personal_color_state = (
            (
                sample.track.file_path,
                sample.track.cover_path,
                source.personal_color_brightness,
                source.personal_color_saturation,
                source.personal_color_hue_shift,
                source.personal_color_strength,
            )
            if source.uses_personal_color else None
        )
        return (source.id, content_state, personal_color_state, animation_state)

    def _stream_parameters(
        self, stream_key: str,
    ) -> tuple[float | None, float | None, bool, int | None]:
        """Resolve a public stream key into its capture band parameters."""
        if stream_key == "base":
            return None, self.z_bands[0][1], False, None
        if stream_key.startswith("layer:"):
            try:
                retained_layer_index = int(stream_key.removeprefix("layer:"))
                if retained_layer_index < 0:
                    raise IndexError
                z_min, z_max = self.z_bands[retained_layer_index + 1]
            except (ValueError, IndexError) as error:
                raise ValueError(f"Unknown Canvas stream: {stream_key}") from error
            return z_min, z_max, True, retained_layer_index
        raise ValueError(f"Unknown Canvas stream: {stream_key}")

    def capture(self, sample: ExportFrameSample) -> RenderFrame:
        """Stage one opaque base frame and its transparent foreground bands."""
        base = self.capture_stream(sample, "base")
        for index, _band in enumerate(self.z_bands[1:]):
            self.capture_stream(sample, f"layer:{index}")
        return base

    @timed("export.frame_generation_seconds")
    def capture_stream(
        self, sample: ExportFrameSample, stream_key: str,
    ) -> RenderFrame:
        """Capture one band, reusing only conservatively proven static pixels."""
        self.ensure_not_cancelled()
        image = self._invariant_images.get(stream_key)
        if self.output_fps is not None:
            previous_seconds = self._stream_seconds.get(stream_key, 0.0)
            seconds = previous_seconds + sample.duration_seconds
            self._stream_seconds[stream_key] = seconds
            if (
                stream_key in self._last_stream_images
                and max(1, floor(seconds * self.output_fps + 0.5))
                == max(1, floor(previous_seconds * self.output_fps + 0.5))
            ):
                # The encoder emits no frame for this state; keep every duration
                # unchanged so cumulative rounding and the next image stay exact.
                image = self._last_stream_images[stream_key]
                self.skipped_cfr_captures += 1
        common: dict[str, object] = {
            "elapsed_seconds": sample.elapsed_seconds,
            "hide_visualizers": self.dynamic_source_ids,
            "playlist_duration_seconds": self.playlist_duration,
            "playlist_tracks": self.tracks,
            "timeline_seconds": sample.timeline_seconds,
            "junction": sample.junction,
            "bass_level": self._bass_level(sample),
            "music_levels": self._music_reaction_levels(sample, stream_key),
        }
        if sample.animation_phase is not None:
            common.update({
                "animation_phase": sample.animation_phase,
                "animation_progress": sample.animation_progress,
                "animation_phase_duration": sample.animation_phase_duration,
            })
        if self.instrumental_spans_by_track is not None:
            common['lyric_instrumental_spans_by_source'] = self.instrumental_spans_by_track[sample.track.id]
        z_min, z_max, transparent, retained_layer_index = (
            self._stream_parameters(stream_key)
        )

        if image is None:
            render_metrics: dict[str, object] = {}
            fixed_capture_rect = self._stream_capture_rects.get(stream_key)
            image = CanvasSnapshot.capture_track(
                self.scene,
                sample.track,
                sample.track_number,
                len(self.tracks),
                sample.track_start_seconds,
                z_min=z_min,
                z_max=z_max,
                transparent=transparent,
                partial_render=(
                    transparent
                    and fixed_capture_rect is None
                    and stream_key not in self.invariant_stream_keys
                ),
                render_metrics=render_metrics,
                capture_rect=fixed_capture_rect,
                band_source_items=self._stream_source_items.get(stream_key),
                output_scale=self.output_scale,
                **common,
            )
            artboard = self.scene.artboard_rect
            full_frame_pixels = max(
                1, round(artboard.width()) * round(artboard.height()),
            )
            self.full_frame_source_pixels += full_frame_pixels
            used_partial_render = (
                fixed_capture_rect is not None
                or bool(render_metrics.get("partial_render"))
            )
            if used_partial_render:
                capture_rect = fixed_capture_rect or render_metrics.get("capture_rect")
                if hasattr(capture_rect, "width") and hasattr(capture_rect, "height"):
                    self.partial_render_capture_count += 1
                    self.scene_render_source_pixels += max(
                        1,
                        round(float(capture_rect.width()))
                        * round(float(capture_rect.height())),
                    )
                else:
                    self.scene_render_source_pixels += full_frame_pixels
            else:
                self.scene_render_source_pixels += full_frame_pixels
            if stream_key in self.invariant_stream_keys:
                # QImage is implicitly shared and safe for the background
                # encoder to read.  Keep a detached copy so later scene paints
                # cannot alter the cached pixels through a reused buffer.
                image = image.copy()
                self._invariant_images[stream_key] = image
        if self.output_fps is not None:
            self._last_stream_images[stream_key] = image
        rendered = self.stage_frame(
            image,
            sample.duration_seconds,
            stream_key,
        )
        if retained_layer_index is not None and self.retain_static_frames:
            self._static_band_frames[retained_layer_index].append(rendered)
        self.after_capture(sample.track_number, stream_key)
        return rendered

    def _music_reaction_levels(self, sample: ExportFrameSample, stream_key: str) -> dict[str, float]:
        return {
            item.source.id: PythonVisualizerRenderer.music_reaction_level(
                self.bass_envelopes.get((sample.track.id, item.source.music_reaction_profile),
                                        self.bass_envelopes.get(sample.track.id)),
                self.bass_fps, sample.elapsed_seconds, item.source.music_reactive_offset,
            )
            for item in self._stream_source_items[stream_key]
            if item.source.source_type in {SourceType.TEXT, SourceType.LYRICS}
            and item.source.uses_bass_reaction
        }

    def _bass_level(self, sample: ExportFrameSample) -> float:
        envelope = self.bass_envelopes.get(sample.track.id)
        if envelope is None:
            return 0.0
        try:
            index = max(0, round(sample.elapsed_seconds * self.bass_fps))
            return float(envelope[min(index, len(envelope) - 1)])
        except (IndexError, TypeError, ValueError):
            return 0.0

    def capture_invariant_stream(
        self,
        sample: ExportFrameSample,
        stream_key: str,
        duration_seconds: float,
    ) -> RenderFrame:
        """Capture a safe stream once and stage it for the complete timeline."""
        if stream_key not in self.invariant_stream_keys:
            raise ValueError(f"Canvas stream is not capture-invariant: {stream_key}")
        return self.capture_stream(
            replace(sample, duration_seconds=duration_seconds), stream_key,
        )

    def static_layers(self) -> list[StaticOverlayLayer]:
        """Return transparent bands in the same Z and timeline order as capture."""
        return [
            StaticOverlayLayer(
                z_min if z_min is not None else -10_000.0,
                frames,
                *self.stream_origin(f"layer:{index}"),
            )
            for index, ((z_min, _z_max), frames) in enumerate(zip(
                self.z_bands[1:], self._static_band_frames, strict=True,
            ))
            if frames
        ]

    def stream_origin(self, stream_key: str) -> tuple[int, int]:
        """Return the cropped stream's top-left position on the artboard."""
        capture_rect = self._stream_capture_rects.get(stream_key)
        if capture_rect is None:
            return 0, 0
        artboard = self.scene.artboard_rect
        return (
            round((capture_rect.left() - artboard.left()) * self.output_scale),
            round((capture_rect.top() - artboard.top()) * self.output_scale),
        )
