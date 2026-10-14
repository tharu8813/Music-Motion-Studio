"""Preview opens on a crossfade plan and hands over safely to a beat mix."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog, _LIVE_MIX_TRACK_INDEX
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Translator


class LiveMixPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def preview(self, mode="crossfade"):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=10),
                  PlaylistTrack("b.wav", "B", duration_seconds=10)]
        preview = ExportPreviewDialog(scene, tracks, Translator(), preferred_backend="cpu",
                                      transition_mode=mode, crossfade_seconds=3)
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        return preview

    def test_crossfade_and_automix_start_with_overlaps_without_waiting_for_ffmpeg(self):
        for mode in ("crossfade", "automix"):
            with self.subTest(mode=mode):
                preview = self.preview(mode)
                self.assertEqual(preview._compiled_plan.duration_seconds, 17)
                self.assertEqual(len(preview._compiled_plan.audio.transitions), 1)
                with patch.object(preview._live_mix, "sync") as sync:
                    preview._playing = True
                    preview.timeline.setValue(850)
                    self.assertEqual(preview._active_track_index, _LIVE_MIX_TRACK_INDEX)
                    self.assertEqual(sync.call_args.args[1], 8.5)
                    self.assertTrue(sync.call_args.kwargs["seek"])
                    preview.volume_slider.setValue(0)
                    self.assertEqual(sync.call_args.args[3], 0)
                with patch.object(preview._live_mix, "pause") as pause:
                    preview._start_scrubbing()
                    pause.assert_called_once()
                preview._stop_preview()

    def test_render_arriving_in_overlap_waits_until_pause(self):
        preview = self.preview()
        with patch.object(preview._live_mix, "sync"):
            preview._playing = True
            preview.timeline.setValue(850)
        preview._on_blended_audio_ready(str(Path("mix.flac").resolve()), preview._compiled_plan)
        self.assertIsNone(preview._blended_audio_path)
        self.assertIsNotNone(preview._pending_swap)
        with patch.object(preview._live_mix, "pause"):
            preview._toggle_playback(False)
        self.assertIsNotNone(preview._blended_audio_path)
        self.assertIsNone(preview._pending_swap)
        self.assertEqual(preview._playhead_seconds, 8.5)

    def test_no_transition_retains_sequential_playback(self):
        preview = self.preview("none")
        self.assertIsNone(preview._live_mix)
        self.assertEqual(preview._compiled_plan.duration_seconds, 20)


if __name__ == "__main__":
    unittest.main()
