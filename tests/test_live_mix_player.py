"""Transport checks without sound hardware; rhythm tests use the real planner."""

from dataclasses import replace
from math import sqrt
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QMediaPlayer

from app.automix.planner import compile_automix
from app.automix.settings import AutoMixTransitionSettings
from app.models.playlist import PlaylistTrack
from app.preview.live_mix_player import LiveMixPlayer
from app.timeline.crossfade import compile_crossfade
from app.timeline.render_plan import validate_compiled_render_plan
from tests.test_automix_candidates import _analysis


class LiveMixTests(unittest.TestCase):
    def setUp(self):
        self.tracks = [PlaylistTrack("a.wav", "A", duration_seconds=10, id="a"),
                       PlaylistTrack("b.wav", "B", duration_seconds=10, id="b")]
        self.plan = compile_crossfade(self.tracks, 3)
        self.players = [MagicMock(), MagicMock()]
        for player in self.players:
            player.source.return_value = QUrl()
            player.mediaStatus.return_value = QMediaPlayer.MediaStatus.LoadedMedia
            player.isSeekable.return_value = True
            player.playbackState.return_value = QMediaPlayer.PlaybackState.PausedState
            player.position.return_value = 0
        self.outputs = [MagicMock(), MagicMock()]
        with patch("app.preview.live_mix_player.QMediaPlayer", side_effect=self.players) as factory, \
             patch("app.preview.live_mix_player.QAudioOutput", side_effect=self.outputs):
            factory.MediaStatus = QMediaPlayer.MediaStatus
            factory.PlaybackState = QMediaPlayer.PlaybackState
            self.engine = LiveMixPlayer(self.tracks)

    def test_preloads_then_plays_both_at_equal_power_midpoint(self):
        self.engine.sync(self.plan, 5, True, 0.8)
        self.players[1].setSource.assert_called_once()
        self.players[1].play.assert_not_called()
        self.engine.sync(self.plan, 8.5, True, 0.8)
        for player, output in zip(self.players, self.outputs):
            player.play.assert_called()
            self.assertAlmostEqual(output.setVolume.call_args.args[0], 0.8 / sqrt(2))
        self.players[1].setPosition.assert_called_with(1500)
        # A normal clock tick does not repeatedly seek already synchronized decks.
        self.players[0].position.return_value = 8550
        self.players[1].position.return_value = 1550
        for player in self.players:
            player.setPosition.reset_mock()
        self.engine.sync(self.plan, 8.55, True, 0.8)
        for player in self.players:
            player.setPosition.assert_not_called()

    def test_late_readiness_uses_latest_seek_and_respects_pause(self):
        self.players[1].mediaStatus.return_value = QMediaPlayer.MediaStatus.LoadingMedia
        self.engine.sync(self.plan, 8, True, 1, seek=True)
        self.players[1].setPosition.assert_not_called()
        self.outputs[0].setVolume.assert_called_with(1.0)
        self.engine.sync(self.plan, 9, False, 1, seek=True)
        self.players[1].mediaStatus.return_value = QMediaPlayer.MediaStatus.LoadedMedia
        self.engine._ready(1)
        self.players[1].setPosition.assert_called_with(2000)
        self.players[1].play.assert_not_called()

    def test_finished_deck_is_reused_and_seeking_back_restores_both(self):
        self.engine.sync(self.plan, 8.5, True, 1)
        self.engine.sync(self.plan, 12, True, 1)
        self.players[0].stop.assert_called()
        self.assertEqual(self.engine.clips[1].track_id, "b")
        self.engine.sync(self.plan, 8, True, 1, seek=True)
        self.assertEqual({c.track_id for c in self.engine.clips}, {"a", "b"})
        self.players[0].setPosition.assert_called_with(8000)
        self.players[1].setPosition.assert_called_with(1000)

    def test_gap_pause_stop_and_stale_ready_never_restart_audio(self):
        plan = compile_crossfade([self.tracks[0], replace(self.tracks[1], start_time_seconds=15)], 3)
        self.engine.sync(plan, 12, True, 1)
        for player in self.players:
            player.play.assert_not_called()
        self.engine.sync(plan, 16, True, 1, seek=True)
        self.engine.pause()
        self.engine._ready(0)
        self.players[0].pause.assert_called()
        self.engine.stop()
        for player in self.players:
            player.play.reset_mock()
        self.engine._ready(0)
        self.engine._ready(1)
        for player in self.players:
            player.play.assert_not_called()
            player.setSource.assert_called_with(QUrl())

    def test_read_error_mutes_only_failed_deck(self):
        self.engine.sync(self.plan, 8.5, True, 1)
        self.players[0].stop.reset_mock()
        messages = []
        self.engine.failed.connect(messages.append)
        self.engine._failed(1, "missing file")
        self.assertEqual(messages, ["missing file"])
        self.outputs[1].setVolume.assert_called_with(0.0)
        self.players[0].stop.assert_not_called()

    def test_real_beat_plan_plays_trimmed_sources_at_planned_tempo(self):
        tracks = [replace(t, duration_seconds=180) for t in self.tracks]
        plan = compile_automix(tracks, {"a": _analysis("a", 120, 180),
                                        "b": _analysis("b", 124, 180)},
                               AutoMixTransitionSettings(enabled=True))
        validate_compiled_render_plan(plan)
        self.assertTrue(plan.audio.transitions)
        outgoing, incoming = plan.audio.clips
        self.assertIsNotNone(outgoing.tempo_ramp)
        at = plan.audio.transitions[0].timeline_start + 1
        self.engine.sync(plan, at, True, 1, seek=True)
        for index, clip in enumerate(self.engine.clips):
            self.players[index].setPosition.assert_called_with(round(clip.source_at(at) * 1000))
        index = self.engine.clips.index(outgoing)
        self.assertAlmostEqual(self.players[index].setPlaybackRate.call_args.args[0], 124 / 120)
        self.assertEqual(incoming.playback_rate, 1)


class CrossfadePlanTests(unittest.TestCase):
    def test_short_tracks_no_triple_overlap_and_explicit_gaps_survive(self):
        tracks = [PlaylistTrack(f"{i}.wav", str(i), duration_seconds=2, id=str(i)) for i in range(4)]
        plan = compile_crossfade(tracks, 30)
        validate_compiled_render_plan(plan)
        self.assertEqual([c.timeline_start for c in plan.audio.clips], [0, 1, 2, 3])
        self.assertEqual(plan.duration_seconds, 5)
        self.assertTrue(all(t.duration == 1 for t in plan.audio.transitions))
        tracks[2].start_time_seconds = 9
        plan = compile_crossfade(tracks, 30)
        self.assertEqual(plan.audio.clips[2].timeline_start - plan.audio.clips[1].timeline_end, 5)

    def test_disabled_tracks_zero_overlap_and_invalid_durations(self):
        tracks = [PlaylistTrack("a", "A", duration_seconds=10),
                  PlaylistTrack("b", "B", duration_seconds=30, enabled=False),
                  PlaylistTrack("c", "C", duration_seconds=10)]
        self.assertEqual(compile_crossfade(tracks, 3).duration_seconds, 17)
        self.assertEqual(compile_crossfade(tracks, 0).duration_seconds, 20)
        self.assertEqual(compile_crossfade([], 3).duration_seconds, 0)
        for seconds in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                compile_crossfade(tracks, seconds)


if __name__ == "__main__":
    unittest.main()
