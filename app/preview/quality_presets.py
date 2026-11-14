"""Preview-only quality policies. Final video export ignores these settings."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings


PREVIEW_QUALITY_KEY = "preview_quality_mode"
DEFAULT_PREVIEW_QUALITY = "balanced"


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


# The user-facing menu has exactly three modes. Keys are intentionally distinct
# from old "power": that value meant what "balanced" means now, so reusing the
# old key for the new, lighter power-saving mode would silently alter a saved
# user's preference at the next launch.
PREVIEW_QUALITY_PRESETS = {
    "eco": PreviewQualityPreset(
        "절전", "Power saving",
        "360p 영상 프록시와 40% 캔버스 배율, 최대 24 FPS로 자원을 절약합니다.",
        "Use 360p video proxies, 40% canvas scale and at most 24 FPS.",
        0.4, (640, 360), 24, 2, 6, 0.35,
    ),
    "balanced": PreviewQualityPreset(
        "균형", "Balanced",
        "기존 절전 모드와 동일하게 480p 영상 프록시, 55% 캔버스 배율 및 최대 30 FPS를 사용합니다.",
        "Keep the former Power saving behavior: 480p proxies, 55% canvas scale and up to 30 FPS.",
        0.55, (854, 480), 30, 2, 6, 0.35,
    ),
    "quality": PreviewQualityPreset(
        "최상", "Maximum quality",
        "프록시 없이 원본 영상과 전체 캔버스 해상도를 사용합니다. 부하가 높을 수 있습니다.",
        "Use original video and full canvas resolution without proxies. Can be demanding.",
        1.0, None, None, 999999, 6, 1.0,
        prefer_resolution=True, keep_resolution=True,
    ),
}

# Upgrade existing preferences without forcing previously lightweight preview
# users onto the new 100%-resolution, original-video "quality" tier.
LEGACY_PREVIEW_QUALITY_ALIASES = {
    "resolution": "balanced",
    "frames": "balanced",
    "power": "balanced",
}


def normalize_preview_quality(value: str) -> str:
    """Resolve saved legacy keys while exposing only the three current modes."""
    normalized = LEGACY_PREVIEW_QUALITY_ALIASES.get(str(value), str(value))
    return normalized if normalized in PREVIEW_QUALITY_PRESETS else DEFAULT_PREVIEW_QUALITY


def load_preview_quality() -> str:
    """Safely migrate a stored preview-only preference to the new tier names."""
    settings = QSettings()
    stored = str(settings.value(PREVIEW_QUALITY_KEY, DEFAULT_PREVIEW_QUALITY))
    normalized = normalize_preview_quality(stored)
    if stored != normalized:
        settings.setValue(PREVIEW_QUALITY_KEY, normalized)
    return normalized


def save_preview_quality(value: str) -> str:
    """Persist a known preview-only preset without affecting export settings."""
    preset = normalize_preview_quality(value)
    QSettings().setValue(PREVIEW_QUALITY_KEY, preset)
    return preset
