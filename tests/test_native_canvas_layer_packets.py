"""Qt source-order / straight-alpha D3D11 packet migration tests.

These run with Qt offscreen and do NOT require Nvidia hardware. They
prove that the existing CanvasSnapshot renderer supplies separate
opaque and transparent full-frame layers without an extra CPU flatten.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import Future
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.native_canvas_layer_packets import (
    NativeCanvasBgraLayer, NativeCanvasFrame, DeferredNativeCanvasLayer,
    capture_native_canvas_frame, capture_native_canvas_track_frame,
    save_native_canvas_frame,
    straight_bgra_bytes, straight_bgra_bytes_with_sha256,
)


class NativeCanvasGpuPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def build_scene() -> CanvasScene:
        scene = CanvasScene()
        scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Background", x=0, y=0,
            width=1280, height=720, z_index=0,
            fill_color="#203040",
        )))
        # Use the actual Qt text SourceItem painter, not a synthetic bitmap
        # or separately drawn QGraphicsTextItem.
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Lyric Text", x=160, y=260,
            width=960, height=160, z_index=20,
            text="Actual Canvas lyric-style text",
            font_size=72, fill_color="#FF7B45",
        )))
        # A visible colored overlay makes the alpha bounds robust even when
        # the CI runner substitutes a different font.
        scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Overlay geometry", x=500, y=500,
            width=140, height=110, z_index=22,
            fill_color="#FF9C27",
        )))
        return scene

    def test_qimage_premultiplied_converts_to_straight_bgra(self):
        image = QImage(2, 2, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(201, 101, 41, 128))
        expected = image.convertToFormat(QImage.Format.Format_ARGB32)
        rgba = expected.pixelColor(0, 0)
        data = straight_bgra_bytes(image)
        self.assertEqual(len(data), 16)
        self.assertEqual(data[:4], bytes([
            rgba.blue(), rgba.green(), rgba.red(), rgba.alpha(),
        ]))
        self.assertGreater(data[2], 195)
        self.assertEqual(data[3], 128)

    def test_converter_sha_matches_exact_straight_pixels_and_deferred_resolution(self):
        for image_format in (
            QImage.Format.Format_RGB32,
            QImage.Format.Format_ARGB32,
            QImage.Format.Format_ARGB32_Premultiplied,
        ):
            with self.subTest(image_format=image_format):
                image = QImage(24, 12, image_format)
                image.fill(QColor(204, 82, 33, 127))
                expected = straight_bgra_bytes(image)
                pixels, digest = straight_bgra_bytes_with_sha256(image)
                self.assertEqual(pixels, expected)
                self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
                future = Future()
                future.set_result((pixels, digest))
                resolved = DeferredNativeCanvasLayer(
                    "base", 24, 12, future,
                ).resolve()
                self.assertEqual(resolved.pixels, expected)
                self.assertEqual(resolved.precomputed_sha256, digest)
                self.assertIsInstance(resolved.pixels, bytes)
                with self.assertRaisesRegex(ValueError, "Precomputed SHA256"):
                    NativeCanvasBgraLayer(
                        "base", 24, 12, bytearray(pixels),
                        precomputed_sha256=digest,
                    )

    def test_real_canvas_source_items_captured_as_disjoint_alpha_layers(self):
        scene = self.build_scene()
        try:
            frame = capture_native_canvas_frame(
                scene, [(None, 9), (10, None)], output_scale=0.25,
            )
            self.assertEqual((frame.width, frame.height), (320, 180))
            self.assertEqual(len(frame.layers), 2)
            self.assertEqual([x.stream_key for x in frame.layers],
                             ["base", "layer:0"])
            self.assertEqual(len(frame.layers[0].pixels), 320 * 180 * 4)
            self.assertTrue(all(alpha == 255
                                for alpha in frame.layers[0].pixels[3::4]))
            alphas = frame.layers[1].pixels[3::4]
            self.assertIn(0, alphas)
            self.assertTrue(any(0 < a <= 255 for a in alphas))
            # Qt source visibility must be fully restored by snapshot.
            self.assertEqual(len([i for i in scene.items()
                                  if isinstance(i, SourceItem) and i.isVisible()]), 3)
        finally:
            scene.clear()

    def test_track_reactive_signals_reach_the_real_canvas_painter(self):
        scene = self.build_scene()
        track = PlaylistTrack("", "Track", duration_seconds=1.0)
        try:
            with patch.object(CanvasSnapshot, "capture_track",
                              wraps=CanvasSnapshot.capture_track) as capture:
                frame = capture_native_canvas_track_frame(
                    scene, track, [(None, None)], elapsed_seconds=0.2,
                    output_scale=0.25, bass_level=0.82,
                    music_levels={"example-lyrics-id": 0.43},
                )
            self.assertEqual((frame.width, frame.height), (320, 180))
            self.assertEqual(capture.call_count, 1)
            self.assertAlmostEqual(capture.call_args.kwargs["bass_level"], 0.82)
            self.assertEqual(capture.call_args.kwargs["music_levels"],
                             {"example-lyrics-id": 0.43})
        finally:
            scene.clear()

    def test_manifest_is_verifiable_and_never_overwrites(self):
        scene = self.build_scene()
        try:
            frame = capture_native_canvas_frame(
                scene, [(None, 9), (10, None)], output_scale=0.25,
            )
            with TemporaryDirectory() as td:
                out = Path(td)
                manifest = save_native_canvas_frame(frame, out, stem="test-layer")
                parsed = json.loads(manifest.read_text(encoding="utf8"))
                self.assertEqual(parsed["schema"], 1)
                self.assertEqual(parsed["format"], "mms_canvas_bgra_layers_v1")
                self.assertEqual(parsed["pixel_format"], "BGRA8_STRAIGHT")
                self.assertTrue(parsed["qt_canvas_rasterized_on_cpu"])
                self.assertEqual(len(parsed["layers"]), 2)
                for layer in parsed["layers"]:
                    file = out / layer["file"]
                    self.assertTrue(file.is_file())
                    content = file.read_bytes()
                    self.assertEqual(len(content), 320 * 180 * 4)
                    self.assertEqual(hashlib.sha256(content).hexdigest(), layer["sha256"])
                with self.assertRaises(FileExistsError):
                    save_native_canvas_frame(frame, out, stem="test-layer")
                self.assertEqual(parsed, json.loads(manifest.read_text(encoding="utf8")))
        finally:
            scene.clear()

    def test_fail_closed_on_bad_bands_geometry_or_packet(self):
        scene = self.build_scene()
        try:
            for bands in (
                [],
                [(0, None)],
                [(None, None), (20, None)],
                [(None, 20), (20, None)],
                [(None, 30), (20, None)],
                [(None, 20), (30, 10)],
            ):
                with self.subTest(bands=bands):
                    with self.assertRaises(ValueError):
                        capture_native_canvas_frame(scene, bands, output_scale=0.25)
            with self.assertRaises(ValueError):
                capture_native_canvas_frame(scene, [(None, None)], output_scale=0.1)
            with self.assertRaises(ValueError):
                NativeCanvasBgraLayer("base", 4, 4, b"\x00" * 63)
            with self.assertRaises(ValueError):
                NativeCanvasFrame(3, 2, (
                    NativeCanvasBgraLayer("base", 3, 2, b"\x00" * 24),
                ))
        finally:
            scene.clear()


if __name__ == "__main__":
    unittest.main()
