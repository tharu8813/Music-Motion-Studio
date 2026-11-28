from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.preview.gpu_texture_surface import (
    GPU_TEXTURE_SURFACE_AVAILABLE,
    GpuBackendInfo, GpuPreviewLayer,
    GpuTexturePreviewSurface, _TextureEntry, _cached_plane_indices, _plane_indices,
    _presentation_layer, _video_shader_parameters,
)


class GpuTextureSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_texture_lifecycle_stress_switch_resize_reuse_and_release(self):
        # Real Qt images, decoder frames, queue and cache logic. Stub only GL
        # allocation/upload; this is not a physical GPU memory measurement.
        from PySide6.QtOpenGL import QOpenGLTexture

        live = {}
        counts = {"created": 0, "destroyed": 0, "updates": 0}

        class Texture:
            def __init__(self, size):
                self.size = size
                self.alive = True
                counts["created"] += 1
                live[id(self)] = self

            def isCreated(self):
                return self.alive

            def setData(self, *_args):
                if not self.alive:
                    raise AssertionError("Updated a destroyed texture")
                counts["updates"] += 1

            def destroy(self):
                if not self.alive:
                    raise AssertionError("Double destruction")
                self.alive = False
                counts["destroyed"] += 1
                del live[id(self)]

        def allocate(width, height, texture_format, _pixel_format, data):
            channels = {
                QOpenGLTexture.TextureFormat.R8_UNorm: 1,
                QOpenGLTexture.TextureFormat.RG8_UNorm: 2,
                QOpenGLTexture.TextureFormat.RGBA8_UNorm: 4,
            }[texture_format]
            size = width * height * channels
            self.assertEqual(len(memoryview(data).cast("B")), size)
            return Texture(size)

        for cycle in range(3):
            surface = GpuTexturePreviewSurface(texture_budget_bytes=1024 * 1024)
            try:
                with patch.object(surface, "_new_raw_texture", side_effect=allocate):
                    for index in range(180):
                        # Reuse each scene twice, then switch source identity,
                        # pixel format or size. Force inactive-cache eviction.
                        group = index // 2
                        key = ("video", group % 11)
                        width, height = (320, 180) if group % 3 else (640, 360)
                        if group % 2:
                            fmt = QVideoFrameFormat(
                                QSize(width, height), QVideoFrameFormat.PixelFormat.Format_NV12,
                            )
                            frame = QVideoFrame(fmt)
                            self.assertTrue(frame.map(QVideoFrame.MapMode.WriteOnly))
                            try:
                                for plane in range(2):
                                    bits = memoryview(frame.bits(plane)).cast("B")
                                    bits[:] = bytes([64 + group % 128]) * len(bits)
                            finally:
                                frame.unmap()
                            layer = GpuPreviewLayer(key, QImage(), QRectF(0, 0, width, height),
                                                    video_frame=frame, native_serial=group + 1)
                        else:
                            image = QImage(width, height, QImage.Format.Format_RGBA8888)
                            image.fill(QColor(20, group % 256, 80))
                            layer = GpuPreviewLayer(key, image, QRectF(0, 0, width, height))
                        if index % 2:
                            # Repaint same scene without a new upload.
                            self.assertFalse(surface.frame_pending)
                            surface._on_frame_swapped()
                        surface.set_layers(QSize(width, height), (layer,))
                        surface._activate_pending_layers()
                        stats = surface.upload_stats
                        self.assertEqual(stats.allocated_bytes, sum(v.size for v in live.values()))
                        self.assertLessEqual(stats.allocated_bytes, stats.texture_budget_bytes)
                        self.assertEqual(counts["created"] - counts["destroyed"], len(live))
                        self.assertEqual(len(surface._active_layers), 1)
                        self.assertIsNone(surface._active_layers[0].video_frame)
                        self.assertTrue(surface._active_layers[0].image.isNull())
                    self.assertGreater(surface.upload_stats.texture_evictions, 0)
                    self.assertGreater(surface.upload_stats.texture_reuses, 0)
                    # Release also while a new scene is pending (close/stop).
                    surface.set_layers(QSize(width, height), (layer,))
                    surface._release_gl_resources()
                    self.assertEqual(surface.upload_stats.allocated_bytes, 0)
                    self.assertEqual(surface._textures, {})
                    self.assertEqual(surface._active_layers, ())
                    self.assertEqual(surface._pending_layers, ())
                    self.assertFalse(surface.frame_pending)
                    self.assertEqual(live, {})
                    surface._release_gl_resources()  # idempotent
            finally:
                surface._release_gl_resources()
                surface.deleteLater()
        self.assertEqual(counts["created"], counts["destroyed"])
        self.assertGreater(counts["created"], 100)
        print("TEXTURE_LIFECYCLE_STRESS", counts, flush=True)

    def test_backend_label_keeps_api_version_and_renderer(self) -> None:
        info = GpuBackendInfo(
            api="OpenGL", version="4.6", vendor="Vendor", renderer="Renderer",
        )
        self.assertEqual(info.label, "OpenGL 4.6 · Renderer")

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_texture_eviction_accepts_mixed_layer_keys_from_the_same_frame(self) -> None:
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        surface._frame_serial = surface._texture_retention_frames + 1
        entry = _TextureEntry(None, 0, 1, 1, 4, 1)
        surface._textures.update({"canvas": entry, ("video", 1): entry})
        with patch.object(surface, "_destroy_texture") as destroy:
            surface._evict_texture_cache(set())
        self.assertEqual([call.args[0] for call in destroy.call_args_list], ["canvas", ("video", 1)])

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_texture_cache_in_budget_only_evicts_expired_inactive_layers(self) -> None:
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        surface._frame_serial = 100
        # Deliberately insert newest first, then oldest: the fast path should
        # not sort by age when all entries fit within the budget.
        surface._textures = {
            "recent": _TextureEntry(None, 0, 1, 1, 4, 99),
            "expired-newer": _TextureEntry(None, 0, 1, 1, 4, 9),
            "expired-older": _TextureEntry(None, 0, 1, 1, 4, 0),
            "active-expired": _TextureEntry(None, 0, 1, 1, 4, 0),
        }
        surface._allocated_bytes = 16
        with patch.object(surface, "_destroy_texture") as destroy:
            surface._evict_texture_cache({"active-expired"})
        self.assertEqual(
            [call.args[0] for call in destroy.call_args_list],
            ["expired-newer", "expired-older"],
        )
        self.assertTrue(all(call.kwargs.get("evicted") for call in destroy.call_args_list))

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_texture_cache_over_budget_preserves_oldest_first_eviction(self) -> None:
        surface = GpuTexturePreviewSurface(texture_budget_bytes=1024 * 1024)
        self.addCleanup(surface.deleteLater)
        surface._frame_serial = 10
        allocation = 512 * 1024
        surface._textures = {
            "newer": _TextureEntry(None, 0, 1, 1, allocation, 9),
            "active-old": _TextureEntry(None, 0, 1, 1, allocation, 0),
            "oldest": _TextureEntry(None, 0, 1, 1, allocation, 1),
        }
        surface._allocated_bytes = allocation * 3
        removed = []

        def destroy(key, *, evicted=False):
            self.assertTrue(evicted)
            removed.append(key)
            entry = surface._textures.pop(key)
            surface._allocated_bytes -= entry.allocated_bytes

        with patch.object(surface, "_destroy_texture", side_effect=destroy):
            surface._evict_texture_cache({"active-old"})
        self.assertEqual(removed, ["oldest"])
        self.assertEqual(surface._allocated_bytes, allocation * 2)
        self.assertEqual(set(surface._textures), {"newer", "active-old"})

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_repaint_swaps_do_not_count_as_new_scene_frames(self) -> None:
        surface = GpuTexturePreviewSurface()
        surface._frame_serial = 1
        surface._on_frame_swapped()
        surface._on_frame_swapped()
        surface._frame_serial = 2
        surface._on_frame_swapped()
        self.assertEqual(surface.upload_stats.presented_frames, 3)
        self.assertEqual(surface.upload_stats.presented_scene_frames, 2)
        surface.deleteLater()

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_plane_preparation_preserves_padded_rows_odd_sizes_and_owned_storage(self) -> None:
        class MappedPlane:
            def __init__(self, width: int, rows: int, channels: int, padding: int) -> None:
                self.stride = width * channels + padding
                self.raw = bytearray((index * 17 + 3) % 256 for index in range(self.stride * rows))

            def bytesPerLine(self, _plane: int) -> int:
                return self.stride

            def bits(self, _plane: int) -> memoryview:
                return memoryview(self.raw)

        for width, rows, channels, padding in ((17, 9, 1, 0), (17, 9, 1, 7), (9, 5, 2, 5), (9, 5, 4, 0)):
            frame = MappedPlane(width, rows, channels, padding)
            original = bytes(frame.raw)
            packed = GpuTexturePreviewSurface._copy_video_plane(frame, 0, width * channels, rows)
            self.assertEqual(packed, b"".join(
                original[row * frame.stride:row * frame.stride + width * channels]
                for row in range(rows)
            ))
            for target_width, target_rows in ((width, rows), (7, 3), (1, 1), (width, 3), (7, rows)):
                with self.subTest(shape=(width, rows, channels, padding, target_width, target_rows)):
                    pixels = np.frombuffer(original, dtype=np.uint8).reshape(rows, frame.stride)
                    pixels = pixels[:, :width * channels].reshape(rows, width, channels)
                    y = np.linspace(0, rows - 1, target_rows).astype(np.intp)
                    x = np.linspace(0, width - 1, target_width).astype(np.intp)
                    expected = pixels[y[:, None], x[None, :], :].tobytes()
                    actual = GpuTexturePreviewSurface._copy_scaled_video_plane(
                        frame, 0, width, rows, target_width, target_rows, channels=channels,
                    )
                    self.assertEqual(actual, expected)
                    if (target_width, target_rows) != (width, rows):
                        self.assertIsInstance(actual, memoryview)
                        self.assertTrue(actual.readonly)
                        self.assertTrue(actual.c_contiguous)
                        self.assertEqual(actual.format, "B")
                        self.assertEqual(actual.nbytes, target_width * target_rows * channels)
                        self.assertTrue(actual.obj.flags.owndata)
                    frame.raw[:] = bytes(len(frame.raw))
                    self.assertEqual(actual, expected)  # pixels outlive mapped storage
                    frame.raw[:] = original

    def test_plane_index_cache_is_bounded_read_only_and_geometry_specific(self):
        _cached_plane_indices.cache_clear()
        self.addCleanup(_cached_plane_indices.cache_clear)
        first = _plane_indices(1920, 1056)
        self.assertIs(first, _plane_indices(1920, 1056))
        self.assertFalse(first.flags.writeable)
        self.assertTrue(np.array_equal(first, np.linspace(0, 1919, 1056).astype(np.intp)))
        self.assertIsNot(first, _plane_indices(1921, 1056))
        before = _cached_plane_indices.cache_info().currsize
        huge = _plane_indices(9000, 9000)
        self.assertEqual(_cached_plane_indices.cache_info().currsize, before)
        self.assertEqual(len(huge), 9000)
        for width in range(100, 200):
            _plane_indices(width, 80)
        self.assertEqual(_cached_plane_indices.cache_info().currsize, 64)

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_surface_queue_keeps_only_the_newest_frame_before_upload(self) -> None:
        surface = GpuTexturePreviewSurface()
        first = QImage(8, 8, QImage.Format.Format_RGBA8888)
        first.fill(QColor(255, 0, 0))
        second = QImage(8, 8, QImage.Format.Format_RGBA8888)
        second.fill(QColor(0, 0, 255))

        surface.set_image(first)
        surface.set_image(second)

        self.assertEqual(
            surface._pending_image.pixelColor(4, 4), QColor(0, 0, 255),
        )
        self.assertEqual(surface.upload_stats.submitted_frames, 2)
        self.assertEqual(surface.upload_stats.dropped_pending_frames, 1)
        self.assertFalse(surface.ready)
        surface.deleteLater()

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_surface_queue_keeps_ordered_layers_and_latest_scene(self) -> None:
        surface = GpuTexturePreviewSurface()
        base = QImage(16, 9, QImage.Format.Format_RGBA8888)
        base.fill(QColor("#112233"))
        overlay = QImage(4, 3, QImage.Format.Format_RGBA8888)
        overlay.fill(QColor(255, 0, 0, 128))
        surface.set_layers(QSize(16, 9), (
            GpuPreviewLayer("base", base, QRectF(0, 0, 16, 9)),
            GpuPreviewLayer("overlay", overlay, QRectF(2, 1, 4, 3), rotation=12),
        ))

        self.assertEqual(surface._pending_canvas_size, QSize(16, 9))
        self.assertEqual(
            [layer.key for layer in surface._pending_layers],
            ["base", "overlay"],
        )
        self.assertEqual(surface._pending_layers[1].rotation, 12)
        overlay.fill(QColor("#00FF00"))
        self.assertEqual(
            surface._pending_layers[1].image.pixelColor(1, 1),
            QColor(255, 0, 0, 128),
        )
        surface.deleteLater()

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_empty_scene_submission_clears_previous_gpu_layers(self) -> None:
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#123456"))
        layer = GpuPreviewLayer("old-frame", image, QRectF(0, 0, 16, 9))

        # Exercise queue transitions without requiring an OpenGL context.
        with (
            patch.object(surface, "_upload_layer"),
            patch.object(surface, "_evict_texture_cache"),
        ):
            surface.set_layers(QSize(16, 9), (layer,))
            self.assertTrue(surface.frame_pending)
            surface._activate_pending_layers()
            self.assertFalse(surface.frame_pending)
            self.assertEqual([entry.key for entry in surface._active_layers], ["old-frame"])
            first_frame_serial = surface._frame_serial

            # Previously this empty queue was skipped by paintGL, so the
            # previously active (potentially stale) scene remained visible.
            surface.set_layers(QSize(), ())
            self.assertTrue(surface.frame_pending)
            surface._activate_pending_layers()
            self.assertFalse(surface.frame_pending)
            self.assertEqual(surface._active_layers, ())
            self.assertEqual(surface._active_canvas_size, QSize())
            self.assertEqual(surface._frame_serial, first_frame_serial + 1)

        surface._on_frame_swapped()
        self.assertTrue(surface.last_swap_had_new_scene)

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_latest_scene_supersedes_pending_empty_scene(self) -> None:
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        first = QImage(16, 9, QImage.Format.Format_RGBA8888)
        first.fill(QColor("#112233"))
        next_image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        next_image.fill(QColor("#445566"))

        surface.set_layers(QSize(), ())
        surface.set_layers(QSize(16, 9), (
            GpuPreviewLayer("replacement", next_image, QRectF(0, 0, 16, 9)),
        ))
        self.assertTrue(surface.frame_pending)
        self.assertEqual(surface.upload_stats.dropped_pending_frames, 1)

        with (
            patch.object(surface, "_upload_layer") as upload,
            patch.object(surface, "_evict_texture_cache"),
        ):
            surface._activate_pending_layers()
            upload.assert_called_once()
        self.assertFalse(surface.frame_pending)
        self.assertEqual([layer.key for layer in surface._active_layers], ["replacement"])

        # A new empty submission after a pending image is also a drop, not a
        # no-op that allows the stale queued texture to be presented.
        surface.set_layers(QSize(16, 9), (
            GpuPreviewLayer("queued", first, QRectF(0, 0, 16, 9)),
        ))
        surface.set_layers(QSize(), ())
        self.assertEqual(surface.upload_stats.dropped_pending_frames, 2)
        with (
            patch.object(surface, "_upload_layer") as upload,
            patch.object(surface, "_evict_texture_cache"),
        ):
            surface._activate_pending_layers()
            upload.assert_not_called()
        self.assertEqual(surface._active_layers, ())

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_surface_queue_retains_native_video_frame_without_image_copy(self) -> None:
        surface = GpuTexturePreviewSurface()
        image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#123456"))
        frame = QVideoFrame(image)

        surface.set_layers(QSize(16, 9), (
            GpuPreviewLayer(
                "native-video", QImage(), QRectF(0, 0, 16, 9),
                video_frame=frame, native_serial=7,
            ),
        ))

        self.assertEqual(len(surface._pending_layers), 1)
        queued = surface._pending_layers[0]
        self.assertTrue(queued.image.isNull())
        self.assertIsNotNone(queued.video_frame)
        assert queued.video_frame is not None
        self.assertTrue(queued.video_frame.isValid())
        self.assertEqual(queued.native_serial, 7)
        surface.deleteLater()

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_activated_scene_releases_uploaded_video_payload_and_keeps_draw_metadata(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        frame_format = QVideoFrameFormat(QSize(16, 8), QVideoFrameFormat.PixelFormat.Format_NV12)
        frame_format.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Full)
        frame_format.setColorSpace(QVideoFrameFormat.ColorSpace.ColorSpace_BT709)
        frame = QVideoFrame(frame_format)
        self.assertTrue(frame.isValid())
        rgba = QImage(16, 8, QImage.Format.Format_RGBA8888)
        surface.set_layers(QSize(16, 8), (
            GpuPreviewLayer("yuv", QImage(), QRectF(1, 2, 14, 6), opacity=.65,
                            rotation=13, video_frame=frame, native_serial=17),
            GpuPreviewLayer("rgb", QImage(), QRectF(0, 0, 16, 8),
                            video_frame=QVideoFrame(rgba), native_serial=18),
            GpuPreviewLayer("image", rgba, QRectF(0, 0, 16, 8)),
        ))
        self.assertIsNotNone(surface._pending_layers[0].video_frame)
        with patch.object(surface, "_upload_layer") as upload, patch.object(surface, "_evict_texture_cache"):
            surface._activate_pending_layers()
            self.assertEqual(upload.call_count, 3)
            self.assertTrue(upload.call_args_list[0].args[0].video_frame.isValid())
        self.assertEqual(surface._pending_layers, ())
        self.assertFalse(surface.frame_pending)
        self.assertTrue(all(layer.video_frame is None and layer.image.isNull()
                            for layer in surface._active_layers))
        active = surface._active_layers[0]
        self.assertEqual(active._video_shader_params, (0, 1))
        self.assertEqual(active.target, QRectF(1, 2, 14, 6))
        self.assertEqual((active.opacity, active.rotation, active.native_serial), (.65, 13, 17))
        self.assertIsNone(surface._active_layers[1]._video_shader_params)
        # Recompacting a paused/repaint scene preserves metadata.
        self.assertIs(_presentation_layer(active), active)

    def test_shader_parameters_preserve_gpu_color_range_and_matrix(self):
        from unittest.mock import MagicMock
        frame = MagicMock()
        frame.handleType.return_value = QVideoFrame.HandleType.RhiTextureHandle
        fmt = frame.surfaceFormat.return_value
        for color_range, color_space, expected in (
            ("ColorRange_Full", "ColorSpace_BT709", (1, 0)),
            ("ColorRange_Video", "ColorSpace_BT601", (0, 1)),
            ("ColorRange_Full", "ColorSpace_BT2020", (1, 2)),
            ("ColorRange_Unknown", "ColorSpace_Undefined", (0, 0)),
        ):
            fmt.colorRange.return_value.name = color_range
            fmt.colorSpace.return_value.name = color_space
            self.assertEqual(_video_shader_parameters(frame), expected)
        frame.handleType.return_value = QVideoFrame.HandleType.NoHandle
        self.assertEqual(_video_shader_parameters(frame), (0, 1))

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_nv12_frame_upload_copies_planes_without_rgba_conversion(self) -> None:
        class TextureStub:
            def isCreated(self) -> bool:
                return True

            def destroy(self) -> None:
                pass

        frame_format = QVideoFrameFormat(
            QSize(4, 4), QVideoFrameFormat.PixelFormat.Format_NV12,
        )
        frame = QVideoFrame(frame_format)
        self.assertTrue(frame.map(QVideoFrame.MapMode.WriteOnly))
        try:
            for plane, rows, row_bytes, value in (
                (0, 4, 4, 0x40), (1, 2, 4, 0x80),
            ):
                bits = memoryview(frame.bits(plane)).cast("B")
                bits[:] = bytes(len(bits))
                stride = frame.bytesPerLine(plane)
                for row in range(rows):
                    start = row * stride
                    bits[start:start + row_bytes] = bytes([value]) * row_bytes
        finally:
            frame.unmap()
        surface = GpuTexturePreviewSurface()
        uploads: list[bytes] = []

        def make_texture(
            _width: int, _height: int, _texture_format: object,
            _pixel_format: object, data: bytearray,
        ) -> TextureStub:
            uploads.append(bytes(data))
            return TextureStub()

        layer = GpuPreviewLayer(
            "nv12", QImage(), QRectF(0, 0, 4, 4),
            video_frame=frame, native_serial=1, native_scale=0.5,
        )
        with patch.object(surface, "_new_raw_texture", side_effect=make_texture):
            surface._upload_video_frame_layer(layer)

        self.assertEqual(uploads, [bytes([0x40]) * 4, bytes([0x80]) * 2])
        entry = surface._textures["nv12"]
        self.assertEqual(entry.video_format, "Format_NV12")
        self.assertEqual((entry.width, entry.height), (2, 2))
        self.assertEqual(entry.allocated_bytes, 6)
        self.assertEqual(surface.upload_stats.texture_allocations, 2)
        with patch.object(surface, "_upload_raw_texture") as upload_again:
            surface._upload_video_frame_layer(layer)
        upload_again.assert_not_called()
        self.assertEqual(surface.upload_stats.texture_reuses, 1)
        self.assertEqual(surface.upload_stats.texture_allocations, 2)
        surface._textures.clear()
        surface.deleteLater()

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_raw_texture_failure_releases_unpublished_allocation(self):
        from PySide6.QtOpenGL import QOpenGLTexture

        for failure in ("storage", "upload", "filter"):
            with self.subTest(failure=failure):
                texture = MagicMock()
                texture.isCreated.return_value = True
                texture.isStorageAllocated.return_value = failure != "storage"
                error = RuntimeError(failure)
                if failure == "filter":
                    texture.setWrapMode.side_effect = error
                with (
                    patch("app.preview.gpu_texture_surface.QOpenGLTexture", wraps=QOpenGLTexture) as constructor,
                    patch.object(GpuTexturePreviewSurface, "_upload_raw_texture") as upload,
                ):
                    constructor.Target = QOpenGLTexture.Target
                    constructor.Filter = QOpenGLTexture.Filter
                    constructor.WrapMode = QOpenGLTexture.WrapMode
                    constructor.PixelType = QOpenGLTexture.PixelType
                    constructor.return_value = texture
                    if failure == "upload":
                        upload.side_effect = error
                    with self.assertRaises(RuntimeError):
                        GpuTexturePreviewSurface._new_raw_texture(
                            2, 2, QOpenGLTexture.TextureFormat.R8_UNorm,
                            QOpenGLTexture.PixelFormat.Red, b"1234",
                        )
                    if failure == "storage":
                        upload.assert_not_called()
                texture.destroy.assert_called_once()

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_failed_second_plane_allocation_releases_first_and_keeps_accounting(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        y_texture = MagicMock()
        y_texture.isCreated.return_value = True
        layer = GpuPreviewLayer("video", QImage(), QRectF(0, 0, 2, 2), native_serial=1)
        with patch.object(surface, "_new_raw_texture",
                          side_effect=[y_texture, RuntimeError("UV allocation failed")]):
            with self.assertRaisesRegex(RuntimeError, "UV allocation failed"):
                surface._upload_yuv_planes(layer, "Format_NV12", 2, 2, b"1234", b"56")
        y_texture.destroy.assert_called_once()
        self.assertNotIn("video", surface._textures)
        self.assertEqual(surface.upload_stats.allocated_bytes, 0)
        self.assertEqual(surface.upload_stats.texture_allocations, 0)
        self.assertEqual(surface.upload_stats.texture_uploads, 0)

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_failed_existing_plane_update_invalidates_pair_and_allows_retry(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        y, uv = MagicMock(), MagicMock()
        y.isCreated.return_value = uv.isCreated.return_value = True
        surface._textures["video"] = _TextureEntry(y, 1, 2, 2, 6, 0, uv, "Format_NV12")
        surface._allocated_bytes = 6
        layer = GpuPreviewLayer("video", QImage(), QRectF(0, 0, 2, 2), native_serial=2)
        with patch.object(surface, "_upload_raw_texture",
                          side_effect=[None, RuntimeError("UV upload failed")]):
            with self.assertRaisesRegex(RuntimeError, "UV upload failed"):
                surface._upload_yuv_planes(layer, "Format_NV12", 2, 2, b"1234", b"56")
        y.destroy.assert_called_once()
        uv.destroy.assert_called_once()
        self.assertNotIn("video", surface._textures)
        self.assertEqual(surface.upload_stats.allocated_bytes, 0)
        self.assertEqual(surface.upload_stats.texture_uploads, 0)
        with patch.object(surface, "_new_raw_texture", side_effect=[MagicMock(), MagicMock()]):
            surface._upload_yuv_planes(layer, "Format_NV12", 2, 2, b"1234", b"56")
        self.assertEqual(surface._textures["video"].cache_key, 2)
        self.assertEqual(surface.upload_stats.allocated_bytes, 6)
        self.assertEqual(surface.upload_stats.texture_allocations, 2)
        self.assertEqual(surface.upload_stats.texture_uploads, 2)
        surface._textures.clear()

    @unittest.skipUnless(GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL unavailable")
    def test_destroy_attempts_both_planes_even_if_first_destroy_raises(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        y, uv = MagicMock(), MagicMock()
        y.isCreated.return_value = uv.isCreated.return_value = True
        y.destroy.side_effect = RuntimeError("context lost")
        surface._textures["video"] = _TextureEntry(y, 1, 2, 2, 6, 0, uv, "Format_NV12")
        surface._allocated_bytes = 6
        with self.assertRaisesRegex(RuntimeError, "context lost"):
            surface._destroy_texture("video")
        uv.destroy.assert_called_once()
        self.assertEqual(surface.upload_stats.allocated_bytes, 0)
        self.assertNotIn("video", surface._textures)

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_dynamic_upload_uses_existing_storage_without_redefining_it(self) -> None:
        from PySide6.QtOpenGL import QOpenGLTexture

        class TextureStub:
            def __init__(self) -> None:
                self.arguments: tuple[object, ...] = ()

            def isCreated(self) -> bool:
                return True

            def setData(self, *arguments: object) -> None:
                # Like PySide6: an int "address" is rejected. The old upload
                # passed one, silently recreating the texture on every frame.
                if isinstance(arguments[-1], int):
                    raise ValueError("setData called with wrong argument values")
                self.arguments = arguments

        surface = GpuTexturePreviewSurface()
        texture = TextureStub()
        surface._textures["dynamic"] = _TextureEntry(
            texture, 1, 16, 8, 16 * 8 * 4, 0,
        )
        image = QImage(16, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#336699"))

        surface._upload_layer(
            GpuPreviewLayer("dynamic", image, QRectF(0, 0, 16, 8)),
        )

        self.assertEqual(
            texture.arguments[:2],
            (
                QOpenGLTexture.PixelFormat.RGBA,
                QOpenGLTexture.PixelType.UInt8,
            ),
        )
        self.assertEqual(len(memoryview(texture.arguments[2]).cast("B")), 16 * 8 * 4)
        self.assertIs(surface._textures["dynamic"].texture, texture)  # in place, not reallocated
        surface._textures.clear()
        surface.deleteLater()

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_visible_surface_allocates_one_texture_per_layer(self) -> None:
        surface = GpuTexturePreviewSurface()
        failures: list[str] = []
        surface.backend_failed.connect(failures.append)
        base = QImage(64, 36, QImage.Format.Format_RGBA8888)
        base.fill(QColor("#112233"))
        overlay = QImage(16, 12, QImage.Format.Format_RGBA8888)
        overlay.fill(QColor(255, 255, 255, 128))
        surface.resize(320, 180)
        surface.set_layers(QSize(64, 36), (
            GpuPreviewLayer("base", base, QRectF(0, 0, 64, 36)),
            GpuPreviewLayer("overlay", overlay, QRectF(8, 6, 16, 12)),
        ))
        surface.show()
        QTest.qWait(250)
        if not surface.ready:
            surface.close()
            self.skipTest(failures[0] if failures else "OpenGL context unavailable")
        self.assertEqual(set(surface._textures), {"base", "overlay"})
        self.assertTrue(all(
            entry.texture.isCreated() for entry in surface._textures.values()
        ))
        self.assertTrue(all(layer.image.isNull() for layer in surface._active_layers))
        initial_uploads = surface.upload_stats.texture_uploads
        surface.set_layers(QSize(64, 36), (
            GpuPreviewLayer("base", base, QRectF(0, 0, 64, 36)),
            GpuPreviewLayer("overlay", overlay, QRectF(8, 6, 16, 12)),
        ))
        QTest.qWait(100)
        self.assertEqual(surface.upload_stats.texture_uploads, initial_uploads)
        self.assertGreaterEqual(surface.upload_stats.texture_reuses, 2)
        frame = surface.grabFramebuffer()
        self.assertFalse(frame.isNull())
        self.assertNotEqual(
            frame.pixelColor(60, 50), frame.pixelColor(10, 10),
        )
        surface.close()
        surface.deleteLater()

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_same_size_dynamic_layer_updates_existing_texture_storage(self) -> None:
        surface = GpuTexturePreviewSurface()
        failures: list[str] = []
        surface.backend_failed.connect(failures.append)
        first = QImage(32, 18, QImage.Format.Format_RGBA8888)
        first.fill(QColor("#AA2233"))
        second = QImage(32, 18, QImage.Format.Format_RGBA8888)
        second.fill(QColor("#2266CC"))
        target = QRectF(0, 0, 32, 18)
        surface.resize(320, 180)
        surface.set_layers(
            QSize(32, 18), (GpuPreviewLayer("dynamic", first, target),),
        )
        surface.show()
        QTest.qWait(150)
        if not surface.ready:
            surface.close()
            self.skipTest(failures[0] if failures else "OpenGL context unavailable")
        texture = surface._textures["dynamic"].texture
        initial_uploads = surface.upload_stats.texture_uploads

        surface.set_layers(
            QSize(32, 18), (GpuPreviewLayer("dynamic", second, target),),
        )
        QTest.qWait(150)

        self.assertIs(surface._textures["dynamic"].texture, texture)
        self.assertEqual(surface.upload_stats.texture_uploads, initial_uploads + 1)
        self.assertFalse(failures)
        surface.close()
        surface.deleteLater()

    @unittest.skipUnless(
        GPU_TEXTURE_SURFACE_AVAILABLE, "Qt OpenGL widgets are unavailable",
    )
    def test_texture_cache_evicts_inactive_layer_when_budget_is_exceeded(self) -> None:
        surface = GpuTexturePreviewSurface(texture_budget_bytes=1024 * 1024)
        first = QImage(512, 512, QImage.Format.Format_RGBA8888)
        first.fill(QColor("#223344"))
        second = QImage(512, 512, QImage.Format.Format_RGBA8888)
        second.fill(QColor("#445566"))
        target = QRectF(0, 0, 512, 512)
        surface.resize(256, 256)
        surface.set_layers(
            QSize(512, 512), (GpuPreviewLayer("first", first, target),),
        )
        surface.show()
        QTest.qWait(150)
        if not surface.ready:
            surface.close()
            self.skipTest("OpenGL context unavailable")
        self.assertEqual(set(surface._textures), {"first"})

        surface.set_layers(
            QSize(512, 512), (GpuPreviewLayer("second", second, target),),
        )
        QTest.qWait(150)
        self.assertEqual(set(surface._textures), {"second"})
        self.assertGreaterEqual(surface.upload_stats.texture_evictions, 1)
        self.assertLessEqual(
            surface.upload_stats.allocated_bytes,
            surface.upload_stats.texture_budget_bytes,
        )
        surface.close()
        surface.deleteLater()


if __name__ == "__main__":
    unittest.main()
