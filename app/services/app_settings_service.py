"""Persistent application and default render settings."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal

from app.renderer.ffmpeg_renderer import (
    RenderSettings,
    WORK_MODE_AUTO,
    WORK_MODES,
)
from app.services.video_encoder_service import AUTO_VIDEO_ENCODER, VideoEncoderAdvisor
from app.services.export_validation_service import EXPORT_FPS_OPTIONS


RESOLUTIONS: dict[str, tuple[int, int]] = {
    "1280 × 720 (HD)": (1280, 720),
    "1920 × 1080 (Full HD)": (1920, 1080),
    "2560 × 1440 (QHD)": (2560, 1440),
    "3840 × 2160 (4K)": (3840, 2160),
}
VIDEO_ENCODERS: dict[str, str] = {
    "자동 선택 (권장)": AUTO_VIDEO_ENCODER,
    "CPU · H.264 (libx264)": "libx264",
    "CPU · H.265 (libx265)": "libx265",
    "NVIDIA GPU · H.264 (NVENC)": "h264_nvenc",
    "NVIDIA GPU · H.265 (NVENC)": "hevc_nvenc",
    "Intel GPU · H.264 (Quick Sync)": "h264_qsv",
    "Intel GPU · H.265 (Quick Sync)": "hevc_qsv",
    "AMD GPU · H.264 (AMF)": "h264_amf",
    "AMD GPU · H.265 (AMF)": "hevc_amf",
}
VIDEO_CODECS = tuple(VIDEO_ENCODERS.values())
ENCODING_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
    "slow", "slower", "veryslow",
)
AUDIO_BITRATES = ("128k", "192k", "256k", "320k")
PREVIEW_BACKENDS = ("gpu_layers", "cpu")
EXPORT_NOTIFICATION_MODES = ("always", "unfocused")
LYRICS_AUTO_ATTACH_MODES = ("always", "ask", "never")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """User-configurable defaults used by each new render."""

    ffmpeg_path: str = ""
    output_directory: str = ""
    resolution_name: str = "1920 × 1080 (Full HD)"
    fps: int = 30
    video_codec: str = AUTO_VIDEO_ENCODER
    crf: int = 18
    preset: str = "medium"
    audio_bitrate: str = "192k"
    work_mode: str = WORK_MODE_AUTO
    smooth_scrolling: bool = True
    smooth_scroll_duration_ms: int = 180
    preview_backend: str = "gpu_layers"
    export_notifications_enabled: bool = False
    export_notification_mode: str = "unfocused"
    export_notify_visuals: bool = True
    export_notify_audio: bool = True
    export_notify_effects: bool = True
    export_notify_encode: bool = True
    export_notify_complete: bool = True
    export_notify_failures: bool = True
    # How a same-name or similar-name lyric file next to an imported song is
    # handled: "always" attaches exact matches automatically, "ask" confirms
    # first, "never" disables the feature. Similar-name matches always prompt
    # unless the mode is "never".
    lyrics_auto_attach_mode: str = "always"
    # Per-export dimensions derived from the active project ratio. They are not
    # persisted as app-wide defaults because every project can have a different ratio.
    render_width: int = 0
    render_height: int = 0

    @property
    def resolution(self) -> tuple[int, int]:
        """Return the validated pixel dimensions for the selected resolution."""
        if self.render_width > 0 and self.render_height > 0:
            return self.render_width, self.render_height
        return RESOLUTIONS.get(self.resolution_name, RESOLUTIONS["1920 × 1080 (Full HD)"])

    def render_settings(self) -> RenderSettings:
        """Convert persisted defaults into settings accepted by the FFmpeg renderer."""
        width, height = self.resolution
        concrete_codec = (
            VideoEncoderAdvisor.automatic_encoder()
            if self.video_codec == AUTO_VIDEO_ENCODER else self.video_codec
        )
        return RenderSettings(
            fps=self.fps,
            video_codec=concrete_codec,
            crf=self.crf,
            preset=self.preset,
            audio_bitrate=self.audio_bitrate,
            output_width=width,
            output_height=height,
            work_mode=(
                self.work_mode if self.work_mode in WORK_MODES else WORK_MODE_AUTO
            ),
        )


class AppSettingsService(QObject):
    """Loads and writes settings independently from the main window."""

    changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings()
        self._current = self._load()

    @property
    def current(self) -> AppSettings:
        """Return the currently active immutable settings snapshot."""
        return self._current

    def save(self, settings: AppSettings) -> None:
        """Persist validated settings and notify dependent UI components."""
        normalized = AppSettings(
            ffmpeg_path=settings.ffmpeg_path.strip(),
            output_directory=settings.output_directory.strip(),
            resolution_name=(settings.resolution_name if settings.resolution_name in RESOLUTIONS
                             else "1920 × 1080 (Full HD)"),
            fps=settings.fps if settings.fps in EXPORT_FPS_OPTIONS else 30,
            video_codec=(
                settings.video_codec if settings.video_codec in VIDEO_CODECS
                else AUTO_VIDEO_ENCODER
            ),
            crf=max(0, min(51, settings.crf)),
            preset=settings.preset if settings.preset in ENCODING_PRESETS else "medium",
            audio_bitrate=(settings.audio_bitrate if settings.audio_bitrate in AUDIO_BITRATES
                           else "192k"),
            work_mode=(
                settings.work_mode if settings.work_mode in WORK_MODES
                else WORK_MODE_AUTO
            ),
            smooth_scrolling=bool(settings.smooth_scrolling),
            smooth_scroll_duration_ms=max(
                80, min(420, int(settings.smooth_scroll_duration_ms))
            ),
            preview_backend=(
                settings.preview_backend
                if settings.preview_backend in PREVIEW_BACKENDS else "gpu_layers"
            ),
            export_notifications_enabled=bool(
                settings.export_notifications_enabled
            ),
            export_notification_mode=(
                settings.export_notification_mode
                if settings.export_notification_mode in EXPORT_NOTIFICATION_MODES
                else "unfocused"
            ),
            export_notify_visuals=bool(settings.export_notify_visuals),
            export_notify_audio=bool(settings.export_notify_audio),
            export_notify_effects=bool(settings.export_notify_effects),
            export_notify_encode=bool(settings.export_notify_encode),
            export_notify_complete=bool(settings.export_notify_complete),
            export_notify_failures=bool(settings.export_notify_failures),
            lyrics_auto_attach_mode=(
                settings.lyrics_auto_attach_mode
                if settings.lyrics_auto_attach_mode in LYRICS_AUTO_ATTACH_MODES
                else "always"
            ),
        )
        self._settings.beginGroup("export")
        self._settings.setValue("ffmpeg_path", normalized.ffmpeg_path)
        self._settings.setValue("output_directory", normalized.output_directory)
        self._settings.setValue("resolution", normalized.resolution_name)
        self._settings.setValue("fps", normalized.fps)
        self._settings.setValue("video_codec", normalized.video_codec)
        self._settings.setValue("video_codec_auto_v1", True)
        self._settings.setValue("crf", normalized.crf)
        self._settings.setValue("preset", normalized.preset)
        self._settings.setValue("audio_bitrate", normalized.audio_bitrate)
        self._settings.setValue("work_mode", normalized.work_mode)
        self._settings.endGroup()
        self._settings.beginGroup("interface")
        self._settings.setValue("smooth_scrolling", normalized.smooth_scrolling)
        self._settings.setValue(
            "smooth_scroll_duration_ms", normalized.smooth_scroll_duration_ms
        )
        self._settings.setValue("preview_backend", normalized.preview_backend)
        self._settings.endGroup()
        self._settings.beginGroup("content_import")
        self._settings.setValue(
            "lyrics_auto_attach_mode", normalized.lyrics_auto_attach_mode
        )
        self._settings.endGroup()
        self._settings.beginGroup("export_notifications")
        self._settings.setValue("enabled", normalized.export_notifications_enabled)
        self._settings.setValue("mode", normalized.export_notification_mode)
        self._settings.setValue("visuals", normalized.export_notify_visuals)
        self._settings.setValue("audio", normalized.export_notify_audio)
        self._settings.setValue("effects", normalized.export_notify_effects)
        self._settings.setValue("encode", normalized.export_notify_encode)
        self._settings.setValue("complete", normalized.export_notify_complete)
        self._settings.setValue("failures", normalized.export_notify_failures)
        self._settings.endGroup()
        self._current = normalized
        self.changed.emit(normalized)

    def _load(self) -> AppSettings:
        self._settings.beginGroup("export")
        stored_video_codec = str(
            self._settings.value("video_codec", AUTO_VIDEO_ENCODER)
        )
        # Existing installations used CPU H.264 as an indistinguishable old
        # default. Migrate it once to Automatic; a later explicit CPU choice is
        # preserved because the marker has already been written.
        if not self._bool_value("video_codec_auto_v1", False):
            if stored_video_codec == "libx264":
                stored_video_codec = AUTO_VIDEO_ENCODER
            self._settings.setValue("video_codec_auto_v1", True)
        values = AppSettings(
            ffmpeg_path=str(self._settings.value("ffmpeg_path", "")),
            output_directory=str(self._settings.value("output_directory", "")),
            resolution_name=str(self._settings.value("resolution", "1920 × 1080 (Full HD)")),
            fps=self._int_value("fps", 30),
            video_codec=stored_video_codec,
            crf=self._int_value("crf", 18),
            preset=str(self._settings.value("preset", "medium")),
            audio_bitrate=str(self._settings.value("audio_bitrate", "192k")),
            work_mode=str(self._settings.value("work_mode", WORK_MODE_AUTO)),
        )
        if values.fps not in EXPORT_FPS_OPTIONS:
            values = replace(values, fps=30)
        self._settings.endGroup()
        self._settings.beginGroup("interface")
        values = replace(
            values,
            smooth_scrolling=self._bool_value("smooth_scrolling", True),
            smooth_scroll_duration_ms=max(
                80, min(420, self._int_value("smooth_scroll_duration_ms", 180))
            ),
            preview_backend=str(
                self._settings.value("preview_backend", "gpu_layers")
            ),
        )
        self._settings.endGroup()
        if values.preview_backend not in PREVIEW_BACKENDS:
            values = replace(values, preview_backend="gpu_layers")
        self._settings.beginGroup("content_import")
        values = replace(
            values,
            lyrics_auto_attach_mode=str(
                self._settings.value("lyrics_auto_attach_mode", "always")
            ),
        )
        self._settings.endGroup()
        if values.lyrics_auto_attach_mode not in LYRICS_AUTO_ATTACH_MODES:
            values = replace(values, lyrics_auto_attach_mode="always")
        self._settings.beginGroup("export_notifications")
        values = replace(
            values,
            export_notifications_enabled=self._bool_value("enabled", False),
            export_notification_mode=str(
                self._settings.value("mode", "unfocused")
            ),
            export_notify_visuals=self._bool_value("visuals", True),
            export_notify_audio=self._bool_value("audio", True),
            export_notify_effects=self._bool_value("effects", True),
            export_notify_encode=self._bool_value("encode", True),
            export_notify_complete=self._bool_value("complete", True),
            export_notify_failures=self._bool_value("failures", True),
        )
        self._settings.endGroup()
        if values.export_notification_mode not in EXPORT_NOTIFICATION_MODES:
            values = replace(values, export_notification_mode="unfocused")
        if values.work_mode not in WORK_MODES:
            values = replace(values, work_mode=WORK_MODE_AUTO)
        return values

    def _int_value(self, key: str, default: int) -> int:
        try:
            return int(self._settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def _bool_value(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def default_output_directory() -> Path:
        """Return a writable default without creating any folder prematurely."""
        return Path.home() / "Videos" / "Music Motion Studio"
