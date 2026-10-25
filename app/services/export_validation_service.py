"""Post-export metadata checks and automatic export workload selection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.utils.subprocess_utils import hidden_process_kwargs


EXPORT_FPS_OPTIONS = (24, 30, 50, 60)


@dataclass(frozen=True, slots=True)
class ExportValidationResult:
    """Observed output metadata and any actionable mismatch messages."""

    available: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration_seconds: float = 0.0
    frame_count: int = 0
    codec: str = ""
    pixel_format: str = ""
    warnings: tuple[str, ...] = ()
    error: str = ""
    audio_only: bool = False

    @property
    def passed(self) -> bool:
        return self.available and not self.warnings and not self.error

    @property
    def summary(self) -> str:
        if not self.available:
            return self.error or "Output metadata was unavailable."
        if self.audio_only:
            return f"{self.codec.upper()} · {self.duration_seconds:.2f}s"
        fps = f"{self.fps:.3f}".rstrip("0").rstrip(".")
        frame_text = f", {self.frame_count} frames" if self.frame_count else ""
        return f"{self.width} × {self.height} · {fps} FPS · {self.duration_seconds:.2f}s{frame_text}"


def select_export_work_mode(width: int, height: int, fps: int) -> str:
    """Choose a conservative workload mode from the requested export cost."""
    pixels = max(1, int(width)) * max(1, int(height))
    logical_cpus = os.cpu_count() or 4
    if pixels >= 3840 * 2160 or (pixels >= 2560 * 1440 and fps >= 50):
        return "stable"
    if logical_cpus >= 8 and pixels <= 1920 * 1080 and fps <= 30:
        return "max_speed"
    return "auto"


def _find_ffprobe(ffmpeg_executable: str | Path | None) -> Path | None:
    if ffmpeg_executable:
        candidate = Path(ffmpeg_executable)
        sibling = candidate.with_name("ffprobe.exe" if candidate.suffix.lower() == ".exe" else "ffprobe")
        if sibling.is_file():
            return sibling
    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    return Path(found) if found else None


def validate_export_output(
    output_path: str | Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_fps: int,
    expected_duration_seconds: float = 0.0,
    ffmpeg_executable: str | Path | None = None,
) -> ExportValidationResult:
    """Probe the completed file without failing exports when ffprobe is absent."""
    probe = _find_ffprobe(ffmpeg_executable)
    if probe is None:
        return ExportValidationResult(False, error="FFprobe was not found.")
    try:
        completed = subprocess.run(
            [str(probe), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,codec_name,pix_fmt",
             "-show_entries", "format=duration", "-of", "json", str(output_path)],
            capture_output=True, text=True, check=False, timeout=15,
            **hidden_process_kwargs(),
        )
        payload = json.loads(completed.stdout or "{}")
        stream = (payload.get("streams") or [{}])[0]
        format_data = payload.get("format") or {}

        def ratio(value: object) -> float:
            text = str(value or "0")
            if "/" in text:
                numerator, denominator = text.split("/", 1)
                return float(numerator) / max(1.0, float(denominator))
            return float(text)

        observed_fps = ratio(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
        duration = float(format_data.get("duration") or 0.0)
        frame_count = int(stream.get("nb_frames") or 0)
        warnings: list[str] = []
        if int(stream.get("width") or 0) != int(expected_width) or int(stream.get("height") or 0) != int(expected_height):
            warnings.append(f"resolution {stream.get('width')}×{stream.get('height')} (expected {expected_width}×{expected_height})")
        if abs(observed_fps - expected_fps) > 0.05:
            warnings.append(f"FPS {observed_fps:.3f} (expected {expected_fps})")
        if expected_duration_seconds > 0 and abs(duration - expected_duration_seconds) > max(0.12, 2.0 / max(1, expected_fps)):
            warnings.append(f"duration {duration:.2f}s (expected {expected_duration_seconds:.2f}s)")
        return ExportValidationResult(
            True, int(stream.get("width") or 0), int(stream.get("height") or 0),
            observed_fps, duration, frame_count, str(stream.get("codec_name") or ""),
            str(stream.get("pix_fmt") or ""), tuple(warnings),
            "" if completed.returncode == 0 else (completed.stderr or "FFprobe failed").strip(),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        return ExportValidationResult(False, error=f"FFprobe failed: {error}")


def validate_audio_export_output(
    output_path: str | Path, expected_duration_seconds: float,
    ffmpeg_executable: str | Path | None = None,
) -> ExportValidationResult:
    """Check that an audio export has sound, no video, and the resolved mix duration."""
    probe = _find_ffprobe(ffmpeg_executable)
    if probe is None:
        return ExportValidationResult(False, error="FFprobe was not found.", audio_only=True)
    try:
        completed = subprocess.run(
            [str(probe), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-show_entries", "format=duration", "-of", "json", str(output_path)],
            capture_output=True, text=True, check=False, timeout=15,
            **hidden_process_kwargs(),
        )
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
        audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        warnings = []
        if not audio:
            warnings.append("No audio stream was found.")
        if any(item.get("codec_type") == "video" for item in streams):
            warnings.append("An unexpected video stream was found.")
        if abs(duration - expected_duration_seconds) > 0.15:
            warnings.append(f"duration {duration:.2f}s (expected {expected_duration_seconds:.2f}s)")
        return ExportValidationResult(
            True, duration_seconds=duration, codec=str(audio.get("codec_name") or ""),
            warnings=tuple(warnings), audio_only=True,
            error="" if completed.returncode == 0 else (completed.stderr or "FFprobe failed").strip(),
        )
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        return ExportValidationResult(False, error=f"FFprobe failed: {error}", audio_only=True)
