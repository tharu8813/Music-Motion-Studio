"""Persistent OpenGL texture presentation for the interactive preview.

This module deliberately owns only the presentation backend.  Later GPU
pipeline stages can feed multiple textures into the same surface without
coupling playback controls to OpenGL resource lifetime.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, replace
from math import cos, radians, sin
from typing import Hashable, Sequence

import numpy as np

from app.utils.performance import timed

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImage, QOpenGLContext, QSurfaceFormat
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtWidgets import QSizePolicy

try:
    from PySide6.QtOpenGL import (
        QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
        QOpenGLTexture, QOpenGLTextureBlitter, QOpenGLVertexArrayObject,
    )
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except ImportError:  # pragma: no cover - optional in some Qt deployments
    QOpenGLTexture = None
    QOpenGLTextureBlitter = None
    QOpenGLBuffer = None
    QOpenGLShader = None
    QOpenGLShaderProgram = None
    QOpenGLWidget = None


GPU_TEXTURE_SURFACE_AVAILABLE = all(
    value is not None
    for value in (
        QOpenGLWidget, QOpenGLTexture, QOpenGLTextureBlitter,
        QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
    )
)


@dataclass(frozen=True, slots=True)
class GpuBackendInfo:
    """Stable diagnostics for the active preview graphics context."""

    api: str
    version: str
    vendor: str = ""
    renderer: str = ""

    @property
    def label(self) -> str:
        detail = self.renderer or self.vendor
        return f"{self.api} {self.version}" + (f" · {detail}" if detail else "")


@dataclass(frozen=True, slots=True)
class GpuColorFilter:
    """Color adjustments executed by the preview fragment shader."""

    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 1.0
    grayscale: bool = False

    @property
    def active(self) -> bool:
        return (
            self.brightness != 0.0
            or self.contrast != 0.0
            or self.saturation != 1.0
            or self.grayscale
        )


@dataclass(frozen=True, slots=True)
class GpuPreviewLayer:
    """One image texture positioned in preview-canvas pixel coordinates."""

    key: Hashable
    image: QImage
    target: QRectF
    opacity: float = 1.0
    rotation: float = 0.0
    color_filter: GpuColorFilter | None = None
    video_frame: QVideoFrame | None = None
    native_serial: int = 0
    native_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class GpuUploadStats:
    """Observable queue and upload counters for preview diagnostics."""

    submitted_frames: int = 0
    presented_frames: int = 0
    dropped_pending_frames: int = 0
    texture_uploads: int = 0
    texture_reuses: int = 0
    uploaded_bytes: int = 0
    texture_evictions: int = 0
    cached_textures: int = 0
    allocated_bytes: int = 0
    peak_allocated_bytes: int = 0
    texture_budget_bytes: int = 0
    filtered_layers: int = 0
    texture_allocations: int = 0
    presented_scene_frames: int = 0


@dataclass(frozen=True, slots=True)
class _TextureEntry:
    texture: object
    cache_key: int
    width: int
    height: int
    allocated_bytes: int
    last_used_frame: int
    aux_texture: object | None = None
    video_format: str = ""


if GPU_TEXTURE_SURFACE_AVAILABLE:
    class GpuTexturePreviewSurface(QOpenGLWidget):
        """Upload and composite the newest preview layers on persistent textures."""

        backend_ready = Signal(object)
        backend_failed = Signal(str)
        frame_presented = Signal()

        def __init__(
            self, parent=None, *, texture_budget_bytes: int = 256 * 1024 * 1024,
        ) -> None:
            super().__init__(parent)
            self._pending_image = QImage()
            self._pending_canvas_size = QSize()
            self._pending_layers: tuple[GpuPreviewLayer, ...] = ()
            self._active_canvas_size = QSize()
            self._active_layers: tuple[GpuPreviewLayer, ...] = ()
            self._textures: dict[Hashable, _TextureEntry] = {}
            self._blitter = None
            self._filter_program = None
            self._filter_buffer = None
            self._filter_vao = None
            self._video_program = None
            self._functions = None
            self._ready = False
            self._failure_reported = False
            self._submitted_frames = 0
            self._presented_frames = 0
            self._presented_scene_frames = 0
            self._last_presented_serial = 0
            self._dropped_pending_frames = 0
            self._texture_uploads = 0
            self._texture_allocations = 0
            self._texture_reuses = 0
            self._uploaded_bytes = 0
            self._texture_evictions = 0
            self._allocated_bytes = 0
            self._peak_allocated_bytes = 0
            self._texture_budget_bytes = max(
                1 * 1024 * 1024, int(texture_budget_bytes),
            )
            self._texture_retention_frames = 90
            self._frame_serial = 0
            self.backend_info: GpuBackendInfo | None = None
            surface_format = QSurfaceFormat()
            surface_format.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
            surface_format.setVersion(3, 3)
            surface_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
            surface_format.setSwapInterval(1)
            surface_format.setSamples(0)
            self.setFormat(surface_format)
            self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
            )
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
            self.frameSwapped.connect(self._on_frame_swapped)

        @property
        def ready(self) -> bool:
            return self._ready

        @property
        def frame_pending(self) -> bool:
            """Return whether a newer scene is waiting for the next GL paint."""
            return bool(self._pending_layers)

        @property
        def upload_stats(self) -> GpuUploadStats:
            return GpuUploadStats(
                submitted_frames=self._submitted_frames,
                presented_frames=self._presented_frames,
                presented_scene_frames=self._presented_scene_frames,
                dropped_pending_frames=self._dropped_pending_frames,
                texture_uploads=self._texture_uploads,
                texture_allocations=self._texture_allocations,
                texture_reuses=self._texture_reuses,
                uploaded_bytes=self._uploaded_bytes,
                texture_evictions=self._texture_evictions,
                cached_textures=len(self._textures),
                allocated_bytes=self._allocated_bytes,
                peak_allocated_bytes=self._peak_allocated_bytes,
                texture_budget_bytes=self._texture_budget_bytes,
                filtered_layers=sum(
                    1 for layer in self._active_layers
                    if layer.color_filter is not None and layer.color_filter.active
                ),
            )

        def set_image(self, image: QImage) -> None:
            """Keep only the newest frame until the next OpenGL paint."""
            self._pending_image = QImage(image)
            self.set_layers(
                image.size(),
                (GpuPreviewLayer(
                    "__flattened_preview__", QImage(image),
                    QRectF(0.0, 0.0, float(image.width()), float(image.height())),
                ),),
            )

        def set_layers(
            self, canvas_size: QSize, layers: Sequence[GpuPreviewLayer],
        ) -> None:
            """Replace the queued scene while preserving reusable GPU textures."""
            self._submitted_frames += 1
            if self._pending_layers:
                self._dropped_pending_frames += 1
            self._pending_canvas_size = QSize(canvas_size)
            self._pending_layers = tuple(
                GpuPreviewLayer(
                    layer.key, QImage(layer.image), QRectF(layer.target),
                    layer.opacity, layer.rotation, layer.color_filter,
                    QVideoFrame(layer.video_frame)
                    if layer.video_frame is not None and layer.video_frame.isValid()
                    else None,
                    layer.native_serial,
                    layer.native_scale,
                )
                for layer in layers
                if (
                    not layer.image.isNull()
                    or (layer.video_frame is not None and layer.video_frame.isValid())
                )
            )
            self.update()

        def _on_frame_swapped(self) -> None:
            self._presented_frames += 1
            if self._frame_serial > self._last_presented_serial:
                self._presented_scene_frames += 1
                self._last_presented_serial = self._frame_serial
            self.frame_presented.emit()

        def initializeGL(self) -> None:  # type: ignore[override]
            try:
                context = self.context()
                if context is None or not context.isValid():
                    raise RuntimeError("Could not create a valid OpenGL context.")
                self._functions = context.functions()
                self._functions.initializeOpenGLFunctions()
                self._blitter = QOpenGLTextureBlitter()
                if not self._blitter.create():
                    raise RuntimeError("Could not create the OpenGL texture blitter.")
                self._create_filter_pipeline()
                context.aboutToBeDestroyed.connect(self._release_gl_resources)
                self.backend_info = self._context_info(context)
                self._ready = True
                self.backend_ready.emit(self.backend_info)
            except Exception as error:
                self._fail(str(error))

        @timed("preview.gl_submission_seconds")
        def paintGL(self) -> None:  # type: ignore[override]
            if not self._ready or self._functions is None:
                return
            try:
                ratio = max(1.0, float(self.devicePixelRatioF()))
                viewport = QRect(
                    0, 0, max(1, round(self.width() * ratio)),
                    max(1, round(self.height() * ratio)),
                )
                self._functions.glViewport(
                    viewport.x(), viewport.y(), viewport.width(), viewport.height(),
                )
                self._functions.glClearColor(0.066, 0.094, 0.125, 1.0)
                self._functions.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT
                if self._pending_layers:
                    self._activate_pending_layers()
                if not self._active_layers or self._active_canvas_size.isEmpty():
                    return
                scale = min(
                    viewport.width() / max(1, self._active_canvas_size.width()),
                    viewport.height() / max(1, self._active_canvas_size.height()),
                )
                origin_x = (
                    viewport.width() - self._active_canvas_size.width() * scale
                ) / 2.0
                origin_y = (
                    viewport.height() - self._active_canvas_size.height() * scale
                ) / 2.0
                self._functions.glEnable(0x0BE2)  # GL_BLEND
                # Source-over alpha is A + dstA*(1-A), not A*A + dstA*(1-A).
                # The latter made framebuffer readback/composition brighter
                # whenever a translucent animated layer crossed an opaque base.
                self._functions.glBlendFuncSeparate(0x0302, 0x0303, 1, 0x0303)
                self._blitter.bind()
                try:
                    for layer in self._active_layers:
                        entry = self._textures.get(layer.key)
                        if entry is None or not entry.texture.isCreated():
                            continue
                        target = QRectF(
                            origin_x + layer.target.x() * scale,
                            origin_y + layer.target.y() * scale,
                            layer.target.width() * scale,
                            layer.target.height() * scale,
                        )
                        transform = QOpenGLTextureBlitter.targetTransform(
                            target, viewport,
                        )
                        if entry.video_format in {"Format_NV12", "Format_NV21"}:
                            self._blitter.release()
                            self._draw_video_layer(entry, layer, target, viewport)
                            self._blitter.bind()
                        elif layer.rotation or (layer.color_filter is not None and layer.color_filter.active):
                            # Rotate in canvas pixel coordinates before NDC
                            # projection. Rotating the already-scaled blitter
                            # matrix reverses Y and skews non-square layers.
                            self._blitter.release()
                            self._draw_filtered_layer(entry, layer, target, viewport)
                            self._blitter.bind()
                        else:
                            self._blitter.setOpacity(
                                max(0.0, min(1.0, layer.opacity)),
                            )
                            self._blitter.blit(
                                entry.texture.textureId(), transform,
                                QOpenGLTextureBlitter.Origin.OriginTopLeft,
                            )
                finally:
                    self._blitter.release()
                    self._functions.glDisable(0x0BE2)
            except Exception as error:
                self._ready = False
                self._fail(str(error))

        def _activate_pending_layers(self) -> None:
            layers = self._pending_layers
            canvas_size = QSize(self._pending_canvas_size)
            self._pending_layers = ()
            self._pending_canvas_size = QSize()
            active_keys = {layer.key for layer in layers}
            self._frame_serial += 1
            for layer in layers:
                self._upload_layer(layer)
            self._evict_texture_cache(active_keys)
            self._active_layers = layers
            self._active_canvas_size = canvas_size
            self._pending_image = QImage()

        @timed("preview.texture_prepare_upload_seconds")
        def _upload_layer(self, layer: GpuPreviewLayer) -> None:
            if layer.video_frame is not None and layer.video_frame.isValid():
                self._upload_video_frame_layer(layer)
                return
            cache_key = int(layer.image.cacheKey())
            existing = self._textures.get(layer.key)
            if (
                existing is not None
                and existing.cache_key == cache_key
                and existing.width == layer.image.width()
                and existing.height == layer.image.height()
            ):
                self._texture_reuses += 1
                self._textures[layer.key] = _TextureEntry(
                    existing.texture, existing.cache_key,
                    existing.width, existing.height,
                    existing.allocated_bytes, self._frame_serial,
                )
                return
            image = layer.image.convertToFormat(
                QImage.Format.Format_RGBA8888,
            )
            same_size = (
                existing is not None
                and existing.texture.isCreated()
                and existing.width == image.width()
                and existing.height == image.height()
            )
            if same_size and existing is not None:
                try:
                    # QOpenGLTexture.setData(QImage) attempts to redefine the
                    # texture storage on some Qt/AMD combinations.  Upload raw
                    # RGBA pixels into the existing allocation instead.
                    # Pass the buffer itself: PySide rejects an int address
                    # with ValueError, which silently recreated the texture on
                    # every frame, and building that error's message rescans
                    # every loaded module (>0.5 s stalls while AutoMix loads
                    # its analysis libraries).
                    existing.texture.setData(
                        QOpenGLTexture.PixelFormat.RGBA,
                        QOpenGLTexture.PixelType.UInt8,
                        image.constBits(),
                    )
                    self._textures[layer.key] = _TextureEntry(
                        existing.texture, cache_key, image.width(), image.height(),
                        existing.allocated_bytes, self._frame_serial,
                    )
                    self._record_texture_upload(image)
                    return
                except (BufferError, RuntimeError, TypeError, ValueError):
                    self._destroy_texture(layer.key)
            elif existing is not None:
                self._destroy_texture(layer.key)
            texture = QOpenGLTexture(
                image, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps,
            )
            if not texture.isCreated():
                raise RuntimeError("Could not allocate the preview texture.")
            self._texture_allocations += 1
            texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._textures[layer.key] = _TextureEntry(
                texture, cache_key, image.width(), image.height(),
                max(0, image.width() * image.height() * 4), self._frame_serial,
            )
            allocation = max(0, image.width() * image.height() * 4)
            self._allocated_bytes += allocation
            self._peak_allocated_bytes = max(
                self._peak_allocated_bytes, self._allocated_bytes,
            )

            self._record_texture_upload(image)

        def _record_texture_upload(self, image: QImage) -> None:
            self._texture_uploads += 1
            self._uploaded_bytes += max(0, image.sizeInBytes())

        @staticmethod
        def _copy_video_plane(
            frame: QVideoFrame, plane: int, row_bytes: int, rows: int,
        ) -> bytearray:
            """Copy mapped rows without invoking QVideoFrame.toImage()."""
            stride = frame.bytesPerLine(plane)
            source = np.frombuffer(
                memoryview(frame.bits(plane)), dtype=np.uint8, count=stride * rows,
            ).reshape(rows, stride)
            # NumPy packs padded rows in native code; keep owned storage after unmap.
            packed = np.ascontiguousarray(source[:, :row_bytes])
            return bytearray(memoryview(packed).cast("B"))

        @staticmethod
        def _copy_scaled_video_plane(
            frame: QVideoFrame, plane: int, source_width: int, source_rows: int,
            target_width: int, target_rows: int, *, channels: int = 1,
        ) -> bytearray:
            """Nearest-sample one mapped plane while respecting padded strides."""
            if source_width == target_width and source_rows == target_rows:
                return GpuTexturePreviewSurface._copy_video_plane(
                    frame, plane, source_width * channels, source_rows,
                )
            stride = frame.bytesPerLine(plane)
            source = np.frombuffer(
                memoryview(frame.bits(plane)), dtype=np.uint8,
                count=stride * source_rows,
            ).reshape(source_rows, stride)
            pixels = source[:, :source_width * channels].reshape(
                source_rows, source_width, channels,
            )
            y_indices = np.linspace(
                0, max(0, source_rows - 1), max(1, target_rows),
            ).astype(np.intp)
            x_indices = np.linspace(
                0, max(0, source_width - 1), max(1, target_width),
            ).astype(np.intp)
            scaled = pixels[y_indices[:, None], x_indices[None, :], :]
            return bytearray(memoryview(scaled).cast("B"))

        @staticmethod
        def _upload_raw_texture(
            texture: object, pixel_format: object, data: bytearray,
        ) -> None:
            if not data:
                return
            # The buffer itself, never an int address (PySide rejects that).
            texture.setData(pixel_format, QOpenGLTexture.PixelType.UInt8, data)

        @staticmethod
        def _new_raw_texture(
            width: int, height: int, texture_format: object,
            pixel_format: object, data: bytearray,
        ) -> object:
            texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            texture.setSize(max(1, width), max(1, height))
            texture.setFormat(texture_format)
            texture.allocateStorage(
                pixel_format, QOpenGLTexture.PixelType.UInt8,
            )
            GpuTexturePreviewSurface._upload_raw_texture(
                texture, pixel_format, data,
            )
            texture.setMinificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            return texture

        def _upload_video_frame_layer(self, layer: GpuPreviewLayer) -> None:
            """Upload mapped RHI planes while avoiding CPU RGB conversion."""
            frame = QVideoFrame(layer.video_frame)
            format_name = getattr(frame.pixelFormat(), "name", "")
            native_scale = max(0.25, min(1.0, float(layer.native_scale)))
            output_width = max(1, round(frame.width() * native_scale))
            output_height = max(1, round(frame.height() * native_scale))
            existing = self._textures.get(layer.key)
            if (
                layer.native_serial > 0
                and existing is not None
                and existing.cache_key == layer.native_serial
                and existing.width == output_width
                and existing.height == output_height
                and existing.video_format == format_name
            ):
                self._texture_reuses += 1
                self._textures[layer.key] = _TextureEntry(
                    existing.texture, existing.cache_key,
                    existing.width, existing.height,
                    existing.allocated_bytes, self._frame_serial,
                    existing.aux_texture, existing.video_format,
                )
                return
            if not frame.map(QVideoFrame.MapMode.ReadOnly):
                raise RuntimeError("Could not map the GPU video frame for preview upload.")
            try:
                width, height = frame.width(), frame.height()
                if format_name in {"Format_NV12", "Format_NV21"}:
                    y_data = self._copy_scaled_video_plane(
                        frame, 0, width, height,
                        output_width, output_height,
                    )
                    uv_width = (width + 1) // 2
                    uv_height = (height + 1) // 2
                    output_uv_width = (output_width + 1) // 2
                    output_uv_height = (output_height + 1) // 2
                    uv_data = self._copy_scaled_video_plane(
                        frame, 1, uv_width, uv_height,
                        output_uv_width, output_uv_height, channels=2,
                    )
                else:
                    image_format = QVideoFrameFormat.imageFormatFromPixelFormat(
                        frame.pixelFormat(),
                    )
                    if image_format == QImage.Format.Format_Invalid:
                        raise RuntimeError(
                            f"Unsupported mapped GPU frame format: {format_name}"
                        )
                    stride = frame.bytesPerLine(0)
                    raw = self._copy_video_plane(frame, 0, stride, height)
                    image = QImage(
                        raw, width, height, stride, image_format,
                    ).copy()
            finally:
                frame.unmap()
            if format_name not in {"Format_NV12", "Format_NV21"}:
                if image.width() != output_width or image.height() != output_height:
                    image = image.scaled(
                        output_width, output_height,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.FastTransformation,
                    )
                self._upload_layer(replace(
                    layer, image=image, video_frame=None, native_serial=0,
                ))
                uploaded = self._textures.get(layer.key)
                if uploaded is not None:
                    self._textures[layer.key] = _TextureEntry(
                        uploaded.texture, layer.native_serial,
                        uploaded.width, uploaded.height,
                        uploaded.allocated_bytes, uploaded.last_used_frame,
                        uploaded.aux_texture, format_name,
                    )
                return
            existing = self._textures.get(layer.key)
            same_size = (
                existing is not None
                and existing.video_format == format_name
                and existing.width == output_width
                and existing.height == output_height
                and existing.texture.isCreated()
                and existing.aux_texture is not None
                and existing.aux_texture.isCreated()
            )
            if same_size and existing is not None:
                self._upload_raw_texture(
                    existing.texture, QOpenGLTexture.PixelFormat.Red, y_data,
                )
                self._upload_raw_texture(
                    existing.aux_texture, QOpenGLTexture.PixelFormat.RG, uv_data,
                )
                self._textures[layer.key] = _TextureEntry(
                    existing.texture, layer.native_serial,
                    output_width, output_height,
                    existing.allocated_bytes, self._frame_serial,
                    existing.aux_texture, format_name,
                )
            else:
                if existing is not None:
                    self._destroy_texture(layer.key)
                y_texture = self._new_raw_texture(
                    output_width, output_height,
                    QOpenGLTexture.TextureFormat.R8_UNorm,
                    QOpenGLTexture.PixelFormat.Red, y_data,
                )
                uv_texture = self._new_raw_texture(
                    (output_width + 1) // 2, (output_height + 1) // 2,
                    QOpenGLTexture.TextureFormat.RG8_UNorm,
                    QOpenGLTexture.PixelFormat.RG, uv_data,
                )
                self._texture_allocations += 2
                allocation = len(y_data) + len(uv_data)
                self._textures[layer.key] = _TextureEntry(
                    y_texture, layer.native_serial,
                    output_width, output_height,
                    allocation, self._frame_serial, uv_texture, format_name,
                )
                self._allocated_bytes += allocation
                self._peak_allocated_bytes = max(
                    self._peak_allocated_bytes, self._allocated_bytes,
                )
            self._texture_uploads += 2
            self._uploaded_bytes += len(y_data) + len(uv_data)

        def _evict_texture_cache(self, active_keys: set[Hashable]) -> None:
            """Retain recent textures briefly, then enforce the VRAM budget."""
            candidates = sorted(
                (
                    (entry.last_used_frame, key)
                    for key, entry in self._textures.items()
                    if key not in active_keys
                ),
                key=lambda candidate: candidate[0],
            )
            for last_used, key in candidates:
                expired = (
                    self._frame_serial - last_used
                    >= self._texture_retention_frames
                )
                over_budget = self._allocated_bytes > self._texture_budget_bytes
                if not expired and not over_budget:
                    continue
                self._destroy_texture(key, evicted=True)

        def _destroy_texture(self, key: Hashable, *, evicted: bool = False) -> None:
            entry = self._textures.pop(key, None)
            if entry is None:
                return
            self._allocated_bytes = max(
                0, self._allocated_bytes - entry.allocated_bytes,
            )
            if evicted:
                self._texture_evictions += 1
            if entry.texture.isCreated():
                entry.texture.destroy()
            if entry.aux_texture is not None and entry.aux_texture.isCreated():
                entry.aux_texture.destroy()

        def _release_gl_resources(self) -> None:
            try:
                if self.context() is not None and self.context().isValid():
                    self.makeCurrent()
                for key in tuple(self._textures):
                    self._destroy_texture(key)
                self._active_layers = ()
                self._pending_layers = ()
                if self._blitter is not None:
                    self._blitter.destroy()
                    self._blitter = None
                if self._filter_buffer is not None:
                    self._filter_buffer.destroy()
                    self._filter_buffer = None
                if self._filter_vao is not None:
                    self._filter_vao.destroy()
                    self._filter_vao = None
                if self._filter_program is not None:
                    self._filter_program.removeAllShaders()
                    self._filter_program = None
                if self._video_program is not None:
                    self._video_program.removeAllShaders()
                    self._video_program = None
                if self.context() is not None and self.context().isValid():
                    self.doneCurrent()
            except RuntimeError:
                pass
            self._ready = False

        def closeEvent(self, event) -> None:  # type: ignore[override]
            # Release textures while the widget can still make its context
            # current.  Waiting for QObject destruction is too late on some
            # Windows OpenGL drivers and leaks the native texture objects.
            if self._ready or self._textures:
                self._release_gl_resources()
            super().closeEvent(event)

        def _create_filter_pipeline(self) -> None:
            program = QOpenGLShaderProgram(self)
            vertex_shader = """
                #version 330 core
                layout(location = 0) in vec2 position;
                layout(location = 1) in vec2 texCoord;
                out vec2 uv;
                void main() {
                    gl_Position = vec4(position, 0.0, 1.0);
                    uv = texCoord;
                }
            """
            fragment_shader = """
                #version 330 core
                in vec2 uv;
                out vec4 fragColor;
                uniform sampler2D sourceTexture;
                uniform float opacity;
                uniform float brightness;
                uniform float contrast;
                uniform float saturation;
                void main() {
                    vec4 sampled = texture(sourceTexture, uv);
                    vec3 rgb = (sampled.rgb - vec3(0.5019608)) * contrast
                               + vec3(0.5019608 + brightness);
                    float luminance = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
                    rgb = mix(vec3(luminance), rgb, saturation);
                    fragColor = vec4(
                        clamp(rgb, 0.0, 1.0), sampled.a * opacity
                    );
                }
            """
            if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, vertex_shader,
            ):
                raise RuntimeError(program.log() or "Could not compile GPU filter vertex shader.")
            if not program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, fragment_shader,
            ):
                raise RuntimeError(program.log() or "Could not compile GPU filter fragment shader.")
            if not program.link():
                raise RuntimeError(program.log() or "Could not link GPU filter shader.")
            buffer = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            if not buffer.create():
                raise RuntimeError("Could not create GPU filter vertex buffer.")
            self._filter_program = program
            self._filter_buffer = buffer
            # The blitter owns its own vertex array. Custom draws must never
            # overwrite that VAO's attribute pointers between consecutive frames.
            self._filter_vao = QOpenGLVertexArrayObject(self)
            if not self._filter_vao.create():
                raise RuntimeError("Could not create the GPU layer vertex array.")
            video_program = QOpenGLShaderProgram(self)
            video_fragment_shader = """
                #version 330 core
                in vec2 uv;
                out vec4 fragColor;
                uniform sampler2D yTexture;
                uniform sampler2D uvTexture;
                uniform float opacity;
                uniform float brightness;
                uniform float contrast;
                uniform float saturation;
                uniform int fullRange;
                uniform int colorMatrix;
                uniform int swapUv;
                void main() {
                    float y = texture(yTexture, uv).r;
                    vec2 chroma = texture(uvTexture, uv).rg;
                    if (swapUv != 0)
                        chroma = chroma.gr;
                    float u = chroma.r - 0.5;
                    float v = chroma.g - 0.5;
                    if (fullRange == 0) {
                        y = (y - 16.0 / 255.0) * (255.0 / 219.0);
                        u *= 255.0 / 224.0;
                        v *= 255.0 / 224.0;
                    }
                    vec3 rgb;
                    if (colorMatrix == 1) {
                        rgb = vec3(
                            y + 1.4020 * v,
                            y - 0.344136 * u - 0.714136 * v,
                            y + 1.7720 * u
                        );
                    } else if (colorMatrix == 2) {
                        rgb = vec3(
                            y + 1.4746 * v,
                            y - 0.164553 * u - 0.571353 * v,
                            y + 1.8814 * u
                        );
                    } else {
                        rgb = vec3(
                            y + 1.5748 * v,
                            y - 0.187324 * u - 0.468124 * v,
                            y + 1.8556 * u
                        );
                    }
                    rgb = (rgb - vec3(0.5019608)) * contrast
                          + vec3(0.5019608 + brightness);
                    float luminance = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
                    rgb = mix(vec3(luminance), rgb, saturation);
                    fragColor = vec4(clamp(rgb, 0.0, 1.0), opacity);
                }
            """
            if not video_program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, vertex_shader,
            ):
                raise RuntimeError(
                    video_program.log() or "Could not compile GPU video vertex shader."
                )
            if not video_program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, video_fragment_shader,
            ):
                raise RuntimeError(
                    video_program.log() or "Could not compile GPU video fragment shader."
                )
            if not video_program.link():
                raise RuntimeError(
                    video_program.log() or "Could not link GPU video shader."
                )
            self._video_program = video_program

        def _draw_video_layer(
            self, entry: _TextureEntry, layer: GpuPreviewLayer,
            target: QRectF, viewport: QRect,
        ) -> None:
            """Convert mapped NV12/NV21 planes in the fragment shader."""
            if (
                self._video_program is None or self._filter_buffer is None or self._filter_vao is None
                or entry.aux_texture is None
            ):
                raise RuntimeError("GPU video plane pipeline is not initialized.")
            vertices = self._filtered_quad_vertices(
                target, viewport, layer.rotation,
            )
            payload = array("f", vertices).tobytes()
            program = self._video_program
            buffer = self._filter_buffer
            self._filter_vao.bind()
            if not program.bind() or not buffer.bind():
                raise RuntimeError("Could not bind GPU video plane resources.")
            try:
                buffer.allocate(payload, len(payload))
                stride = 4 * 4
                program.enableAttributeArray(0)
                program.enableAttributeArray(1)
                program.setAttributeBuffer(0, 0x1406, 0, 2, stride)
                program.setAttributeBuffer(1, 0x1406, 2 * 4, 2, stride)
                color_filter = layer.color_filter or GpuColorFilter()
                surface_format = (
                    layer.video_frame.surfaceFormat()
                    if layer.video_frame is not None else QVideoFrameFormat()
                )
                range_name = getattr(surface_format.colorRange(), "name", "")
                space_name = getattr(surface_format.colorSpace(), "name", "")
                # Explicit GL scalar calls avoid PySide's ambiguous scalar/
                # matrix setUniformValue overloads (which can leave zeros).
                self._functions.glUniform1i(program.uniformLocation(b"yTexture"), 0)
                self._functions.glUniform1i(program.uniformLocation(b"uvTexture"), 1)
                self._functions.glUniform1f(program.uniformLocation(b"opacity"), max(0.0, min(1.0, layer.opacity)))
                self._functions.glUniform1f(program.uniformLocation(b"brightness"), color_filter.brightness / 100.0)
                self._functions.glUniform1f(program.uniformLocation(b"contrast"), 1.0 + color_filter.contrast / 100.0)
                self._functions.glUniform1f(
                    program.uniformLocation(b"saturation"),
                    0.0 if color_filter.grayscale else
                    max(0.0, min(3.0, color_filter.saturation)),
                )
                self._functions.glUniform1i(program.uniformLocation(b"fullRange"), int(range_name == "ColorRange_Full"))
                color_matrix = (
                    1 if space_name == "ColorSpace_BT601" else
                    2 if space_name == "ColorSpace_BT2020" else 0
                )
                self._functions.glUniform1i(program.uniformLocation(b"colorMatrix"), color_matrix)
                self._functions.glUniform1i(program.uniformLocation(b"swapUv"), int(entry.video_format == "Format_NV21"))
                entry.texture.bind(0)
                entry.aux_texture.bind(1)
                self._functions.glDrawArrays(0x0005, 0, 4)
                entry.aux_texture.release(1)
                entry.texture.release(0)
                program.disableAttributeArray(0)
                program.disableAttributeArray(1)
            finally:
                buffer.release()
                self._filter_vao.release()
                program.release()

        def _draw_filtered_layer(
            self, entry: _TextureEntry, layer: GpuPreviewLayer,
            target: QRectF, viewport: QRect,
        ) -> None:
            if self._filter_program is None or self._filter_buffer is None or self._filter_vao is None:
                raise RuntimeError("GPU filter pipeline is not initialized.")
            vertices = self._filtered_quad_vertices(
                target, viewport, layer.rotation,
            )
            payload = array("f", vertices).tobytes()
            program = self._filter_program
            buffer = self._filter_buffer
            self._filter_vao.bind()
            if not program.bind() or not buffer.bind():
                raise RuntimeError("Could not bind GPU filter resources.")
            try:
                buffer.allocate(payload, len(payload))
                stride = 4 * 4
                program.enableAttributeArray(0)
                program.enableAttributeArray(1)
                program.setAttributeBuffer(0, 0x1406, 0, 2, stride)  # GL_FLOAT
                program.setAttributeBuffer(1, 0x1406, 2 * 4, 2, stride)
                color_filter = layer.color_filter or GpuColorFilter()
                # See the video path: use explicit scalar uniform setters.
                self._functions.glUniform1i(program.uniformLocation(b"sourceTexture"), 0)
                self._functions.glUniform1f(program.uniformLocation(b"opacity"), max(0.0, min(1.0, layer.opacity)))
                self._functions.glUniform1f(program.uniformLocation(b"brightness"), color_filter.brightness / 100.0)
                self._functions.glUniform1f(program.uniformLocation(b"contrast"), 1.0 + color_filter.contrast / 100.0)
                self._functions.glUniform1f(
                    program.uniformLocation(b"saturation"),
                    0.0 if color_filter.grayscale else
                    max(0.0, min(3.0, color_filter.saturation)),
                )
                entry.texture.bind(0)
                self._functions.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
                entry.texture.release(0)
                program.disableAttributeArray(0)
                program.disableAttributeArray(1)
            finally:
                buffer.release()
                self._filter_vao.release()
                program.release()

        @staticmethod
        def _filtered_quad_vertices(
            target: QRectF, viewport: QRect, rotation: float,
        ) -> tuple[float, ...]:
            """Return NDC position/UV vertices for the filtered texture quad."""
            center_x, center_y = target.center().x(), target.center().y()
            angle = radians(rotation)
            cosine, sine = cos(angle), sin(angle)
            result: list[float] = []
            # QImage rows start at the top; invert V for OpenGL texture space.
            for local_x, local_y, u, v in (
                (-0.5, -0.5, 0.0, 1.0),
                (-0.5, 0.5, 0.0, 0.0),
                (0.5, -0.5, 1.0, 1.0),
                (0.5, 0.5, 1.0, 0.0),
            ):
                x_offset = local_x * target.width()
                y_offset = local_y * target.height()
                screen_x = center_x + x_offset * cosine - y_offset * sine
                screen_y = center_y + x_offset * sine + y_offset * cosine
                ndc_x = 2.0 * screen_x / max(1, viewport.width()) - 1.0
                ndc_y = 1.0 - 2.0 * screen_y / max(1, viewport.height())
                result.extend((ndc_x, ndc_y, u, v))
            return tuple(result)

        def _fail(self, message: str) -> None:
            if self._failure_reported:
                return
            self._failure_reported = True
            self.backend_failed.emit(
                message or "The GPU texture preview backend could not start.",
            )

        @staticmethod
        def _context_info(context: QOpenGLContext) -> GpuBackendInfo:
            surface_format = context.format()
            api = (
                "OpenGL ES" if context.isOpenGLES() else "OpenGL"
            )
            version = f"{surface_format.majorVersion()}.{surface_format.minorVersion()}"
            functions = context.functions()

            def gl_string(identifier: int) -> str:
                try:
                    value = functions.glGetString(identifier)
                    if isinstance(value, bytes):
                        return value.decode("utf-8", "replace")
                    return str(value or "")
                except (AttributeError, RuntimeError, TypeError):
                    return ""

            return GpuBackendInfo(
                api=api,
                version=version,
                vendor=gl_string(0x1F00),  # GL_VENDOR
                renderer=gl_string(0x1F01),  # GL_RENDERER
            )
else:
    GpuTexturePreviewSurface = None
