from __future__ import annotations

import unittest

from dataclasses import replace

from app.automix.candidates import TransitionStrategy, generate_candidates, select_best_candidate
from app.automix.compatibility import evaluate_compatibility
from app.automix.models import TrackAnalysis
from app.automix.planner import compile_automix
from app.automix.settings import AutoMixTransitionSettings
from app.automix.structure.models import TrackSection, TrackStructureAnalysis
from app.models.playlist import PlaylistTrack
from app.timeline.compiler import compile_playlist
from app.timeline.crossfade import compile_crossfade
from app.timeline.models import TransitionType
from app.timeline.render_plan import TransitionDsp, validate_compiled_render_plan


def _beats(bpm: float, duration: float) -> tuple[float, ...]:
    interval = 60.0 / bpm
    beats: list[float] = []
    t = 0.0
    while t < duration:
        beats.append(round(t, 6))
        t += interval
    return tuple(beats)


def _track(track_id: str, duration: float, start: float | None = None, enabled: bool = True) -> PlaylistTrack:
    return PlaylistTrack(
        file_path=f"{track_id}.mp3", title=track_id, duration_seconds=duration,
        start_time_seconds=start, id=track_id, enabled=enabled,
    )


def _analysis(
    track_id: str, bpm: float | None, duration: float, *,
    bpm_confidence: float = 0.9, meter_confidence: float = 0.8,
) -> TrackAnalysis:
    beats = _beats(bpm, duration) if bpm else ()
    downbeats = beats[0::4] if beats else ()
    return TrackAnalysis(
        track_id=track_id, source_path=f"{track_id}.mp3", duration_seconds=duration,
        bpm=bpm, bpm_confidence=bpm_confidence if bpm else 0.0,
        beats=beats, downbeats=downbeats,
        meter_numerator=4 if downbeats else None, meter_denominator=4 if downbeats else None,
        meter_confidence=meter_confidence if downbeats else 0.0,
    )


ENABLED = AutoMixTransitionSettings(enabled=True, preferred_bars=8, max_transition_seconds=30.0)


class CompileAutomixTwoTrackTests(unittest.TestCase):
    def test_compatible_bpm_overlaps_and_shortens_total_duration(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0),
            "b": _analysis("b", 120.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)

        clip_a, clip_b = plan.audio.clips
        self.assertEqual(clip_a.timeline_start, 0.0)
        self.assertEqual(clip_a.timeline_end, 60.0)
        self.assertLess(clip_b.timeline_start, clip_a.timeline_end)
        self.assertGreater(clip_b.timeline_start, 0.0)
        self.assertEqual(len(plan.audio.transitions), 1)
        self.assertLess(plan.duration_seconds, 120.0)
        self.assertEqual(plan.duration_seconds, clip_b.timeline_end)

    def test_no_analysis_at_all_matches_basic_crossfade(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        plan = compile_automix(tracks, {}, ENABLED)
        crossfade = compile_crossfade(tracks, ENABLED.fallback_crossfade_seconds)
        self.assertEqual(
            [(c.track_id, c.timeline_start, c.timeline_end) for c in plan.audio.clips],
            [(c.track_id, c.timeline_start, c.timeline_end) for c in crossfade.audio.clips],
        )
        self.assertEqual(plan.audio.transitions[0].type, TransitionType.EQUAL_POWER)
        self.assertEqual(plan.duration_seconds, crossfade.duration_seconds)

    def test_settings_disabled_never_overlaps(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        disabled = AutoMixTransitionSettings(enabled=False)
        plan = compile_automix(tracks, analyses, disabled)
        self.assertEqual(plan.duration_seconds, 120.0)
        self.assertEqual(plan.audio.transitions, ())

    def test_unsafe_tempo_gap_falls_back_without_overlap_or_rate(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 90.0, 60.0), "b": _analysis("b", 140.0, 60.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b = plan.audio.clips
        self.assertEqual(clip_a.playback_rate, 1.0)
        self.assertEqual(clip_b.playback_rate, 1.0)
        for transition in plan.audio.transitions:
            self.assertNotEqual(transition.type, TransitionType.BEAT_MATCH)

    def test_beat_match_eases_the_outgoing_track_onto_the_incoming_beat(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 124.0, 60.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b = plan.audio.clips
        (transition,) = plan.audio.transitions
        self.assertEqual(transition.type, TransitionType.BEAT_MATCH)
        # The incoming track plays at its own tempo; the outgoing one moves.
        self.assertEqual((clip_b.playback_rate, clip_b.tempo_ramp), (1.0, None))
        self.assertEqual(clip_a.playback_rate, 1.0)
        self.assertAlmostEqual(clip_a.tempo_ramp.end_rate, 124.0 / 120.0, places=6)
        cue = dict(transition.details)["outgoing_cue"]
        self.assertAlmostEqual(clip_a.tempo_ramp.source_end, cue)
        self.assertAlmostEqual(clip_a.timeline_at(cue), transition.timeline_start, places=9)
        # Every beat of both tracks lands together through the whole overlap.
        for k in range(int(transition.duration / (60.0 / 124.0))):
            outgoing_beat = clip_a.timeline_at(cue + k * 60.0 / 120.0)
            incoming_beat = clip_b.timeline_at(clip_b.source_in + k * 60.0 / 124.0)
            self.assertAlmostEqual(outgoing_beat, incoming_beat, delta=1e-4)

    def test_missing_analysis_on_one_track_keeps_a_basic_crossfade(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0)}  # b has no analysis
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b = plan.audio.clips
        self.assertEqual(clip_a.timeline_end - clip_b.timeline_start, 3.0)
        self.assertTrue(dict(plan.audio.transitions[0].details)["missing_analysis"])

    def test_missing_analysis_caps_fades_on_short_tracks_and_preserves_gaps(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 2.0), _track("c", 60.0), _track("d", 60.0, start=130.0)]
        plan = compile_automix(tracks, {}, ENABLED)
        validate_compiled_render_plan(plan)
        self.assertEqual([t.duration for t in plan.audio.transitions], [1.0, 1.0])
        self.assertEqual(plan.audio.clips[3].timeline_start - plan.audio.clips[2].timeline_end, 8.0)

    def test_disabled_track_is_skipped_cleanly(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 30.0, enabled=False), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 30.0),
            "c": _analysis("c", 120.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        self.assertEqual({c.track_id for c in plan.audio.clips}, {"a", "c"})

    def test_explicit_gap_is_preserved_not_overlapped(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0, start=75.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b = plan.audio.clips
        self.assertEqual(clip_a.timeline_end, 60.0)
        self.assertEqual(clip_b.timeline_start, 75.0)
        self.assertEqual(plan.audio.transitions, ())

    def test_determinism_same_input_produces_an_equal_plan(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 124.0, 60.0)}
        first = compile_automix(tracks, analyses, ENABLED)
        second = compile_automix(tracks, analyses, ENABLED)
        self.assertEqual(first, second)

    def test_presentation_switches_exactly_when_incoming_clip_starts(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b = plan.audio.clips
        # Just before B's clip starts, A still owns the Canvas; from that
        # instant on, B does -- even though A's audio is still playing.
        self.assertEqual(plan.presentation.track_at(clip_b.timeline_start - 0.01), "a")
        self.assertEqual(plan.presentation.track_at(clip_b.timeline_start), "b")
        self.assertEqual(plan.presentation.track_at(clip_a.timeline_end - 0.01), "b")

    def test_metadata_chapter_starts_equal_presentation_window_starts(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 124.0, 60.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        for chapter, window in zip(plan.metadata.chapters, plan.presentation.windows):
            self.assertEqual(chapter.track_id, window.track_id)
            self.assertEqual(chapter.start, window.timeline_start)

    def test_legacy_sequential_compiler_is_unaffected(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        before = compile_playlist(tracks)
        compile_automix(tracks, {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}, ENABLED)
        after = compile_playlist(tracks)
        self.assertEqual(before, after)


class CompileAutomixThreeTrackTests(unittest.TestCase):
    def test_three_track_accumulation_positions_correctly(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0),
            "c": _analysis("c", 120.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)
        clip_a, clip_b, clip_c = plan.audio.clips
        self.assertEqual(len(plan.audio.transitions), 2)
        # B's placement already reflects the A->B overlap; C's placement
        # must be computed against B's *actual* (overlap-shifted) end, not
        # against the original sequential 120.0 mark.
        self.assertLess(clip_b.timeline_start, clip_a.timeline_end)
        self.assertLess(clip_c.timeline_start, clip_b.timeline_end)
        self.assertGreater(clip_c.timeline_start, clip_b.timeline_start)
        self.assertEqual(plan.duration_seconds, clip_c.timeline_end)
        self.assertLess(plan.duration_seconds, 180.0)

    def test_missing_analysis_on_middle_track_only_that_pair_falls_back(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0),
            # "b" has no analysis: neither A->B nor B->C can beat-match,
            # but this must not disturb any other pair's own eligibility.
            "c": _analysis("c", 120.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b, clip_c = plan.audio.clips
        self.assertEqual(clip_a.timeline_end - clip_b.timeline_start, 3.0)
        self.assertEqual(clip_b.timeline_end - clip_c.timeline_start, 3.0)
        self.assertTrue(all(dict(t.details)["missing_analysis"] for t in plan.audio.transitions))

    def test_one_bad_pair_does_not_invalidate_a_good_neighboring_pair(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 90.0, 60.0),
            "b": _analysis("b", 140.0, 60.0),  # incompatible with both neighbors
            "c": _analysis("c", 140.0, 60.0),  # compatible with b
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        clip_a, clip_b, clip_c = plan.audio.clips
        self.assertEqual(len(plan.audio.transitions), 2)
        # The first pair can fade safely without beat matching or cutting an unmeasured tail.
        self.assertNotEqual(plan.audio.transitions[0].type, TransitionType.BEAT_MATCH)
        self.assertIs(plan.audio.transitions[0].dsp, TransitionDsp.SHORT_FADE)
        self.assertLessEqual(plan.audio.transitions[0].duration, 6.0)
        # b->c is fully compatible and still gets a real (larger) overlap.
        self.assertEqual(plan.audio.transitions[1].type, TransitionType.BEAT_MATCH)
        self.assertLess(clip_c.timeline_start, clip_b.timeline_end)


class TempoReturnsTests(unittest.TestCase):
    """Every track plays at its own tempo; only its last bars bend toward the next one.

    The old policy played the whole incoming track at the outgoing tempo and
    carried that into the next pair, so a slowed-down track never came back.
    """

    def test_each_track_starts_at_its_own_tempo_along_a_chain(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0),
            "b": _analysis("b", 124.0, 60.0),
            "c": _analysis("c", 128.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)
        clip_a, clip_b, clip_c = plan.audio.clips
        self.assertEqual([clip.playback_rate for clip in plan.audio.clips], [1.0, 1.0, 1.0])
        # Each ramp targets the NEXT track's own tempo, never a propagated one.
        self.assertAlmostEqual(clip_a.tempo_ramp.end_rate, 124.0 / 120.0, places=6)
        self.assertAlmostEqual(clip_b.tempo_ramp.end_rate, 128.0 / 124.0, places=6)
        self.assertIsNone(clip_c.tempo_ramp)
        # B is back at rate 1.0 before its own ramp starts.
        self.assertEqual(clip_b.rate_segments()[0][2], 1.0)

    def test_frame_quantized_beats_still_line_up_through_the_overlap(self) -> None:
        """The reported bug: Beat This reports beats on a 50 fps grid, whose
        median interval reads 128 BPM as 130.4 and 126 as 125. Matching on
        those numbers drifted the two tracks half a beat apart within bars."""
        def quantized(track_id: str, bpm: float) -> TrackAnalysis:
            beats = tuple(round(beat * 50) / 50 for beat in _beats(bpm, 120.0))
            skewed = 60.0 / sorted(b - a for a, b in zip(beats, beats[1:]))[len(beats) // 2]
            return replace(_analysis(track_id, bpm, 120.0), bpm=skewed, beats=beats, downbeats=beats[0::4])

        plan = compile_automix([_track("a", 120.0), _track("b", 120.0)],
                               {"a": quantized("a", 128.0), "b": quantized("b", 126.0)}, ENABLED)
        clip_a, clip_b = plan.audio.clips
        (transition,) = plan.audio.transitions
        cue = clip_a.source_at(transition.timeline_start)
        first = round(cue / (60.0 / 128.0))  # the true beat the cue sits on
        for k in range(int(transition.duration / (60.0 / 126.0)) - 1):
            outgoing = clip_a.timeline_at((first + k) * 60.0 / 128.0)
            incoming = clip_b.timeline_at(round(clip_b.source_in / (60.0 / 126.0)) * 60.0 / 126.0 + k * 60.0 / 126.0)
            self.assertAlmostEqual(outgoing, incoming, delta=0.005)  # 5 ms, the whole window

    def test_the_ramp_is_exactly_the_winning_candidates_rate(self) -> None:
        outgoing = _analysis("a", 120.0, 60.0)
        incoming = _analysis("b", 124.0, 60.0)
        compatibility = evaluate_compatibility(outgoing, incoming, ENABLED)
        best = select_best_candidate(generate_candidates(outgoing, incoming, compatibility, ENABLED))
        plan = compile_automix([_track("a", 60.0), _track("b", 60.0)], {"a": outgoing, "b": incoming}, ENABLED)
        self.assertAlmostEqual(plan.audio.clips[0].tempo_ramp.end_rate, best.outgoing_rate)
        self.assertEqual(best.incoming_rate, 1.0)


def _structure(
    track_id: str, duration: float, *,
    outro_start: float | None = None, intro_end: float | None = None,
) -> TrackStructureAnalysis:
    return TrackStructureAnalysis(
        track_id=track_id, source_path=f"{track_id}.wav", duration_seconds=duration,
        outro_start_seconds=outro_start, intro_end_seconds=intro_end,
        analyzer_id="sonara_structure", analyzer_version="1",
    )


class CompileAutomixStructureTests(unittest.TestCase):
    """Commit C: structures is optional and additive; missing/malformed/
    one-sided structure data must never break the existing fallback chain."""

    def test_structures_omitted_matches_the_pre_commit_c_result(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 124.0, 60.0)}
        without_param = compile_automix(tracks, analyses, ENABLED)
        with_empty = compile_automix(tracks, analyses, ENABLED, structures={})
        with_none = compile_automix(tracks, analyses, ENABLED, structures=None)
        self.assertEqual(without_param, with_empty)
        self.assertEqual(without_param, with_none)

    def test_early_structure_hint_cannot_discard_the_tail(self) -> None:
        tracks = [_track("a", 200.0), _track("b", 200.0)]
        analyses = {"a": _analysis("a", 120.0, 200.0), "b": _analysis("b", 120.0, 200.0)}
        without_structure = compile_automix(tracks, analyses, ENABLED)
        structures = {"a": _structure("a", 200.0, outro_start=150.0)}
        with_structure = compile_automix(tracks, analyses, ENABLED, structures=structures)
        validate_compiled_render_plan(with_structure)
        # A structure label does not prove the remaining audio is disposable.
        self.assertEqual(
            without_structure.audio.transitions[0].timeline_start,
            with_structure.audio.transitions[0].timeline_start,
        )

    def test_one_sided_structure_data_does_not_break_the_pair(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        structures = {"a": _structure("a", 60.0, outro_start=45.0)}  # "b" has none
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        validate_compiled_render_plan(plan)
        self.assertEqual(len(plan.audio.transitions), 1)

    def test_structure_present_for_a_track_missing_from_analyses_is_harmless(self) -> None:
        """A structures entry with no matching rhythm analysis must not
        crash -- that pair simply falls back exactly as it already does
        when rhythm analysis alone is missing."""
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0)}  # "b" has no rhythm analysis
        structures = {"b": _structure("b", 60.0, intro_end=5.0)}
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        self.assertEqual(plan, compile_automix(tracks, analyses, ENABLED))
        self.assertTrue(dict(plan.audio.transitions[0].details)["missing_analysis"])

    def test_incompatible_tempo_still_falls_back_regardless_of_structure(self) -> None:
        """Structure-aware scoring must never resurrect a pair that
        compatibility already rejected -- the fallback chain (roadmap:
        Structure-aware Beat Match -> Beat Match -> Beat-aligned Crossfade
        -> Fixed Crossfade -> Sequential) still starts from the same
        compatibility gate as before."""
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 90.0, 60.0), "b": _analysis("b", 140.0, 60.0)}
        structures = {
            "a": _structure("a", 60.0, outro_start=45.0),
            "b": _structure("b", 60.0, intro_end=5.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        clip_a, clip_b = plan.audio.clips
        self.assertEqual(clip_a.playback_rate, 1.0)
        self.assertEqual(clip_b.playback_rate, 1.0)
        for transition in plan.audio.transitions:
            self.assertNotEqual(transition.type, TransitionType.BEAT_MATCH)


class TransitionGeometryTests(unittest.TestCase):
    """Commit C.1: AudioRenderTransition.duration must always match the
    winning candidate's own duration_seconds -- the exact bug reported: a
    structure anchor far from the track's natural end used to inflate the
    actual rendered overlap far beyond what was scored (200s track,
    outro_start=170, candidate duration=16 rendered as ~30s)."""

    def test_structure_anchor_transition_duration_matches_the_candidate(self) -> None:
        tracks = [_track("a", 200.0), _track("b", 200.0)]
        analyses = {"a": _analysis("a", 120.0, 200.0), "b": _analysis("b", 120.0, 200.0)}
        structures = {"a": _structure("a", 200.0, outro_start=170.0)}
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        validate_compiled_render_plan(plan)
        self.assertEqual(len(plan.audio.transitions), 1)
        transition = plan.audio.transitions[0]
        # The reported bug: this landed near 30s (200 - 170) instead of
        # the ~16s an 8-bar/120 BPM candidate actually scores.
        self.assertLess(transition.duration, 20.0)
        self.assertGreater(transition.duration, 7.5)  # the last phrase start is 4 bars (8 s) from the end
        clip_a, _clip_b = plan.audio.clips
        # Finalize candidate geometry without discarding audible source audio.
        self.assertEqual(clip_a.source_out, 200.0)
        self.assertAlmostEqual(clip_a.timeline_end, transition.timeline_start + transition.duration, places=6)

    def test_outgoing_clip_source_out_stays_within_bounds(self) -> None:
        tracks = [_track("a", 200.0), _track("b", 200.0)]
        analyses = {"a": _analysis("a", 120.0, 200.0), "b": _analysis("b", 120.0, 200.0)}
        structures = {"a": _structure("a", 200.0, outro_start=170.0)}
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        clip_a = plan.audio.clips[0]
        self.assertGreater(clip_a.source_out, clip_a.source_in)
        self.assertLessEqual(clip_a.source_out, 200.0)

    def test_max_transition_seconds_invariant_holds_with_a_non_bar_aligned_anchor(self) -> None:
        tracks = [_track("a", 400.0), _track("b", 400.0)]
        analyses = {"a": _analysis("a", 120.0, 400.0), "b": _analysis("b", 120.0, 400.0)}
        # An anchor nowhere near a clean bar boundary -- exercises real
        # downbeat snapping, not just a convenient round number.
        structures = {"a": _structure("a", 400.0, outro_start=311.3)}
        plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        validate_compiled_render_plan(plan)
        self.assertTrue(plan.audio.transitions)
        for transition in plan.audio.transitions:
            self.assertGreaterEqual(transition.duration, ENABLED.min_transition_seconds)
            self.assertLessEqual(transition.duration, ENABLED.max_transition_seconds)

    def test_outgoing_source_span_follows_its_overlap_rate(self) -> None:
        """120 -> 114 BPM slows the outgoing track (rate < 1), 120 -> 126
        speeds it up: the tail it plays in the overlap is duration * rate."""
        for incoming_bpm, slower in ((114.0, True), (126.0, False)):
            with self.subTest(incoming_bpm=incoming_bpm):
                outgoing = _analysis("a", 120.0, 60.0)
                incoming = _analysis("b", incoming_bpm, 60.0)
                compatibility = evaluate_compatibility(outgoing, incoming, ENABLED)
                best = select_best_candidate(generate_candidates(outgoing, incoming, compatibility, ENABLED))
                self.assertEqual(best.outgoing_rate < 1.0, slower)
                self.assertEqual(best.incoming_rate, 1.0)
                self.assertAlmostEqual(best.outgoing_source_out - best.outgoing_source_time,
                                       best.duration_seconds * best.outgoing_rate)

    def test_preview_and_export_produce_identical_transition_geometry(self) -> None:
        """Both Preview and Export call this exact same compile_automix()
        with the same inputs -- documents/confirms that identity."""
        tracks = [_track("a", 200.0), _track("b", 200.0)]
        analyses = {"a": _analysis("a", 120.0, 200.0), "b": _analysis("b", 120.0, 200.0)}
        structures = {"a": _structure("a", 200.0, outro_start=170.0)}
        preview_plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        export_plan = compile_automix(tracks, analyses, ENABLED, structures=structures)
        self.assertEqual(preview_plan, export_plan)

    def test_three_track_chain_transition_durations_stay_within_bounds(self) -> None:
        """Item 22: effective BPM propagation must keep holding, and now
        also produce in-bounds transition durations throughout the chain."""
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0),
            "b": _analysis("b", 124.0, 60.0),
            "c": _analysis("c", 128.0, 60.0),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)
        self.assertTrue(plan.audio.transitions)
        for transition in plan.audio.transitions:
            self.assertGreaterEqual(transition.duration, ENABLED.min_transition_seconds)
            self.assertLessEqual(transition.duration, ENABLED.max_transition_seconds)


class SingleTrackAndEdgeCaseTests(unittest.TestCase):
    def test_single_track_has_no_transitions(self) -> None:
        plan = compile_automix([_track("a", 60.0)], {"a": _analysis("a", 120.0, 60.0)}, ENABLED)
        self.assertEqual(len(plan.audio.clips), 1)
        self.assertEqual(plan.audio.transitions, ())
        self.assertEqual(plan.duration_seconds, 60.0)

    def test_empty_playlist_is_empty_plan(self) -> None:
        plan = compile_automix([], {}, ENABLED)
        self.assertEqual(plan.audio.clips, ())
        self.assertEqual(plan.duration_seconds, 0.0)

    def test_very_short_tracks_fall_back_without_crashing(self) -> None:
        tracks = [_track("a", 1.0), _track("b", 1.0)]
        analyses = {"a": _analysis("a", 120.0, 1.0), "b": _analysis("b", 120.0, 1.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)


class LightAnalysisPolicyTests(unittest.TestCase):
    """What AutoMix does with only the light analyzer: bar phase and vocals unmeasured."""

    def test_unknown_bar_phase_halves_the_preset_length(self) -> None:
        tracks = [_track("a", 64.0), _track("b", 64.0)]  # ends on a phrase boundary: 8 bars back starts one
        for meter_confidence, bars in ((0.8, 8), (0.3, 4)):
            with self.subTest(meter_confidence=meter_confidence):
                analyses = {i: _analysis(i, 120.0, 64.0, meter_confidence=meter_confidence) for i in "ab"}
                (transition,) = compile_automix(tracks, analyses, ENABLED).audio.transitions
                self.assertIn(("bars", bars), transition.details)
                self.assertAlmostEqual(transition.duration, bars * 2.0, delta=0.5)  # 2 s per bar at 120 BPM

    def test_outgoing_lyrics_keep_the_blend_after_the_last_sung_line(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {i: _analysis(i, 120.0, 60.0, meter_confidence=0.3) for i in "ab"}
        (plain,) = compile_automix(tracks, analyses, ENABLED).audio.transitions
        self.assertLess(plain.timeline_start, 53.0)  # without lyrics the blend starts ~52 s

        tracks[0].lyrics = [{"start": 50.0, "end": 55.0, "text": "last line"},
                            {"start": 55.0, "end": 58.0, "text": ""}]
        tracks[0].lyrics_timing_offset_seconds = 0.5  # lyric time = audio time + 0.5
        plan = compile_automix(tracks, analyses, ENABLED)
        (sung,) = plan.audio.transitions
        cue = plan.audio.clips[0].source_at(sung.timeline_start)
        self.assertGreaterEqual(cue, 54.5 - 0.1)  # the line ends at 55 - 0.5 in audio time
        self.assertLess(cue, 55.5)
        # Lyrics only say "still singing": they never prove the rest is silent.
        self.assertIs(sung.dsp, TransitionDsp.VOCAL_SAFE_EQ)


class TransitionDspSelectionTests(unittest.TestCase):
    """DSP Phase 2: the planner, not the renderer, decides each window's mix."""

    def test_reliable_beat_match_is_planned_as_bass_swap(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        # Vocals measured and nowhere near the junction: a clean bass swap.
        measured = {"a": replace(analyses["a"], vocal_activity=((5.0, 20.0),)),
                    "b": replace(analyses["b"], vocal_activity=((40.0, 50.0),))}
        (transition,) = compile_automix(tracks, measured, ENABLED).audio.transitions
        self.assertIs(transition.type, TransitionType.BEAT_MATCH)
        self.assertIs(transition.dsp, TransitionDsp.BASS_SWAP)
        # Vocals unknown (the light analyzer): the voice band hands over mid-window.
        (transition,) = compile_automix(tracks, analyses, ENABLED).audio.transitions
        self.assertIs(transition.dsp, TransitionDsp.VOCAL_SAFE_EQ)

    def test_singing_through_the_junction_is_only_a_short_handoff(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        plain = {"a": _analysis("a", 120.0, 60.0), "b": _analysis("b", 120.0, 60.0)}
        vocal = {key: replace(value, vocal_activity=((0.0, 60.0),)) for key, value in plain.items()}
        (transition,) = compile_automix(tracks, vocal, ENABLED).audio.transitions
        self.assertEqual(dict(transition.details)["bars"], 2)
        self.assertLessEqual(transition.duration, 2 * 2.0 + 1e-6)  # two 2 s bars at 120 BPM
        self.assertIn(transition.dsp, (TransitionDsp.VOCAL_SAFE_EQ, TransitionDsp.SHORT_FADE))

    def test_fixed_crossfade_fallback_mixes_at_equal_power(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", None, 60.0), "b": _analysis("b", None, 60.0)}
        (transition,) = compile_automix(tracks, analyses, ENABLED).audio.transitions
        self.assertIs(transition.type, TransitionType.CROSSFADE)
        self.assertIs(transition.dsp, TransitionDsp.SHORT_FADE)

    def test_same_inputs_always_plan_the_same_styles(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0), _track("c", 60.0)]
        analyses = {
            "a": _analysis("a", 120.0, 60.0),
            "b": replace(_analysis("b", 120.0, 60.0), energy=0.9),
            "c": replace(_analysis("c", 124.0, 60.0), energy=0.2),
        }
        plans = [compile_automix(tracks, analyses, ENABLED) for _ in range(5)]
        self.assertTrue(all(plan == plans[0] for plan in plans))
        self.assertEqual([t.dsp for t in plans[0].audio.transitions],
                         [TransitionDsp.VOCAL_SAFE_EQ, TransitionDsp.VOCAL_SAFE_EQ])  # vocals unknown

if __name__ == "__main__":
    unittest.main()
