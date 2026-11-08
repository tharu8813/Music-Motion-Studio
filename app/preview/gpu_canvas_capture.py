"""Experimental offscreen OpenGL Canvas painter for export.

This is a migration experiment, NOT a zero-copy GPU->NVENC implementation.
It uses the same QGraphicsScene/QPainter rendering commands as the CPU path
but draws to an OpenGL framebuffer. FFmpeg's current rawvideo pipe still
requires a QImage readback into system memory. The feature is disabled until
project-specific image quality and end-to-end export timings are validated.

A GPU/context initialization or render failure disables this experiment for
the current worker thread and leaves the established CPU rasterizer available.
"""

from __future__ import annotations

import logging
import os
from threading import local

from PySide6.QtCore import QCoreApplication, QRectF, QSize, Qt, QThread
from PySide6.QtGui import QImage, QOffscreenSurface, QOpenGLContext, QPainter, QSurfaceFormat

from app.utils.performance import timed

LOGGER = logging.getLogger(__name__)
ENABLE_ENV = "PLAYLIST_CANVAS_EXPERIMENTAL_GL_EXPORT"
_TRUE = frozenset(("1", "true", "yes", "on"))
_local = local()

try:
    from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLPaintDevice
except ImportError:  # pragma: no cover: some PySide6 wheels omit QtOpenGL
    QOpenGLFramebufferObject = None
    QOpenGLPaintDevice = None


def gpu_canvas_export_requested() -> bool:
    """Opt in explicitly; never change existing project exports by default."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() in _TRUE


class _OpenGLCanvasRaster:
    """One Qt OpenGL context per capture worker thread; retained across frames."""

    def __init__(self) -> None:
        if QOpenGLFramebufferObject is None or QOpenGLPaintDevice is None:
            raise RuntimeError("QtOpenGL/QOpenGLPaintDevice is not installed")
        self.surface = QOffscreenSurface()
        self.surface.setFormat(QSurfaceFormat.defaultFormat())
        self.surface.create()
        if not self.surface.isValid():
            raise RuntimeError("Qt could not create an offscreen OpenGL surface")
        self.context = QOpenGLContext()
        self.context.setFormat(self.surface.format())
        if not self.context.create() or not self.context.isValid():
            raise RuntimeError("Qt could not create an OpenGL context")
        if not self.context.makeCurrent(self.surface):
            raise RuntimeError("Qt OpenGL context could not be made current")
        # The FBO is recreated only when dimensions change. Avoid context/FBO
        # initialization and GPU allocations on every exported frame.
        self._framebuffer = None
        self._device = None
        self._size = None
        self.context.doneCurrent()

    @timed("canvas.opengl_gpu_capture_seconds")
    def paint(self, scene, target: QRectF, source: QRectF,
              transparent: bool) -> QImage:
        width, height = round(target.width()), round(target.height())
        if not (0 < width <= 8192 and 0 < height <= 8192):
            raise ValueError("Invalid offscreen OpenGL capture geometry")
        if not self.context.makeCurrent(self.surface):
            raise RuntimeError("OpenGL context was lost")
        try:
            dimensions = (width, height)
            if self._size != dimensions or self._framebuffer is None:
                self._framebuffer = QOpenGLFramebufferObject(
                    QSize(width, height),
                    QOpenGLFramebufferObject.Attachment.CombinedDepthStencil,
                )
                if not self._framebuffer.isValid():
                    raise RuntimeError("Unable to allocate OpenGL framebuffer")
                self._device = QOpenGLPaintDevice(width, height)
                # Match the raster QImage coordinate system (top-left origin).
                self._device.setPaintFlipped(True)
                self._size = dimensions
            framebuffer = self._framebuffer
            if not framebuffer.bind():
                raise RuntimeError("OpenGL framebuffer bind failed")
            try:
                painter = QPainter(self._device)
                try:
                    painter.fillRect(
                        QRectF(0, 0, width, height),
                        Qt.GlobalColor.transparent if transparent else Qt.GlobalColor.black,
                    )
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    scene.render(painter, target, source, Qt.AspectRatioMode.IgnoreAspectRatio)
                finally:
                    painter.end()
                # This readback is mandatory for the existing FFmpeg pipe. It
                # costs a GPU/CPU sync and is NOT a zero-copy NVENC transport.
                image = framebuffer.toImage()
            finally:
                framebuffer.release()
            destination_format = (
                QImage.Format.Format_ARGB32_Premultiplied
                if transparent else QImage.Format.Format_RGB32
            )
            return image.convertToFormat(destination_format)
        finally:
            self.context.doneCurrent()


def capture_canvas_opengl(scene, target: QRectF, source: QRectF,
                          transparent: bool) -> QImage | None:
    """Return an experimental OpenGL capture, or None for CPU fallback."""
    if not gpu_canvas_export_requested():
        return None
    if getattr(_local, "failed", False):
        return None
    # On Windows some Qt platform implementations require the offscreen
    # surface to be created on the QGuiApplication thread. Never allocate a
    # graphics context from an arbitrary Python worker thread.
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return None
    try:
        renderer = getattr(_local, "renderer", None)
        if renderer is None:
            renderer = _OpenGLCanvasRaster()
            _local.renderer = renderer
        return renderer.paint(scene, target, source, transparent)
    except Exception as error:
        _local.failed = True
        _local.renderer = None
        LOGGER.warning(
            "Experimental OpenGL Canvas export failed; reverting to CPU QImage: %s",
            error,
        )
        return None


def reset_gpu_capture_for_tests() -> None:
    """Drop the current thread's experimental context/error state."""
    _local.renderer = None
    _local.failed = False
