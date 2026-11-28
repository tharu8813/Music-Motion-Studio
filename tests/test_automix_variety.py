"""AutoMix's wider toolkit: phrase exits, tempo bridges, key shifts, filter sweeps, timbre and phrases."""
from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from app.automix.analysis.key import camelot_compatible, harmonic_shift, shift_key
from app.automix.candidates import TransitionStrategy
from app.automix.exits import phrase_exits
from app.automix.phrases import PHRASE_BARS, peak_sections, phrase_starts
from app.automix.planner import compile_automix
from app.automix.renderer import build_filter_graph
from app.automix.structure.models import TrackSection, TrackStructureAnalysis
from app.automix.structure.timbre import timbre_curves
from app.automix.transition_style import select_transition_dsp
from app.timeline.models import TransitionType
from app.timeline.render_plan import AudioRenderClip, TempoRamp, TransitionDsp, validate_compiled_render_plan
from tests.test_automix_planner import ENABLED, _analysis, _track
from tests.test_automix_transition_style import _candidate, _compatibility, _quiet


def _structure(track_id: str, duration: float, energy: float, drums: float,
               sections: tuple[TrackSection, ...] = ()) -> TrackStructureAnalysis:
    seconds = int(duration) + 1
    return TrackStructureAnalysis(
        track_id=track_id, source_path=f"{track_id}.mp3", duration_seconds=duration,
        sections=sections, energy_curve=(energy,) * seconds, energy_curve_hop_seconds=1.0,
        percussive_curve=(drums,) * seconds,
    )


class KeyShiftTests(unittest.TestCase):
    def test_a_semitone_makes_clashing_keys_compatible(self) -> None:
        self.assertEqual(shift_key("A minor", 1), "A# minor")
        self.assertEqual(shift_key("B major", 1), "C major")
        shift = harmonic_shift("C# major", "C major")  # a semitone apart: clashes
        self.assertIn(shift, (1, -1))
        self.assertTrue(camelot_compatible(shift_key("C# major", shift), "C major"))

    def test_compatible_or_unfixable_keys_are_left_alone(self) -> None:
        self.assertIsNone(harmonic_shift("C major", "G major"))
        self.assertIsNone(harmonic_shift("C major", "A major"))  # 8B vs 11B: no single semitone helps

    def test_planner_glides_the_outgoing_tail_into_the_incoming_key(self) -> None:
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {
            "a": replace(_analysis("a", 120.0, 60.0), key="C# major", key_confidence=0.8),
            "b": replace(_analysis("b", 124.0, 60.0), key="C major", key_confidence=0.8),
        }
        plan = compile_automix(tracks, analyses, ENABLED)
        ramp = plan.audio.clips[0].tempo_ramp
        shift = dict(plan.audio.transitions[0].details)["key_shift_semitones"]
        self.assertIn(shift, (1, -1))
        self.assertAlmostEqual(ramp.end_pitch, 2.0 ** (shift / 12.0))
        graph, _label = build_filter_graph(plan.audio.clips, plan.audio.transitions)
        self.assertIn("rubberband@ramp0 pitch", graph)
        # An unsure key estimate is not worth re-pitching a song for.
        unsure = {**analyses, "b": replace(analyses["b"], key_confidence=0.5)}
        self.assertEqual(compile_automix(tracks, unsure, ENABLED).audio.clips[0].tempo_ramp.end_pitch, 1.0)

    def test_pitch_follows_the_ramp_and_holds_after_it(self) -> None:
        ramp = TempoRamp(10.0, 20.0, 1.05, end_pitch=1.06)
        self.assertEqual(ramp.pitch_at(5.0), 1.0)
        self.assertAlmostEqual(ramp.pitch_at(15.0), 1.03)
        self.assertAlmostEqual(ramp.pitch_at(40.0), 1.06)


class TempoBridgeTests(unittest.TestCase):
    def test_a_moderate_tempo_gap_is_beat_matched_over_a_longer_ramp(self) -> None:
        tracks = [_track("a", 120.0), _track("b", 120.0)]
        analyses = {"a": _analysis("a", 110.0, 120.0), "b": _analysis("b", 123.0, 120.0)}  # 11.8%
        plan = compile_automix(tracks, analyses, ENABLED)
        (transition,) = plan.audio.transitions
        self.assertIs(transition.type, TransitionType.BEAT_MATCH)
        details = dict(transition.details)
        bar = 4 * 60.0 / 110.0
        self.assertGreater(details["tempo_ramp_seconds"], 8 * bar + 1e-6)  # longer than a direct match's
        self.assertAlmostEqual(plan.audio.clips[0].tempo_ramp.end_rate, 123.0 / 110.0, places=2)

    def test_beyond_the_bridge_it_is_not_beat_matched(self) -> None:
        tracks = [_track("a", 120.0), _track("b", 120.0)]
        analyses = {"a": _analysis("a", 100.0, 120.0), "b": _analysis("b", 125.0, 120.0)}  # 25%
        (transition,) = compile_automix(tracks, analyses, ENABLED).audio.transitions
        self.assertIsNot(transition.type, TransitionType.BEAT_MATCH)


class PhraseExitTests(unittest.TestCase):
    def _exit(self, out_energy=0.6, in_energy=0.6, out_drums=0.9, in_drums=0.9, previous=None, **fields):
        fields.setdefault("vocal_coverage", ((0.0, 180.0),))
        outgoing = replace(_analysis("a", 100.0, 180.0), **fields)
        incoming = replace(_analysis("b", 140.0, 180.0), vocal_coverage=((0.0, 180.0),))
        return next(phrase_exits(
            outgoing, incoming, _structure("a", 180.0, out_energy, out_drums),
            _structure("b", 180.0, in_energy, in_drums), previous_style=previous,
        ), None)

    def test_drums_on_both_sides_at_the_same_energy_cut_on_the_downbeat(self) -> None:
        plan = self._exit()
        self.assertIs(plan.decision.dsp, TransitionDsp.DOWNBEAT_CUT)
        self.assertIs(plan.candidate.strategy, TransitionStrategy.PHRASE_EXIT)
        self.assertAlmostEqual(plan.candidate.duration_seconds, 0.05)
        source = _analysis("a", 100.0, 180.0)
        self.assertIn(plan.candidate.outgoing_source_time, source.downbeats)
        self.assertLessEqual(source.duration_seconds - plan.candidate.outgoing_source_time, 2.4 + 1e-6)

    def test_a_big_energy_drop_brakes_like_a_turntable(self) -> None:
        plan = self._exit(out_energy=0.9, in_energy=0.3)
        self.assertIs(plan.decision.dsp, TransitionDsp.TAPE_STOP)
        self.assertEqual(plan.candidate.incoming_source_time, 0.0)  # clamped: its downbeat is at 0

    def test_otherwise_the_last_beat_echoes_out(self) -> None:
        plan = self._exit(in_drums=0.2)
        self.assertIs(plan.decision.dsp, TransitionDsp.ECHO_OUT)
        self.assertAlmostEqual(plan.beat_seconds, 0.6, places=3)
        # As the song ends: its last downbeat whose beat still sounds (the 180 s file
        # leaves one bar after it, so the echo gets that bar instead of two).
        self.assertAlmostEqual(plan.candidate.outgoing_source_time, 177.6, places=3)
        self.assertAlmostEqual(plan.candidate.duration_seconds, 2.4, places=3)
        self.assertIn("as the song ends", plan.decision.reasons[0])

    def test_the_echo_gets_two_bars_when_the_file_has_room(self) -> None:
        plan = self._exit(in_drums=0.2, audible_end_seconds=170.0)  # 10 s of silence after the sound
        self.assertAlmostEqual(plan.candidate.outgoing_source_time, 168.0, places=3)  # last downbeat + beat <= 170
        self.assertAlmostEqual(plan.candidate.duration_seconds, 4.8, places=3)

    def test_the_same_move_twice_in_a_row_gives_way_when_another_fits(self) -> None:
        self.assertIs(self._exit(previous=TransitionDsp.DOWNBEAT_CUT).decision.dsp, TransitionDsp.ECHO_OUT)
        # Only an echo fits after an echo: the plain end fade takes this one instead.
        self.assertIsNone(self._exit(in_drums=0.2, previous=TransitionDsp.ECHO_OUT))
        self.assertIs(self._exit(previous=TransitionDsp.ECHO_OUT).decision.dsp, TransitionDsp.DOWNBEAT_CUT)

    def test_a_short_natural_ending_keeps_the_regular_fade(self) -> None:
        self.assertIsNone(self._exit(decay_start_seconds=175.0))
        long_fade = self._exit(decay_start_seconds=150.0)  # echo starts with a hit before the radio fade
        self.assertIs(long_fade.decision.dsp, TransitionDsp.ECHO_OUT)
        self.assertLessEqual(long_fade.candidate.outgoing_source_time + long_fade.beat_seconds, 150.0 + 1e-6)

    def test_phrase_exits_preserve_a_sung_line_after_the_candidate_cue(self) -> None:
        clean = self._exit()
        start = clean.candidate.outgoing_source_time
        for drums in (0.9, 0.2):  # a cut and an echo
            with self.subTest(drums=drums):
                sung = self._exit(in_drums=drums, vocal_activity=((start - 1.0, 180.0),))
                self.assertIsNone(sung)

    def test_sung_to_the_end_rules_out_all_exit_effects(self) -> None:
        sung = dict(vocal_activity=((100.0, 180.0),), vocal_coverage=((0.0, 180.0),))
        self.assertIsNone(self._exit(**sung))
        self.assertIsNone(self._exit(out_energy=0.9, in_energy=0.3, **sung))

    def test_unmeasured_tail_does_not_get_trimmed(self) -> None:
        self.assertIsNone(self._exit(vocal_coverage=()))

    def test_no_bar_grid_no_phrase_exit(self) -> None:
        self.assertIsNone(self._exit(meter_confidence=0.0))

    def test_the_planner_keeps_a_fade_when_phrase_exit_cannot_be_scored(self) -> None:
        tracks = [_track("a", 180.0), _track("b", 180.0)]
        analyses = {"a": _analysis("a", 100.0, 180.0), "b": _analysis("b", 140.0, 180.0)}
        plan = compile_automix(tracks, analyses, ENABLED)
        validate_compiled_render_plan(plan)
        (transition,) = plan.audio.transitions
        self.assertIs(transition.dsp, TransitionDsp.SHORT_FADE)
        self.assertEqual(dict(transition.details)["strategy"], "fixed_crossfade")
        clip_a = plan.audio.clips[0]
        self.assertAlmostEqual(clip_a.timeline_end, transition.timeline_start + transition.duration)


class ManualAndDiagramTests(unittest.TestCase):
    def test_hand_set_echo_and_cut_get_their_beat_and_splice(self) -> None:
        from app.automix.overrides import TransitionOverride
        from app.automix.settings import AUTOMIX_SETTINGS

        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": _analysis("a", 100.0, 60.0), "b": _analysis("b", 140.0, 60.0)}
        for style, duration in (("echo_out", 4.8), ("downbeat_cut", 0.05)):
            with self.subTest(style=style):
                settings = AUTOMIX_SETTINGS.with_overrides({"a>b": TransitionOverride(
                    outgoing_cue=48.0, incoming_cue=0.0, duration=8.0, style=style, tempo_match=False)})
                (transition,) = compile_automix(tracks, analyses, settings).audio.transitions
                self.assertIs(transition.dsp, TransitionDsp(style))
                self.assertAlmostEqual(transition.beat_seconds, 0.6)
                self.assertAlmostEqual(transition.duration, 8.0 if style == "echo_out" else duration)

    def test_the_diagram_draws_the_echo_and_the_slowing_platter(self) -> None:
        from app.automix.renderer import TAPE_STOP_MIN_RATE
        from app.timeline.render_plan import AudioRenderTransition
        from app.widgets.transition_inspector import mix_lanes

        echo = mix_lanes(AudioRenderTransition("a", "b", 10.0, 4.0, TransitionType.EQUAL_POWER,
                                               TransitionDsp.ECHO_OUT, beat_seconds=0.5))
        self.assertEqual([lane.key for lane in echo], ["level", "echo"])
        self.assertEqual(echo[1].outgoing(0.05), 0.0)                  # before the first repeat
        self.assertGreater(echo[1].outgoing(0.2), echo[1].outgoing(0.6))  # repeats die away
        stop = mix_lanes(AudioRenderTransition("a", "b", 10.0, 2.0, TransitionType.EQUAL_POWER,
                                               TransitionDsp.TAPE_STOP))
        self.assertEqual([lane.key for lane in stop], ["level", "speed"])
        self.assertAlmostEqual(stop[1].outgoing(0.99), TAPE_STOP_MIN_RATE)
        self.assertEqual(stop[0].incoming(0.5), 0.0)  # silent until it enters


class EffectSettingsTests(unittest.TestCase):
    def _plan(self, **fields):
        from app.automix.overrides import TransitionOverride
        from app.automix.settings import AUTOMIX_SETTINGS

        tracks = [_track("a", 60.0), _track("b", 60.0)]
        analyses = {"a": replace(_analysis("a", 100.0, 60.0), key="C# major", key_confidence=0.9),
                    "b": replace(_analysis("b", 140.0, 60.0), key="C major", key_confidence=0.9)}
        values = dict(outgoing_cue=48.0, incoming_cue=0.0, duration=4.8, tempo_match=False)
        values.update(fields)
        settings = AUTOMIX_SETTINGS.with_overrides({"a>b": TransitionOverride(**values)})
        return compile_automix(tracks, analyses, settings)

    def test_settings_round_trip_and_old_saves_keep_the_defaults(self) -> None:
        from app.automix.overrides import TransitionOverride

        custom = TransitionOverride(outgoing_cue=1.0, style="echo_out", echo_beats=2.0, echo_feedback=0.7,
                                    echo_low_cut=False, tape_entry=0.8, key_shift=-1)
        self.assertEqual(TransitionOverride.from_dict(custom.to_dict()), custom)
        old = TransitionOverride.from_dict({"outgoing_cue": 1.0, "style": "echo_out"})
        self.assertEqual((old.echo_beats, old.echo_feedback, old.echo_low_cut, old.tape_entry, old.key_shift),
                         (1.0, 0.55, True, 0.6, None))
        for bad in (dict(echo_beats=3.0), dict(echo_feedback=0.99), dict(tape_entry=0.1), dict(key_shift=5)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                TransitionOverride(outgoing_cue=1.0, **bad)

    def test_echo_settings_reach_the_render(self) -> None:
        plan = self._plan(style="echo_out", echo_beats=0.5, echo_feedback=0.7, echo_low_cut=False)
        (transition,) = plan.audio.transitions
        self.assertAlmostEqual(transition.beat_seconds, 0.3)  # half a 100 BPM beat
        graph, _label = build_filter_graph(plan.audio.clips, plan.audio.transitions)
        self.assertIn("delays=300.000|600.000", graph)
        self.assertIn("decays=0.900000|0.630000|0.441000", graph)
        self.assertNotIn("highpass=f=200", graph)

    def test_tape_entry_moves_where_the_next_song_comes_in(self) -> None:
        plan = self._plan(style="tape_stop", duration=2.0, tape_entry=0.8)
        graph, _label = build_filter_graph(plan.audio.clips, plan.audio.transitions, ramp_filter="rubberband")
        self.assertIn("afade=t=in:st=1.600000", graph)

    def test_key_shift_by_hand_glides_even_without_a_tempo_match(self) -> None:
        clip = self._plan(style="short_fade", key_shift=1).audio.clips[0]
        self.assertAlmostEqual(clip.tempo_ramp.end_pitch, 2.0 ** (1 / 12))
        self.assertEqual(clip.tempo_ramp.end_rate, 1.0)  # the tempo itself is left alone
        self.assertIsNone(self._plan(style="short_fade", key_shift=0).audio.clips[0].tempo_ramp)
        # Automatic re-pitches only alongside a tempo match, as the planner does.
        self.assertIsNone(self._plan(style="short_fade").audio.clips[0].tempo_ramp)


class EditorPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, style: str, advanced: bool = True, duration: float = 16.0):
        from app.automix.overrides import TransitionOverride
        from app.widgets.transition_editor import TransitionPropertiesPanel

        panel = TransitionPropertiesPanel()
        self.addCleanup(panel.deleteLater)
        panel.set_advanced(advanced)
        panel.set_state(TransitionOverride(outgoing_cue=10.0, duration=duration, style=style), manual=True,
                        pair_text="", songs_html="", analyses=(_analysis("a", 120.0, 60.0), None))
        return panel

    def test_styles_are_grouped_and_old_saves_light_their_merged_button(self) -> None:
        panel = self._panel("legacy")
        self.assertTrue(panel.style_buttons["short_fade"].isChecked())  # plain crossfade -> "Crossfade"
        self.assertNotIn("legacy", panel.style_buttons)
        self.assertNotIn("cut", panel.style_buttons)
        self.assertTrue(self._panel("cut").style_buttons["downbeat_cut"].isChecked())
        self.assertFalse(self._panel("cut").length_box.isEnabled())
        self.assertEqual(sum(not label.isHidden() for label in panel.group_labels), 3)
        simple = self._panel("short_fade", advanced=False)
        shown = [style for style, button in simple.style_buttons.items() if not button.isHidden()]
        self.assertEqual(shown, ["auto", "short_fade", "vocal_safe_eq", "echo_out", "downbeat_cut",
                                 "beat_roll", "lowpass_out"])

    def test_only_the_chosen_effects_settings_show(self) -> None:
        echo = self._panel("echo_out")
        self.assertFalse(echo.echo_box.isHidden())
        self.assertTrue(echo.tape_box.isHidden())
        self.assertTrue(echo.echo_beat_buttons[1.0].isChecked())
        self.assertTrue(self._panel("short_fade").detail_box.isHidden())
        self.assertFalse(self._panel("tape_stop", advanced=False).tape_box.isHidden())  # simple mode too

    def test_picking_an_effect_gives_it_its_own_length(self) -> None:
        panel = self._panel("short_fade")
        emitted = []
        panel.changed.connect(emitted.append)
        panel.style_buttons["tape_stop"].click()
        self.assertEqual(emitted[-1], {"style": "tape_stop", "duration": 2.0})  # one 120 BPM bar
        panel.style_buttons["echo_out"].click()
        self.assertEqual(emitted[-1], {"style": "echo_out", "duration": 4.0})   # two bars


class FilterSweepSelectionTests(unittest.TestCase):
    def _select(self, out_energy: float, in_energy: float, duration: float = 16.0, previous=None):
        structures = dict(
            outgoing_structure=_structure("a", 200.0, out_energy, 0.5),
            incoming_structure=_structure("b", 200.0, in_energy, 0.5),
        )
        return select_transition_dsp(_candidate(duration=duration), _compatibility(), _quiet("a"), _quiet("b"),
                                     **structures, previous_style=previous).dsp

    def test_rising_energy_sweeps_up_falling_energy_blends(self) -> None:
        self.assertIs(self._select(0.3, 0.8), TransitionDsp.FILTER_SWEEP)
        self.assertIs(self._select(0.8, 0.3), TransitionDsp.FILTER_BLEND)
        self.assertIs(self._select(0.3, 0.8, duration=6.0), TransitionDsp.FILTER_BLEND)  # no room to sweep

    def test_a_filter_style_is_not_repeated_back_to_back(self) -> None:
        self.assertIs(self._select(0.3, 0.8, previous=TransitionDsp.FILTER_SWEEP), TransitionDsp.FILTER_BLEND)
        self.assertIs(self._select(0.8, 0.3, previous=TransitionDsp.FILTER_BLEND), TransitionDsp.LOWPASS_OUT)


class PhraseTests(unittest.TestCase):
    def test_phrases_follow_the_structure_sections(self) -> None:
        analysis = _analysis("a", 120.0, 120.0)  # 2 s bars
        self.assertEqual(phrase_starts(analysis)[:2], (0.0, 16.0))
        shifted = TrackStructureAnalysis(
            track_id="a", source_path="a.mp3", duration_seconds=120.0,
            sections=tuple(TrackSection(start, start + 16.0) for start in (4.0, 20.0, 36.0, 52.0)),
        )
        self.assertEqual(phrase_starts(analysis, shifted)[:2], (4.0, 20.0))
        self.assertEqual(len(phrase_starts(analysis)), len(analysis.downbeats[::PHRASE_BARS]))

    def test_peak_sections_are_the_loudest(self) -> None:
        structure = TrackStructureAnalysis(
            track_id="a", source_path="a.mp3", duration_seconds=60.0,
            sections=(TrackSection(0.0, 20.0, energy=0.3), TrackSection(20.0, 40.0, energy=0.9),
                      TrackSection(40.0, 60.0, energy=0.8)),
        )
        self.assertEqual([s.start_seconds for s in peak_sections(structure)], [20.0, 40.0])


class TimbreTests(unittest.TestCase):
    def test_drums_are_percussive_a_held_bass_note_is_not(self) -> None:
        rate = 22050
        t = np.arange(rate * 8) / rate
        pad = 0.3 * np.sin(2 * np.pi * 60 * t) + 0.05 * np.sin(2 * np.pi * 2000 * t)
        noise = np.random.default_rng(0).standard_normal(len(t))
        drums = noise * np.exp(-((t % 0.5) * 30.0)) * 0.5  # a noise hit every half second
        bass_pad, _bright, perc_pad = timbre_curves(pad.astype(np.float32), rate)
        _bass, bright_drums, perc_drums = timbre_curves(drums.astype(np.float32), rate)
        self.assertGreater(min(bass_pad[1:-1]), 0.8)
        self.assertLess(max(perc_pad[1:-1]), 0.2)
        self.assertGreater(min(perc_drums[1:-1]), 0.6)
        self.assertGreater(min(bright_drums[1:-1]), 0.5)


if __name__ == "__main__":
    unittest.main()
