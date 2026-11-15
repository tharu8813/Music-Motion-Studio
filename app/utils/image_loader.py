"""Image loading helpers with an explicit SVG fallback for Canvas sources."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QImageReader, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


def load_pixmap(path: str | Path) -> QPixmap:
    """Load a raster image or rasterize a valid SVG into a transparent pixmap."""
    image_path = Path(path)
    pixmap = QPixmap(str(image_path))
    if not pixmap.isNull() or image_path.suffix.lower() != ".svg":
        return pixmap
    renderer = QSvgRenderer(str(image_path))
    if not renderer.isValid():
        return QPixmap()
    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        size = QSize(512, 512)
    image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(image)


def can_load_canvas_image(path: str | Path) -> bool:
    """Lightweight preflight using the same Qt image plugins/SVG fallback as Canvas.

    Checks that the file has a readable image header, without creating a
    QPixmap off the GUI thread or fully decoding large export images.
    """
    image_path = Path(path)
    if QImageReader(str(image_path)).canRead():
        return True
    return image_path.suffix.lower() == ".svg" and QSvgRenderer(str(image_path)).isValid()
