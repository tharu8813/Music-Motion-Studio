"""Manual AutoMix junctions: the saved override, the planner's manual path, and the editor."""
from __future__ import annotations

import os
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.automix.overrides import (  # noqa: E402
    TransitionOverride, parse_overrides, serialize_overrides,
)
from app.automix.planner import compile_automix, plan_manual_junction  # noqa: E402
from app.automix.settings import automix_settings_for  # noqa: E402
from app.models.project import ProjectDocument, ProjectSettings  # noqa: E402
from app.services.playlist_service import PlaylistService  # noqa: E402
from app.timeline.models import TransitionType  # noqa: E402
from app.timeline.render_plan import TransitionDsp, validate_compiled_render_plan  # noqa: E402
from app.utils.i18n import Translator  # noqa: E402
from app.widgets.playlist_editor import PlaylistEditor, TransitionChip  # noqa: E402
from app.widgets.transition_editor import (  # noqa: E402
    EditContext, draft_junction, override_from_junction, snap, snap_length,
)
from app.widgets.transition_inspector import plan_junctions  # noqa: E402
from tests.test_automix_planner import ENABLED, _analysis, _track  # noqa: E402


def _tracks():
    return [_track("a", 200.0), _track("b", 180.0), _track("c", 210.0)]


def _analyses(tracks, bpms=(120.0, 120.0, 120.0)):
    return {track.id: _analysis(track.id, bpm, track.duration_seconds) for track, bpm in zip(tracks, bpms)}


def _manual(settings, **overrides):
    return settings.with_overrides({key: value for key, value in overrides.items()})


class OverrideStorageTests(unittest.TestCase):
    def test_dj_effect_parameters_survive_project_save_and_validate(self) -> None:
        for style in ("beat_roll", "lowpass_out"):
            override = TransitionOverride(150.0, style=style, roll_beats=0.25, filter_cutoff_hz=350.0)
            document = ProjectDocument(settings=ProjectSettings(automix_overrides={"a>b": override.to_dict()}))
            settings = automix_settings_for(ProjectDocument.from_dict(document.to_dict()).settings)
            self.assertEqual(settings.override_for("a", "b"), override)
        for values in ({"roll_beats": 0}, {"roll_beats": True}, {"roll_beats": float("nan")},
                       {"filter_cutoff_hz": 100}, {"filter_cutoff_hz": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                TransitionOverride(150.0, **values)

    def test_round_trip_and_broken_entries_are_dropped(self) -> None:
        good = TransitionOverride(150.0, 4.0, 12.0, "filter_sweep", False, None)
        parsed = parse_overrides({
            "a>b": good.to_dict(),
            "no-separator": good.to_dict(),
            "b>c": {"outgoing_cue": -3},
            "c>d": {"outgoing_cue": 10.0, "style": "reverb"},
            "d>e": {"outgoing_cue": 10 ** 400},
            "x>y>z": good.to_dict(),
        })
        self.assertEqual(parsed, {"a>b": good, "x>y>z": good})
        self.assertEqual(parse_overrides(serialize_overrides(parsed)), parsed)

    def test_project_file_keeps_manual_junctions(self) -> None:
        document = ProjectDocument(settings=ProjectSettings(
            transition_mode="automix",
            automix_overrides={"a>b": TransitionOverride(150.0).to_dict(), "x": {"bad": 1}},
        ))
        self.assertEqual(list(document.settings.automix_overrides), ["a>b"])
        restored = ProjectDocument.from_dict(document.to_dict())
        self.assertEqual(restored.settings.automix_overrides, document.settings.automix_overrides)
        settings = automix_settings_for(restored.settings)
        self.assertEqual(settings.override_for("a", "b"), TransitionOverride(150.0))
        self.assertIsNone(settings.override_for("b", "a"))

    def test_old_projects_load_with_every_junction_automatic(self) -> None:
        restored = ProjectDocument.from_dict({"settings": {"transition_mode": "automix"}})
        self.assertEqual(restored.settings.automix_overrides, {})
        self.assertEqual(automix_settings_for(restored.settings).overrides, ())


class ManualPlanTests(unittest.TestCase):
    def test_dj_effects_keep_custom_parameters_in_the_render_plan(self) -> None:
        tracks = _tracks()
        for style in ("beat_roll", "lowpass_out"):
            override = TransitionOverride(150.0, duration=4.0, style=style, tempo_match=False,
                                          roll_beats=0.25, filter_cutoff_hz=350.0, echo_beats=2.0)
            plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
            validate_compiled_render_plan(plan)
            transition = plan.audio.transitions[0]
            self.assertEqual(transition.dsp.value, style)
            self.assertEqual((transition.roll_beats, transition.filter_cutoff_hz), (0.25, 350.0))
            if style == "beat_roll":
                self.assertAlmostEqual(transition.beat_seconds, 0.5)  # independent of echo settings

    def test_manual_junction_uses_the_users_cues_length_and_style(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(150.0, 4.0, 12.0, "filter_sweep", tempo_match=False)
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        validate_compiled_render_plan(plan)
        first, second, _third = plan.audio.clips
        transition = plan.audio.transitions[0]
        self.assertAlmostEqual(first.source_out, 162.0)
        self.assertAlmostEqual(transition.timeline_start, first.timeline_at(150.0))
        self.assertAlmostEqual(transition.duration, 12.0)
        self.assertEqual(transition.dsp, TransitionDsp.FILTER_SWEEP)
        self.assertEqual(dict(transition.details)["mode"], "manual")
        self.assertEqual((second.source_in, second.timeline_start), (4.0, transition.timeline_start))
        # The next junction is still automatic.
        self.assertEqual(dict(plan.audio.transitions[1].details)["mode"], "auto")

    def test_cut_stops_at_the_cue_and_starts_the_next_song_at_its_cue(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(120.0, 10.0, style="cut")
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        first, second, _third = plan.audio.clips
        self.assertAlmostEqual(first.source_out, 120.0)
        self.assertAlmostEqual(second.timeline_start, first.timeline_end)
        self.assertEqual(second.source_in, 10.0)
        self.assertNotIn(first.clip_id, {t.clip_a for t in plan.audio.transitions})

    def test_tempo_match_ramps_the_outgoing_song_onto_the_incoming_tempo(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(150.0, 0.0, 10.0, "legacy", tempo_match=True)
        plan = compile_automix(tracks, _analyses(tracks, (120.0, 125.0, 120.0)),
                               _manual(ENABLED, **{"a>b": override}))
        first = plan.audio.clips[0]
        transition = plan.audio.transitions[0]
        self.assertIsNotNone(first.tempo_ramp)
        self.assertAlmostEqual(first.tempo_ramp.end_rate, 125.0 / 120.0)
        # A plain crossfade must not become BEAT_MATCH's bass swap.
        self.assertEqual(transition.type, TransitionType.EQUAL_POWER)
        self.assertIsNone(transition.dsp)

    def test_a_hand_set_tempo_match_applies_however_far_apart_the_tempos_are(self) -> None:
        tracks = _tracks()
        analyses = _analyses(tracks, (120.0, 150.0, 120.0))  # 25% apart: beyond the automatic bridge
        automatic = compile_automix(tracks, analyses, ENABLED)
        self.assertIsNone(automatic.audio.clips[0].tempo_ramp)
        override = TransitionOverride(150.0, 0.0, 10.0, "legacy", tempo_match=True)
        plan = compile_automix(tracks, analyses, _manual(ENABLED, **{"a>b": override}))
        self.assertAlmostEqual(plan.audio.clips[0].tempo_ramp.end_rate, 1.25)
        validate_compiled_render_plan(plan)

    def test_a_hand_set_window_may_run_into_the_quiet_tail_up_to_the_files_end(self) -> None:
        from dataclasses import replace as replaced

        tracks = _tracks()
        analyses = _analyses(tracks)
        analyses["a"] = replaced(analyses["a"], audible_end_seconds=190.0)  # "a" (200 s) fades out from 190 s
        override = TransitionOverride(180.0, 0.0, 18.0, "legacy", tempo_match=False)
        plan = compile_automix(tracks, analyses, _manual(ENABLED, **{"a>b": override}))
        self.assertAlmostEqual(plan.audio.transitions[0].duration, 18.0)
        longer = compile_automix(tracks, analyses, _manual(ENABLED, **{"a>b": replaced(override, duration=30.0)}))
        self.assertAlmostEqual(longer.audio.transitions[0].duration, 20.0)  # the file itself ends at 200 s
        validate_compiled_render_plan(longer)

    def test_an_echo_out_rings_on_past_the_end_of_the_outgoing_file(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(196.0, 0.0, 12.0, "echo_out", tempo_match=False, echo_feedback=0.95)
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        transition = plan.audio.transitions[0]
        self.assertAlmostEqual(transition.duration, 12.0)  # "a" has only 4 s left after 196 s
        self.assertAlmostEqual(plan.audio.clips[0].source_out, 208.0)  # the renderer pads the rest
        self.assertEqual(transition.echo_feedback, 0.95)
        validate_compiled_render_plan(plan)
        with self.assertRaises(ValueError):
            TransitionOverride(196.0, echo_feedback=0.99)

    def test_out_of_range_values_are_clamped_to_what_the_songs_have(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(195.0, 0.0, 30.0, "short_fade", tempo_match=False)
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        transition = plan.audio.transitions[0]
        self.assertAlmostEqual(transition.duration, 5.0)  # only 5 s of "a" left after 195 s
        validate_compiled_render_plan(plan)

    def test_a_cue_never_starts_before_the_previous_transition_ends(self) -> None:
        tracks = _tracks()
        auto = compile_automix(tracks, _analyses(tracks), ENABLED)
        into_b = auto.audio.transitions[0]
        override = TransitionOverride(0.0, 0.0, 8.0, "short_fade", tempo_match=False)
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"b>c": override}))
        self.assertGreaterEqual(plan.audio.transitions[1].timeline_start,
                                into_b.timeline_start + into_b.duration - 1e-6)
        validate_compiled_render_plan(plan)

    def test_works_without_analysis(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(150.0, 0.0, 6.0)
        plan = compile_automix(tracks, {}, _manual(ENABLED, **{"a>b": override}))
        manual, fallback = plan.audio.transitions
        self.assertAlmostEqual(manual.duration, 6.0)
        self.assertEqual(dict(manual.details)["mode"], "manual")
        self.assertTrue(dict(fallback.details)["missing_analysis"])

    def test_reordered_tracks_fall_back_to_automatic(self) -> None:
        tracks = _tracks()
        override = TransitionOverride(150.0, 0.0, 6.0, "cut")
        settings = _manual(ENABLED, **{"a>b": override})
        reordered = [tracks[1], tracks[0], tracks[2]]
        plan = compile_automix(reordered, _analyses(reordered), settings)
        self.assertTrue(all(dict(t.details)["mode"] == "auto" for t in plan.audio.transitions))

    def test_the_editor_draft_matches_the_compiled_plan(self) -> None:
        tracks = _tracks()
        analyses = _analyses(tracks, (120.0, 124.0, 120.0))
        override = TransitionOverride(140.0, 2.0, 10.0, "vocal_safe_eq", True, 0.4)
        compiled = compile_automix(tracks, analyses, _manual(ENABLED, **{"b>c": override}))
        auto = plan_junctions(compile_automix(tracks, analyses, ENABLED))
        draft = draft_junction(auto, 1, override, EditContext({t.id: t for t in tracks}, analyses))
        actual = plan_junctions(compiled)[1]
        self.assertAlmostEqual(draft.start, actual.start, places=6)
        self.assertAlmostEqual(draft.end, actual.end, places=6)
        self.assertEqual(draft.transition.dsp, actual.transition.dsp)
        self.assertEqual(draft.transition.vocal_handoff, 0.4)
        outgoing, incoming, transition = plan_manual_junction(
            auto[1].outgoing, tracks[1], tracks[2], override, analyses)
        self.assertAlmostEqual(transition.timeline_start, actual.start, places=6)


class EditorHelperTests(unittest.TestCase):
    def test_prefill_reproduces_the_automatic_window(self) -> None:
        tracks = _tracks()
        analyses = _analyses(tracks)
        junction = plan_junctions(compile_automix(tracks, analyses, ENABLED))[0]
        override = override_from_junction(junction)
        manual = compile_automix(tracks, analyses, _manual(ENABLED, **{"a>b": override}))
        again = plan_junctions(manual)[0]
        self.assertAlmostEqual(again.start, junction.start, places=4)
        self.assertAlmostEqual(again.end, junction.end, places=4)

    def test_prefill_keeps_a_junction_without_overlap_as_it_is(self) -> None:
        tracks = _tracks()
        plan = compile_automix(tracks, {}, replace(ENABLED, fallback_crossfade_seconds=0.0))
        junction = plan_junctions(plan)[0]
        override = override_from_junction(junction)
        self.assertEqual(override.style, "cut")
        again = plan_junctions(compile_automix(tracks, {}, ENABLED.with_overrides({"a>b": override})))[0]
        self.assertEqual((again.start, again.transition), (junction.start, None))
        self.assertEqual(again.incoming.source_in, junction.incoming.source_in)

    def test_snapping(self) -> None:
        self.assertEqual(snap(10.3, (8.0, 10.0, 12.0)), 10.0)
        self.assertEqual(snap(10.3, ()), 10.3)
        self.assertEqual(snap(10.9, (8.0, 12.0), tolerance=0.5), 10.9)
        self.assertAlmostEqual(snap_length(7.8, _analysis("x", 120.0, 60.0)), 8.0)


class PlaylistChipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_chips_sit_between_enabled_tracks_and_show_manual_ones(self) -> None:
        service = PlaylistService()
        service.replace([_track("a", 100.0), _track("b", 100.0, enabled=False), _track("c", 100.0)])
        editor = PlaylistEditor(service, Translator())
        self.addCleanup(editor.deleteLater)

        def chips():
            rows = [editor.list_widget.itemWidget(editor.list_widget.item(i))
                    for i in range(editor.list_widget.count())]
            return [row.transition_chip for row in rows]

        self.assertEqual(chips(), [None, None, None])  # not AutoMix: no chips
        self.assertTrue(editor.automix_editor_button.isHidden())
        editor.set_transitions(True, {"a>c": TransitionOverride(80.0, style="cut").to_dict()})
        self.assertFalse(editor.automix_editor_button.isHidden())  # the whole-playlist entry
        opened = []
        editor.automix_editor_requested.connect(lambda: opened.append(True))
        editor.automix_editor_button.click()
        self.assertEqual(opened, [True])
        first, excluded, third = chips()
        self.assertIsNone(first)
        self.assertIsNone(excluded)
        self.assertIsInstance(third, TransitionChip)
        self.assertTrue(third.property("manual"))
        requested = []
        editor.transition_edit_requested.connect(lambda *pair: requested.append(pair))
        third.click()
        self.assertEqual(requested, [("a", "c")])


if __name__ == "__main__":
    unittest.main()


CUSTOM_BANDS = (((0.10, 0.30), (0.20, 0.40)), ((0.30, 0.80), (0.30, 0.80)), ((0.0, 1.0), (0.0, 0.50)))


class CustomEqTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_band_windows_round_trip_and_bad_ones_are_dropped(self) -> None:
        override = TransitionOverride(150.0, style="eq", eq_bands=CUSTOM_BANDS)
        data = override.to_dict()
        self.assertEqual(data["eq"]["low"], {"out": [0.10, 0.30], "in": [0.20, 0.40]})
        broken = dict(data, eq={"low": data["eq"]["low"], "mid": data["eq"]["mid"]})
        backwards = dict(data, eq=dict(data["eq"], high={"out": [0.9, 0.2], "in": [0.0, 1.0]}))
        parsed = parse_overrides({"a>b": data, "b>c": broken, "c>d": backwards})
        self.assertEqual(parsed, {"a>b": override})
        self.assertIsNone(TransitionOverride(150.0).to_dict().get("eq"))
        with self.assertRaises(ValueError):
            TransitionOverride(150.0, style="eq", eq_bands=(((0.5, 0.5), (0.0, 1.0)),) * 3)

    def test_custom_eq_renders_the_users_band_windows(self) -> None:
        from app.automix.diagnostics import transition_rows
        from app.automix.renderer import band_fade_windows, build_filter_graph
        from app.widgets.transition_inspector import mix_lanes

        tracks = _tracks()
        override = TransitionOverride(150.0, 0.0, 10.0, "eq", tempo_match=False, eq_bands=CUSTOM_BANDS)
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        validate_compiled_render_plan(plan)
        transition = plan.audio.transitions[0]
        self.assertEqual(transition.band_windows, CUSTOM_BANDS)
        self.assertIs(transition.dsp, TransitionDsp.BASS_SWAP)
        self.assertEqual(transition_rows(plan)[0]["dsp"], "eq")
        side = (transition.dsp, transition.duration, transition.vocal_handoff, transition.band_windows)
        (_fade, start, length), = band_fade_windows("low", 200.0, side, None)
        self.assertAlmostEqual(start, transition.duration * 0.20)
        self.assertAlmostEqual(length, transition.duration * 0.20)
        graph, _output = build_filter_graph(plan.audio.clips, plan.audio.transitions)
        self.assertIn(f"afade=t=in:st={transition.duration * 0.20:.6f}", graph)
        lanes = {lane.key: lane for lane in mix_lanes(transition)}
        self.assertAlmostEqual(lanes["high"].incoming(0.5), 1.0)
        self.assertAlmostEqual(lanes["low"].outgoing(0.31), 0.0)
        # Without eq_bands, Custom EQ starts from the bass swap; other styles never carry windows.
        plain = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{
            "a>b": TransitionOverride(150.0, style="eq"),
            "b>c": TransitionOverride(150.0, style="vocal_safe_eq", eq_bands=CUSTOM_BANDS),
        }))
        from app.automix.renderer import default_eq_bands

        self.assertEqual(plain.audio.transitions[0].band_windows, default_eq_bands())
        self.assertIsNone(plain.audio.transitions[1].band_windows)

    def test_window_helpers(self) -> None:
        from app.widgets.transition_editor import band_hole, move_window

        self.assertEqual(move_window((0.2, 0.4), "move", 0.9), (0.8, 1.0))
        self.assertEqual(move_window((0.2, 0.4), "start", 0.5)[0], 0.4 - 0.02)
        self.assertEqual(move_window((0.2, 0.4), "end", -1.0), (0.2, 0.2 + 0.02))
        self.assertAlmostEqual(band_hole((0.1, 0.3), (0.5, 0.7)), 0.2)
        self.assertEqual(band_hole((0.1, 0.6), (0.5, 0.7)), 0.0)

class TimelineBandDragTests(unittest.TestCase):
    """Band bars on the AutoMix editor's timeline: free with Shift, else onto a beat."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_band_bars_move_freely_and_click_onto_beats(self) -> None:
        from app.widgets.automix_timeline import AutoMixTimeline

        tracks = _tracks()
        analyses = _analyses(tracks)
        timeline = AutoMixTimeline()
        self.addCleanup(timeline.deleteLater)
        timeline.resize(900, 500)
        junction = plan_junctions(compile_automix(tracks, analyses, ENABLED))[0]
        timeline.analyses = (analyses["a"], analyses["b"])
        timeline.set_junction(junction, refit=True)
        length = junction.end - junction.start
        start_bands = (((0.3, 0.5), (0.3, 0.5)),) * 3
        press = timeline.x_of(junction.start)
        free = (timeline.seconds_at(press + 40.0) - timeline.seconds_at(press)) / length
        timeline._band_drag = ("low", "out", "move", press, start_bands)
        moved = timeline._dragged_bands(press + 40.0, True)
        self.assertAlmostEqual(moved[0][0][0], 0.3 + free, places=6)
        self.assertEqual(moved[0][1], moved[0][0])  # linked: B's bar moves with A's
        snapped = timeline._dragged_bands(press + 40.0, False)
        beat = 0.5 / length  # 120 BPM
        self.assertAlmostEqual(snapped[0][0][0] / beat, round(snapped[0][0][0] / beat), places=6)
        timeline._band_drag = None

class TempoRampStartTests(unittest.TestCase):
    """Where the outgoing song starts easing onto the new tempo: saved, planned, dragged and shown."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _plan(self, override):
        tracks = _tracks()
        analyses = _analyses(tracks, (120.0, 125.0, 120.0))
        return tracks, analyses, compile_automix(tracks, analyses, _manual(ENABLED, **{"a>b": override}))

    def test_the_ramp_length_is_saved_and_planned(self) -> None:
        override = TransitionOverride(150.0, 0.0, 10.0, "legacy", tempo_match=True, ramp_seconds=12.0)
        self.assertEqual(parse_overrides({"a>b": override.to_dict()})["a>b"], override)
        with self.assertRaises(ValueError):
            TransitionOverride(150.0, ramp_seconds=-1.0)
        ramp = self._plan(override)[2].audio.clips[0].tempo_ramp
        self.assertAlmostEqual((ramp.source_start, ramp.source_end), (138.0, 150.0))
        at_once = self._plan(TransitionOverride(150.0, 0.0, 10.0, "legacy", True, ramp_seconds=0.0))[2]
        self.assertEqual(at_once.audio.clips[0].tempo_ramp.source_start, 150.0)
        automatic = self._plan(TransitionOverride(150.0, 0.0, 10.0, "legacy", True))[2]
        self.assertAlmostEqual(automatic.audio.clips[0].tempo_ramp.source_start, 150.0 - 8 * 2.0)  # 8 bars

    def test_dragging_the_ramp_start_sets_its_length_within_what_the_song_allows(self) -> None:
        from app.widgets.transition_editor import drag_override

        base = TransitionOverride(150.0, 0.0, 10.0, "legacy", tempo_match=True)
        tracks, analyses, plan = self._plan(base)
        junctions = plan_junctions(plan)
        context = EditContext({t.id: t for t in tracks}, analyses)
        drag = lambda delta, snapping=False: drag_override(  # noqa: E731
            junctions, 0, junctions[0], base, "ramp", delta, context,
            snapping=snapping, tolerance=0.3, korean=True)
        later, hint, _guide = drag(6.0)  # 16 s ramp starting 6 s later: 10 s
        self.assertAlmostEqual(later.ramp_seconds, 10.0, places=4)
        self.assertIn("템포 변경 시작", hint)
        self.assertEqual(drag(60.0)[0].ramp_seconds, 0.0)  # past the mix start: at once
        self.assertLessEqual(drag(-1000.0)[0].ramp_seconds, 150.0)  # never before the song starts
        snapped = drag(6.1, snapping=True)[0]
        self.assertAlmostEqual(snapped.ramp_seconds / 2.0, round(snapped.ramp_seconds / 2.0), places=4)  # a bar

    def test_the_view_shows_where_the_tempo_starts_to_change_and_its_handle_is_grabbable(self) -> None:
        from app.widgets.automix_timeline import RAMP_GRIP, AutoMixTimeline

        tracks, analyses, plan = self._plan(TransitionOverride(150.0, 0.0, 10.0, "legacy", True, ramp_seconds=40.0))
        junction = plan_junctions(plan)[0]
        timeline = AutoMixTimeline()
        self.addCleanup(timeline.deleteLater)
        timeline.resize(1200, 500)
        timeline.analyses = (analyses["a"], analyses["b"])
        timeline.set_junction(junction, refit=True)
        ramp_start = junction.outgoing.timeline_at(junction.outgoing.tempo_ramp.source_start)
        self.assertLess(timeline.view()[0], ramp_start)
        x = timeline.x_of(ramp_start)
        grip_y = timeline._lane_top(0) + timeline.track_height - RAMP_GRIP / 2
        self.assertIsNone(timeline.hit(x, grip_y))  # simple mode shows the ramp but offers no handle
        timeline.set_advanced(True)
        self.assertEqual(timeline.hit(x, grip_y), ("ramp",))
        timeline.grab()  # paints the ramp band and its handle without error

class KeptValueTests(unittest.TestCase):
    """"Keep this value": saved with the junction, and held by the planner's own recommendation."""

    def _plan(self, override):
        tracks = _tracks()
        plan = compile_automix(tracks, _analyses(tracks), _manual(ENABLED, **{"a>b": override}))
        return plan.audio.transitions[0]

    def test_kept_values_and_the_recommend_mode_are_saved_and_loaded(self) -> None:
        kept = TransitionOverride(150.0, 1.0, 12.0, locked=["duration", "outgoing_cue"], recommend=True)
        self.assertEqual(kept.locked, ("outgoing_cue", "duration"))  # normalized order
        self.assertEqual(parse_overrides({"a>b": kept.to_dict()})["a>b"], kept)
        document = ProjectDocument(settings=ProjectSettings(automix_overrides={"a>b": kept.to_dict()}))
        restored = automix_settings_for(ProjectDocument.from_dict(document.to_dict()).settings)
        self.assertEqual(restored.override_for("a", "b"), kept)
        self.assertNotIn("locked", TransitionOverride(150.0).to_dict())  # old files stay as they were
        with self.assertRaises(ValueError):
            TransitionOverride(150.0, locked=("style",))

    def test_a_new_recommendation_keeps_a_kept_length(self) -> None:
        automatic = compile_automix(_tracks(), _analyses(_tracks()), ENABLED).audio.transitions[0]
        transition = self._plan(TransitionOverride(10.0, 0.0, 12.0, style="echo_out",
                                                   locked=("duration",), recommend=True))
        details = dict(transition.details)
        self.assertEqual((details["mode"], details["recommendation"]), ("auto", "locked"))
        self.assertAlmostEqual(transition.duration, 12.0, places=6)
        self.assertNotEqual(transition.dsp, TransitionDsp.ECHO_OUT)  # unkept values follow analysis
        self.assertNotAlmostEqual(automatic.duration, 12.0)

    def test_a_kept_cue_no_recommendation_can_hold_plays_as_saved_and_says_why(self) -> None:
        transition = self._plan(TransitionOverride(50.0, 0.0, 12.0, style="short_fade",
                                                   locked=("outgoing_cue",), recommend=True))
        details = dict(transition.details)
        self.assertEqual((details["mode"], details["recommendation"]), ("manual", "no_fit"))
        self.assertAlmostEqual(details["outgoing_cue"], 50.0)  # nothing was quietly moved
        self.assertEqual(transition.dsp, TransitionDsp.SHORT_FADE)
        self.assertGreater(details["nearest_outgoing_cue"], 100.0)  # what analysis would pick instead

    def test_a_kept_cue_near_a_recommendation_moves_the_other_cue_with_it(self) -> None:
        automatic = dict(compile_automix(_tracks(), _analyses(_tracks()), ENABLED).audio.transitions[0].details)
        cue = automatic["outgoing_cue"] + 1.0  # half a bar after what analysis picked (120 BPM)
        transition = self._plan(TransitionOverride(cue, 0.0, 8.0, locked=("outgoing_cue",), recommend=True))
        details = dict(transition.details)
        self.assertEqual(details["recommendation"], "locked")
        self.assertAlmostEqual(details["outgoing_cue"], cue, places=6)
        self.assertAlmostEqual(details["incoming_cue"], automatic["incoming_cue"] + 1.0, places=6)  # beats aligned
        self.assertLessEqual(details["outgoing_cut"], 200.0 + 1e-6)  # the window ends with the song


class MarkerLabelTests(unittest.TestCase):
    def test_close_labels_stack_instead_of_overlapping(self) -> None:
        from app.widgets.automix_timeline import MARKER_ROWS, stack_labels

        self.assertEqual(stack_labels([(100.0, 80.0), (300.0, 80.0)]), [0, 0])  # apart: one row
        self.assertEqual(stack_labels([(100.0, 80.0), (120.0, 80.0), (400.0, 50.0)]), [0, 1, 0])
        crowded = stack_labels([(float(x), 200.0) for x in range(0, 50, 10)])
        self.assertEqual(crowded[:MARKER_ROWS], list(range(MARKER_ROWS)))
        self.assertTrue(all(0 <= row < MARKER_ROWS for row in crowded))
