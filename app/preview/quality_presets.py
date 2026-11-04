"""Preview-only quality policies. Final video export ignores these settings."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings


PREVIEW_QUALITY_KEY = "preview_quality_mode"
DEFAULT_PREVIEW_QUALITY = "resolution"


@dataclass(frozen=True, slots=True)
class PreviewQualityPreset:
    name_ko: str
    name_en: str
    description_ko: str
    description_en: str
    canvas_scale: float
    proxy_size: tuple[int, int] | None
    fps_cap: int | None
    downscale_samples: int
    recovery_samples: int
    minimum_factor: float
    prefer_resolution: bool = False
    keep_resolution: bool = False


PREVIEW_QUALITY_PRESETS = {
    "resolution": PreviewQualityPreset(
        "해상도 우선", "Prioritize resolution",
        "캔버스 해상도 100%와 최대 1080p 영상 프록시를 유지하고 FPS를 먼저 양보합니다.",
        "Keep a sharp canvas and up to 1080p video proxies, yielding FPS first.",
        1.0, (1920, 1080), None, 8, 3, 1.0,
        prefer_resolution=True,
    ),
    "frames": PreviewQualityPreset(
        "프레임 우선", "Prioritize frame rate",
        "캔버스와 영상을 가볍게 렌더링해 부드러운 재생을 우선합니다.",
        "Render lighter canvas and video frames for smoother playback.",
        0.8, (1280, 720), None, 2, 4, 0.35,
    ),
    "power": PreviewQualityPreset(
        "절전", "Power saving",
        "480p 영상 프록시, 낮은 캔버스 해상도 및 최대 30 FPS를 사용합니다.",
        "Use 480p video proxies, a smaller canvas and at most 30 FPS.",
        0.55, (854, 480), 30, 2, 6, 0.35,
    ),
    "quality": PreviewQualityPreset(
        "품질", "Maximum quality",
        "프록시 없이 원본 영상과 전체 캔버스 해상도를 사용합니다. 부하가 높을 수 있습니다.",
        "Use original video and full canvas resolution without proxies. Can be demanding.",
        1.0, None, None, 999999, 6, 1.0,
        prefer_resolution=True, keep_resolution=True,
    ),
}


def load_preview_quality() -> str:
    """Safely load a shared preference; default to resolution priority."""
    stored = str(QSettings().value(PREVIEW_QUALITY_KEY, DEFAULT_PREVIEW_QUALITY))
    return stored if stored in PREVIEW_QUALITY_PRESETS else DEFAULT_PREVIEW_QUALITY


def save_preview_quality(value: str) -> str:
    """Persist a known preview-only preset without affecting export settings."""
    preset = value if value in PREVIEW_QUALITY_PRESETS else DEFAULT_PREVIEW_QUALITY
    QSettings().setValue(PREVIEW_QUALITY_KEY, preset)
    return preset
