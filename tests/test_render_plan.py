from __future__ import annotations

import unittest

from app.models.playlist import PlaylistTrack
from app.timeline.compiler import compile_playlist, compile_timeline
from app.timeline.models import AudioClip, AudioTrack, AudioTransition, Timeline, TransitionType
from app.timeline.render_plan import (
    CompiledRenderPlan, PresentationPlan, PresentationWindow, VisualSegment, visual_segment_at,
)
from app.timeline.track_schedule import playlist_duration, resolve_track_windows


def _track(duration: float, start: float | None = None, track_id: str | None = None,
           enabled: bool = True) -> PlaylistTrack:
    return PlaylistTrack(
        file_path="x.mp3", title="x", duration_seconds=duration, start_time_seconds=start,
        id=track_id, enabled=enabled,
    )


class RenderPlanCompilerTests(unittest.TestCase):
    def test_large_plan_lookup_does_not_scan_every_window(self) -> None:
        class IndexedOnly(tuple):
            def __iter__(self):
                raise AssertionError("A frame lookup must not scan the whole playlist")

        windows = IndexedOnly(PresentationWindow(str(i), i * 10, i * 10 + 5)
                              for i in range(10000))
        segments = IndexedOnly(VisualSegment(i, str(i), i * 10, i * 10 + 5)
                               for i in range(10000))
        plan = PresentationPlan(windows)
        for seconds, expected in [(-1, 0), (0, 0), (50000, 5000), (50007, 5000), (100001, 9999)]:
            with self.subTest(seconds=seconds):
                self.assertIs(plan._window_at(seconds), windows[expected])
                self.assertIs(visual_segment_at(segments, seconds), segments[expected])
        self.assertIsNone(PresentationPlan()._window_at(0))
        self.assertIsNone(visual_segment_at((), 0))

    def test_sequential_timeline_matches_legacy_windows(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(20.0, track_id="b"), _track(30.0, track_id="c")]
        plan = compile_playlist(tracks)
        self.assertEqual(
            [(c.track_id, c.timeline_start, c.timeline_end) for c in plan.audio.clips],
            [("a", 0.0, 10.0), ("b", 10.0, 30.0), ("c", 30.0, 60.0)],
        )

    def test_gap_is_preserved(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(20.0, start=15.0, track_id="b")]
        plan = compile_playlist(tracks)
        self.assertEqual(
            [(c.track_id, c.timeline_start, c.timeline_end) for c in plan.audio.clips],
            [("a", 0.0, 10.0), ("b", 15.0, 35.0)],
        )

    def test_disabled_track_excluded_when_enabled_only(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(5.0, track_id="b", enabled=False),
                   _track(20.0, track_id="c")]
        plan = compile_playlist(tracks, enabled_only=True)
        self.assertEqual([c.track_id for c in plan.audio.clips], ["a", "c"])
        self.assertEqual(plan.duration_seconds, 30.0)

    def test_empty_timeline_duration_is_zero(self) -> None:
        plan = compile_playlist([])
        self.assertEqual(plan.duration_seconds, 0.0)
        self.assertEqual(plan.audio.clips, ())
        self.assertEqual(plan.presentation.windows, ())
        self.assertEqual(plan.metadata.chapters, ())

    def test_presentation_lookup_and_boundaries(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(20.0, track_id="b")]
        plan = compile_playlist(tracks)
        self.assertEqual(plan.presentation.track_at(5.0), "a")
        self.assertEqual(plan.presentation.local_time(5.0), 5.0)
        self.assertEqual(plan.presentation.track_at(10.0), "b")
        self.assertEqual(plan.presentation.local_time(10.0), 0.0)
        self.assertEqual(plan.presentation.track_at(15.0), "b")
        self.assertEqual(plan.presentation.local_time(15.0), 5.0)
        self.assertEqual(plan.presentation.track_at(1000.0), "b")
        self.assertIsNone(compile_playlist([]).presentation.track_at(0.0))

    def test_metadata_chapters_match_legacy_windows(self) -> None:
        # No gap in this playlist, so "chapter ends at the next track's start"
        # and "chapter ends at its own track's end" agree; a gap case is
        # covered separately below.
        tracks = [_track(10.0, track_id="a"), _track(20.0, track_id="b"),
                   _track(8.0, track_id="c")]
        plan = compile_playlist(tracks)
        windows = resolve_track_windows(tracks)
        self.assertEqual(
            [(ch.track_id, ch.start, ch.end) for ch in plan.metadata.chapters],
            [(w.track.id, w.start, w.end) for w in windows],
        )

    def test_metadata_chapter_bridges_gap_to_next_track_start(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(10.0, start=15.0, track_id="b")]
        plan = compile_playlist(tracks)
        self.assertEqual(
            [(ch.track_id, ch.start, ch.end) for ch in plan.metadata.chapters],
            [("a", 0.0, 15.0), ("b", 15.0, 25.0)],
        )

    def test_metadata_chapter_start_matches_presentation_and_youtube_source(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(10.0, start=15.0, track_id="b")]
        plan = compile_playlist(tracks)
        for chapter, window in zip(plan.metadata.chapters, plan.presentation.windows):
            self.assertEqual(chapter.track_id, window.track_id)
            self.assertEqual(chapter.start, window.timeline_start)

    def test_timeline_duration_matches_legacy_playlist_duration(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(5.0, start=20.0, track_id="b")]
        plan = compile_playlist(tracks)
        self.assertEqual(plan.duration_seconds, playlist_duration(tracks))

    def test_deterministic_and_no_mutation(self) -> None:
        tracks = [_track(10.0, track_id="a"), _track(20.0, track_id="b")]
        before = [t.to_dict() for t in tracks]
        plan_1 = compile_playlist(tracks)
        plan_2 = compile_playlist(tracks)
        self.assertEqual(plan_1, plan_2)
        self.assertEqual([t.to_dict() for t in tracks], before)

    def test_compile_timeline_does_not_mutate_input(self) -> None:
        tracks = [_track(10.0, track_id="a")]
        from app.timeline.models import timeline_from_playlist
        timeline = timeline_from_playlist(tracks)
        before = timeline
        compile_timeline(timeline)
        self.assertEqual(timeline, before)

    def test_returns_compiled_render_plan_type(self) -> None:
        self.assertIsInstance(compile_playlist([_track(1.0, track_id="a")]), CompiledRenderPlan)

    def test_presentation_gap_keeps_the_most_recently_ended_owner(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", 0.0, 0.0, 10.0),
            AudioClip("b", "b", 15.0, 0.0, 10.0),
            AudioClip("c", "c", 30.0, 0.0, 10.0),
        )),))
        presentation = compile_timeline(timeline).presentation
        for seconds, expected_track in (
            (5.0, "a"), (12.0, "a"), (15.0, "b"), (27.0, "b"), (30.0, "c"), (50.0, "c"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(presentation.track_at(seconds), expected_track)
        # In the A/B gap, A's local time holds at its own source end (10.0)
        # instead of drifting forward with global time.
        self.assertEqual(presentation.local_time(12.0), 10.0)

    def test_presentation_local_time_reflects_source_offset(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", timeline_start=100.0, source_in=20.0, source_out=40.0),
        )),))
        presentation = compile_timeline(timeline).presentation
        self.assertEqual(presentation.local_time(100.0), 20.0)
        self.assertEqual(presentation.local_time(110.0), 30.0)
        self.assertEqual(presentation.local_time(120.0), 40.0)

    def test_presentation_local_time_reflects_playback_rate(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", timeline_start=100.0, source_in=20.0, source_out=40.0,
                      playback_rate=2.0),
        )),))
        presentation = compile_timeline(timeline).presentation
        self.assertEqual(presentation.local_time(100.0), 20.0)
        self.assertEqual(presentation.local_time(105.0), 30.0)
        self.assertEqual(presentation.local_time(110.0), 40.0)

    def test_timeline_transitions_are_compiled_into_audio_render_plan(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", 0.0, 0.0, 10.0),
            AudioClip("b", "b", 8.0, 0.0, 10.0),
        )),), (
            AudioTransition("a", "b", start=8.0, duration=2.0, type=TransitionType.EQUAL_POWER),
        ))
        plan = compile_timeline(timeline)
        self.assertEqual(len(plan.audio.transitions), 1)
        transition = plan.audio.transitions[0]
        self.assertEqual(transition.clip_a, "a")
        self.assertEqual(transition.clip_b, "b")
        self.assertEqual(transition.timeline_start, 8.0)
        self.assertEqual(transition.duration, 2.0)
        self.assertEqual(transition.type, TransitionType.EQUAL_POWER)

    def test_transition_referencing_a_disabled_clip_is_excluded(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", 0.0, 0.0, 10.0),
            AudioClip("b", "b", 8.0, 0.0, 10.0, enabled=False),
        )),), (
            AudioTransition("a", "b", start=8.0, duration=2.0),
        ))
        plan = compile_timeline(timeline)
        self.assertEqual(plan.audio.transitions, ())

    def test_disabled_clip_does_not_inflate_compiled_duration(self) -> None:
        timeline = Timeline((AudioTrack("lane", (
            AudioClip("a", "a", 0.0, 0.0, 10.0),
            AudioClip("b", "b", 10.0, 0.0, 90.0, enabled=False),
        )),))
        plan = compile_timeline(timeline)
        self.assertEqual([c.timeline_end for c in plan.audio.clips], [10.0])
        self.assertEqual(plan.duration_seconds, 10.0)


if __name__ == "__main__":
    unittest.main()
