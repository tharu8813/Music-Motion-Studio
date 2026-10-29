"""Short opening cues must finish the temporary row before the next scroll."""

from copy import deepcopy
import unittest

from app.models.playlist import PlaylistTrack
from app.preview.frame_state import resolve_lyrics_intro_state
from tests import test_lyrics_intro as intro_tests


class LyricsShortIntroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        intro_tests.LyricsIntroTests.setUpClass()
        cls.fixture = intro_tests.LyricsIntroTests()

    def track(self, first_end=13.041, next_start=13.041):
        return PlaylistTrack("", "Short opening", duration_seconds=25, lyrics=[
            {"start": 12.46, "end": first_end, "text": "Short", "format": "ttml",
             "words": [{"start": 12.46, "end": first_end, "text": "Short", "offset": 0}]},
            {"start": next_start, "end": 15, "text": "Next phrase", "format": "ttml",
             "words": [{"start": next_start, "end": 15, "text": "Next phrase", "offset": 0}]},
            {"start": 16, "end": 18, "text": "Future"},
        ])

    def source(self, **options):
        return self.fixture.source(subtitle_animation_duration=.81,
                                   subtitle_karaoke_enabled=True, **options)

    def test_exit_finishes_before_the_next_cue_including_clock_tolerance(self):
        track, source = self.track(), self.source(subtitle_motion_easing="linear")
        for offset in (0, 1.25, -1.25):
            track.lyrics_timing_offset_seconds = offset / 2
            source.subtitle_timing_offset = offset / 2
            state = resolve_lyrics_intro_state(track, source, 12.75 - offset)
            self.assertTrue(state.exiting)
            self.assertAlmostEqual(state.raw_progress, .29 / .5805)
            for clock in (13.0405, 13.041, 13.1, 13.27):
                with self.subTest(offset=offset, clock=clock):
                    self.assertIsNone(resolve_lyrics_intro_state(track, source, clock - offset))
        self.assertEqual(source.subtitle_animation_duration, .81)

    def test_short_ttml_end_also_limits_exit_when_a_gap_follows(self):
        track, source = self.track(first_end=12.8), self.source()
        self.assertIsNotNone(resolve_lyrics_intro_state(track, source, 12.7))
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 12.8))

    def test_waiting_indicator_keeps_the_configured_entry_duration(self):
        source, track = self.source(), self.track()
        state = resolve_lyrics_intro_state(track, source, .2025)
        self.assertFalse(state.exiting)
        self.assertAlmostEqual(state.raw_progress, .25)

    def test_next_scroll_is_continuous_in_every_direction_and_easing(self):
        track = self.track()
        for direction in ("up", "down", "left", "right"):
            for effect in ("glow", "cascade"):
                for easing in ("linear", "smooth", "ease_in", "ease_out"):
                    source = self.source(subtitle_flow_direction=direction,
                        subtitle_animation=effect, subtitle_motion_easing=easing,
                        subtitle_role_styles={role: {"blur": 0, "opacity": 1, "scale": 1}
                                              for role in ("previous", "current", "next")})
                    axis = int(direction in {"up", "down"})
                    before = self.fixture.capture(source, 13.04, track=track, axis=axis)[0]
                    start, marker, _ = self.fixture.capture(source, 13.041, track=track, axis=axis)
                    with self.subTest(direction=direction, effect=effect, easing=easing):
                        self.assertFalse(marker)
                        for text in before:
                            self.assertAlmostEqual(before[text], start[text], delta=1)
                        middle = self.fixture.capture(source, 13.446, track=track, axis=axis)[0]
                        sign = 1 if direction in {"up", "left"} else -1
                        self.assertGreater(sign * (start["Next phrase"] - middle["Next phrase"]), 0)

    def test_completed_intro_matches_ordinary_scroll_without_changing_settings(self):
        track = self.track()
        for direction in ("up", "down", "left", "right"):
            source = self.source(subtitle_flow_direction=direction,
                                 subtitle_animation="cascade", subtitle_motion_easing="linear")
            plain = deepcopy(source)
            plain.subtitle_intro_enabled = False
            before = deepcopy(source.to_dict())
            for clock in (13.0405, 13.041, 13.15, 13.269, 13.271, 13.5, 13.851):
                actual = self.fixture.capture(source, clock, track=track)[2]
                expected = self.fixture.capture(plain, clock, track=track)[2]
                with self.subTest(direction=direction, clock=clock):
                    self.assertEqual(actual, expected)
            self.assertEqual(before, source.to_dict())

    def test_short_intro_matches_export_and_reverse_seeking_at_both_scales(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        from app.preview.export_canvas_capture import ExportCanvasCapturer
        from app.renderer.export_timeline import ExportFrameSample
        from app.renderer.ffmpeg_renderer import RenderFrame

        track = self.track()
        source = self.source(subtitle_animation="cascade", subtitle_motion_easing="ease_out")
        original = deepcopy(source.to_dict())
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        scene.addItem(SourceItem(source))
        for scale in (1, 2):
            capturer = ExportCanvasCapturer(scene, [track], 25, set(), [(None, None)],
                lambda image, duration, _key: RenderFrame(image, duration), lambda: None,
                lambda *_: None, output_scale=scale)
            frames = {}
            for clock in (12.7, 13.04, 13.0405, 13.041, 13.15, 13.5, 13.15, 12.7):
                actual = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                    elapsed_seconds=clock, output_scale=scale, playlist_tracks=[track],
                    playlist_duration_seconds=25, timeline_seconds=clock)
                exported = capturer.capture(ExportFrameSample(track, 1, 0, .1, clock, clock)).image
                with self.subTest(scale=scale, clock=clock):
                    self.assertEqual(actual, exported)
                    if clock in frames:
                        self.assertEqual(actual, frames[clock])
                frames[clock] = actual
        self.assertEqual(original, source.to_dict())


if __name__ == "__main__":
    unittest.main()
