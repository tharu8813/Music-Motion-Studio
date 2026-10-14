"""Exercise actual Qt WAV decoding and both decks with muted outputs."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.models.playlist import PlaylistTrack
from app.preview.live_mix_player import LiveMixPlayer
from app.timeline.crossfade import compile_crossfade
from tests.test_automix_ffmpeg_integration import _write_tone_wav


class LiveMixMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_actual_wav_decks_load_seek_and_play_together_muted(self):
        with TemporaryDirectory(prefix="live-mix-media-") as directory:
            tracks = []
            for index, frequency in enumerate((440, 660)):
                path = Path(directory) / f"{index}.wav"
                _write_tone_wav(path, frequency, 3)
                tracks.append(PlaylistTrack(str(path), str(index), duration_seconds=3))
            plan = compile_crossfade(tracks, 1)
            engine = LiveMixPlayer(tracks)
            try:
                for output in engine.outputs:
                    output.setMuted(True)
                engine.sync(plan, 0, False, 0)
                for _ in range(150):
                    if all(player.isSeekable() for player in engine.players):
                        break
                    QTest.qWait(20)
                self.assertTrue(all(player.isSeekable() for player in engine.players))
                engine.sync(plan, 2.5, False, 0, seek=True)
                QTest.qWait(60)
                self.assertAlmostEqual(engine.players[0].position(), 2500, delta=80)
                self.assertAlmostEqual(engine.players[1].position(), 500, delta=80)
                engine.sync(plan, 2.5, True, 0)
                QTest.qWait(100)
                for player in engine.players:
                    self.assertEqual(player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
                engine.pause()
                for player in engine.players:
                    self.assertEqual(player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
            finally:
                engine.stop()
                engine.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
