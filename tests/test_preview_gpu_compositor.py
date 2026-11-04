"""GPU preview content reuse and keyframe transform parity tests."""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QImage, QPainter
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.animation.keyframes import Keyframe
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.gpu_texture_surface import GpuPreviewLayer, GpuTexturePreviewSurface
from app.preview.texture_fingerprint import image_content_signature
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.utils.i18n import Translator


@dataclass(frozen=True)
class DummyBinding:
    source: object
    output_scale: float = 1.0
    windows: tuple = ()
    def values(self, seconds):
        return dict(x=50.0, y=60.0, scale=1.5, opacity=0.6, rotation=30.0)


class GpuCompositorOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_identical_pixels_different_qimage_cache_keys_share_signature(self):
        image = QImage(48, 32, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xff123456)
        copy = QImage(48, 32, QImage.Format.Format_ARGB32_Premultiplied)
        copy.fill(0xff123456)
        self.assertNotEqual(image.cacheKey(), copy.cacheKey())
        self.assertEqual(image_content_signature(image), image_content_signature(copy))
        copy.setPixel(4, 3, 0xff113355)
        self.assertNotEqual(image_content_signature(image), image_content_signature(copy))
        self.assertIsNone(image_content_signature(image, max_bytes=32))

    def test_track_static_policy_excludes_animated_or_timed_sources(self):
        self.assertTrue(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.ALBUM_COVER, "Cover")
        ))
        self.assertTrue(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.BACKGROUND, "Backdrop", background_mode="album_art")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.LYRICS, "Lyrics")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.TEXT, "Time", text="%current_time%")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.TEXT, "Moving", loop_motion="float")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.BACKGROUND, "Animated", background_mode="album_art",
                   background_ambient=True)
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.ALBUM_COVER, "Temporary", timeline_start=1.0)
        ))

    def test_static_z_band_cache_reuses_images_and_invalidates_on_change(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        frame = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(0xff304050)
        bands = [(None, 0.0), (1.0, None)]

        def render():
            return preview._gpu_z_band_layers(
                preview.tracks[0], 0, 5.0, 0.0,
                None, 1.0, 0.0, [], (),
            )

        with patch.object(CanvasSnapshot, "z_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value={"base", "layer:0"}), \
             patch.object(CanvasSnapshot, "capture_track", return_value=frame) as capture:
            first = render()
            second = render()
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(len(first), len(second))
            self.assertEqual(preview._static_band_cache_hits, 2)
            preview._invalidate_preview_render_cache()
            render()
            self.assertEqual(capture.call_count, 4)
            preview._frame_junction = object()
            render()
            self.assertEqual(capture.call_count, 6)

    def test_ordered_canvas_band_cache_renders_only_changed_runs(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        preview._cached_source_items = ()
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xff444444)
        bands = [(None, 0), (1, 1), (2, None)]
        with patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value={"base", "layer:1"}), \
             patch.object(CanvasSnapshot, "band_capture_envelope", return_value=None), \
             patch.object(CanvasSnapshot, "capture_track", return_value=image) as capture:
            first = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.0, 0.0, frozenset(),
            )
            second = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.1, 0.0, frozenset(),
            )
            self.assertEqual(len(first), 3)
            self.assertEqual(len(second), 3)
            self.assertEqual(capture.call_count, 4)
            self.assertEqual(preview._ordered_cache_hits, 2)
            preview._invalidate_preview_render_cache()
            preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.2, 0.0, frozenset(),
            )
            self.assertEqual(capture.call_count, 7)

    def test_ordered_canvas_uses_crop_coordinates_for_gpu_layers(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        preview._cached_source_items = ()
        image = QImage(100, 60, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xffaabbcc)
        bands = [(None, 0), (1, None)]
        crop = QRectF(40, 30, 100, 60)
        with patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value=set()), \
             patch.object(CanvasSnapshot, "band_capture_envelope", return_value=crop), \
             patch.object(CanvasSnapshot, "capture_track", return_value=image):
            layers = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.0, 0.0, frozenset(),
            )
        self.assertEqual((layers[1].target.x(), layers[1].target.y()), (40, 30))
        self.assertEqual((layers[1].target.width(), layers[1].target.height()), (100, 60))

    def test_large_ordered_band_reuses_working_image_without_changing_previous_layer(self):
        scene = CanvasScene()
        scene.set_artboard_size(1920, 1200)
        scene.addItem(SourceItem(Source(
            SourceType.PROGRESS_BAR, "Progress", width=1920, height=1200,
        )))
        track = PlaylistTrack("", "Preview", duration_seconds=10)
        with patch("app.dialogs.export_preview_dialog.load_preview_quality", return_value="resolution"):
            preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)

        first = preview._ordered_canvas_gpu_layers(track, 0, 2.0, 0.0, frozenset())[0].image
        original_pixels = first.copy()
        scratch = preview._gpu_ordered_scratch
        self.assertIsNotNone(scratch)

        second = preview._ordered_canvas_gpu_layers(track, 0, 7.0, 0.0, frozenset())[0].image
        self.assertIs(preview._gpu_ordered_scratch, scratch)
        self.assertEqual(first, original_pixels)
        self.assertNotEqual(second, first)
        preview._invalidate_preview_render_cache()
        self.assertIsNone(preview._gpu_ordered_scratch)

    def test_gpu_ordered_layers_match_full_scene_in_overlapping_z_case(self):
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        for source in (
            Source(SourceType.SHAPE, "base", x=0, y=0, width=320, height=180,
                   fill_color="#102030", z_index=0),
            Source(SourceType.PROGRESS_BAR, "progress", x=20, y=35,
                   width=200, height=30, z_index=1),
            Source(SourceType.SHAPE, "foreground", x=150, y=40,
                   width=80, height=75, fill_color="#44cc33", z_index=2),
        ):
            scene.addItem(SourceItem(source))
        track = PlaylistTrack("missing.wav", "Preview", duration_seconds=30)
        with patch("app.dialogs.export_preview_dialog.load_preview_quality", return_value="resolution"):
            preview = ExportPreviewDialog(
                scene, [track], Translator(), preferred_backend="cpu",
            )

        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        preview._refresh_source_partitions()
        baseline = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0,
            elapsed_seconds=5.0, timeline_seconds=5.0,
            playlist_duration_seconds=30.0, playlist_tracks=[track],
            compiled_plan=preview._compiled_plan,
        )
        layers = preview._ordered_canvas_gpu_layers(
            track, 0, 5.0, 0.0, frozenset(),
        )
        self.assertGreaterEqual(len(layers), 3)
        assembled = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
        assembled.fill(0)
        painter = QPainter(assembled)
        for layer in layers:
            painter.drawImage(layer.target, layer.image)
        painter.end()
        for point in ((12, 12), (30, 45), (170, 75), (275, 140)):
            self.assertEqual(
                assembled.pixelColor(*point), baseline.pixelColor(*point),
                msg=f"Z-band pixel mismatch at {point}",
            )

    def test_still_lyrics_only_recompose_on_cue_changes_and_seek(self):
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(
            SourceType.LYRICS, "Bilingual", width=220, height=140,
            subtitle_animation="none", subtitle_context_lines=0,
            subtitle_next_lines=0, subtitle_intro_enabled=False,
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("", "Preview", duration_seconds=10, lyrics=[
            {"start": 0.0, "end": 3.0, "text": "하나\nOne"},
            {"start": 3.0, "end": 9.0, "text": "둘\nTwo"},
        ])
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)

        def key(seconds):
            return preview._preview_composition_key(
                track, None, frozenset({source.id}), frozenset(), seconds,
                elapsed=seconds, ordered_canvas=True, video_z_banded=False,
            )

        first = key(1.0)
        self.assertEqual(first, key(2.0))
        self.assertNotEqual(first, key(4.0))
        self.assertEqual(first, key(1.0))

    def test_ordered_canvas_does_not_paint_separate_video_twice(self):
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        backdrop = Source(SourceType.SHAPE, "Backdrop", width=320, height=180,
                          z_index=0, fill_color="#102030")
        direct = Source(SourceType.SHAPE, "Separate", x=10, y=10, width=100, height=100,
                        z_index=1, fill_color="#0000ff")
        foreground = Source(SourceType.SHAPE, "Foreground", x=55, y=45, width=80, height=80,
                            z_index=2, fill_color="#44cc33")
        for source in (backdrop, direct, foreground):
            scene.addItem(SourceItem(source))
        track = PlaylistTrack("", "Preview", duration_seconds=10)
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        audio = VisualizerOverlay(35, 25, 70, 70, "bars", "#ffff00", z_index=1)
        red = QImage(30, 30, QImage.Format.Format_RGB32)
        red.fill(0xffff0000)
        yellow = QImage(70, 70, QImage.Format.Format_RGB32)
        yellow.fill(0xffffff00)
        video = GpuPreviewLayer("direct", red, QRectF(40, 30, 30, 30))
        with (
            patch.object(preview, "gpu_surface") as surface,
            patch.object(preview, "_prepared_overlay_images", return_value=([audio], (yellow,))),
        ):
            self.assertTrue(preview._submit_gpu_ordered_canvas(
                track, 0, 1.0, 0.0, frozenset(), frozenset({direct.id}), [(1, video)],
                None, 1.0, 0.0, None, [audio],
            ))
        size, layers = surface.set_layers.call_args.args
        assembled = QImage(size, QImage.Format.Format_RGB32)
        painter = QPainter(assembled)
        for layer in layers:
            painter.drawImage(layer.target, layer.image)
        painter.end()
        for x, y, color in (
            (20, 20, "#102030"), (38, 28, "#ffff00"),
            (45, 35, "#ff0000"), (60, 50, "#44cc33"),
        ):
            self.assertEqual(assembled.pixelColor(x, y).name(), color)

    def test_compact_keyframed_band_matches_full_raster_at_fractional_scale(self):
        scene = CanvasScene()
        scene.set_artboard_size(321, 181)
        source = Source(SourceType.SHAPE, "Moving", x=12, y=42, width=60, height=42,
                        fill_color="#ff8800", z_index=1)
        source.keyframes.global_curves = {
            "x": [Keyframe(0, 12), Keyframe(8, 206)],
            "rotation": [Keyframe(0, 0), Keyframe(8, 25)],
        }
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("", "Preview", duration_seconds=10)
        common = dict(transparent=True, timeline_seconds=6, elapsed_seconds=6, output_scale=.8)
        full = CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0, **common)
        metrics = {}
        compact = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, compact_preview_band=True, render_metrics=metrics, **common,
        )
        rect = metrics["capture_rect"]
        self.assertLess(compact.sizeInBytes(), full.sizeInBytes() // 2)
        assembled = QImage(full.size(), full.format())
        assembled.fill(0)
        painter = QPainter(assembled)
        painter.drawImage(round(rect.x() * full.width() / 321),
                          round(rect.y() * full.height() / 181), compact)
        painter.end()
        self.assertEqual(assembled, full)

    @unittest.skipUnless(os.getenv("PLAYLIST_CANVAS_RUN_PREVIEW_SOAK") == "1",
                         "requires native OpenGL preview")
    def test_bgra_premultiplied_upload_matches_qpainter_source_over(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        self.addCleanup(surface.close)
        size = QSize(64, 48)
        surface.resize(size)
        surface.show()
        QTest.qWait(100)
        base = QImage(size, QImage.Format.Format_RGB32)
        base.fill(0xff215597)
        alpha = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        alpha.fill(0x80582212)
        expected = base.copy()
        painter = QPainter(expected)
        painter.drawImage(0, 0, alpha)
        painter.end()
        target = QRectF(0, 0, size.width(), size.height())
        surface.set_layers(size, [GpuPreviewLayer("base", base, target),
                                  GpuPreviewLayer("alpha", alpha, target)])
        actual = surface.grabFramebuffer()
        self.assertTrue(surface.ready)
        center = actual.pixelColor(actual.width() // 2, actual.height() // 2)
        for got, want in zip(center.getRgb(), expected.pixelColor(16, 16).getRgb(), strict=True):
            self.assertLessEqual(abs(got - want), 1)

    @unittest.skipUnless(os.getenv("PLAYLIST_CANVAS_RUN_PREVIEW_SOAK") == "1",
                         "requires native OpenGL preview")
    def test_mapped_cpu_yuv_upload_matches_qt_and_reuses_odd_stride_textures(self):
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        self.addCleanup(surface.close)
        surface.resize(130, 68)
        surface.show()
        QTest.qWait(100)
        self.assertTrue(surface.ready)

        for name in ("Format_YUV420P", "Format_NV12", "Format_NV21"):
            with self.subTest(pixel_format=name):
                # Cover both tight rows and Qt's odd-width padded rows.
                size = QSize(64 if name == "Format_NV21" else 65, 34)
                fmt = QVideoFrameFormat(size, getattr(QVideoFrameFormat.PixelFormat, name))
                frame = QVideoFrame(fmt)
                self.assertTrue(frame.map(QVideoFrame.MapMode.WriteOnly))
                frame.bits(0)[:] = bytes([100]) * frame.mappedBytes(0)
                if name == "Format_YUV420P":
                    frame.bits(1)[:] = bytes([90]) * frame.mappedBytes(1)
                    frame.bits(2)[:] = bytes([180]) * frame.mappedBytes(2)
                else:
                    chroma = bytes([180, 90] if name == "Format_NV21" else [90, 180])
                    frame.bits(1)[:] = chroma * (frame.mappedBytes(1) // 2)
                frame.unmap()
                expected = frame.toImage().pixelColor(16, 16)

                def draw(serial):
                    surface.set_layers(size, [GpuPreviewLayer(
                        name, QImage(), QRectF(0, 0, size.width(), size.height()),
                        video_frame=frame, native_serial=serial,
                    )])
                    return surface.grabFramebuffer().pixelColor(32, 32)

                with patch.object(surface, "_copy_scaled_video_plane", wraps=surface._copy_scaled_video_plane) as pack:
                    actual = draw(1)
                    if name == "Format_NV21":
                        pack.assert_not_called()
                self.assertTrue(surface.ready)
                for got, want in zip(actual.getRgb(), expected.getRgb(), strict=True):
                    self.assertLessEqual(abs(got - want), 2)
                initial = surface.upload_stats
                draw(1)
                repeated = surface.upload_stats
                self.assertEqual(repeated.texture_uploads, initial.texture_uploads)
                draw(2)
                updated = surface.upload_stats
                self.assertEqual(updated.texture_allocations, initial.texture_allocations)
                self.assertEqual(updated.texture_uploads, initial.texture_uploads + 2)

    def test_gpu_keyframe_uses_unchanged_texture_pixels(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        source = SimpleNamespace(x=0.0, y=0.0, width=100.0, height=50.0,
                                 scale=1.0, opacity=1.0, rotation=0.0)
        overlay = VisualizerOverlay(
            10, 20, 100, 50, "bars", "#ffffff",
            keyframe_binding=DummyBinding(source),
        )
        image = QImage(100, 50, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xffabcdef)
        layers = preview._gpu_audio_layers([overlay], (image,))
        self.assertEqual(len(layers), 1)
        layer = layers[0]
        self.assertEqual(layer.image.cacheKey(), image.cacheKey())
        self.assertEqual((layer.target.width(), layer.target.height()), (150.0, 75.0))
        self.assertEqual(layer.rotation, 30.0)
        self.assertAlmostEqual(layer.opacity, 0.6)
        # CPU path keeps its pixel-scaling fallback for a GPU context loss.
        _overlays, cpu_images = preview._apply_overlay_keyframes(
            [overlay], (image,), 0.0,
        )
        self.assertEqual((cpu_images[0].width(), cpu_images[0].height()), (150, 75))


if __name__ == "__main__":
    unittest.main()
