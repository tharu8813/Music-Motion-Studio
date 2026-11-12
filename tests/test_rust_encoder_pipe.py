"""Opt-in Rust BGR24 FFmpeg pipe transport safety and byte-parity tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

import numpy as np
from PySide6.QtGui import QColor, QImage

import app.renderer.canvas_pipe as canvas_pipe_module
from app.renderer.canvas_pipe import PipedCanvasStream
from app.renderer.ffmpeg.filter_graph import is_raw_canvas_cfr_input, layered_filter_graph
from app.renderer.native_export import (
    RUST_BGR24_PIPE_ENV,
    native_bgr24_packer,
    pack_rgb32_for_bgr24,
)


class _FakePipe:
    def __init__(self):
        self.path = "fake-pipe"
        self.sent = bytearray()

    def connect(self, cancelled):
        pass

    def write(self, data, cancelled):
        self.sent.extend(data)

    def close(self):
        pass


class RustFramePipeTests(TestCase):
    def test_opt_in_switch_defaults_to_32_bit(self):
        with patch.dict(os.environ, {RUST_BGR24_PIPE_ENV: "0"}):
            self.assertIsNone(native_bgr24_packer())

    def test_missing_native_api_does_not_switch_pixel_format(self):
        with patch.dict(os.environ, {RUST_BGR24_PIPE_ENV: "1"}), patch(
            "app.renderer.native_export.importlib.import_module",
            side_effect=ImportError("native extension absent"),
        ):
            self.assertIsNone(native_bgr24_packer())
            pipe = _FakePipe()
            with patch.object(canvas_pipe_module, "_create_endpoint", return_value=pipe):
                stream = PipedCanvasStream(Path("."), "base", 30, preserve_alpha=False)
                image = QImage(7, 3, QImage.Format.Format_RGB32)
                image.fill(QColor(23, 45, 67))
                stream.submit(image, 1 / 30)
                args = stream.input_arguments()
                stream.finish()
            self.assertEqual(args[args.index("-pix_fmt") + 1], "bgr0")
            self.assertEqual(len(pipe.sent), 7 * 3 * 4)

    def test_fallback_packer_always_preserves_bgr24_bytes(self):
        raw = bytes((11, 22, 33, 255, 44, 55, 66, 255))
        def fail(*_args):
            raise ValueError("simulated native failure")
        actual = pack_rgb32_for_bgr24(memoryview(raw), 2, 1, fail)
        self.assertEqual(actual, bytes((11, 22, 33, 44, 55, 66)))

    def test_fallback_validates_input_stride(self):
        with self.assertRaisesRegex(ValueError, "stride"):
            pack_rgb32_for_bgr24(memoryview(bytes(8)), 3, 1, lambda *_: b"")

    def test_bgr24_filter_path_normalizes_before_overlay_conversion(self):
        original = layered_filter_graph(
            [], [], 30, 32, 32, base_is_cfr=True, base_bgr24=False,
        )
        packed = layered_filter_graph(
            [], [], 30, 32, 32, base_is_cfr=True, base_bgr24=True,
        )
        self.assertTrue(original.startswith("[0:v]settb=1/30,setpts=N"))
        self.assertTrue(packed.startswith("[0:v]format=bgr0,settb=1/30,setpts=N"))
        # The timebase and compositor keep identical semantics.
        self.assertEqual(packed.replace("[0:v]format=bgr0,", "[0:v]", 1), original)

    @skipUnless(importlib.util.find_spec("music_motion_native"), "Rust wheel missing")
    def test_zero_copy_input_native_packer_matches_reference_for_odd_widths(self):
        with patch.dict(os.environ, {RUST_BGR24_PIPE_ENV: "1"}):
            packer = native_bgr24_packer()
            self.assertIsNotNone(packer, "Native packing feature must really execute")
            for width, height in ((1, 1), (3, 2), (13, 7), (192, 45)):
                with self.subTest(width=width, height=height):
                    image = QImage(width, height, QImage.Format.Format_RGB32)
                    for y in range(height):
                        for x in range(width):
                            image.setPixelColor(
                                x, y,
                                QColor((17 * x + 7 * y) % 256,
                                       (23 * x + 13 * y) % 256,
                                       (3 * x + 31 * y) % 256),
                            )
                    source = memoryview(image.constBits()).cast("B")
                    expected = np.frombuffer(source, dtype=np.uint8).reshape(
                        height, width, 4,
                    )[:, :, :3].copy().tobytes()
                    self.assertEqual(packer(source, width, height), expected)
                    self.assertEqual(len(expected), width * height * 3)
            with self.assertRaises(ValueError):
                packer(memoryview(b"one pixel too short"), 4, 2)

    @skipUnless(importlib.util.find_spec("music_motion_native"), "Rust wheel missing")
    def test_native_pipe_outputs_expected_bytes_and_retains_alpha_pipe(self):
        with patch.dict(os.environ, {RUST_BGR24_PIPE_ENV: "1"}):
            opaque_endpoint = _FakePipe()
            image = QImage(13, 7, QImage.Format.Format_RGB32)
            image.fill(QColor(15, 140, 245))
            with patch.object(canvas_pipe_module, "_create_endpoint", return_value=opaque_endpoint):
                stream = PipedCanvasStream(Path("."), "base", 30, preserve_alpha=False)
                stream.submit(image, 1 / 30)
                args = stream.input_arguments()
                stream.finish()
            self.assertEqual(args[args.index("-pix_fmt") + 1], "bgr24")
            self.assertEqual(len(opaque_endpoint.sent), 13 * 7 * 3)
            self.assertEqual(opaque_endpoint.sent[:3], bytes((245, 140, 15)))
            self.assertTrue(is_raw_canvas_cfr_input(tuple(args), 30, 13, 7))

            transparent_endpoint = _FakePipe()
            alpha = QImage(7, 5, QImage.Format.Format_ARGB32_Premultiplied)
            alpha.fill(QColor(255, 0, 0, 128))
            with patch.object(canvas_pipe_module, "_create_endpoint", return_value=transparent_endpoint):
                stream = PipedCanvasStream(Path("."), "layer:0", 30, preserve_alpha=True)
                stream.submit(alpha, 1 / 30)
                args = stream.input_arguments()
                stream.finish()
            self.assertEqual(args[args.index("-pix_fmt") + 1], "rgba")
            self.assertEqual(len(transparent_endpoint.sent), 7 * 5 * 4)
            self.assertAlmostEqual(transparent_endpoint.sent[3], 128, delta=1)


if __name__ == "__main__":
    import unittest
    unittest.main()
