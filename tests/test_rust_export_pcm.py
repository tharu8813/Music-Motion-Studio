"""Test optional native PCM analysis against the original export NumPy path."""

from __future__ import annotations

import importlib.util
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.renderer.native_export import ENABLE_ENV, analyze_pcm_windows
from app.renderer.python_visualizer import PythonVisualizerError, PythonVisualizerRenderer


class NativePcmAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = PythonVisualizerRenderer(Path("ffmpeg"))
        self.rng = np.random.default_rng(20261109)

    def test_opt_in_defaults_to_fallback(self) -> None:
        with patch.dict(os.environ, {ENABLE_ENV: "0"}):
            self.assertIsNone(analyze_pcm_windows(np.zeros(8, np.float32), 30, 8000))
            self.assertIsNone(analyze_pcm_windows(np.zeros(8, np.float32), 30, 8000, points=32))

    def test_missing_or_old_extension_falls_back(self) -> None:
        with patch.dict(os.environ, {ENABLE_ENV: "1"}), patch(
            "app.renderer.native_export.importlib.import_module",
            side_effect=ImportError("not installed"),
        ):
            self.assertIsNone(analyze_pcm_windows(np.zeros(8, np.float32), 30, 8000))
        with patch.dict(os.environ, {ENABLE_ENV: "1"}), patch(
            "app.renderer.native_export.importlib.import_module",
            return_value=object(),  # Older wheel lacks pcm_rms/pcm_waveform.
        ):
            self.assertIsNone(analyze_pcm_windows(np.zeros(8, np.float32), 30, 8000, points=12))

    def test_native_pcm_parity(self) -> None:
        if importlib.util.find_spec("music_motion_native") is None:
            self.skipTest("Optional native extension is not installed")
        # Verify native functions really ran; parity alone can mask a fallback.
        with patch.dict(os.environ, {ENABLE_ENV: "1"}):
            self.assertIsNotNone(analyze_pcm_windows(np.ones(400, np.float32), 30, 8000))
            self.assertIsNotNone(analyze_pcm_windows(np.ones(400, np.float32), 30, 8000, points=32))

        for sample_count in (0, 1, 2, 16_003, 32_000):
            samples = self.rng.normal(0, 0.15, size=sample_count).astype(np.float32)
            for fps in (12, 30, 59, 60):
                with self.subTest(sample_count=sample_count, fps=fps, operation="rms"):
                    with patch.dict(os.environ, {ENABLE_ENV: "0"}):
                        expected = self.renderer._analyze_rms(samples, fps, threading.Event())
                    with patch.dict(os.environ, {ENABLE_ENV: "1"}):
                        actual = self.renderer._analyze_rms(samples, fps, threading.Event())
                    self.assertEqual(actual.dtype, np.dtype("float32"))
                    self.assertEqual(actual.shape, expected.shape)
                    np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-6)

                for points in (1, 2, 32, 96):
                    with self.subTest(sample_count=sample_count, fps=fps, points=points):
                        with patch.dict(os.environ, {ENABLE_ENV: "0"}):
                            expected = self.renderer._analyze_waveform(
                                samples, fps, points, threading.Event(),
                            )
                        with patch.dict(os.environ, {ENABLE_ENV: "1"}):
                            actual = self.renderer._analyze_waveform(
                                samples, fps, points, threading.Event(),
                            )
                        self.assertEqual(actual.dtype, np.dtype("float32"))
                        self.assertEqual(actual.shape, expected.shape)
                        np.testing.assert_allclose(actual, expected, rtol=0, atol=2e-6)

    def test_cancel_still_raises_for_both_paths(self) -> None:
        cancel = threading.Event()
        cancel.set()
        samples = np.zeros(800, np.float32)
        with patch.dict(os.environ, {ENABLE_ENV: "1"}):
            with self.assertRaises(PythonVisualizerError):
                self.renderer._analyze_rms(samples, 30, cancel)
            with self.assertRaises(PythonVisualizerError):
                self.renderer._analyze_waveform(samples, 30, 32, cancel)


if __name__ == "__main__":
    unittest.main()
