"""Plan native video commands on the same clock as FFmpeg video overlays."""
from concurrent.futures import CancelledError
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import subprocess

from app.models.source import SourceType
from app.preview.native_canvas_layer_packets import NativeCanvasVideoLayer
from app.renderer.ffmpeg_renderer import RenderCancelledError, RenderError
from app.services.export_validation_service import _find_ffprobe
from app.utils.cancellable_subprocess import run_probe
from app.video.timeline import VideoClipOccurrence, build_video_occurrences


def video_preflight_reason(source):
    return (
        f"{source.name or source.id} [video; {source.id}]: "
        "native_video_frame_provider_unavailable for these export settings. "
        "Use the existing FFmpeg export engine for the entire project."
    )


def supports_native_video(source):
    return (
        source.video_timing_mode == "timeline" and len(source.video_paths) == 1
        and source.video_repeat_mode in {"once", "loop_one"}
        and source.video_muted and 0.05 <= source.video_speed <= 5.0
        and source.image_fit_mode in {"stretch", "cover"}
        and source.animation_in == source.animation_out == source.loop_motion == source.mask_shape == "none"
        and not (source.keyframes.animated or source.rotation or source.frame_width
                 or source.brightness or source.contrast or source.blur
                 or source.shadow.enabled or source.uses_bass_reaction)
        and 0.0 <= source.video_saturation <= 3.0
    )


@dataclass(frozen=True, slots=True)
class NativeVideoSource:
    index: int
    path: Path
    duration: float
    occurrence: VideoClipOccurrence | None
    rect: tuple[int, int, int, int]
    speed: float
    opacity: float
    crop: bool
    flip_horizontal: bool
    flip_vertical: bool
    border_radius: float
    saturation: float

    def layer_at(self, seconds):
        occurrence = self.occurrence
        media_time = None
        media_limit = None
        if (occurrence is not None and occurrence.timeline_start - 1e-9 <= seconds
                < occurrence.timeline_start + occurrence.duration_seconds - 1e-9):
            elapsed = max(0.0, seconds - occurrence.timeline_start) * self.speed
            if occurrence.loop_media:
                cycle = round(elapsed / self.duration)
                elapsed = 0.0 if abs(elapsed - cycle * self.duration) < 1e-8 else elapsed % self.duration
            else:
                media_limit = round(occurrence.duration_seconds * self.speed * 10_000_000)
            media_time = round((occurrence.media_start_seconds + elapsed) * 10_000_000)
        x, y, w, h = self.rect
        return NativeCanvasVideoLayer(
            "pending", 1, 1, b"", self.opacity, clip=(x, y, x + w, y + h),
            asset=self.index, media_time=media_time, media_limit=media_limit, crop=self.crop,
            flip_horizontal=self.flip_horizontal, flip_vertical=self.flip_vertical,
            border_radius=self.border_radius,
            saturation=self.saturation,
        )


def native_video_rect(source, scale):
    width, height = max(8, round(source.width * source.scale)), max(8, round(source.height * source.scale))
    x = round(source.x + (source.width - width) / 2)
    y = round(source.y + (source.height - height) / 2)
    return round(x * scale), round(y * scale), max(8, round(width * scale)), max(8, round(height * scale))


def prepare_native_video_sources(project, plan, scale, ffmpeg, cancel):
    sources = [source for source in project.sources
               if source.visible and source.source_type is SourceType.VIDEO]
    if not sources:
        return {}
    probe = _find_ffprobe(ffmpeg)
    if probe is None:
        raise RenderError("FFprobe is required to validate native H.264 video sources.")
    prepared, durations = {}, {}
    for source in sources:
        if not supports_native_video(source):
            raise RenderError(video_preflight_reason(source))
        path = Path(source.video_paths[0]).resolve()
        if path.suffix.lower() != ".mp4" or not path.is_file():
            raise RenderError(f"Native video requires a local H.264 MP4 file: {path.name}")
        if path not in durations:
            durations[path] = _probe_video(probe, path, cancel)
        duration = durations[path]
        start = min(plan.duration_seconds, max(0.0, source.timeline_start))
        available = plan.duration_seconds - start
        if source.timeline_duration > 0:
            available = min(source.timeline_duration, available)
        occurrences = build_video_occurrences(
            source, [str(path)], {str(path): duration}, start, available,
        )
        rect = native_video_rect(source, scale)
        prepared[source.id] = NativeVideoSource(
            len(prepared), path, duration, next(iter(occurrences), None),
            rect, source.video_speed, source.opacity, source.image_fit_mode == "cover",
            source.flip_horizontal, source.flip_vertical,
            max(0.0, min(source.border_radius * source.scale * scale, min(rect[2:]) / 2)),
            0.0 if source.video_grayscale else source.video_saturation,
        )
    return prepared


def _probe_video(probe, path, cancel):
    try:
        result = run_probe([
            str(probe), "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=codec_name,pix_fmt,width,height,sample_aspect_ratio,field_order,"
            "color_space,color_transfer,color_range,start_time,duration:stream_side_data=rotation",
            "-of", "json", str(path),
        ], cancel_event=cancel, timeout=15)
        if result.returncode:
            raise ValueError("Could not read media metadata")
        video, = json.loads(result.stdout)["streams"]
        width, height = video["width"], video["height"]
        duration = float(Fraction(video["duration"]))
        if not (
            video["codec_name"] == "h264" and video["pix_fmt"] in {"yuv420p", "yuvj420p"}
            and 0 < width <= 3840 and 0 < height <= 2160 and width % 2 == height % 2 == 0
            and video.get("field_order") in (None, "unknown", "progressive")
            and video.get("sample_aspect_ratio") in (None, "N/A", "1:1")
            and video.get("color_space") in (None, "unknown", "bt709", "smpte170m", "bt470bg")
            and video.get("color_transfer") in (None, "unknown", "bt709", "smpte170m", "bt470bg")
            and video.get("color_range") in (None, "unknown", "tv", "pc")
            and all(item.get("rotation", 0) == 0 for item in video.get("side_data_list", ()))
            and abs(float(video.get("start_time", "0"))) < 1e-6
            and 0 < duration <= 86400
        ):
            raise ValueError("Unsupported video format or timing")
        return duration
    except CancelledError as error:
        raise RenderCancelledError("Native GPU export was cancelled.") from error
    except (ValueError, TypeError, KeyError, ZeroDivisionError, OSError, subprocess.TimeoutExpired) as error:
        raise RenderError(f"Native video requires one progressive SDR H.264 stream in {path.name}: {error}") from error
