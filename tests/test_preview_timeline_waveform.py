"""The preview seek ruler's audio envelopes are visual-only and bounded."""

from __future__ import annotations

import io
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication

from app.dialogs.export_preview_dialog import PlaylistTimeline, TIMELINE_SCALE
from app.models.playlist import PlaylistTrack
from app.preview.timeline_waveform import TimelineWaveformWorker, pcm_peak_bins


class TimelineWaveformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_pcm_peaks_include_partial_bucket_and_negative_full_scale(self) -> None:
        raw = np.array(
            [0, -32768, 120, 120, 0, -16384, 2, -3, -8192],
            dtype="<i2",
        ).tobytes()
        result = pcm_peak_bins(raw, samples_per_bin=4)
        np.testing.assert_allclose(result, [1.0, 0.5, 0.25])

    def test_pcm_peaks_handle_empty_and_reject_invalid_bin_size(self) -> None:
        self.assertEqual(pcm_peak_bins(b"").size, 0)
        with self.assertRaises(ValueError):
            pcm_peak_bins(b"\\x00\\x00", samples_per_bin=0)

    def test_worker_decodes_to_ten_bars_per_second_without_ffmpeg_process(self) -> None:
        samples = np.concatenate((
            np.zeros(400, dtype="<i2"),
            np.full(400, -16384, dtype="<i2"),
            np.full(120, 8192, dtype="<i2"),
        ))
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(samples.tobytes())
            def poll(self) -> int:
                return 0
            def wait(self, timeout=None) -> int:
                return 0
        worker = TimelineWaveformWorker(Path("ffmpeg"), [(0, "example.mp3")])
        with patch("app.preview.timeline_waveform.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = worker._decode("example.mp3")
        self.assertIsNotNone(result)
        np.testing.assert_allclose(result, [0.0, 0.5, 0.25])
        self.assertIn("-ac", popen.call_args.args[0])

    def test_ruler_renders_in_both_modes_without_changing_seek(self) -> None:
        track = PlaylistTrack("missing-audio.mp3", "Track One", duration_seconds=10.0)
        ruler = PlaylistTimeline(((0, track, 0.0, 10.0),))
        try:
            ruler.resize(640, 55)
            ruler.setRange(0, 10 * TIMELINE_SCALE)
            ruler.set_waveform(0, np.linspace(0.0, 1.0, 100, dtype=np.float32))
            self.assertEqual(len(ruler._waveform_columns(0, 200)), 100)
            self.assertEqual(ruler._waveform_columns(0, 200), ruler._waveform_columns(0, 200))
            ruler.setValue(200)
            before = ruler.value()
            ruler.show()
            self.assertFalse(ruler.grab().isNull())
            ruler.set_current_track_mode(True)
            self.assertFalse(ruler.grab().isNull())
            self.assertEqual(ruler.value(), before)
            ruler.set_waveform(0, np.zeros(8, dtype=np.float32))
            self.assertEqual(len(ruler._waveform_columns(0, 200)), 8)
        finally:
            ruler.close()


if __name__ == "__main__":
    unittest.main()
