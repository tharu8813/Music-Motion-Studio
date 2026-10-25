"""FFmpeg-based static playlist video rendering pipeline."""

from __future__ import annotations

from app.utils.performance import timed

import json
import math
import shutil
import subprocess
import sys
import threading
import os
import logging
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp

from PySide6.QtGui import QImage, QImageReader

from app.models.playlist import PlaylistTrack
from app.timeline.compiler import compile_playlist
from app.timeline.render_plan import (
    CompiledRenderPlan, reactive_layer_windows,
)
from app.renderer import loudness
from app.renderer.ffmpeg import filter_graph
from app.renderer.python_visualizer import PythonVisualizerError, PythonVisualizerRenderer
from app.utils.subprocess_utils import hidden_process_kwargs
from app.services.export_validation_service import ExportValidationResult
from app.services.export_policy import ExportPolicy
from app.services.export_formats import (
    VIDEO_FORMATS, AUDIO_FORMATS, AUDIO_ENCODERS, is_audio_export,
    audio_encoding_arguments, video_container_arguments,
)
from app.services.export_validation_service import validate_audio_export_output


LOGGER = logging.getLogger(__name__)

WORK_MODE_STABLE = "stable"
WORK_MODE_AUTO = "auto"
WORK_MODE_MAX_SPEED = "max_speed"
WORK_MODES = (WORK_MODE_STABLE, WORK_MODE_AUTO, WORK_MODE_MAX_SPEED)

_EBUR128_SUMMARY = re.compile(
    r"Integrated loudness:\s+I:\s+(?P<i>-?(?:[\d.]+|inf)) LUFS\s+Threshold:\s+(?P<threshold>-?[\d.]+) LUFS"
    r".*?LRA:\s+(?P<lra>-?[\d.]+) LU"
    r".*?True peak:\s+Peak:\s+(?P<true_peak>-?(?:[\d.]+|inf)) dBFS",
    re.S,
)


class FFmpegNotFoundError(RuntimeError):
    """Raised when no runnable FFmpeg executable can be found."""


class RenderError(RuntimeError):
    """Raised when FFmpeg rejects an input or cannot produce the video."""


class EncoderUnavailableError(RenderError):
    """Raised when the requested video encoder cannot start on this computer."""


class RenderCancelledError(RenderError):
    """Raised after a requested render cancellation safely stops FFmpeg."""


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """Video and audio settings used for the Phase 3B renderer."""

    fps: int = 30
    video_codec: str = "libx264"
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    output_width: int = 1920
    output_height: int = 1080
    work_mode: str = WORK_MODE_AUTO


@dataclass(frozen=True, slots=True)
class RenderResult:
    """Summary of a successfully generated video."""

    output_path: Path
    track_count: int
    validation: ExportValidationResult | None = None


@dataclass(frozen=True, slots=True)
class ExportMetadata:
    """Container tags and per-track chapters written into the output MP4."""

    title: str = ""
    artist: str = ""
    comment: str = ""
    include_chapters: bool = True


@dataclass(frozen=True, slots=True)
class RenderFrame:
    """One Canvas image and its exact on-screen duration."""

    image: QImage | Path
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PreparedVideoInput:
    """A CFR Canvas stream prepared without a PNG image sequence."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: int
    ready_for_mux: bool = False
    encoded_codec: str = ""


@dataclass(frozen=True, slots=True)
class PipedVideoInput:
    """A Canvas stream captured live into the final FFmpeg through a pipe.

    ``input_arguments`` are the complete FFmpeg input options, ending in
    ``-i <pipe>``; nothing is written to disk for this input.
    """

    input_arguments: tuple[str, ...]
    duration_seconds: float
    width: int
    height: int
    fps: int


@dataclass(frozen=True, slots=True)
class PipedStaticOverlayLayer:
    """A transparent Canvas Z band streamed live as RGBA through a pipe."""

    z_index: float
    video: PipedVideoInput
    x: int = 0
    y: int = 0


@dataclass(frozen=True, slots=True)
class StaticOverlayLayer:
    """A transparent, time-synchronised Canvas Z band for compositing."""

    z_index: float
    frames: list[RenderFrame]
    x: int = 0
    y: int = 0


@dataclass(frozen=True, slots=True)
class PreparedStaticOverlayLayer:
    """A lossless static Z band prepared as colour and alpha video tracks."""

    z_index: float
    video: PreparedVideoInput
    x: int = 0
    y: int = 0


@dataclass(frozen=True, slots=True)
class VisualizerOverlay:
    """One Python-rendered dynamic overlay layer for export."""

    x: int
    y: int
    width: int
    height: int
    style: str
    color: str
    personal_colors: tuple[str, ...] = ()
    opacity: float = 1.0
    bar_count: int = 32
    line_width: float = 3.0
    sensitivity: float = 1.2
    reactivity: float = 0.22
    noise_gate: float = 0.003
    min_level: float = 0.0
    max_level: float = 0.96
    attack: float = 0.55
    release: float = 0.16
    smoothing: float = 0.18
    curve: float = 0.9
    kind: str = "visualizer"
    effect_style: str = "bars"
    density: int = 42
    speed: float = 1.0
    level_meter_mode: str = "stereo"
    level_meter_style: str = "gradient"
    level_meter_orientation: str = "vertical"
    level_meter_sensitivity: float = 1.2
    level_meter_attack: float = 0.65
    level_meter_release: float = 0.18
    level_meter_min_level: float = 0.0
    level_meter_max_level: float = 1.0
    level_meter_segments: int = 16
    level_meter_gap: float = 4.0
    level_meter_show_peak: bool = True
    level_meter_peak_hold: float = 0.35
    level_meter_peak_decay: float = 0.7
    level_meter_track_color: str = "#263244"
    level_meter_low_color: str = "#22C55E"
    level_meter_mid_color: str = "#FACC15"
    level_meter_high_color: str = "#EF4444"
    particle_min_size: float = 1.0
    particle_max_size: float = 4.0
    particle_opacity: float = 0.62
    particle_direction: float = -90.0
    particle_drift: float = 0.25
    particle_twinkle: float = 0.25
    particle_glow: float = 0.2
    particle_secondary_color: str = "#7DD3FC"
    particle_seed: int = 17
    rotation: float = 0.0
    z_index: float = 0.0
    timeline_start: float = 0.0
    timeline_duration: float = 0.0
    animation_in: str = "none"
    animation_out: str = "none"
    animation_in_duration: float = 0.45
    animation_out_duration: float = 0.45
    animation_fit_mix: bool = False
    loop_motion: str = "none"
    loop_motion_period: float = 4.0
    loop_motion_amount: float = 1.0
    mask_shape: str = "none"
    inner_radius: float = 0.55


@dataclass(frozen=True, slots=True)
class VideoClipOverlay:
    """One scheduled video-file interval composited as a Canvas source."""

    path: Path
    timeline_start: float
    duration_seconds: float
    media_start_seconds: float
    x: int
    y: int
    width: int
    height: int
    z_index: float
    rotation: float = 0.0
    opacity: float = 1.0
    fit_mode: str = "cover"
    fill_color: str = "#000000"
    border_radius: float = 0.0
    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 1.0
    grayscale: bool = False
    blur: float = 0.0
    speed: float = 1.0
    loop_input: bool = False
    animation_in: str = "none"
    animation_out: str = "none"
    # Absolute start/end and effective in/out duration of each visual ownership window.
    animation_windows: tuple[tuple[float, float, float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class VideoFileInput:
    """One physical FFmpeg input shared by matching scheduled occurrences."""

    path: Path
    media_start_seconds: float
    duration_seconds: float
    loop_input: bool = False


class FFmpegRenderer:
    """Normalizes tracks, concatenates them with FFmpeg, then renders an MP4."""

    def __init__(self, executable: str | Path | None = None) -> None:
        self.executable = self.find_executable(executable)
        self._available_encoders: frozenset[str] | None = None

    @staticmethod
    def find_executable(configured_path: str | Path | None = None) -> Path:
        """Resolve a configured FFmpeg executable or one available on PATH."""
        if configured_path:
            candidate = Path(configured_path)
            if candidate.is_file():
                return candidate
        bundled_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
        candidates = [
            Path(__file__).resolve().parents[2] / "ffmpeg" / "bin" / "ffmpeg.exe",
        ]
        if bundled_root is not None:
            candidates.insert(0, bundled_root / "ffmpeg" / "bin" / "ffmpeg.exe")
        for project_ffmpeg in candidates:
            if project_ffmpeg.is_file():
                return project_ffmpeg
        found = shutil.which("ffmpeg")
        if found:
            return Path(found)
        raise FFmpegNotFoundError("FFmpeg executable was not found.")

    @timed("export.renderer_seconds")
    def render(self, image: QImage | list[QImage] | list[RenderFrame] | PreparedVideoInput
               | PipedVideoInput,
               tracks: list[PlaylistTrack], output_path: str | Path,
               settings: RenderSettings | None = None,
               progress_callback: Callable[[str, float, str], None] | None = None,
               cancel_event: threading.Event | None = None,
               visualizers: list[VisualizerOverlay] | None = None,
               static_layers: list[
                   StaticOverlayLayer | PreparedStaticOverlayLayer | PipedStaticOverlayLayer
               ] | None = None,
               video_clips: list[VideoClipOverlay] | None = None,
               metadata: "ExportMetadata | None" = None,
               storage_path_callback: Callable[[str, Path | None], None] | None = None,
               transition_mode: str = "none",
               crossfade_seconds: float = 3.0,
               compiled_plan: CompiledRenderPlan | None = None,
               prepared_audio_path: Path | None = None,
               automix_settings: "AutoMixTransitionSettings | None" = None,
               ) -> RenderResult:
        """Create a static Canvas video whose audio is the ordered enabled playlist.

        Audio, Canvas duration and chapters consume the same resolved plan.
        UI exports supply the prepared audio and plan before capturing frames.
        """
        cancel_event = cancel_event or threading.Event()
        if is_audio_export(output_path):
            return self.render_audio(
                tracks, output_path, settings or RenderSettings(),
                progress_callback=progress_callback, cancel_event=cancel_event,
                metadata=metadata, storage_path_callback=storage_path_callback,
                transition_mode=transition_mode, crossfade_seconds=crossfade_seconds,
                automix_settings=automix_settings,
            )
        if (compiled_plan is None) != (prepared_audio_path is None):
            raise RenderError("Prepared audio and its compiled plan must be supplied together.")
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        active_tracks = [track for track in tracks if track.enabled]
        if not active_tracks:
            raise RenderError("Select at least one playlist track before exporting.")
        invalid_track = next(
            (track for track in active_tracks if track.duration_seconds <= 0.0), None
        )
        if invalid_track is not None:
            raise RenderError(
                f"Audio duration could not be determined: {invalid_track.title}"
            )
        visualizers = visualizers or []
        video_clips = video_clips or []
        video_file_inputs, video_input_slots = self._video_input_plan(video_clips)
        static_layers = static_layers or []
        piped_video = image if isinstance(image, PipedVideoInput) else None
        piped = piped_video is not None or any(
            isinstance(layer, PipedStaticOverlayLayer) for layer in static_layers
        )
        if piped and progress_callback is not None:
            progress_callback = self._piped_progress(
                progress_callback, bool(visualizers),
            )
        prepared_video = image if isinstance(image, PreparedVideoInput) else None
        supplied_frames = (
            [] if prepared_video is not None or piped_video is not None
            else list(image) if isinstance(image, list) else [image]
        )
        if prepared_video is None and piped_video is None and not supplied_frames:
            raise RenderError("No Canvas frames were supplied for export.")
        explicit_frames = bool(
            supplied_frames and isinstance(supplied_frames[0], RenderFrame)
        )
        if explicit_frames and not all(
            isinstance(frame, RenderFrame) for frame in supplied_frames
        ):
            raise RenderError("Export frames must use one consistent frame format.")
        frames = (
            [frame.image for frame in supplied_frames]
            if explicit_frames else supplied_frames
        )
        durations = (
            [frame.duration_seconds for frame in supplied_frames]
            if explicit_frames else []
        )
        if explicit_frames and any(duration <= 0 for duration in durations):
            raise RenderError("Each Canvas frame duration must be greater than zero.")
        if (prepared_video is None and piped_video is None and not explicit_frames
                and len(frames) not in {1, len(active_tracks)}):
            raise RenderError(
                "The number of Canvas frames does not match the enabled playlist tracks."
            )
        # Legacy callers may supply one plain QImage for every track. Explicit
        # RenderFrame input already carries its own exact duration and may
        # intentionally represent an entire multi-track playlist with one image.
        if (
            prepared_video is None
            and piped_video is None
            and not explicit_frames
            and len(frames) == 1
            and len(active_tracks) > 1
        ):
            frames *= len(active_tracks)
        self._report(
            progress_callback, "Preparing export", 0.005,
            (
                "Validating lossless Canvas stream"
                if prepared_video is not None or piped_video is not None
                else f"Validating visual frames 0/{len(frames)}"
            ),
        )
        if piped_video is not None:
            if (piped_video.duration_seconds <= 0.0
                    or piped_video.width <= 0 or piped_video.height <= 0
                    or not piped_video.input_arguments):
                raise RenderError("The piped Canvas stream is invalid.")
        elif prepared_video is not None:
            if (not prepared_video.path.is_file()
                    or prepared_video.duration_seconds <= 0.0
                    or prepared_video.width <= 0 or prepared_video.height <= 0):
                raise RenderError("The prepared Canvas video is missing or invalid.")
        else:
            self._validate_frames(
                frames,
                lambda completed, total: self._report(
                    progress_callback, "Preparing export",
                    0.005 + 0.015 * completed / max(1, total),
                    f"Validating visual frames {completed}/{total}",
                ),
            )
        missing = [track.file_path for track in active_tracks if not Path(track.file_path).is_file()]
        if missing:
            raise RenderError(f"Audio file is missing: {missing[0]}")
        selected_settings = settings or RenderSettings()
        self._validate_settings(selected_settings)
        if piped_video is not None and piped_video.fps != selected_settings.fps:
            raise RenderError(
                "The piped Canvas stream frame rate does not match export settings."
            )
        if prepared_video is not None and prepared_video.fps != selected_settings.fps:
            raise RenderError(
                "The prepared Canvas video frame rate does not match export settings."
            )
        direct_mux = bool(
            prepared_video is not None
            and prepared_video.ready_for_mux
            and not visualizers
            and not video_clips
            and not static_layers
        )
        if prepared_video is not None and prepared_video.ready_for_mux:
            if not direct_mux:
                raise RenderError(
                    "A directly encoded Canvas stream cannot be used with additional visual layers."
                )
            if prepared_video.encoded_codec != selected_settings.video_codec:
                raise RenderError(
                    "The prepared Canvas video codec does not match the selected encoder."
                )
            if (
                prepared_video.width != selected_settings.output_width
                or prepared_video.height != selected_settings.output_height
            ):
                raise RenderError(
                    "The prepared Canvas video resolution does not match the export settings."
                )
        if not direct_mux:
            self.ensure_encoder_available(selected_settings.video_codec)
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        self._report(progress_callback, "Preparing export", 0.02, "Preparing temporary files")
        target = Path(output_path).expanduser().resolve()
        if target.suffix.lower().lstrip(".") not in VIDEO_FORMATS:
            target = target.with_suffix(".mp4")
        target.parent.mkdir(parents=True, exist_ok=True)
        with (TemporaryDirectory(prefix="playlist-video-") as temporary_directory,
              ExitStack() as visualizer_streams):
            temporary = Path(temporary_directory)
            if storage_path_callback:
                storage_path_callback("render", temporary)
            resolved_plans = []
            if prepared_audio_path is None and transition_mode != "none":
                prepared_audio_path = self.prepare_playlist_audio(
                    active_tracks, temporary, selected_settings,
                    transition_mode=transition_mode, crossfade_seconds=crossfade_seconds,
                    progress_callback=progress_callback, cancel_event=cancel_event,
                    plan_callback=resolved_plans.append, automix_settings=automix_settings,
                )
                compiled_plan = resolved_plans[0]
            compiled_plan = compiled_plan or compile_playlist(active_tracks)
            if prepared_audio_path is not None and not prepared_audio_path.is_file():
                raise RenderError("The prepared playlist audio is missing.")
            metadata_path = self._write_export_ffmetadata(
                temporary, active_tracks, metadata or ExportMetadata(), target, compiled_plan,
            )
            if piped_video is not None:
                visual_sequence = [
                    (Path("canvas-pipe"), piped_video.duration_seconds),
                ]
                base_input_arguments = list(piped_video.input_arguments)
            elif prepared_video is not None:
                prepared_path = prepared_video.path.resolve()
                visual_sequence = [
                    (prepared_path, prepared_video.duration_seconds),
                ]
                base_input_arguments = [
                    # Offload the lossless intermediate's decode to any working
                    # GPU decoder; FFmpeg falls back to software when the RGB /
                    # 4:4:4 stream has no hardware path, and downloads frames to
                    # system memory automatically before the software filters.
                    "-hwaccel", "auto",
                    "-i", str(prepared_path),
                ]
            else:
                frame_paths: list[Path] = []
                for index, frame in enumerate(frames):
                    if cancel_event.is_set():
                        raise RenderCancelledError("Rendering was cancelled.")
                    if isinstance(frame, Path):
                        frame_path = frame.resolve()
                        if not frame_path.is_file():
                            raise RenderError(
                                f"A staged export frame is missing: {frame_path}"
                            )
                    else:
                        frame_path = temporary / f"canvas_{index:04d}.png"
                        if not frame.save(str(frame_path), "PNG"):
                            raise RenderError(
                                "Could not create a temporary Canvas image."
                            )
                    frame_paths.append(frame_path)
                visual_sequence = (
                    list(zip(frame_paths, durations, strict=True))
                    if explicit_frames
                    else self._visual_sequence(active_tracks, frame_paths, compiled_plan)
                )
                base_input_arguments = []
            total_duration = compiled_plan.duration_seconds
            self._validate_visual_timeline(
                visual_sequence, static_layers, total_duration, selected_settings.fps,
            )
            audio_path = prepared_audio_path or self.prepare_playlist_audio(
                active_tracks, temporary, selected_settings,
                transition_mode=transition_mode, crossfade_seconds=crossfade_seconds,
                progress_callback=progress_callback, cancel_event=cancel_event,
                automix_settings=automix_settings,
            )
            visualizer_inputs: list[list[str]] = []
            if visualizers:
                self._report(progress_callback, "Preparing visualizers", 0.64,
                             "Analyzing audio for visualizers")

                def visualizer_progress(fraction: float, message: str) -> None:
                    self._report(progress_callback, "Preparing visualizers", 0.64 + fraction * 0.12,
                                 message)

                try:
                    track_windows = reactive_layer_windows(compiled_plan)
                    visualizer_inputs = visualizer_streams.enter_context(
                        PythonVisualizerRenderer(self.executable).stream_layers(
                            audio_path, visualizers, selected_settings.fps, temporary,
                            cancel_event, visualizer_progress, track_windows,
                        )
                    )
                except PythonVisualizerError as error:
                    if cancel_event.is_set():
                        raise RenderCancelledError("Rendering was cancelled.") from error
                    raise RenderError(str(error)) from error
            encoding_start = 0.78 if visualizers else 0.66
            encoding_span = 0.21 if visualizers else 0.33
            static_inputs: list[tuple[float, Path, str, int, int]] = []
            piped_input_arguments: dict[int, tuple[str, ...]] = {}
            base_video = piped_video or prepared_video
            static_stage_start = 0.76 if visualizers else 0.64
            static_stage_span = max(0.0, encoding_start - static_stage_start)
            static_frame_total = sum(
                len(layer.frames)
                for layer in static_layers
                if isinstance(layer, StaticOverlayLayer)
            )
            # Each static frame is first validated and then staged/registered.
            # Counting both passes prevents the UI from appearing frozen during
            # header validation on projects with thousands of PNG frames.
            static_work_total = static_frame_total * 2
            static_work_completed = 0
            if static_frame_total:
                self._report(
                    progress_callback, "Preparing visual layers", static_stage_start,
                    f"Preparing visual layers 0/{static_frame_total} frames",
                )
            for layer_index, layer in enumerate(static_layers):
                if cancel_event.is_set():
                    raise RenderCancelledError("Rendering was cancelled.")
                if isinstance(layer, PipedStaticOverlayLayer):
                    piped_layer = layer.video
                    if (piped_layer.width <= 0 or piped_layer.height <= 0
                            or not piped_layer.input_arguments
                            or piped_layer.fps != selected_settings.fps
                            or layer.x < 0 or layer.y < 0
                            or (base_video is not None and (
                                layer.x + piped_layer.width > base_video.width
                                or layer.y + piped_layer.height > base_video.height
                            ))):
                        raise RenderError("A piped Canvas layer is invalid.")
                    piped_input_arguments[len(static_inputs)] = piped_layer.input_arguments
                    static_inputs.append((
                        layer.z_index, Path("canvas-pipe"), "pipe", layer.x, layer.y,
                    ))
                    continue
                if isinstance(layer, PreparedStaticOverlayLayer):
                    prepared_layer = layer.video
                    if (not prepared_layer.path.is_file()
                            or prepared_layer.width <= 0 or prepared_layer.height <= 0
                            or prepared_layer.fps != selected_settings.fps
                            or layer.x < 0 or layer.y < 0
                            or (prepared_video is not None and (
                                layer.x + prepared_layer.width > prepared_video.width
                                or layer.y + prepared_layer.height > prepared_video.height
                            ))):
                        raise RenderError(
                            "A prepared static overlay video is missing or invalid."
                        )
                    static_inputs.append((
                        layer.z_index, prepared_layer.path.resolve(), "alpha_pair",
                        layer.x, layer.y,
                    ))
                    continue
                if not layer.frames:
                    continue
                layer_images = [frame.image for frame in layer.frames]
                validation_completed = 0

                def visual_validation_progress(
                    completed: int, total: int, *, current_layer: int = layer_index,
                ) -> None:
                    nonlocal validation_completed, static_work_completed
                    static_work_completed += max(0, completed - validation_completed)
                    validation_completed = completed
                    fraction = static_work_completed / max(1, static_work_total)
                    self._report(
                        progress_callback, "Preparing visual layers",
                        static_stage_start + static_stage_span * fraction,
                        (
                            f"Checking visual layer {current_layer + 1}/{len(static_layers)}"
                            f" · frame {completed}/{total}"
                            f" · {round(fraction * 100)}%"
                        ),
                    )

                self._validate_frames(layer_images, visual_validation_progress)
                layer_paths: list[Path] = []
                for frame_index, frame in enumerate(layer_images):
                    if cancel_event.is_set():
                        raise RenderCancelledError("Rendering was cancelled.")
                    if isinstance(frame, Path):
                        layer_path = frame.resolve()
                    else:
                        layer_path = temporary / f"layer_{layer_index:02d}_{frame_index:06d}.png"
                        if not frame.save(str(layer_path), "PNG"):
                            raise RenderError("Could not create a static overlay frame.")
                    if not layer_path.is_file():
                        raise RenderError(f"A static overlay frame is missing: {layer_path}")
                    layer_paths.append(layer_path)
                    static_work_completed += 1
                    if (static_work_completed == static_work_total
                            or static_work_completed % max(1, static_work_total // 100) == 0):
                        fraction = static_work_completed / static_work_total
                        self._report(
                            progress_callback, "Preparing visual layers",
                            static_stage_start + static_stage_span * fraction,
                            (
                                f"Preparing visual layer {layer_index + 1}/{len(static_layers)}"
                                f" · frame {frame_index + 1}/{len(layer_images)}"
                                f" · {round(fraction * 100)}%"
                            ),
                        )
                layer_manifest = temporary / f"layer_{layer_index:02d}.ffconcat"
                self._write_visual_concat(
                    layer_manifest,
                    list(zip(layer_paths, [frame.duration_seconds for frame in layer.frames], strict=True)),
                )
                static_inputs.append((
                    layer.z_index, layer_manifest, "concat", layer.x, layer.y,
                ))
            if prepared_video is None and piped_video is None:
                video_concat_path = temporary / "video.ffconcat"
                self._write_visual_concat(video_concat_path, visual_sequence)
                base_input_arguments = [
                    "-f", "concat", "-safe", "0", "-i", str(video_concat_path),
                ]

            encoding_stage = "Capturing and encoding" if piped else "Encoding video"

            def encoding_progress(line: str) -> None:
                seconds = self._parse_progress_seconds(line)
                if seconds is not None and total_duration > 0:
                    bounded = min(total_duration, max(0.0, seconds))
                    fraction = bounded / total_duration
                    finishing = bounded >= max(
                        0.0,
                        total_duration - max(0.5, 2.0 / selected_settings.fps),
                    )
                    self._report(
                        progress_callback,
                        "Finalizing export" if direct_mux or finishing else encoding_stage,
                        encoding_start + fraction * encoding_span,
                        (
                            "Finishing the MP4 file"
                            if finishing else
                            f"Muxing {bounded:.1f}s / {total_duration:.1f}s"
                            if direct_mux else
                            f"Capturing and encoding {bounded:.1f}s / {total_duration:.1f}s"
                            if piped else
                            f"Encoding {bounded:.1f}s / {total_duration:.1f}s"
                        ),
                    )

            self._report(
                progress_callback,
                "Finalizing export" if direct_mux else encoding_stage,
                encoding_start,
                (
                    "Muxing prepared video and audio"
                    if direct_mux else
                    "Capturing the Canvas into the final video"
                    if piped else "Rendering the final video"
                ),
            )
            try:
                output_descriptor, output_staging_name = mkstemp(
                    prefix=f".{target.stem}-",
                    suffix=f".rendering{target.suffix}",
                    dir=target.parent,
                )
                os.close(output_descriptor)
            except OSError as error:
                raise RenderError(
                    "Could not create a temporary output video beside the selected "
                    "destination. Check the destination folder and try again."
                ) from error
            temporary_video = Path(output_staging_name)
            if storage_path_callback:
                storage_path_callback("output", temporary_video)
            video_arguments = [
                # Let FFmpeg use all available CPU workers for PNG decoding,
                # filtering and software encoding. Hardware encoders ignore this
                # safely while their video encode stage runs on the GPU.
                # A single filter worker prevents QHD/60 FPS filter graphs from
                # retaining many full-resolution RGBA frames at once. Encoding
                # threads still use the available CPU cores (or the selected GPU).
                "-threads", "0",
                "-filter_threads", str(self._filter_worker_count(selected_settings)),
                "-filter_complex_threads", str(self._filter_worker_count(selected_settings)),
                *base_input_arguments,
                "-i", str(audio_path),
            ]
            for input_arguments in visualizer_inputs:
                video_arguments.extend(input_arguments)
            for video_input in video_file_inputs:
                if video_input.loop_input:
                    video_arguments.extend(["-stream_loop", "-1"])
                video_arguments.extend([
                    "-ss", f"{video_input.media_start_seconds:.6f}",
                    "-t", f"{video_input.duration_seconds:.6f}",
                    "-i", str(video_input.path),
                ])
            for static_index, (_z_index, layer_path, input_kind, _x, _y) in enumerate(
                static_inputs
            ):
                if input_kind == "pipe":
                    video_arguments.extend(piped_input_arguments[static_index])
                elif input_kind == "concat":
                    video_arguments.extend([
                        "-f", "concat", "-safe", "0", "-i", str(layer_path),
                    ])
                else:
                    video_arguments.extend(["-i", str(layer_path)])
            metadata_arguments: list[str] = []
            if metadata_path is not None:
                metadata_input_index = (
                    2 + len(visualizer_inputs) + len(video_file_inputs)
                    + len(static_inputs)
                )
                video_arguments.extend(
                    ["-f", "ffmetadata", "-i", str(metadata_path)]
                )
                metadata_arguments = [
                    "-map_metadata", str(metadata_input_index),
                    "-map_chapters", str(metadata_input_index),
                ]
            if direct_mux:
                video_arguments.extend([
                    "-map", "0:v:0", "-map", "1:a:0",
                    *metadata_arguments,
                ])
            elif visualizer_inputs or video_clips or static_inputs:
                graph = self._layered_filter_graph(
                    visualizers, static_inputs, selected_settings.fps,
                    selected_settings.output_width, selected_settings.output_height,
                    video_clips=video_clips,
                    video_input_slots=video_input_slots,
                )
                graph_arguments = ["-filter_complex", graph]
                if len(graph) > 8000:
                    # Per-track fades can exceed Windows' command-line limit.
                    graph_path = temporary / "video-filters.txt"
                    graph_path.write_text(graph, encoding="utf-8")
                    graph_arguments = ["-filter_complex_script", str(graph_path)]
                video_arguments.extend([
                    *graph_arguments,
                    "-map", "[vout]", "-map", "1:a",
                    *metadata_arguments,
                ])
            else:
                video_arguments.extend([
                    "-vf", self._output_scaling_filter(selected_settings.fps, selected_settings.output_width,
                                                        selected_settings.output_height),
                    *metadata_arguments,
                ])
            if direct_mux:
                video_arguments.extend([
                    # Both streams were already encoded once at their selected
                    # final settings. This pass only writes the MP4 container.
                    "-c:v", "copy", "-c:a", "copy",
                    # Do not use -shortest here. Sparse Canvas inputs and framesync
                    # filters can disagree by a fraction of a frame at EOF, causing
                    # FFmpeg to retain queued frames while the UI appears stuck at
                    # 99%. The validated project timeline is authoritative.
                    "-t", f"{total_duration:.6f}",
                    *video_container_arguments(target),
                    "-progress", "pipe:1", "-nostats", "-y", str(temporary_video),
                ])
            else:
                video_arguments.extend([
                    # Keep presentation timestamps strictly CFR.  The Canvas concat input is
                    # intentionally variable-duration, while Python layers are FPS-based.
                    "-fps_mode", "cfr", "-r", str(selected_settings.fps),
                    "-c:v", selected_settings.video_codec,
                    *self._video_encoding_arguments(selected_settings),
                    # playlist_audio.m4a is already the final AAC stream. Copying it
                    # avoids a redundant lossy pass and preserves the prepared audio
                    # bytes and timing exactly.
                    "-c:a", "copy",
                    "-pix_fmt", "yuv420p",
                    # Bound the filter graph and muxer to the exact timeline.
                    # Without this output limit, a one-frame EOF mismatch between
                    # the base, alpha-pair and audio streams can make FFmpeg keep
                    # buffering after the last visible frame.
                    "-t", f"{total_duration:.6f}",
                    *video_container_arguments(target),
                    "-progress", "pipe:1", "-nostats", "-y", str(temporary_video),
                ])
            LOGGER.info(
                "Final FFmpeg stage: codec=%s direct_mux=%s piped=%s duration=%.3fs "
                "fps=%d resolution=%dx%d filter_workers=%d visualizers=%d "
                "video_clips=%d static_inputs=%d",
                "copy" if direct_mux else selected_settings.video_codec,
                direct_mux,
                piped,
                total_duration,
                selected_settings.fps,
                selected_settings.output_width,
                selected_settings.output_height,
                self._filter_worker_count(selected_settings),
                len(visualizer_inputs),
                len(video_clips),
                len(static_inputs),
            )
            try:
                self._run(
                    video_arguments,
                    progress_parser=encoding_progress,
                    cancel_event=cancel_event,
                )
                try:
                    visualizer_streams.close()
                except PythonVisualizerError as error:
                    if cancel_event.is_set():
                        raise RenderCancelledError("Rendering was cancelled.") from error
                    raise RenderError(str(error)) from error
                if cancel_event.is_set():
                    raise RenderCancelledError("Rendering was cancelled.")
                self._report(
                    progress_callback, "Finalizing export", 0.995,
                    "Moving the completed video to the selected location",
                )
                try:
                    temporary_video.replace(target)
                except OSError as error:
                    raise RenderError(
                        "Could not replace the output video. Close any program using "
                        f"'{target.name}', check the destination folder, and try again."
                    ) from error
            finally:
                try:
                    temporary_video.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning(
                        "Could not remove incomplete output staging file: %s",
                        temporary_video,
                    )
        self._report(progress_callback, "Complete", 1.0, "Export completed")
        validation = ExportPolicy.validate_output(
            target,
            width=selected_settings.output_width,
            height=selected_settings.output_height,
            fps=selected_settings.fps,
            duration_seconds=total_duration,
            ffmpeg_executable=self.executable,
        )
        return RenderResult(target, len(active_tracks), validation)

    @staticmethod
    def _visual_sequence(tracks: list[PlaylistTrack], frame_paths: list[Path],
                         compiled_plan: CompiledRenderPlan | None = None) -> list[tuple[Path, float]]:
        """Pair per-track Canvas frames with durations, including manual silent gaps."""
        sequence: list[tuple[Path, float]] = []
        last_frame = frame_paths[0]
        track_by_id = {track.id: track for track in tracks}
        cursor = 0.0
        plan = compiled_plan or compile_playlist(tracks)
        for window, chapter, frame_path in zip(
            plan.presentation.windows, plan.metadata.chapters, frame_paths, strict=True,
        ):
            track = track_by_id[window.track_id]
            gap = window.timeline_start - cursor
            if gap > 0.001:
                sequence.append((last_frame, gap))
            sequence.append((frame_path, max(0.001, min(window.timeline_end, chapter.end) - window.timeline_start)))
            last_frame = frame_path
            cursor = min(window.timeline_end, chapter.end)
        return sequence

    @staticmethod
    def _python_visualizer_filter_graph(visualizers: list[VisualizerOverlay], fps: int,
                                         output_width: int, output_height: int) -> str:
        """Composite pre-rendered Python alpha layers; no FFmpeg analyzer filters are used."""
        return filter_graph.python_visualizer_filter_graph(visualizers, fps, output_width, output_height)

    @staticmethod
    def _layered_filter_graph(visualizers: list[VisualizerOverlay],
                              static_layers: list[
                                  tuple[float, Path]
                                  | tuple[float, Path, str]
                                  | tuple[float, Path, str, int, int]
                              ], fps: int,
                              output_width: int, output_height: int,
                              video_clips: list[VideoClipOverlay] | None = None,
                              video_input_slots: list[int] | None = None) -> str:
        """Interleave reactive video and transparent static Z bands in Canvas order."""
        return filter_graph.layered_filter_graph(
            visualizers, static_layers, fps, output_width, output_height,
            video_clips, video_input_slots,
        )

    @staticmethod
    def _video_input_plan(
        video_clips: list[VideoClipOverlay],
    ) -> tuple[list[VideoFileInput], list[int]]:
        """Deduplicate matching files while retaining per-occurrence filter branches."""
        return filter_graph.video_input_plan(video_clips)

    @staticmethod
    def _filter_color(value: str) -> str:
        """Return an FFmpeg-safe opaque RGB literal for Canvas fill colors."""
        return filter_graph.filter_color(value)

    @staticmethod
    def _output_scaling_filter(fps: int, output_width: int, output_height: int,
                               include_fps: bool = True) -> str:
        """Scale without stretching the authored canvas and pad any aspect mismatch."""
        return filter_graph.output_scaling_filter(fps, output_width, output_height, include_fps)

    @staticmethod
    def _validate_frames(
        frames: list[QImage | Path],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Reject inconsistent or empty snapshots before FFmpeg can shift a layout."""
        if not frames:
            raise RenderError("The export canvas frame is empty.")
        path_size_cache: dict[Path, object] = {}

        def frame_size(frame: QImage | Path) -> object:
            if isinstance(frame, Path):
                normalized = frame.resolve()
                cached = path_size_cache.get(normalized)
                if cached is not None:
                    return cached
                if not normalized.is_file():
                    raise RenderError(f"A staged export frame is missing: {frame}")
                reader = QImageReader(str(normalized))
                size = reader.size()
                if not size.isValid():
                    raise RenderError(
                        f"Could not read the staged export frame header: {frame}"
                    )
                path_size_cache[normalized] = size
                return size
            else:
                image = frame
            if image.isNull():
                raise RenderError("The export canvas frame is empty.")
            size = image.size()
            if isinstance(frame, Path):
                path_size_cache[normalized] = size
            return size

        reference_size = frame_size(frames[0])
        if progress_callback:
            progress_callback(1, len(frames))
        for index, frame in enumerate(frames[1:], start=2):
            if frame_size(frame) != reference_size:
                raise RenderError(
                    "All export frames must use one canvas size. "
                    f"Frame {index} does not match the first canvas frame."
                )
            if (progress_callback and (
                    index == len(frames)
                    or index % max(1, len(frames) // 100) == 0)):
                progress_callback(index, len(frames))

    @staticmethod
    def _validate_settings(settings: RenderSettings) -> None:
        """Reject invalid encoder geometry before it can create a corrupt output."""
        if settings.fps <= 0 or settings.fps > 240:
            raise RenderError("The export frame rate must be between 1 and 240 FPS.")
        if settings.output_width <= 0 or settings.output_height <= 0:
            raise RenderError("The export resolution must be greater than zero.")
        if settings.output_width % 2 or settings.output_height % 2:
            raise RenderError(
                "The export width and height must be even numbers for YUV video."
            )
        if not settings.video_codec.strip():
            raise RenderError("Select a video encoder before exporting.")

    @staticmethod
    def _validate_visual_timeline(
        visual_sequence: list[tuple[Path, float]],
        static_layers: list[
            StaticOverlayLayer | PreparedStaticOverlayLayer | PipedStaticOverlayLayer
        ],
        expected_duration: float,
        fps: int,
    ) -> None:
        """Require every composited stream to cover the same playlist timeline."""
        tolerance = max(0.002, 1.0 / max(1, fps) + 0.001)

        def validate(label: str, durations: list[float]) -> None:
            if not durations or any(duration <= 0.0 for duration in durations):
                raise RenderError(f"{label} contains an invalid frame duration.")
            actual = sum(durations)
            if abs(actual - expected_duration) > tolerance:
                raise RenderError(
                    f"{label} is {actual:.3f}s long, but the playlist is "
                    f"{expected_duration:.3f}s. Export was stopped to prevent "
                    "misaligned video and audio."
                )

        validate("The prepared Canvas video", [duration for _path, duration in visual_sequence])
        for index, layer in enumerate(static_layers, start=1):
            if isinstance(layer, (PreparedStaticOverlayLayer, PipedStaticOverlayLayer)):
                validate(
                    f"Static overlay layer {index}",
                    [layer.video.duration_seconds],
                )
                continue
            if not layer.frames:
                continue
            validate(
                f"Static overlay layer {index}",
                [frame.duration_seconds for frame in layer.frames],
            )

    def ensure_encoder_available(self, encoder: str) -> None:
        """Fail early when the active FFmpeg build lacks the selected encoder."""
        if self._available_encoders is None:
            try:
                completed = subprocess.run(
                    [str(self.executable), "-hide_banner", "-encoders"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    **hidden_process_kwargs(),
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise EncoderUnavailableError(
                    "Could not inspect the available FFmpeg encoders."
                ) from error
            if completed.returncode != 0:
                raise EncoderUnavailableError(
                    "Could not inspect the available FFmpeg encoders."
                )
            self._available_encoders = frozenset(completed.stdout.split())
        if encoder not in self._available_encoders:
            if encoder in AUDIO_ENCODERS.values():
                raise EncoderUnavailableError(
                    f"The audio encoder '{encoder}' is unavailable in this FFmpeg build. "
                    "Choose another audio format or install an FFmpeg build with this encoder."
                )
            raise EncoderUnavailableError(
                f"The selected video encoder '{encoder}' is unavailable in this FFmpeg build. "
                "Choose a supported CPU/GPU encoder in Settings."
            )

    def preflight_export(
        self,
        tracks: list[PlaylistTrack],
        output_path: str | Path,
        settings: RenderSettings,
    ) -> None:
        """Validate inputs, destination, and real encoder startup before capture."""
        active_tracks = [track for track in tracks if track.enabled]
        if not active_tracks:
            raise RenderError("Select at least one playlist track before exporting.")
        invalid_track = next(
            (track for track in active_tracks if track.duration_seconds <= 0.0), None
        )
        if invalid_track is not None:
            raise RenderError(
                f"Audio duration could not be determined: {invalid_track.title}"
            )
        missing = next(
            (Path(track.file_path) for track in active_tracks
             if not Path(track.file_path).is_file()),
            None,
        )
        if missing is not None:
            raise RenderError(f"Audio file is missing: {missing}")

        audio_only = is_audio_export(output_path)
        if not audio_only:
            self._validate_settings(settings)
        target = Path(output_path).expanduser().resolve()
        if target.suffix.lower().lstrip(".") not in VIDEO_FORMATS + AUDIO_FORMATS:
            target = target.with_suffix(".mp4")
        if target.exists() and not target.is_file():
            raise RenderError("The selected output path is not a file.")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, probe_name = mkstemp(
                prefix=".playlist-canvas-write-test-",
                suffix=".tmp",
                dir=target.parent,
            )
            os.close(descriptor)
            Path(probe_name).unlink()
        except OSError as error:
            raise RenderError(
                "The selected output folder is not writable. Choose another "
                "folder and try again."
            ) from error

        if audio_only:
            self.ensure_encoder_available(AUDIO_ENCODERS[target.suffix.lower().lstrip(".")])
        else:
            self.ensure_encoder_available(settings.video_codec)
            self.ensure_encoder_usable(settings)

    def render_audio(
        self, tracks: list[PlaylistTrack], output_path: str | Path, settings: RenderSettings,
        *, progress_callback=None, cancel_event=None, metadata=None,
        storage_path_callback=None, transition_mode="none", crossfade_seconds=3.0,
        automix_settings=None,
    ) -> RenderResult:
        """Export the same playlist mix through a lossless intermediate, without visuals."""
        cancel_event = cancel_event or threading.Event()
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        self.preflight_export(tracks, output_path, settings)
        target = Path(output_path).expanduser().resolve()
        format_name = target.suffix.lower().lstrip(".")
        active_tracks = [track for track in tracks if track.enabled]
        with TemporaryDirectory(prefix="playlist-audio-") as directory:
            temporary = Path(directory)
            if storage_path_callback:
                storage_path_callback("render", temporary)
            plans = []
            audio_path = self.prepare_playlist_audio(
                active_tracks, temporary, settings,
                transition_mode=transition_mode, crossfade_seconds=crossfade_seconds,
                progress_callback=progress_callback, cancel_event=cancel_event,
                plan_callback=plans.append, automix_settings=automix_settings,
                audio_codec="flac",
            )
            plan = plans[0]
            duration = plan.duration_seconds
            metadata_path = self._write_export_ffmetadata(
                temporary, active_tracks, metadata or ExportMetadata(), target, plan,
            )
            arguments = ["-i", str(audio_path)]
            if metadata_path:
                arguments += ["-f", "ffmetadata", "-i", str(metadata_path)]
            arguments += ["-map", "0:a:0", "-vn"]
            if metadata_path:
                arguments += ["-map_metadata", "1", "-map_chapters",
                              "1" if format_name in {"mp3", "m4a"} else "-1"]
            arguments += audio_encoding_arguments(format_name, settings.audio_bitrate)
            arguments += ["-ar", "48000", "-ac", "2", "-t", f"{duration:.6f}"]
            try:
                descriptor, staging_name = mkstemp(
                    prefix=f".{target.stem}-", suffix=f".rendering{target.suffix}", dir=target.parent,
                )
                os.close(descriptor)
            except OSError as error:
                raise RenderError("Could not create the temporary output audio file.") from error
            staging = Path(staging_name)
            if storage_path_callback:
                storage_path_callback("output", staging)

            def progress(line: str) -> None:
                seconds = self._parse_progress_seconds(line)
                if seconds is not None:
                    fraction = min(1.0, max(0.0, seconds / max(duration, 0.001)))
                    self._report(progress_callback, "Encoding audio", 0.64 + fraction * 0.35,
                                 f"Saving audio {min(seconds, duration):.1f}s / {duration:.1f}s")

            try:
                self._report(progress_callback, "Encoding audio", 0.64, "Saving audio")
                self._run(arguments + ["-progress", "pipe:1", "-nostats", "-y", str(staging)],
                          progress_parser=progress, cancel_event=cancel_event)
                if cancel_event.is_set():
                    raise RenderCancelledError("Rendering was cancelled.")
                try:
                    staging.replace(target)
                except OSError as error:
                    raise RenderError(f"Could not replace '{target.name}'. Check the destination folder.") from error
            finally:
                staging.unlink(missing_ok=True)
        validation = validate_audio_export_output(target, duration, self.executable)
        self._report(progress_callback, "Complete", 1.0, "Export completed")
        return RenderResult(target, len(active_tracks), validation)

    def ensure_encoder_usable(self, settings: RenderSettings) -> None:
        """Encode at the selected format so hardware startup checks are realistic."""
        encoder = settings.video_codec
        pixel_format = (
            "nv12"
            if encoder in {
                "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv",
                "h264_amf", "hevc_amf",
            }
            else "yuv420p"
        )
        probe_source = (
            f"color=c=black:s={settings.output_width}x{settings.output_height}"
            f":r={settings.fps}"
        )
        try:
            completed = subprocess.run(
                [
                    str(self.executable), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", probe_source,
                    "-frames:v", "1", "-an", "-c:v", encoder,
                    *self._video_encoding_arguments(settings),
                    "-pix_fmt", pixel_format, "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **hidden_process_kwargs(),
            )
        except subprocess.TimeoutExpired as error:
            raise EncoderUnavailableError(
                f"The selected video encoder '{encoder}' did not respond during "
                "the selected-format startup check."
            ) from error
        except OSError as error:
            raise EncoderUnavailableError(
                "Could not start FFmpeg for the video encoder check."
            ) from error
        if completed.returncode == 0:
            return
        details = completed.stderr.strip()
        if len(details) > 1600:
            details = details[-1600:]
        suffix = f"\n\nFFmpeg: {details}" if details else ""
        raise EncoderUnavailableError(
            f"The selected video encoder '{encoder}' is installed but could not "
            f"start on this computer. Check the GPU driver or choose CPU H.264."
            f"{suffix}"
        )

    def direct_encoding_profile(
        self, settings: RenderSettings,
    ) -> "DirectVideoEncodingProfile":
        """Describe how to encode an opaque Canvas stream straight to the final codec.

        Callers that stream Canvas frames use this instead of reaching into the
        renderer's private encoding arguments.
        """
        from app.renderer.static_video_stream import DirectVideoEncodingProfile

        return DirectVideoEncodingProfile(
            settings.output_width,
            settings.output_height,
            settings.video_codec,
            tuple(self._video_encoding_arguments(settings)),
        )

    @staticmethod
    def _video_encoding_arguments(settings: RenderSettings) -> list[str]:
        """Return quality controls compatible with CPU and common GPU encoders."""
        encoder = settings.video_codec
        if encoder in {"libx264", "libx265"}:
            # The Canvas can now contain animated overlays; avoid the still-image tuning.
            return ["-crf", str(settings.crf), "-preset", settings.preset]
        if encoder in {"h264_nvenc", "hevc_nvenc"}:
            # NVENC uses p1 (fastest) through p7 (best compression).  The app's
            # familiar preset names now have a real effect for GPU exports.
            nvenc_presets = {
                "ultrafast": "p1", "superfast": "p1", "veryfast": "p2",
                "faster": "p2", "fast": "p3", "medium": "p4",
                "slow": "p5", "slower": "p6", "veryslow": "p7",
            }
            return [
                "-preset", nvenc_presets.get(settings.preset, "p3"),
                "-rc", "vbr", "-cq", str(settings.crf), "-b:v", "0",
            ]
        if encoder in {"h264_qsv", "hevc_qsv"}:
            qsv_presets = {
                "ultrafast": "veryfast", "superfast": "veryfast",
                "veryfast": "veryfast", "faster": "faster", "fast": "fast",
                "medium": "medium", "slow": "slow", "slower": "slower",
                "veryslow": "veryslow",
            }
            return [
                "-preset", qsv_presets.get(settings.preset, "fast"),
                "-global_quality", str(settings.crf),
            ]
        if encoder in {"h264_amf", "hevc_amf"}:
            quality = (
                "speed" if settings.preset in {"ultrafast", "superfast", "veryfast", "faster"}
                else "quality" if settings.preset in {"slow", "slower", "veryslow"}
                else "balanced"
            )
            return [
                "-quality", quality, "-rc", "cqp", "-qp_i", str(settings.crf),
                "-qp_p", str(settings.crf),
            ]
        return ["-crf", str(settings.crf), "-preset", settings.preset]

    @staticmethod
    def _filter_worker_count(settings: RenderSettings) -> int:
        """Parallelise the filter graph up to a resolution-bounded cap.

        Each in-flight worker can retain a few full-resolution RGBA frames, so
        the ceiling drops as the frame grows: ~8 at <=1080p60, 4 at <=1440p60,
        3 at 4K and above.  STABLE stays single-worker for the lowest possible
        peak memory; AUTO leaves half the cores for the encoder and the OS while
        MAX_SPEED uses every core it is allowed.
        """
        if settings.work_mode == WORK_MODE_STABLE:
            return 1
        pixels_per_second = (
            settings.output_width * settings.output_height * settings.fps
        )
        if pixels_per_second <= 1920 * 1080 * 60:
            ceiling = 8
        elif pixels_per_second <= 2560 * 1440 * 60:
            ceiling = 4
        else:
            ceiling = 3
        cpu = os.cpu_count() or 4
        if settings.work_mode == WORK_MODE_MAX_SPEED:
            return max(2, min(ceiling, cpu))
        return max(2, min(ceiling, cpu // 2))

    @staticmethod
    def _audio_worker_count(settings: RenderSettings, track_count: int) -> int:
        """Choose bounded independent-track concurrency for the work mode."""
        if track_count <= 0:
            return 0
        if settings.work_mode == WORK_MODE_STABLE:
            return 1
        cpu_count = os.cpu_count() or 2
        if settings.work_mode == WORK_MODE_MAX_SPEED:
            cap = max(2, min(6, cpu_count - 1))
        else:
            cap = max(1, min(4, cpu_count // 2))
        return min(track_count, cap)

    @timed("export.audio_prepare_seconds")
    def prepare_playlist_audio(
        self, active_tracks: list[PlaylistTrack], output_directory: Path,
        settings: RenderSettings,
        transition_mode: str = "none", crossfade_seconds: float = 3.0,
        progress_callback: Callable[[str, float, str], None] | None = None,
        cancel_event: threading.Event | None = None,
        plan_callback: Callable[[CompiledRenderPlan], None] | None = None,
        automix_settings: "AutoMixTransitionSettings | None" = None,
        audio_codec: str = "aac",
    ) -> Path:
        """Prepare audio and report the exact plan actually rendered, including fallback.

        ``automix_settings``: ``None`` is ``app.automix.settings.AUTOMIX_SETTINGS``.
        ``audio_codec``: "aac" (the export's MP4 audio) or "flac" -- Preview's
        copy of the very same normalized audio, lossless and ~7x faster to
        write than AAC (which took 27 s of every 40 min of audio).
        """
        cancel_event = cancel_event or threading.Event()
        resolved_plan = compile_playlist(active_tracks)

        def accept_plan(plan: CompiledRenderPlan) -> None:
            nonlocal resolved_plan
            resolved_plan = plan

        total_duration = resolved_plan.duration_seconds
        blended_segments = None
        flac = audio_codec == "flac"
        audio_path = output_directory / ("playlist_audio.flac" if flac else "playlist_audio.m4a")
        codec_args = (["-c:a", "flac", "-compression_level", "0"] if flac else
                      ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-movflags", "+faststart"])
        if transition_mode == "automix":
            parts = self._render_automix_audio_segments(
                active_tracks, output_directory, total_duration, progress_callback, cancel_event, accept_plan,
                automix_settings,
            )
            if parts is not None:
                # Mixed in parts on every core: measure, normalize and encode them the same way.
                self._finish_automix_parts(parts, audio_path, codec_args, progress_callback, cancel_event)
                if plan_callback is not None:
                    plan_callback(resolved_plan)
                return audio_path
        elif transition_mode == "crossfade":
            blended_segments = self._render_fixed_crossfade_audio_segments(
                active_tracks, output_directory, total_duration, crossfade_seconds,
                progress_callback, cancel_event, accept_plan,
            )
        if blended_segments is not None:
            segments, segment_durations = blended_segments
        else:
            segments = self._normalize_audio(
                active_tracks, output_directory, settings, progress_callback, cancel_event
            )
            segment_durations = self._insert_silence_for_gaps(
                active_tracks, segments, output_directory, settings, progress_callback, cancel_event
            )
        total_duration = resolved_plan.duration_seconds
        concat_path = output_directory / "playlist.ffconcat"
        self._write_concat_file(concat_path, segments, segment_durations)
        self._report(
            progress_callback, "Combining audio", 0.53,
            f"Measuring loudness 0.0s / {total_duration:.1f}s · 0%",
        )

        combining_label = "Combining audio"

        def combining_audio_progress(line: str) -> None:
            seconds = self._parse_progress_seconds(line)
            if seconds is None or total_duration <= 0.0:
                return
            bounded = min(total_duration, max(0.0, seconds))
            fraction = bounded / total_duration
            self._report(
                progress_callback, "Combining audio", 0.56 + fraction * 0.08,
                self._timed_progress_message(
                    combining_label, bounded, total_duration, fraction,
                ),
            )

        combining_input_args = ["-f", "concat", "-safe", "0", "-i", str(concat_path)]
        combining_output_args = [
            *codec_args, "-ar", "48000", "-ac", "2",
            "-progress", "pipe:1", "-nostats",
            "-y", str(audio_path),
        ]
        # Two-pass EBU R128 loudness normalization applied once here, after
        # concatenation/blending, so every transition_mode (legacy sequential,
        # AutoMix, crossfade) gets a consistent, accurately-measured playback
        # loudness without needing its own normalization step. Pass 1 measures
        # this exact combined audio; pass 2 applies the measured gain
        # (linear=true), which -- unlike single-pass loudnorm's on-the-fly
        # estimate -- does not risk audible pumping on wildly different
        # source mastering.
        def measuring_progress(line: str) -> None:
            seconds = self._parse_progress_seconds(line)
            if seconds is None or total_duration <= 0.0:
                return
            bounded = min(total_duration, max(0.0, seconds))
            self._report(
                progress_callback, "Combining audio", 0.53 + bounded / total_duration * 0.03,
                self._timed_progress_message("Measuring loudness", bounded, total_duration,
                                             bounded / total_duration),
            )

        loudness_filter = self._measure_loudness_filter(combining_input_args, cancel_event, measuring_progress)
        if loudness_filter is not None:
            combining_label = "Normalizing loudness and saving audio"
            try:
                self._run(
                    combining_input_args + ["-af", loudness_filter] + combining_output_args,
                    progress_parser=combining_audio_progress, cancel_event=cancel_event,
                )
            except RenderCancelledError:
                raise
            except RenderError as error:
                # Never let a best-effort loudness pass break an export that
                # would otherwise have worked fine -- fall back to the plain
                # combine, same discipline as every other optional audio
                # enhancement in this file.
                LOGGER.warning("Loudness normalization failed, exporting without it: %s", error)
                loudness_filter = None
                combining_label = "Combining audio"
        if loudness_filter is None:
            self._run(
                combining_input_args + combining_output_args,
                progress_parser=combining_audio_progress, cancel_event=cancel_event,
            )
        self._report(
            progress_callback, "Combining audio", 0.64,
            self._timed_progress_message(
                combining_label, total_duration, total_duration, 1.0,
            ),
        )
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        # The PCM/float intermediates (~11-23 MB per minute, one file per track in
        # sequential mode) are never read again; free them now instead of holding
        # them until the whole temp folder goes at the end of the export.
        for segment in (*segments, concat_path):
            segment.unlink(missing_ok=True)
        if plan_callback is not None:
            plan_callback(resolved_plan)
        return audio_path

    def _measure_loudness_filter(
        self, combining_input_args: list[str], cancel_event: threading.Event,
        progress_parser: Callable[[str], None] | None = None,
    ) -> str | None:
        """Measure this exact combined audio and build the filter that normalizes it.

        The policy is ``app.renderer.loudness``: one linear gain, plus an
        oversampled true-peak limiter only where that gain lifts a peak over
        the ceiling. Measured with ``ebur128`` in ~1/10 of the time of
        loudnorm's analysis pass (40 min of audio: 4.6 s vs 43.7 s); a plain
        gain is bit-identical to loudnorm's linear mode (checked on FFmpeg 9).
        Only a mix ebur128 cannot measure still goes through loudnorm's own
        two passes.

        Returns ``None`` (never raises, except on cancellation) whenever a
        usable measurement can't be produced -- a failed measurement pass,
        unparseable stats, or a near-silent input that measures at -inf
        LUFS (a gain cannot correct for that without producing NaN/Inf
        samples). The caller falls back to combining audio with no loudness
        filter at all in every such case.
        """
        stats = self._ebur128_stats(combining_input_args, cancel_event, progress_parser)
        if stats is not None:
            integrated, threshold, loudness_range, true_peak = stats
            if not math.isfinite(integrated) or not math.isfinite(true_peak):
                LOGGER.info(
                    "Input measured %.1f LUFS (near-silent); skipping loudness normalization", integrated,
                )
                return None
            chain = loudness.normalization_filter(integrated, true_peak)
            LOGGER.info("Loudness: %.1f LUFS, %.1f dBTP, LRA %.1f -> %s",
                        integrated, true_peak, loudness_range, chain)
            return chain
        stderr_lines: list[str] = []
        try:
            self._run(
                combining_input_args + [
                    # _run() hard-codes "-loglevel error" ahead of these
                    # arguments; loudnorm's print_format=json stats are
                    # logged at the "info" level, so without this override
                    # they never reach stderr at all and every measurement
                    # silently looked like a failure. FFmpeg takes the last
                    # of two repeated global options, so this "-loglevel
                    # info" wins over the earlier "-loglevel error".
                    "-loglevel", "info",
                    "-af", f"{loudness.LOUDNORM}:print_format=json",
                    "-f", "null", "-",
                ],
                cancel_event=cancel_event, capture_stderr=stderr_lines,
            )
        except RenderCancelledError:
            raise
        except RenderError as error:
            LOGGER.warning(
                "Loudness measurement pass failed, exporting without loudness normalization: %s", error,
            )
            return None
        text = "".join(stderr_lines)
        start, end = text.rfind("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            LOGGER.warning(
                "Loudness measurement pass produced no stats, exporting without loudness normalization",
            )
            return None
        try:
            stats = json.loads(text[start:end + 1])
            measured_i = float(stats["input_i"])
            measured_tp = float(stats["input_tp"])
            measured_lra = float(stats["input_lra"])
            measured_thresh = float(stats["input_thresh"])
            target_offset = float(stats["target_offset"])
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            LOGGER.warning(
                "Loudness measurement pass returned unusable stats, exporting without "
                "loudness normalization: %s", error,
            )
            return None
        if not math.isfinite(measured_i) or not math.isfinite(measured_tp):
            LOGGER.info(
                "Input measured %.1f LUFS (near-silent); skipping loudness normalization", measured_i,
            )
            return None
        return (
            f"{loudness.LOUDNORM}:"
            f"measured_I={measured_i}:measured_TP={measured_tp}:"
            f"measured_LRA={measured_lra}:measured_thresh={measured_thresh}:"
            f"offset={target_offset}:linear=true"
        )

    def _ebur128_stats(
        self, combining_input_args: list[str], cancel_event: threading.Event,
        progress_parser: Callable[[str], None] | None,
    ) -> tuple[float, float, float, float] | None:
        """(integrated LUFS, its gate threshold, LRA, true peak dBTP), or None if unmeasurable."""
        stderr_lines: list[str] = []
        try:
            self._run(
                combining_input_args + [
                    "-loglevel", "info",  # the summary is logged at info (see below)
                    "-af", "ebur128=peak=true:framelog=quiet",
                    "-progress", "pipe:1", "-nostats", "-f", "null", "-",
                ],
                progress_parser=progress_parser, cancel_event=cancel_event, capture_stderr=stderr_lines,
            )
        except RenderCancelledError:
            raise
        except RenderError as error:
            LOGGER.warning("ebur128 loudness measurement failed, trying loudnorm's: %s", error)
            return None
        match = _EBUR128_SUMMARY.search("".join(stderr_lines))
        if match is None:
            return None
        return tuple(float(match.group(name)) for name in ("i", "threshold", "lra", "true_peak"))

    def _render_automix_audio_segments(
        self, active_tracks: list[PlaylistTrack], temporary: Path, sequential_duration: float,
        progress_callback: Callable[[str, float, str], None] | None,
        cancel_event: threading.Event,
        plan_callback: Callable[[CompiledRenderPlan], None] | None = None,
        automix_settings: "AutoMixTransitionSettings | None" = None,
    ) -> tuple[list[Path], list[float]] | None:
        """Render the mix and expose its plan only after successful validation."""
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        try:
            from app.automix.analysis.registry import create_analysis_provider
            from app.automix.planner import compile_automix
            from app.automix.renderer import AutoMixAudioPipeline, AutoMixRenderError, render_workers
            from app.automix.settings import AutoMixTransitionSettings
            from app.automix.workflow import AutoMixWorkflow
            from app.automix.structure.service import StructureAnalysisService
            from app.automix.structure.sonara import SonaraStructureProvider, sonara_available
            # "auto" -- the same policy the interactive playlist-badge
            # analysis path uses (AutoMixAnalysisController.start), so
            # Preview and Export can never resolve to a different analyzer
            # for the same playlist. Constructed here, inside the same
            # try/except as the other lazy imports above, so a missing/
            # broken analyzer dependency is reported exactly like before
            # instead of raising from inside the AutoMixRenderError-only
            # block below.
            provider = create_analysis_provider("auto", self.executable)
        except ImportError as error:
            LOGGER.warning("AutoMix export skipped, a dependency is unavailable: %s", error)
            return None
        except ValueError as error:
            LOGGER.error("AutoMix export misconfigured: %s", error)
            return None
        try:
            self._report(progress_callback, "Preparing audio", 0.05, "Analyzing tracks for AutoMix")
            workflow = AutoMixWorkflow(provider)
            analysis_result = workflow.analyze(
                active_tracks, cancel_event=cancel_event,
                progress=lambda done, total, _message: self._report(
                    progress_callback, "Preparing audio", 0.05 + 0.1 * done / max(1, total),
                    f"Analyzing beats and vocals {done}/{total}",
                ),
            )

            # Structure analysis (Commit C): the same StructureAnalysisService
            # + persistent StructureAnalysisCache Commit B already built for
            # the interactive playlist-badge path, reused here directly --
            # this is the one place Preview/Export actually compile a
            # CompiledRenderPlan, so it must not depend on
            # MainWindow.automix_structures (a UI session cache the renderer
            # has no business reading). Optional and independent from rhythm
            # analysis: unavailable/failed structure analysis never blocks
            # AutoMix, it only means compile_automix() gets no structure
            # data for the affected track(s) (see its own docstring).
            structures: dict = {}
            if sonara_available():
                self._report(progress_callback, "Preparing audio", 0.15, "Analyzing track structure")
                structure_result = StructureAnalysisService(SonaraStructureProvider(self.executable)).analyze_tracks(
                    active_tracks, cancel_event=cancel_event,
                    progress=lambda done, total, _message: self._report(
                        progress_callback, "Preparing audio", 0.15 + 0.05 * done / max(1, total),
                        f"Analyzing track structure {done}/{total}",
                    ),
                )
                structures = structure_result.analyses

            plan = compile_automix(
                active_tracks, analysis_result.analyses, automix_settings or AutoMixTransitionSettings(enabled=True),
                structures=structures,
            )
            if not plan.audio.clips:
                return None
            self._report(progress_callback, "Preparing audio", 0.2, "Rendering AutoMix transitions")
            from app.automix.parallel_mix import render_parts

            parts = render_parts(
                AutoMixAudioPipeline(self.executable),
                plan.audio,
                {track.id: track.file_path for track in active_tracks},
                temporary,
                workers=render_workers(),
                cancel_event=cancel_event,
                progress=lambda _stage, fraction, message: self._report(
                    progress_callback, "Preparing audio", 0.2 + fraction * 0.3, message,
                ),
                track_filters={track.id: track.audio_filter for track in active_tracks},
            )
        except AutoMixRenderError as error:
            if cancel_event.is_set():
                raise RenderCancelledError("Rendering was cancelled.") from error
            LOGGER.warning("AutoMix export failed, falling back to sequential audio: %s", error)
            return None
        if plan_callback is not None:
            plan_callback(plan)
        return parts

    def _finish_automix_parts(
        self, parts: list, audio_path: Path, codec_args: list[str],
        progress_callback: Callable[[str, float, str], None] | None, cancel_event: threading.Event,
    ) -> None:
        """Loudness-normalize and encode AutoMix mix parts into ``audio_path`` (see app.automix.parallel_mix)."""
        from app.automix.parallel_mix import finish_parts
        from app.automix.renderer import AutoMixAudioPipeline, AutoMixRenderCancelled, AutoMixRenderError, render_workers

        try:
            finish_parts(
                AutoMixAudioPipeline(self.executable), parts, audio_path,
                [*codec_args, "-ar", "48000", "-ac", "2"],
                workers=render_workers(), cancel_event=cancel_event,
                progress=lambda fraction, message: self._report(
                    progress_callback, "Combining audio", 0.53 + fraction * 0.11, message),
            )
        except AutoMixRenderCancelled as error:
            raise RenderCancelledError("Rendering was cancelled.") from error
        except AutoMixRenderError as error:
            raise RenderError(f"Could not finish the AutoMix audio: {error}") from error
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        self._report(progress_callback, "Combining audio", 0.64, "AutoMix audio ready")

    def _render_fixed_crossfade_audio_segments(
        self, active_tracks: list[PlaylistTrack], temporary: Path, sequential_duration: float,
        crossfade_seconds: float,
        progress_callback: Callable[[str, float, str], None] | None,
        cancel_event: threading.Event,
        plan_callback: Callable[[CompiledRenderPlan], None] | None = None,
    ) -> tuple[list[Path], list[float]] | None:
        """Render the mix and expose its plan only after successful validation."""
        if cancel_event.is_set():
            raise RenderCancelledError("Rendering was cancelled.")
        try:
            from app.automix.renderer import AutoMixAudioPipeline, AutoMixRenderError, render_workers
            from app.timeline.crossfade import compile_crossfade
        except ImportError as error:
            LOGGER.warning("Crossfade export skipped, a dependency is unavailable: %s", error)
            return None
        compiled = compile_crossfade(active_tracks, crossfade_seconds)
        plan = compiled.audio
        try:
            self._report(progress_callback, "Preparing audio", 0.1, "Rendering crossfades")
            prepared = AutoMixAudioPipeline(self.executable).render(
                plan,
                {track.id: track.file_path for track in active_tracks},
                temporary,
                cancel_event=cancel_event,
                progress=lambda _stage, fraction, message: self._report(
                    progress_callback, "Preparing audio", 0.1 + fraction * 0.4, message,
                ),
                track_filters={track.id: track.audio_filter for track in active_tracks},
                workers=render_workers(),
            )
        except AutoMixRenderError as error:
            if cancel_event.is_set():
                raise RenderCancelledError("Rendering was cancelled.") from error
            LOGGER.warning("Crossfade export failed, falling back to sequential audio: %s", error)
            return None
        if plan_callback is not None:
            plan_callback(compiled)
        return [prepared.path], [compiled.duration_seconds]

    def _normalize_audio(self, tracks: list[PlaylistTrack], directory: Path,
                         settings: RenderSettings,
                         progress_callback: Callable[[str, float, str], None] | None,
                         cancel_event: threading.Event) -> list[Path]:
        """Normalize every track to an exact-duration lossless segment."""
        segments: list[Path | None] = [None] * len(tracks)
        completed = 0
        progress_lock = threading.Lock()
        track_seconds = [0.0] * len(tracks)
        total_seconds = max(0.001, sum(track.duration_seconds for track in tracks))
        self._report(
            progress_callback, "Preparing audio", 0.05,
            f"Normalizing audio 0/{len(tracks)} complete · 0% total",
        )

        def normalize(index: int, track: PlaylistTrack) -> tuple[int, Path]:
            output = directory / f"track_{index:04d}.nut"
            duration = f"{track.duration_seconds:.6f}"
            pad = f"apad=whole_dur={duration}"

            def normalization_progress(line: str) -> None:
                seconds = self._parse_progress_seconds(line)
                if seconds is None:
                    return
                with progress_lock:
                    track_seconds[index] = max(
                        track_seconds[index],
                        min(track.duration_seconds, max(0.0, seconds)),
                    )
                    fraction = min(1.0, sum(track_seconds) / total_seconds)
                    self._report(
                        progress_callback, "Preparing audio",
                        0.05 + 0.48 * fraction,
                        (
                            f"Normalizing audio {completed}/{len(tracks)} complete"
                            f" · {track.filename}"
                            f" · {track_seconds[index]:.1f}s / {track.duration_seconds:.1f}s"
                            f" · {round(fraction * 100)}% total"
                        ),
                    )

            self._run([
                # Independent tracks are normalized concurrently, so each small
                # audio decode gets one FFmpeg worker instead of oversubscribing
                # every CPU core. PCM output and ordering remain unchanged.
                "-threads", "1", "-i", track.file_path, "-vn", "-map", "0:a:0",
                "-af", f"{track.audio_filter},{pad}" if track.audio_filter else pad, "-t", duration,
                # Float: a track's volume/EQ boost may pass full scale, which
                # loudness normalization brings back down only if nothing clipped.
                "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "-f", "nut",
                "-progress", "pipe:1", "-nostats", "-y", str(output),
            ], progress_parser=normalization_progress, cancel_event=cancel_event)
            return index, output

        worker_count = self._audio_worker_count(settings, len(tracks))
        self._report(
            progress_callback, "Preparing audio", 0.05,
            f"Normalizing {len(tracks)} independent track(s) with "
            f"{worker_count} parallel worker(s)",
        )
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="audio-normalize",
        ) as executor:
            futures = [
                executor.submit(normalize, index, track)
                for index, track in enumerate(tracks)
            ]
            for future in as_completed(futures):
                index, output = future.result()
                segments[index] = output
                with progress_lock:
                    track_seconds[index] = tracks[index].duration_seconds
                    completed += 1
                    timeline_fraction = min(1.0, sum(track_seconds) / total_seconds)
                    self._report(
                        progress_callback, "Preparing audio",
                        0.05 + 0.48 * timeline_fraction,
                        f"Normalized {completed}/{len(tracks)} tracks",
                    )
        self._report(progress_callback, "Preparing audio", 0.53, "Audio normalization complete")
        return [segment for segment in segments if segment is not None]

    def _insert_silence_for_gaps(self, tracks: list[PlaylistTrack], segments: list[Path],
                                 directory: Path, settings: RenderSettings,
                                 progress_callback: Callable[[str, float, str], None] | None,
                                 cancel_event: threading.Event) -> list[float]:
        """Insert lossless silence segments for user-defined timeline gaps."""
        if not any(track.start_time_seconds is not None for track in tracks):
            return [track.duration_seconds for track in tracks]
        combined: list[Path] = []
        combined_durations: list[float] = []
        track_by_id = {track.id: track for track in tracks}
        windows = compile_playlist(tracks).presentation.windows
        # PresentationWindow has no "floor" field (the previous track's end,
        # used below to size the gap): windows are contiguous per-clip, so
        # it's just the running end of the window before this one.
        floors: list[float] = []
        cursor = 0.0
        for window in windows:
            floors.append(cursor)
            cursor = window.timeline_end
        total_gap_seconds = 0.0
        gap_count = 0
        for window, floor in zip(windows, floors):
            planned_gap = window.timeline_start - floor
            total_gap_seconds += planned_gap
            if planned_gap > 0.001:
                gap_count += 1
        created_gap_seconds = 0.0
        created_gap_count = 0
        for index, (window, floor, segment) in enumerate(
            zip(windows, floors, segments, strict=True)
        ):
            track = track_by_id[window.track_id]
            start = window.timeline_start
            gap = start - floor
            if gap > 0.001:
                silence = directory / f"silence_{index:04d}.nut"

                def silence_progress(line: str, gap_seconds: float = gap) -> None:
                    seconds = self._parse_progress_seconds(line)
                    if seconds is None or total_gap_seconds <= 0.0:
                        return
                    current = min(gap_seconds, max(0.0, seconds))
                    fraction = min(
                        1.0, (created_gap_seconds + current) / total_gap_seconds,
                    )
                    self._report(
                        progress_callback, "Preparing audio", 0.53 + 0.03 * fraction,
                        (
                            f"Creating silence {created_gap_count + 1}/{max(1, gap_count)}"
                            f" · {current:.1f}s / {gap_seconds:.1f}s"
                            f" · {round(fraction * 100)}% total"
                        ),
                    )

                self._run([
                    "-f", "lavfi", "-t", f"{gap:.6f}", "-i", "anullsrc=r=48000:cl=stereo",
                    "-c:a", "pcm_f32le", "-ar", "48000", "-ac", "2", "-f", "nut",
                    "-progress", "pipe:1", "-nostats", "-y", str(silence),
                ], progress_parser=silence_progress, cancel_event=cancel_event)
                combined.append(silence)
                combined_durations.append(gap)
                created_gap_seconds += gap
                created_gap_count += 1
                fraction = min(1.0, created_gap_seconds / max(0.001, total_gap_seconds))
                self._report(
                    progress_callback, "Preparing audio", 0.53 + 0.03 * fraction,
                    f"Inserted {gap:.1f}s of silence",
                )
            combined.append(segment)
            combined_durations.append(track.duration_seconds)
        segments[:] = combined
        return combined_durations

    @staticmethod
    def _write_concat_file(
        path: Path, segments: list[Path], durations: list[float] | None = None,
    ) -> None:
        """Write an FFmpeg concat-demuxer manifest with safely quoted file paths."""
        def quote(segment: Path) -> str:
            return segment.resolve().as_posix().replace("'", "'\\''")

        lines = ["ffconcat version 1.0"]
        if durations is not None and len(durations) != len(segments):
            raise RenderError("Audio segment durations do not match the prepared files.")
        for index, segment in enumerate(segments):
            lines.append(f"file '{quote(segment)}'")
            if durations is not None:
                duration = durations[index]
                if duration <= 0.0:
                    raise RenderError("Audio segment duration must be greater than zero.")
                lines.append(f"duration {duration:.6f}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _write_visual_concat(path: Path, frames: list[tuple[Path, float]]) -> None:
        """Write an image concat manifest with exact still-frame durations."""
        def quote(frame: Path) -> str:
            return frame.resolve().as_posix().replace("'", "'\\''")

        lines = ["ffconcat version 1.0"]
        for frame, duration in frames:
            lines.append(f"file '{quote(frame)}'")
            lines.append(f"duration {duration:.6f}")
        if frames:
            lines.append(f"file '{quote(frames[-1][0])}'")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @timed("ffmpeg.process_wall_seconds")
    def _run(self, arguments: list[str], progress_parser: Callable[[str], None] | None = None,
             cancel_event: threading.Event | None = None,
             capture_stderr: list[str] | None = None) -> None:
        """Run FFmpeg, forward machine progress, and terminate safely on cancellation."""
        command = [str(self.executable), "-hide_banner", "-loglevel", "error", *arguments]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                **hidden_process_kwargs(),
            )
        except OSError as error:
            raise RenderError(f"Could not start FFmpeg: {error}") from error
        # FFmpeg can print several progress lines per encoded frame.  Keeping the
        # entire stream made long QHD/60 exports grow memory for hours, although
        # only the newest diagnostics are useful after a failure.
        stdout_lines: deque[str] = deque(maxlen=80)
        stderr_lines: deque[str] = deque(maxlen=400)

        def read_stream(stream: object, sink: deque[str]) -> None:
            for line in iter(stream.readline, ""):  # type: ignore[union-attr]
                sink.append(line)
                if sink is stdout_lines and progress_parser:
                    progress_parser(line.strip())

        stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout_lines), daemon=True)
        stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr_lines), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        cancelled = False
        while process.poll() is None:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=3.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                break
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        for stream in (process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except OSError:
                pass
        if stdout_thread.is_alive():
            stdout_thread.join(timeout=1.0)
        if stderr_thread.is_alive():
            stderr_thread.join(timeout=1.0)
        if capture_stderr is not None:
            capture_stderr.extend(stderr_lines)
        if cancelled:
            raise RenderCancelledError("Rendering was cancelled.")
        if process.returncode != 0:
            message = "".join(stderr_lines).strip() or "FFmpeg returned an unknown error."
            raise RenderError(message)

    @staticmethod
    def _piped_progress(
        callback: Callable[[str, float, str], None], has_visualizers: bool,
    ) -> Callable[[str, float, str], None]:
        """Rescale progress when Canvas capture runs inside the encode stage.

        The staged path reserves 66-78% of its scale for preparation because the
        Canvas video already exists. With live capture almost all the work
        happens while encoding, so preparation gets a small share instead.
        """
        encoding_start = 0.78 if has_visualizers else 0.66
        preparation_share = 0.12 if has_visualizers else 0.04
        encoding_stages = {"Capturing and encoding", "Finalizing export", "Complete"}

        def report(stage: str, fraction: float, message: str) -> None:
            if stage in encoding_stages:
                encoded = max(0.0, fraction - encoding_start) / (1.0 - encoding_start)
                scaled = preparation_share + encoded * (1.0 - preparation_share)
            else:
                scaled = min(1.0, fraction / encoding_start) * preparation_share
            callback(stage, min(1.0, max(0.0, scaled)), message)

        return report

    @staticmethod
    def _report(callback: Callable[[str, float, str], None] | None, stage: str,
                fraction: float, message: str) -> None:
        if callback:
            callback(stage, min(1.0, max(0.0, fraction)), message)

    @staticmethod
    def _timeline_duration(tracks: list[PlaylistTrack]) -> float:
        return compile_playlist(tracks, enabled_only=True).duration_seconds

    @staticmethod
    def _ffmetadata_escape(value: str) -> str:
        for character in ("\\", "=", ";", "#", "\n"):
            value = value.replace(character, "\\" + character)
        return value

    def _write_export_ffmetadata(
        self,
        temporary: Path,
        tracks: list[PlaylistTrack],
        metadata: "ExportMetadata",
        target: Path,
        compiled_plan: CompiledRenderPlan | None = None,
    ) -> Path | None:
        """Write an FFmetadata file with container tags and per-track chapters.

        Returns ``None`` when there is nothing worth embedding so the caller can
        skip the extra FFmpeg input entirely.
        """
        lines = [";FFMETADATA1"]
        title = metadata.title.strip() or target.stem
        if title:
            lines.append(f"title={self._ffmetadata_escape(title)}")
        if metadata.artist.strip():
            lines.append(f"artist={self._ffmetadata_escape(metadata.artist.strip())}")
        comment = metadata.comment.strip() or "Playlist Canvas"
        lines.append(f"comment={self._ffmetadata_escape(comment)}")

        chapters = metadata.include_chapters and len(tracks) > 1
        if chapters:
            track_by_id = {track.id: track for track in tracks}
            plan_chapters = (compiled_plan or compile_playlist(tracks, enabled_only=True)).metadata.chapters
            for index, chapter in enumerate(plan_chapters):
                track = track_by_id[chapter.track_id]
                start_ms = max(0, round(chapter.start * 1000))
                end_ms = max(start_ms + 1, round(chapter.end * 1000))
                name = (track.title or track.filename or f"Track {index + 1}").strip()
                lines.extend([
                    "",
                    "[CHAPTER]",
                    "TIMEBASE=1/1000",
                    f"START={start_ms}",
                    f"END={end_ms}",
                    f"title={self._ffmetadata_escape(name)}",
                ])
        path = temporary / "metadata.ffmeta"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _parse_progress_seconds(line: str) -> float | None:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                return float(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                return None
        if line.startswith("out_time="):
            try:
                hours, minutes, seconds = line.split("=", 1)[1].split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except ValueError:
                return None
        return None

    @staticmethod
    def _timed_progress_message(
        action: str, seconds: float, total_seconds: float, fraction: float,
    ) -> str:
        """Build one consistent live FFmpeg progress detail."""
        return (
            f"{action} {seconds:.1f}s / {total_seconds:.1f}s"
            f" · {round(min(1.0, max(0.0, fraction)) * 100)}%"
        )
