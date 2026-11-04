"""Display timing shared by the interactive previews."""
from math import isfinite

from PySide6.QtGui import QScreen


def screen_preview_fps(screen: QScreen | None) -> int:
    refresh_rate = screen.refreshRate() if screen is not None else 0.0
    return max(1, round(refresh_rate)) if isfinite(refresh_rate) and refresh_rate > 0 else 60


def preview_interval_ms(fps: int) -> int:
    return max(1, round(1000 / max(1, fps)))
