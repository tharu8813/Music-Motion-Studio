from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

import numpy as np
import soundfile

from app.services.audio_decode import read_pcm
from app.services.eq_preview import EqPreviewAudio, PEAK_LIMIT


class EqPreviewTests(TestCase):
    def test_reuses_pcm_with_different_settings_and_limits_both_channels(self):
        rate = 16_000
        signal = np.sin(2 * np.pi * 400 * np.arange(rate * 2) / rate)
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            output = Path(directory) / "preview.flac"
            soundfile.write(source, np.column_stack((0.7 * signal, 0.35 * signal)), rate)
            preview = EqPreviewAudio()
            with patch("app.services.eq_preview.read_pcm", wraps=read_pcm) as decode:
                preview.render(source, output, "volume=-6.00dB", 0.0, 1.0, Event())
                quiet, _ = soundfile.read(output)
                preview.render(source, output, "volume=12.00dB", 0.1, 1.0, Event())
                loud, output_rate = soundfile.read(output)
            decode.assert_called_once()

        self.assertEqual(output_rate, rate)
        self.assertEqual(loud.shape, (rate, 2))
        self.assertAlmostEqual(np.max(np.abs(quiet[:, 0])), 0.7 * 10 ** (-6 / 20), places=4)
        self.assertGreater(np.max(np.abs(loud[:, 0])), 0.9)
        self.assertLessEqual(np.max(np.abs(loud)), PEAK_LIMIT + 1e-6)
        np.testing.assert_allclose(loud[:, 1], loud[:, 0] / 2, atol=5e-5)
