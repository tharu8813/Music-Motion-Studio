"""Production dynamic-layer transforms after the shared audio painter.

FFmpeg applies these transforms after PythonVisualizerRenderer, too. Keeping
them here avoids applying opacity twice or clipping rotation to the source box.
"""
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage, QPainter

from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, straight_bgra_bytes


def dynamic_layer_frame(image, overlay, width, height, *, seconds, output_scale=1.0):
    if image.isNull():
        raise ValueError("Cannot composite a missing dynamic overlay frame")
    binding = getattr(overlay, "keyframe_binding", None)
    canvas = QImage(width, height, QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if binding is not None:
            values = binding.values(seconds)
            source = binding.source
            painter.setOpacity(values.get("opacity", source.opacity))
            painter.translate((values.get("x", source.x) + source.width / 2) * output_scale,
                              (values.get("y", source.y) + source.height / 2) * output_scale)
            painter.rotate(values.get("rotation", source.rotation))
            ratio = values.get("scale", source.scale) / (source.scale or 1.0)
            painter.scale(ratio, ratio)
        else:
            painter.translate(overlay.x + image.width() / 2,
                              overlay.y + image.height() / 2)
            painter.rotate(getattr(overlay, "rotation", 0.0))
        painter.drawImage(QPointF(-image.width() / 2, -image.height() / 2), image)
    finally:
        painter.end()
    return NativeCanvasBgraLayer("pending", width, height, straight_bgra_bytes(canvas))


def dynamic_gpu_layer(image, overlay, width, height, *, seconds, output_scale=1.0):
    """Send untransformed painter pixels and an output-to-source matrix to D3D11."""
    from PySide6.QtGui import QTransform
    if image.isNull():
        raise ValueError("Cannot composite a missing dynamic overlay frame")
    binding = getattr(overlay, "keyframe_binding", None)
    transform = QTransform()
    opacity = 1.0
    if binding is not None:
        values = binding.values(seconds)
        source = binding.source
        opacity = values.get("opacity", source.opacity)
        transform.translate((values.get("x", source.x) + source.width / 2) * output_scale,
                            (values.get("y", source.y) + source.height / 2) * output_scale)
        transform.rotate(values.get("rotation", source.rotation))
        ratio = values.get("scale", source.scale) / (source.scale or 1.0)
        if ratio == 0:
            opacity = 0.0
            ratio = 1.0
        transform.scale(ratio, ratio)
    else:
        transform.translate(overlay.x + image.width() / 2, overlay.y + image.height() / 2)
        transform.rotate(getattr(overlay, "rotation", 0.0))
    transform.translate(-image.width() / 2, -image.height() / 2)
    inverse, valid = transform.inverted()
    if not valid:
        raise ValueError("Dynamic GPU transform is singular")
    return NativeCanvasBgraLayer("pending", image.width(), image.height(), straight_bgra_bytes(image),
        opacity, (inverse.m11(), inverse.m12(), inverse.m21(), inverse.m22(), inverse.dx(), inverse.dy()),
        (0, 0, width, height))
