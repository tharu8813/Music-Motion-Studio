"""A/B parity and fallback coverage for the optional Rust export FFT reducer."""

from __future__ import annotations

import importlib.util
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.renderer.native_export import ENABLE_ENV, reduce_frequency_bands
from app.renderer.python_visualizer import PythonVisualizerRenderer


class RustExportAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = PythonVisualizerRenderer(Path("ffmpeg"))
        self.samples = np.random.default_rng(20261109).normal(
            0.0, 0.15, size=8_000 * 3,
        ).astype(np.float32)

    def test_disabled_returns_python_fallback(self) -> None:
        spectrum = np.ones((1, 257), dtype=np.float64)
        with patch.dict(os.environ, {ENABLE_ENV: "0"}):
            self.assertIsNone(reduce_frequency_bands(
                spectrum,
                np.arange(257, dtype=np.float64),
                (np.array([3, 4]),),
                np.array([[1, 2, 3, 4, 5]], dtype=np.float32),
            ))

    def test_missing_extension_falls_back_without_crashing(self) -> None:
        with patch.dict(os.environ, {ENABLE_ENV: "1"}), patch(
            "app.renderer.native_export.importlib.import_module",
            side_effect=ImportError("module unavailable"),
        ):
            self.assertIsNone(reduce_frequency_bands(
                np.ones((1, 257)), np.arange(257, dtype=np.float64),
                (np.array([1]),), np.ones((1, 5), dtype=np.float32),
            ))

    def test_native_and_numpy_analyses_match(self) -> None:
        if importlib.util.find_spec("music_motion_native") is None:
            self.skipTest("Rust extension not built (optional in Python-only development)")
        # Fail if the native path silently fell back. A/B parity alone could
        # otherwise pass even when the extension is broken.
        test_spectrum = np.ones((2, 257), dtype=np.float64)
        test_frequencies = np.arange(257, dtype=np.float64)
        test_probes = np.array([[2.0, 2.5, 3.0, 3.5, 4.0]], dtype=np.float32)
        with patch.dict(os.environ, {ENABLE_ENV: "1"}):
            direct = reduce_frequency_bands(
                test_spectrum, test_frequencies,
                (np.array([2, 3, 4]),), test_probes,
            )
        self.assertIsNotNone(direct, "Rust reduction failed and fell back to NumPy")
        np.testing.assert_array_equal(direct, np.ones((2, 1), dtype=np.float32))
        for bands in (4, 32, 96):
            for fps in (12, 60):
                with self.subTest(bands=bands, fps=fps):
                    with patch.dict(os.environ, {ENABLE_ENV: "0"}):
                        reference = self.renderer._analyze_levels(
                            self.samples, fps, bands, threading.Event(),
                        )
                    with patch.dict(os.environ, {ENABLE_ENV: "1"}):
                        result = self.renderer._analyze_levels(
                            self.samples, fps, bands, threading.Event(),
                        )
                    self.assertEqual(result.shape, reference.shape)
                    self.assertEqual(result.dtype, np.dtype("float32"))
                    np.testing.assert_allclose(result, reference, rtol=0, atol=1e-6)

    def test_cancelled_analysis_still_raises(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with patch.dict(os.environ, {ENABLE_ENV: "1"}):
            with self.assertRaisesRegex(Exception, "cancelled"):
                self.renderer._analyze_levels(self.samples, 30, 32, cancelled)


if __name__ == "__main__":
    unittest.main()
