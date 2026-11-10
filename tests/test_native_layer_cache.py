"""Real Qt raster comparisons; no GPU/codec required."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.native_canvas_layer_packets import capture_native_canvas_track_frame
from app.renderer.native_dynamic_layers import build_dynamic_stack
from app.renderer.native_frame_transport import frame_chunks
from app.renderer.native_layer_cache import NativeRasterCache, split_resident_bands
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.models.project import ProjectDocument, CanvasSettings


class RasterCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scene = CanvasScene()
        self.scene.set_artboard_size(128, 128)
        self.scene.show_grid = False
        self.addCleanup(self.scene.clear)
        self.track = PlaylistTrack("", "First", duration_seconds=2)

    def add(self, source):
        self.scene.addItem(SourceItem(source))

    def capture(self, bands, t, cache=None):
        return capture_native_canvas_track_frame(self.scene, self.track, bands,
            elapsed_seconds=t, playlist_duration_seconds=2, raster_cache=cache)

    def test_identical_static_pixels_repaint_once_including_alpha_rotation_effects_clip(self):
        # Full-frame snapshots preserve artboard clipping and existing painter
        # effects. Cached bytes must match those same snapshots exactly.
        for source in [
            Source(SourceType.SHAPE, "Background", width=128, height=128, z_index=0),
            Source(SourceType.TEXT, "Text", text="Cache", x=-12, y=12,
                   width=96, height=80, z_index=2, rotation=23, opacity=.43,
                   scale=1.2, blur=1.5, frame_width=2, mask_shape="circle", font_size=20),
        ]:
            source.shadow.enabled = True
            source.shadow.blur_radius = 5
            self.add(source)
        bands = ((None, 0), (2, None))
        cache = NativeRasterCache(self.scene, bands, 2)
        reference = self.capture(bands, 0)
        with patch.object(CanvasSnapshot, "capture_track", wraps=CanvasSnapshot.capture_track) as paint:
            for i in range(60):
                frame = self.capture(bands, i / 60, cache)
                self.assertEqual(frame, reference)
            self.assertEqual(paint.call_count, 2)
        self.assertEqual(cache.rasterizations, 2)
        self.assertEqual(cache.hits, 118)
        self.assertEqual(cache.retained_bytes, 128 * 128 * 4 * 2)
        cache.clear()
        self.assertEqual(cache.retained_bytes, 0)

    def test_native_frozen_lyric_settings_match_uncached_pixels(self):
        # Production-native export owns a detached project and may reuse the
        # resolved advanced-lyrics configuration across frames, not pixels.
        source = Source(
            SourceType.LYRICS, "Karaoke", x=15, y=16,
            width=105, height=85, font_size=21,
            subtitle_advanced_categories=["layout", "styles"],
            subtitle_advanced_settings={
                "subtitle_context_lines": 2,
                "subtitle_next_lines": 2,
                "subtitle_line_spacing": 18.0,
            },
        )
        self.add(source)
        item = next(item for item in self.scene.items() if isinstance(item, SourceItem))
        bands = ((None, None),)
        original = [self.capture(bands, i / 60) for i in range(3)]
        cache = NativeRasterCache(self.scene, bands, 2)
        original_method = Source.resolved_lyrics
        resolutions = []

        def counted(obj):
            if obj is source:
                resolutions.append(True)
            return original_method(obj)

        with patch.object(Source, "resolved_lyrics", counted):
            native = [self.capture(bands, i / 60, cache) for i in range(3)]
        self.assertEqual(native, original)
        self.assertEqual(cache.rasterizations, 3)
        self.assertEqual(len(resolutions), 1)
        self.assertIs(item.source, source)
        self.assertEqual(source.subtitle_context_lines, 1)
        self.assertEqual(len(cache.frozen_lyric_settings_cache), 1)
        cache.clear()
        self.assertEqual(cache.frozen_lyric_settings_cache, {})

    def test_band_source_items_precomputed_in_z_order_without_leaking_sources(self):
        bottom = Source(SourceType.SHAPE, "Bottom", width=128, height=128, z_index=0)
        top = Source(SourceType.TEXT, "Top", width=80, height=60, z_index=3)
        self.add(bottom)
        self.add(top)
        bands = ((None, 1), (2, None))
        cache = NativeRasterCache(self.scene, bands, 2)
        self.assertEqual(
            tuple(tuple(item.source.name for item in items) for items in cache.band_source_items),
            (("Bottom",), ("Top",)),
        )
        with patch.object(CanvasSnapshot, "capture_track", wraps=CanvasSnapshot.capture_track) as capture:
            result = self.capture(bands, 0.25, cache)
        self.assertEqual(len(result.layers), 2)
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(
            tuple(tuple(item.source.name for item in call.kwargs["band_source_items"])
                  for call in capture.call_args_list),
            (("Bottom",), ("Top",)),
        )

    def test_explicit_gui_profile_overrides_legacy_experiment_environment(self):
        self.add(Source(SourceType.SHAPE, "Base", width=128, height=128))
        with patch.dict(os.environ, {
            "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1",
            "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "1",
        }):
            safe = NativeRasterCache(
                self.scene, ((None, None),), 2,
                optimizations=NativeGpuOptimizations.for_profile("standard"),
            )
            fast = NativeRasterCache(
                self.scene, ((None, None),), 2,
                optimizations=NativeGpuOptimizations.for_profile("accelerated"),
            )
        self.assertFalse(safe.partial_band_enabled)
        self.assertFalse(safe.compact_band_enabled)
        self.assertTrue(fast.partial_band_enabled)
        self.assertTrue(fast.compact_band_enabled)

    def test_opt_in_partial_transparent_band_preserves_unchanged_native_packets(self):
        # 0.5 output scale exercises otherwise misaligned original bounds.
        self.add(Source(
            SourceType.SHAPE, "Opaque base", width=128, height=128, z_index=0,
        ))
        clock = Source(
            SourceType.TEXT, "Moving clock", text="%current_time%",
            x=32, y=31, width=68, height=35, font_size=20, z_index=2,
        )
        clock.shadow.enabled = True
        clock.shadow.offset_x = 3.0
        clock.shadow.offset_y = 2.0
        clock.shadow.blur_radius = 8.0
        self.add(clock)
        bands = ((None, 0), (2, None))

        def run(active, scale):
            with patch.dict(os.environ, {
                "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1" if active else "0",
            }):
                cache = NativeRasterCache(self.scene, bands, 2)
            frames = [
                capture_native_canvas_track_frame(
                    self.scene, self.track, bands, elapsed_seconds=seconds,
                    playlist_duration_seconds=2, output_scale=scale, raster_cache=cache,
                )
                for seconds in (0, 0.5, 1.0)
            ]
            return frames, cache

        for scale in (0.5, 1.0, 1.5):
            with self.subTest(scale=scale):
                original, baseline = run(False, scale)
                optimized, roi = run(True, scale)
                self.assertEqual(original, optimized)
                self.assertTrue(all(frame.schema == 1 for frame in optimized))
                self.assertTrue(all(frame.layers[0].width == round(128 * scale)
                                    for frame in optimized))
                self.assertEqual(baseline.partial_band_captures, 0)
                self.assertGreater(roi.partial_band_captures, 0)
                self.assertLess(roi.scene_render_source_pixels,
                                roi.full_frame_source_pixels)
                self.assertEqual(roi.rasterizations, baseline.rasterizations)

    def test_compact_roi_v2_packets_are_exactly_the_same_pixels_as_expanded_roi(self):
        # Current implementation expands pixel-aligned transparent crop back
        # to the full-size BGRA plane. Compact mode sends those same pixels,
        # plus an exact integer translation and half-open output clip.
        self.add(Source(
            SourceType.SHAPE, "Opaque background",
            width=128, height=128, z_index=0,
        ))
        text = Source(
            SourceType.TEXT, "Clock", text="%current_time%",
            x=32, y=31, width=68, height=35, font_size=20, z_index=2,
        )
        text.shadow.enabled = True
        text.shadow.offset_x = 3.0
        text.shadow.offset_y = 2.0
        text.shadow.blur_radius = 8.0
        self.add(text)
        bands = ((None, 0), (2, None))

        for scale in (0.5, 1.0, 1.5):
            for elapsed in (0.0, 0.5, 1.0):
                with self.subTest(scale=scale, elapsed=elapsed):
                    with patch.dict(os.environ, {
                        "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1",
                        "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "0",
                    }):
                        expanded_cache = NativeRasterCache(self.scene, bands, 2)
                    with patch.dict(os.environ, {
                        "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1",
                        "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "1",
                    }):
                        compact_cache = NativeRasterCache(self.scene, bands, 2)
                    kw = dict(
                        elapsed_seconds=elapsed,
                        playlist_duration_seconds=2,
                        output_scale=scale,
                    )
                    expanded = capture_native_canvas_track_frame(
                        self.scene, self.track, bands,
                        raster_cache=expanded_cache, **kw,
                    )
                    compact = capture_native_canvas_track_frame(
                        self.scene, self.track, bands,
                        raster_cache=compact_cache, **kw,
                    )
                    self.assertEqual(compact.schema, 2)
                    self.assertEqual(compact.layers[0], expanded.layers[0])
                    self.assertEqual(len(compact.layers), len(expanded.layers))
                    full = expanded.layers[1]
                    small = compact.layers[1]
                    self.assertLess(len(small.pixels), len(full.pixels))
                    self.assertEqual(small.inverse_transform[:4], (1, 0, 0, 1))
                    x, y, right, bottom = small.clip
                    self.assertEqual(small.inverse_transform[4:], (-x, -y))
                    self.assertEqual((right - x, bottom - y), (small.width, small.height))

                    restored = bytearray(len(full.pixels))
                    for row in range(small.height):
                        dest = ((y + row) * full.width + x) * 4
                        src = row * small.width * 4
                        restored[dest:dest + small.width * 4] = small.pixels[
                            src:src + small.width * 4
                        ]
                    # Compare every BGRA byte including transparent pixels.
                    self.assertEqual(bytes(restored), full.pixels)
                    self.assertEqual(compact_cache.compact_band_captures, 1)
                    self.assertEqual(
                        compact_cache.compact_canvas_bytes_saved,
                        len(full.pixels) - len(small.pixels),
                    )
                    self.assertLess(
                        sum(len(chunk) for chunk in frame_chunks(compact, 0)),
                        sum(len(chunk) for chunk in frame_chunks(expanded, 0)),
                    )

    def test_compact_roi_requires_partial_mode_and_full_overlays_fall_back(self):
        self.add(Source(SourceType.SHAPE, "Base", width=128, height=128, z_index=0))
        self.add(Source(SourceType.SHAPE, "Full", width=128, height=128, z_index=2))
        bands = ((None, 0), (2, None))
        for partial in ("0", "1"):
            with patch.dict(os.environ, {
                "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": partial,
                "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS": "1",
            }):
                cache = NativeRasterCache(self.scene, bands, 2)
            frame = capture_native_canvas_track_frame(
                self.scene, self.track, bands, elapsed_seconds=0,
                raster_cache=cache,
            )
            self.assertEqual(frame.schema, 1)
            self.assertEqual(cache.compact_band_captures, 0)
            self.assertEqual(cache.compact_canvas_bytes_saved, 0)
            self.assertFalse(any(layer.inverse_transform for layer in frame.layers))

    def test_opt_in_partial_band_does_not_crop_full_screen_or_opaque_base(self):
        self.add(Source(SourceType.SHAPE, "Base", width=128, height=128, z_index=0))
        self.add(Source(SourceType.SHAPE, "Full overlay", width=128, height=128, z_index=2))
        bands = ((None, 0), (2, None))
        with patch.dict(os.environ, {"PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS": "1"}):
            cache = NativeRasterCache(self.scene, bands, 2)
        native = capture_native_canvas_track_frame(
            self.scene, self.track, bands, elapsed_seconds=0,
            raster_cache=cache,
        )
        original = capture_native_canvas_track_frame(
            self.scene, self.track, bands, elapsed_seconds=0,
        )
        self.assertEqual(native, original)
        self.assertEqual(cache.partial_band_captures, 0)
        self.assertEqual(cache.scene_render_source_pixels,
                         cache.full_frame_source_pixels)

    def test_mixed_bands_split_and_dynamic_clock_never_freezes(self):
        sources = [Source(SourceType.SHAPE, "Static", width=128, height=128, z_index=0),
                   Source(SourceType.PROGRESS_BAR, "Clock", progress_mode="track", z_index=2,
                          width=128, height=90)]
        for source in sources:
            self.add(source)
        project = ProjectDocument(canvas=CanvasSettings(128, 128), sources=sources)
        stack = split_resident_bands(self.scene, build_dynamic_stack(project, 1), 2)
        self.assertEqual(len(stack.bands), 2)
        self.assertEqual(stack.order, (("canvas", 0), ("canvas", 1)))
        cache = NativeRasterCache(self.scene, stack.bands, 2)
        first = self.capture(stack.bands, 0, cache)
        second = self.capture(stack.bands, 1, cache)
        self.assertEqual(first.layers[0], second.layers[0])
        self.assertTrue(first.layers[1].pixels != second.layers[1].pixels, "clock pixels must advance")
        self.assertEqual(cache.rasterizations, 3)
        self.assertEqual(cache.hits, 1)

    def test_equal_z_and_stream_bound_keep_original_order(self):
        sources = [Source(SourceType.SHAPE if i % 2 == 0 else SourceType.TIME,
                          str(i), z_index=i, width=128, height=128) for i in range(20)]
        sources.append(Source(SourceType.TIME, "Same Z", z_index=0))
        for source in sources:
            self.add(source)
        project = ProjectDocument(sources=sources)
        stack = split_resident_bands(self.scene, build_dynamic_stack(project, 1), 2)
        self.assertLessEqual(len(stack.bands), 8)
        cache = NativeRasterCache(self.scene, stack.bands, 2)
        self.assertNotIn("base", cache.keys)
        for i, band in enumerate(stack.bands):
            if i:
                self.assertGreater(band[0], stack.bands[i - 1][1])

    def test_new_export_never_reuses_prior_project_pixels(self):
        source = Source(SourceType.TEXT, "Label", text="Old", width=128, height=128)
        self.add(source)
        old = self.capture(((None, None),), 0, NativeRasterCache(self.scene, ((None, None),), 2))
        source.text = "New longer label"
        new = self.capture(((None, None),), 0, NativeRasterCache(self.scene, ((None, None),), 2))
        self.assertTrue(old.layers[0].pixels != new.layers[0].pixels, "new export must repaint changed text")
