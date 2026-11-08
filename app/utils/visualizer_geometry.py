"""Place an exported reactive overlay exactly where the Canvas source sits.

The Canvas uses a scale centered on the unscaled source rectangle. Round
only after mapping both rectangle edges onto the output pixel grid; two
intermediate integer rounds can move an exported source by one or more pixels.
"""
from __future__ import annotations


def visualizer_export_rect(source: object, render_scale: float) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) at the target render resolution."""
    scale = float(getattr(source, "scale", 1.0))
    output = max(1.0, float(render_scale))
    width = float(getattr(source, "width"))
    height = float(getattr(source, "height"))
    left = float(getattr(source, "x")) + (width - width * scale) / 2.0
    top = float(getattr(source, "y")) + (height - height * scale) / 2.0
    return (
        round(left * output),
        round(top * output),
        max(8, round(width * scale * output)),
        max(8, round(height * scale * output)),
    )
