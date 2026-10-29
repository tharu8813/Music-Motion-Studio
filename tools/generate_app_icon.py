"""Render SVG masters into PNG and multi-size Windows ICO assets.

Both icons share one motion-shaped M, without gradients, shadows or lettering.
Run with the project's Python: python tools/generate_app_icon.py
"""

from __future__ import annotations

from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / "app" / "resources"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def render(source: Path, size: int) -> QImage:
    """Rasterize the vector master independently at each Explorer/taskbar size."""
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError(f"Invalid SVG: {source}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def png_data(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Qt could not encode the icon as PNG.")
    return bytes(buffer.data())


def write_ico(source: Path, destination: Path) -> None:
    """Windows ICO container with independently rendered RGBA PNG entries."""
    payloads = [png_data(render(source, size)) for size in ICON_SIZES]
    offset = 6 + 16 * len(ICON_SIZES)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(ICON_SIZES)))
    for size, payload in zip(ICON_SIZES, payloads):
        directory.extend(struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset,
        ))
        offset += len(payload)
    destination.write_bytes(bytes(directory) + b"".join(payloads))


def main() -> int:
    for stem in ("app_icon", "project_file_icon"):
        source = RESOURCES / f"{stem}.svg"
        write_ico(source, RESOURCES / f"{stem}.ico")
        if not render(source, 512).save(str(RESOURCES / f"{stem}.png"), "PNG"):
            raise RuntimeError(f"Could not save {stem}.png")
        print(f"{stem}: SVG + 512px PNG + {len(ICON_SIZES)} ICO sizes")
    readme_icon = ROOT / "docs" / "images" / "music-motion-studio-icon.png"
    readme_icon.parent.mkdir(parents=True, exist_ok=True)
    readme_icon.write_bytes((RESOURCES / "app_icon.png").read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
