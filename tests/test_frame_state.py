"""Phase 8 Frame Evaluation Layer: resolve_lyrics_cue_state() and
resolve_now_playing_exit_state() are the shared resolvers used by both
CanvasSnapshot.capture_track (preview/export painting) and
ExportCanvasCapturer._source_state_key (export frame coalescing). These
tests check they resolve the same input to the same FrameState
deterministically, and match the specific timing math both call sites
relied on before the extraction."""

import unittest

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.frame_state import (
    LyricsCueState, lyrics_display_groups, resolve_lyrics_cue_state,
    resolve_now_playing_exit_state, resolve_timeline_window_phase,
)


def _track(lyrics: list[dict[str, object]], offset: float = 0.0) -> PlaylistTrack:
    return PlaylistTrack(
        file_path="song.mp3", title="Song", duration_seconds=180.0,
        lyrics=lyrics, lyrics_timing_offset_seconds=offset,
    )


class FrameStateTests(unittest.TestCase):
    def test_same_input_produces_same_state(self) -> None:
        track = _track([
            {"start": 0.0, "text": "first"},
            {"start": 2.0, "text": "second"},
        ])
        first = resolve_lyrics_cue_state(track, 2.5, 0.0, 0.0, "glow", 0.4)
        second = resolve_lyrics_cue_state(track, 2.5, 0.0, 0.0, "glow", 0.4)
        self.assertEqual(first, second)

    def test_no_lyrics_resolves_to_no_cue(self) -> None:
        state = resolve_lyrics_cue_state(_track([]), 5.0, 0.0, 0.0, "glow", 0.4)
        self.assertIsNone(state.cue_index)
        self.assertIsNone(state.active_cue_index)
        self.assertFalse(state.transitioning)

    def test_transition_progresses_over_the_animation_duration(self) -> None:
        track = _track([{"start": 10.0, "text": "line"}])
        just_started = resolve_lyrics_cue_state(track, 10.0, 0.0, 0.0, "glow", 0.5)
        self.assertTrue(just_started.transitioning)
        self.assertAlmostEqual(just_started.transition_progress, 0.0)

        halfway = resolve_lyrics_cue_state(track, 10.25, 0.0, 0.0, "glow", 0.5)
        self.assertAlmostEqual(halfway.transition_progress, 0.5)

        # A single cue has no successor to hand off to, so `transitioning`
        # (== "this is the active cue") stays true; progress just clamps at 1.0.
        finished = resolve_lyrics_cue_state(track, 11.0, 0.0, 0.0, "glow", 0.5)
        self.assertTrue(finished.transitioning)
        self.assertEqual(finished.transition_progress, 1.0)

    def test_short_cascade_tail_keeps_scroll_and_release_clocks_independent(self) -> None:
        track = _track([
            {"start": 148.11, "end": 149.79, "text": "Repeat", "format": "ttml"},
            {"start": 149.79, "end": 151.16, "text": "Repeat", "format": "ttml"},
            {"start": 151.16, "end": 151.26, "text": "Final", "format": "ttml"},
        ])
        for offset in (0.0, 1.25, -1.25):
            def state(clock):
                return resolve_lyrics_cue_state(track, clock - offset, offset, 0,
                                               "cascade", .81, whole_lines=True)
            before = state(151.259)
            after = state(151.26)
            middle = state(151.565)
            self.assertTrue(before.transitioning)
            self.assertTrue(after.transitioning)
            self.assertIsNone(after.active_cue_index)
            self.assertEqual(after.active_cue_indices, ())
            self.assertEqual(after.cue_index, 2)
            self.assertAlmostEqual(after.transition_progress, .1 / .81)
            self.assertAlmostEqual(after.release_progress, 0)
            self.assertAlmostEqual(middle.transition_progress, .5)
            self.assertGreater(middle.release_progress, 0)
            self.assertFalse(state(152).transitioning)
            self.assertEqual(state(152).transition_progress, 1)
            self.assertEqual(state(151.565), middle)

    def test_short_cascade_completes_before_next_cue(self) -> None:
        track = _track([
            {"start": 0, "end": 2, "text": "First", "format": "ttml"},
            {"start": 2, "end": 2.1, "text": "Short", "format": "ttml"},
            {"start": 2.3, "end": 4, "text": "Next", "format": "ttml"},
        ])
        def state(clock):
            return resolve_lyrics_cue_state(track, clock, 0, 0, "cascade", .81,
                                           whole_lines=True)
        self.assertTrue(state(2.15).transitioning)
        self.assertIsNone(state(2.15).active_cue_index)
        self.assertGreater(state(2.2994).transition_progress, .99)
        self.assertEqual(state(2.3).cue_index, 2)
        self.assertAlmostEqual(state(2.3).transition_progress, 0)

    def test_animation_none_never_transitions(self) -> None:
        track = _track([{"start": 0.0, "text": "line"}])
        state = resolve_lyrics_cue_state(track, 0.0, 0.0, 0.0, "none", 0.5)
        self.assertFalse(state.transitioning)
        self.assertEqual(state.transition_progress, 1.0)

    def test_held_cue_releases_its_highlight_across_a_gap(self) -> None:
        track = _track([
            {"start": 1.0, "end": 4.0, "text": "first"},
            {"start": 10.0, "end": 12.0, "text": "second"},
            {"start": 12.0, "end": 14.0, "text": "third"},
        ])
        active = resolve_lyrics_cue_state(track, 3.0, 0.0, 0.0, "glow", 0.5)
        self.assertIsNone(active.release_progress)
        halfway = resolve_lyrics_cue_state(track, 4.25, 0.0, 0.0, "glow", 0.5)
        self.assertEqual(halfway.cue_index, 0)
        self.assertAlmostEqual(halfway.release_progress, 0.5)
        released = resolve_lyrics_cue_state(track, 8.0, 0.0, 0.0, "glow", 0.5)
        self.assertEqual(released.release_progress, 1.0)
        self.assertIsNone(
            resolve_lyrics_cue_state(track, 4.25, 0.0, 0.0, "none", 0.5).release_progress,
        )
        # Before the first cue nothing was highlighted, so nothing releases.
        self.assertIsNone(
            resolve_lyrics_cue_state(track, 0.5, 0.0, 0.0, "glow", 0.5).release_progress,
        )
        # After the gap the previous cue hands over no highlight; back-to-back
        # cues hand over all of it.
        after_gap = resolve_lyrics_cue_state(track, 10.1, 0.0, 0.0, "glow", 0.5)
        self.assertAlmostEqual(after_gap.previous_emphasis, 0.0)
        contiguous = resolve_lyrics_cue_state(track, 12.1, 0.0, 0.0, "glow", 0.5)
        self.assertAlmostEqual(contiguous.previous_emphasis, 1.0)

    def test_timing_offsets_shift_the_effective_elapsed_time(self) -> None:
        track = _track([{"start": 5.0, "text": "line"}], offset=2.0)
        # A +2s track offset plus a +1s per-source offset means elapsed=2.0
        # lines up with the cue at start=5.0 (2.0 + 2.0 + 1.0 = 5.0).
        state = resolve_lyrics_cue_state(track, 2.0, 2.0, 1.0, "glow", 0.5)
        self.assertEqual(state.cue_index, 0)
        self.assertTrue(state.transitioning)
        self.assertAlmostEqual(state.transition_progress, 0.0)


class LyricDisplayGroupTests(unittest.TestCase):
    def test_lazy_groups_match_previous_eager_mapping(self):
        from itertools import combinations

        cues = [{"start": float(i), "end": float(i + 1), "text": f"Line {i}",
                 "format": "ttml"} for i in range(8)]
        track = _track(cues)
        for last in range(len(cues)):
            for count in range(1, last + 2):
                for displayed in combinations(range(last + 1), count):
                    if displayed == (last,):
                        continue
                    previous = [(i,) for i in range(last + 1) if i not in displayed]
                    expected = previous + [displayed] + [(i,) for i in range(last + 1, len(cues))]
                    for held in (False, True):
                        state = LyricsCueState(
                            displayed[0], None if held else displayed[0], False, 1.0, None,
                            active_cue_indices=() if held else displayed,
                            held_cue_indices=displayed if held else (),
                            latest_started_cue_index=last,
                        )
                        groups, anchor = lyrics_display_groups(track, state)
                        self.assertEqual(anchor, len(previous))
                        self.assertEqual(len(groups), len(expected))
                        self.assertEqual([group["display_cue_indices"] for group in groups], expected)
                        self.assertEqual(groups[-1]["display_cue_indices"], expected[-1])
                        self.assertEqual([group["display_cue_indices"] for group in groups[::2]], expected[::2])
                        self.assertEqual(groups[anchor]["text"],
                                         "\n".join(f"Line {i}" for i in displayed))
                        for position, indices in enumerate(expected):
                            for index in indices:
                                self.assertEqual(groups.index_for_cue(index), position)
                        with self.assertRaises(IndexError):
                            _ = groups[len(groups)]

    def test_long_overlap_only_prepares_requested_rows(self):
        cues = [{"start": float(i), "end": float(i + 1), "text": str(i),
                 "format": "ttml"} for i in range(10000)]
        track = _track(cues)
        displayed = (2, 4999, 5000)
        state = LyricsCueState(
            2, 2, False, 1.0, None, active_cue_indices=displayed,
            latest_started_cue_index=5000,
        )
        groups, anchor = lyrics_display_groups(track, state)
        self.assertEqual(len(groups), 9998)
        self.assertEqual(len(groups.prepared), 0)
        self.assertEqual(len(groups._removed_ranks), len(displayed))
        visible = groups[anchor - 2:anchor + 3]
        self.assertEqual(len(visible), 5)
        self.assertEqual(visible[2]["display_cue_indices"], displayed)
        self.assertEqual(len(groups.prepared), 5)
        self.assertEqual(groups.index_for_cue(2), anchor)
        self.assertEqual(groups.index_for_cue(9999), len(groups) - 1)


class NowPlayingExitStateTests(unittest.TestCase):
    def test_same_input_produces_same_state(self) -> None:
        first = resolve_now_playing_exit_state(3.0, 5.0, 1.0)
        second = resolve_now_playing_exit_state(3.0, 5.0, 1.0)
        self.assertEqual(first, second)

    def test_hidden_after_duration_elapses(self) -> None:
        state = resolve_now_playing_exit_state(6.0, 5.0, 1.0)
        self.assertFalse(state.visible)
        self.assertIsNone(state.exit_progress)

    def test_visible_with_no_exit_progress_before_exit_window(self) -> None:
        state = resolve_now_playing_exit_state(2.0, 5.0, 1.0)
        self.assertTrue(state.visible)
        self.assertIsNone(state.exit_progress)

    def test_exit_progress_ramps_within_the_exit_window(self) -> None:
        # duration=5, exit_duration=1 -> exit window is [4.0, 5.0].
        just_started = resolve_now_playing_exit_state(4.0, 5.0, 1.0)
        self.assertEqual(just_started.exit_progress, 0.0)

        halfway = resolve_now_playing_exit_state(4.5, 5.0, 1.0)
        self.assertAlmostEqual(halfway.exit_progress, 0.5)

        finished = resolve_now_playing_exit_state(5.0, 5.0, 1.0)
        self.assertEqual(finished.exit_progress, 1.0)

    def test_exit_duration_clamped_to_total_duration(self) -> None:
        # exit_duration longer than the card's whole lifetime clamps to it,
        # so the card starts exiting immediately from elapsed=0.
        state = resolve_now_playing_exit_state(0.0, 3.0, 10.0)
        self.assertTrue(state.visible)
        self.assertEqual(state.exit_progress, 0.0)

    def test_zero_exit_duration_never_exits(self) -> None:
        state = resolve_now_playing_exit_state(5.0, 5.0, 0.0)
        self.assertTrue(state.visible)
        self.assertIsNone(state.exit_progress)


class TimelineWindowPhaseTests(unittest.TestCase):
    def test_no_window_never_phases(self) -> None:
        source = Source(SourceType.SHAPE, "Static", animation_in="slide_left")
        phase, progress = resolve_timeline_window_phase(source, 5.0)
        self.assertIsNone(phase)
        self.assertEqual(progress, 1.0)

    def test_window_with_no_animation_style_never_phases(self) -> None:
        source = Source(SourceType.SHAPE, "Windowed", timeline_start=1.0)
        phase, progress = resolve_timeline_window_phase(source, 1.0)
        self.assertIsNone(phase)
        self.assertEqual(progress, 1.0)

    def test_entrance_progresses_across_the_window_start(self) -> None:
        source = Source(
            SourceType.SHAPE, "Windowed", timeline_start=1.0,
            animation_in="slide_left", animation_in_duration=1.0,
        )
        just_started = resolve_timeline_window_phase(source, 1.0)
        self.assertEqual(just_started, ("in", 0.0))
        halfway = resolve_timeline_window_phase(source, 1.5)
        self.assertEqual(halfway, ("in", 0.5))
        after = resolve_timeline_window_phase(source, 2.0)
        self.assertEqual(after, (None, 1.0))

    def test_exit_progresses_across_the_window_end(self) -> None:
        source = Source(
            SourceType.SHAPE, "Windowed", timeline_start=1.0,
            timeline_duration=4.0,
            animation_out="slide_right", animation_out_duration=1.0,
        )
        # Window spans [1.0, 5.0); exit ramps across the final second.
        just_started = resolve_timeline_window_phase(source, 4.0)
        self.assertEqual(just_started, ("out", 0.0))
        halfway = resolve_timeline_window_phase(source, 4.5)
        self.assertEqual(halfway, ("out", 0.5))


if __name__ == "__main__":
    unittest.main()
