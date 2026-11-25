from __future__ import annotations

import os
import struct
import subprocess
import threading
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from app.automix.renderer import (
    SAMPLE_RATE,
    AutoMixAudioPipeline,
    AutoMixRenderCancelled,
    AutoMixRenderError,
    _atempo_filters,
    band_fade_windows,
    build_filter_graph,
    ffmpeg_filter_names,
    ffmpeg_supports_transition_dsp,
    PreparedAudio,
)
from app.timeline.models import TransitionType
from app.timeline.render_plan import AudioRenderClip, AudioRenderPlan, AudioRenderTransition, TempoRamp, TransitionDsp
from app.utils.subprocess_utils import hidden_process_kwargs


def _clip(clip_id: str, track_id: str, start: float, source_out: float, *,
          source_in: float = 0.0, rate: float = 1.0, gain: float = 1.0) -> AudioRenderClip:
    return AudioRenderClip(
        clip_id=clip_id, track_id=track_id, timeline_start=start,
        source_in=source_in, source_out=source_out, playback_rate=rate, gain=gain,
    )


class AtempoFiltersTests(unittest.TestCase):
    def test_identity_rate_needs_no_filter(self) -> None:
        self.assertEqual(_atempo_filters(1.0), [])

    def test_in_range_rate_is_a_single_filter(self) -> None:
        self.assertEqual(_atempo_filters(1.5), ["atempo=1.500000"])

    def test_out_of_range_rate_is_decomposed_within_bounds(self) -> None:
        filters = _atempo_filters(3.0)
        self.assertGreater(len(filters), 1)
        product = 1.0
        for entry in filters:
            factor = float(entry.split("=")[1])
            self.assertGreaterEqual(factor, 0.5)
            self.assertLessEqual(factor, 2.0)
            product *= factor
        self.assertAlmostEqual(product, 3.0, places=4)

    def test_very_slow_rate_is_decomposed_within_bounds(self) -> None:
        filters = _atempo_filters(0.2)
        product = 1.0
        for entry in filters:
            factor = float(entry.split("=")[1])
            self.assertGreaterEqual(factor, 0.5)
            self.assertLessEqual(factor, 2.0)
            product *= factor
        self.assertAlmostEqual(product, 0.2, places=4)


class BuildFilterGraphTests(unittest.TestCase):
    def test_empty_clips_raises(self) -> None:
        with self.assertRaises(AutoMixRenderError):
            build_filter_graph([], [])

    def test_single_clip_has_no_concat_or_crossfade(self) -> None:
        graph, label = build_filter_graph([_clip("a", "a", 0.0, 60.0)], [])
        self.assertEqual(label, "c0")
        self.assertIn("[0:a]atrim=start=0.000000:end=60.000000", graph)
        self.assertNotIn("concat", graph)
        self.assertNotIn("acrossfade", graph)

    def test_adjacent_clips_with_no_transition_use_concat(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 60.0, 60.0)]
        graph, label = build_filter_graph(clips, [])
        self.assertIn("concat=n=2:v=0:a=1", graph)
        self.assertNotIn("acrossfade", graph)
        self.assertEqual(label, "m1")

    def test_gap_between_clips_inserts_silence(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 65.0, 60.0)]
        graph, _label = build_filter_graph(clips, [])
        self.assertIn(f"anullsrc=r={SAMPLE_RATE}:cl=stereo:d=5.000000", graph)
        self.assertEqual(graph.count("concat=n=2:v=0:a=1"), 2)

    def test_transition_uses_acrossfade_with_matching_duration(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0, source_in=8.0)]
        transitions = [AudioRenderTransition(
            clip_a="a", clip_b="b", timeline_start=52.0, duration=8.0, type=TransitionType.EQUAL_POWER,
        )]
        graph, _label = build_filter_graph(clips, transitions)
        self.assertIn("acrossfade=d=8.000000:curve1=qsin:curve2=qsin", graph)
        self.assertNotIn("concat", graph)

    def test_crossfade_type_uses_linear_curve(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 57.0, 60.0)]
        transitions = [AudioRenderTransition(
            clip_a="a", clip_b="b", timeline_start=57.0, duration=3.0, type=TransitionType.CROSSFADE,
        )]
        graph, _label = build_filter_graph(clips, transitions)
        self.assertIn("curve1=tri:curve2=tri", graph)

    def test_beat_match_fallback_uses_equal_power_style_curve(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0)]
        transitions = [AudioRenderTransition(
            clip_a="a", clip_b="b", timeline_start=52.0, duration=8.0, type=TransitionType.BEAT_MATCH,
        )]
        graph, _label = build_filter_graph(clips, transitions, transition_dsp=False)
        self.assertIn("acrossfade=d=8.000000:curve1=qsin:curve2=qsin", graph)
        self.assertNotIn("acrossover", graph)

    def test_rate_and_gain_are_applied_per_clip(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0, rate=1.2, gain=0.5)]
        graph, _label = build_filter_graph(clips, [])
        self.assertIn("atempo=1.200000", graph)
        self.assertIn("volume=0.500000", graph)

    def test_three_clip_chain_folds_left_to_right(self) -> None:
        clips = [
            _clip("a", "a", 0.0, 60.0),
            _clip("b", "b", 52.0, 60.0),
            _clip("c", "c", 108.0, 60.0),
        ]
        transitions = [
            AudioRenderTransition("a", "b", 52.0, 8.0, TransitionType.EQUAL_POWER),
            AudioRenderTransition("b", "c", 108.0, 4.0, TransitionType.EQUAL_POWER),
        ]
        graph, label = build_filter_graph(clips, transitions)
        self.assertEqual(graph.count("acrossfade"), 2)
        self.assertEqual(label, "m2")


def _beat_match(clip_a: str, clip_b: str, start: float, duration: float) -> AudioRenderTransition:
    return AudioRenderTransition(clip_a, clip_b, start, duration, TransitionType.BEAT_MATCH)


class BassSwapGraphTests(unittest.TestCase):
    def test_beat_match_uses_band_split_dsp_not_plain_qsin(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0)]
        graph, label = build_filter_graph(clips, [_beat_match("a", "b", 52.0, 8.0)])
        self.assertEqual(graph.count("acrossover=split=200 2500"), 2)
        self.assertNotIn("qsin:curve2=qsin", graph)
        self.assertIn("acrossfade=d=8.000000:curve1=nofade:curve2=nofade", graph)
        self.assertIn("alimiter=", graph)
        self.assertEqual(label, "m1")

    def test_equal_power_and_crossfade_graphs_are_unchanged(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0), _clip("c", "c", 108.0, 60.0)]
        transitions = [
            AudioRenderTransition("a", "b", 52.0, 8.0, TransitionType.EQUAL_POWER),
            AudioRenderTransition("b", "c", 108.0, 4.0, TransitionType.CROSSFADE),
        ]
        graph, _label = build_filter_graph(clips, transitions)
        chain = "atrim=start=0.000000:end=60.000000,asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo"
        self.assertEqual(graph, ";".join([
            f"[0:a]{chain}[c0]", f"[1:a]{chain}[c1]", f"[2:a]{chain}[c2]",
            "[c0][c1]acrossfade=d=8.000000:curve1=qsin:curve2=qsin[m1]",
            "[m1][c2]acrossfade=d=4.000000:curve1=tri:curve2=tri[m2]",
        ]))

    def test_envelopes_and_limiter_window_use_the_transition_geometry(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0)]
        graph, _label = build_filter_graph(clips, [_beat_match("a", "b", 52.0, 8.0)])
        # Outgoing: mid/high fade across the whole last 8 s; low hands off at 35%-60%.
        self.assertIn("[c0mid]afade=t=out:st=52.000000:d=8.000000:curve=qsin", graph)
        self.assertIn("[c0low]afade=t=out:st=54.800000:d=2.000000:curve=qsin", graph)
        self.assertIn("[c1mid]afade=t=in:st=0.000000:d=8.000000:curve=qsin", graph)
        self.assertIn("[c1low]afade=t=in:st=2.800000:d=2.000000:curve=qsin", graph)
        # The window cut by sample count (52 s and 60 s at 48 kHz).
        self.assertIn("[m1a]atrim=end_sample=2496000[m1pre]", graph)
        self.assertIn("[m1b]atrim=start_sample=2496000:end_sample=2880000", graph)
        self.assertIn("[m1c]atrim=start_sample=2880000", graph)

    def test_fade_windows_scale_with_min_and_max_transition_lengths(self) -> None:
        for duration in (2.0, 20.0):
            with self.subTest(duration=duration):
                low_in, = band_fade_windows("low", 60.0, (TransitionDsp.BASS_SWAP, duration, None), None)
                self.assertEqual(low_in[0], "in")
                self.assertAlmostEqual(low_in[1], 0.35 * duration)
                self.assertAlmostEqual(low_in[2], 0.25 * duration)
                self.assertGreater(low_in[2], 0.0)
                mid_out, = band_fade_windows("mid", 60.0, None, (TransitionDsp.BASS_SWAP, duration, None))
                self.assertEqual(mid_out, ("out", 60.0 - duration, duration))

    def test_vocal_handoff_moves_only_the_vocal_safe_mid_swap(self) -> None:
        side = (TransitionDsp.VOCAL_SAFE_EQ, 8.0, 0.75)
        mid_in, = band_fade_windows("mid", 60.0, side, None)
        self.assertEqual(mid_in, ("in", 5.0, 2.0))  # 0.625-0.875 of the window
        mid_out, = band_fade_windows("mid", 60.0, None, side)
        self.assertEqual(mid_out, ("out", 57.0, 2.0))
        default_low, = band_fade_windows("low", 60.0, (TransitionDsp.VOCAL_SAFE_EQ, 8.0, None), None)
        self.assertEqual(band_fade_windows("low", 60.0, side, None), [default_low])  # lows keep their swap
        default_mid, = band_fade_windows("mid", 60.0, (TransitionDsp.VOCAL_SAFE_EQ, 8.0, None), None)
        self.assertEqual(default_mid, ("in", 3.2, 2.0))  # unchanged 0.40-0.65 default

    def test_tempo_adjusted_clips_keep_timeline_domain_durations(self) -> None:
        # 63.6 s of source at 1.06x = 60 s of timeline; windows must use the latter.
        clips = [_clip("a", "a", 0.0, 63.6, rate=1.06), _clip("b", "b", 52.0, 56.4, rate=0.94)]
        graph, _label = build_filter_graph(clips, [_beat_match("a", "b", 52.0, 8.0)])
        self.assertIn("[c0mid]afade=t=out:st=52.000000:d=8.000000", graph)
        self.assertIn("acrossfade=d=8.000000:curve1=nofade", graph)
        self.assertLess(graph.index("atempo=1.060000"), graph.index("[c0pre]acrossover"))

    def test_leading_silence_shifts_the_limiter_window_but_not_the_clip_envelopes(self) -> None:
        clips = [_clip("a", "a", 3.0, 60.0), _clip("b", "b", 55.0, 60.0)]
        graph, _label = build_filter_graph(clips, [_beat_match("a", "b", 55.0, 8.0)])
        self.assertIn("anullsrc=r=48000:cl=stereo:d=3.000000[lead]", graph)
        self.assertIn("[c0mid]afade=t=out:st=52.000000:d=8.000000", graph)  # clip-local
        self.assertIn("[leading][c1]acrossfade=d=8.000000:curve1=nofade", graph)
        self.assertIn("[m1b]atrim=start_sample=2640000:end_sample=3024000", graph)  # global timeline, 55-63 s

    def test_chained_beat_matches_envelope_the_middle_clip_on_both_ends(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0), _clip("c", "c", 106.0, 60.0)]
        transitions = [_beat_match("a", "b", 52.0, 8.0), _beat_match("b", "c", 106.0, 6.0)]
        graph, label = build_filter_graph(clips, transitions)
        self.assertIn("[c1mid]afade=t=in:st=0.000000:d=8.000000:curve=qsin,"
                      "afade=t=out:st=54.000000:d=6.000000:curve=qsin[c1mide]", graph)
        self.assertIn("[m1][c2]acrossfade=d=6.000000:curve1=nofade", graph)
        self.assertIn("[m2a]atrim=end_sample=5088000[m2pre]", graph)  # 106 s
        self.assertIn("[m2b]atrim=start_sample=5088000:end_sample=5376000", graph)
        self.assertEqual(label, "m2")

    def test_beat_match_next_to_a_cut_only_processes_the_participating_clips(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 60.0, 60.0), _clip("c", "c", 112.0, 60.0)]
        graph, _label = build_filter_graph(clips, [_beat_match("b", "c", 112.0, 8.0)])
        self.assertNotIn("[c0pre]", graph)
        self.assertIn("[c0][c1]concat=n=2:v=0:a=1[m1]", graph)
        self.assertIn("[c1low]afade=t=out:st=54.800000:d=2.000000:curve=qsin[c1lowe]", graph)
        self.assertIn("[m1][c2]acrossfade=d=8.000000:curve1=nofade", graph)


def _styled(clip_a: str, clip_b: str, start: float, duration: float, dsp: TransitionDsp | None,
            kind: TransitionType = TransitionType.BEAT_MATCH) -> AudioRenderTransition:
    return AudioRenderTransition(clip_a, clip_b, start, duration, kind, dsp)


class TransitionStyleGraphTests(unittest.TestCase):
    def test_dj_effect_graphs_loop_samples_and_command_the_lowpass(self) -> None:
        from app.automix.renderer import roll_slice_seconds

        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 56.0, 60.0)]
        roll = replace(_styled("a", "b", 56.0, 4.0, TransitionDsp.BEAT_ROLL),
                       beat_seconds=0.5, roll_beats=0.25)
        graph, _ = build_filter_graph(clips, [roll])
        self.assertIn("aloop=loop=31:size=6000", graph)
        self.assertIn("atrim=end_sample=192000", graph)
        self.assertIn("curve1=nofade:curve2=qsin", graph)
        lowpass = replace(roll, dsp=TransitionDsp.LOWPASS_OUT, filter_cutoff_hz=350.0)
        graph, _ = build_filter_graph(clips, [lowpass])
        self.assertIn("lowpass@close0=f=18000", graph)
        self.assertIn("lowpass@close0 f 350.00", graph)
        self.assertNotIn("aloop", graph)
        fallback, _ = build_filter_graph(clips, [roll], transition_dsp=False)
        self.assertNotIn("aloop", fallback)
        self.assertEqual(roll_slice_seconds(replace(roll, beat_seconds=3.0, roll_beats=1.0)), 3.0)
        self.assertEqual(roll_slice_seconds(replace(roll, duration=0.05)), 0.05)

    def _pair_graph(self, dsp: TransitionDsp | None, kind: TransitionType = TransitionType.BEAT_MATCH) -> str:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0)]
        return build_filter_graph(clips, [_styled("a", "b", 52.0, 8.0, dsp, kind)])[0]

    def test_explicit_bass_swap_matches_the_phase1_default(self) -> None:
        self.assertEqual(self._pair_graph(TransitionDsp.BASS_SWAP), self._pair_graph(None))

    def test_vocal_safe_swaps_mids_just_after_the_bass(self) -> None:
        graph = self._pair_graph(TransitionDsp.VOCAL_SAFE_EQ)
        self.assertIn("[c0low]afade=t=out:st=54.800000:d=2.000000", graph)
        self.assertIn("[c0mid]afade=t=out:st=55.200000:d=2.000000", graph)   # 40%-65%
        self.assertIn("[c1mid]afade=t=in:st=3.200000:d=2.000000", graph)
        self.assertIn("[c0high]afade=t=out:st=52.000000:d=8.000000", graph)  # full window
        self.assertIn("curve1=nofade", graph)

    def test_filter_blend_staggers_bands_asymmetrically(self) -> None:
        graph = self._pair_graph(TransitionDsp.FILTER_BLEND)
        self.assertIn("[c0low]afade=t=out:st=55.360000:d=0.960000", graph)   # short staggered swap,
        self.assertIn("[c1low]afade=t=in:st=3.680000:d=0.960000", graph)     # outgoing leads by 4%
        self.assertIn("[c0high]afade=t=out:st=54.800000:d=5.200000", graph)  # out highs linger
        self.assertIn("[c1high]afade=t=in:st=0.000000:d=5.200000", graph)    # in highs arrive first
        self.assertIn("[c1mid]afade=t=in:st=2.000000:d=4.800000", graph)

    def test_short_fade_is_full_band_qsin_with_the_window_limiter(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 57.0, 60.0)]
        graph, _label = build_filter_graph(clips, [_styled("a", "b", 57.0, 3.0, TransitionDsp.SHORT_FADE)])
        self.assertNotIn("acrossover", graph)
        self.assertIn("[c0][c1]acrossfade=d=3.000000:curve1=qsin:curve2=qsin,asetpts=N/SR/TB[m1sum]", graph)
        self.assertIn("[m1b]atrim=start_sample=2736000:end_sample=2880000,asetpts=PTS-STARTPTS,"
                      "asetnsamples=n=1024:p=0,apad=pad_len=4800,alimiter=", graph)

    def test_drop_in_fades_only_the_outgoing_side_behind_a_click_guard(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 57.0, 60.0)]
        graph, _label = build_filter_graph(clips, [_styled("a", "b", 57.0, 3.0, TransitionDsp.DROP_IN)])
        self.assertNotIn("acrossover", graph)
        self.assertIn("[c1]afade=t=in:d=0.02[m1attack]", graph)
        self.assertIn("[c0][m1attack]acrossfade=d=3.000000:curve1=qsin:curve2=nofade,asetpts=N/SR/TB[m1sum]", graph)
        self.assertIn("alimiter=", graph)

    def test_echo_out_echoes_the_windows_first_beat_under_a_full_level_incoming_track(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 56.0, 60.0)]
        transition = AudioRenderTransition("a", "b", 56.0, 4.0, TransitionType.EQUAL_POWER,
                                           TransitionDsp.ECHO_OUT, beat_seconds=0.5)
        graph, _label = build_filter_graph(clips, [transition])
        # The first beat (the last hit) plays dry, then the dry track is gone within half a beat...
        self.assertIn("[c0dry]afade=t=out:st=56.500000:d=0.250000:curve=qsin", graph)
        self.assertIn("[c0wetsrc]afade=t=in:st=56.000000:d=0.01", graph)  # ...and that hit feeds the echo
        self.assertIn("aecho=in_gain=0:out_gain=1:delays=500.000|1000.000|1500.000|2000.000", graph)
        self.assertIn("highpass=f=200", graph)
        self.assertIn("[c1]afade=t=in:d=0.02[m1attack]", graph)
        self.assertIn("[c0][m1attack]acrossfade=d=4.000000:curve1=nofade:curve2=nofade,asetpts=N/SR/TB[m1sum]", graph)

    def test_tape_stop_slows_the_window_with_the_stretcher_and_delays_the_entry(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 58.0, 60.0)]
        transition = _styled("a", "b", 58.0, 2.0, TransitionDsp.TAPE_STOP, TransitionType.EQUAL_POWER)
        graph, _label = build_filter_graph(clips, [transition])
        self.assertIn(f"[c0head]atrim=end_sample={58 * SAMPLE_RATE}", graph)
        self.assertIn("rubberband@stop0=tempo=1:pitch=1", graph)
        self.assertIn("rubberband@stop0 tempo", graph)
        self.assertIn(f"atrim=end_sample={2 * SAMPLE_RATE}[c0t]", graph)
        self.assertIn("[c1]afade=t=in:st=1.200000:d=0.02[m1attack]", graph)
        # Without a command-driven stretcher it is a plain fade, same timing.
        plain, _label = build_filter_graph(clips, [transition], ramp_filter=None)
        self.assertNotIn("rubberband", plain)
        self.assertIn("[c0body]afade=t=out:st=59.100000:d=0.900000:curve=qsin[c0]", plain)

    def test_tape_stop_schedule_slows_monotonically_and_consumes_less_input(self) -> None:
        from app.automix.renderer import TAPE_STOP_MIN_RATE, tape_stop_schedule

        schedule = tape_stop_schedule(2.0)
        rates = [rate for _second, rate in schedule]
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertAlmostEqual(rates[-1], TAPE_STOP_MIN_RATE, places=6)
        self.assertLess(schedule[-1][0], 2.0)  # a slowing platter plays less than the window's source

    def test_downbeat_cut_is_a_millisecond_equal_power_splice(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 59.95, 60.0)]
        graph, _label = build_filter_graph(
            clips, [_styled("a", "b", 59.95, 0.05, TransitionDsp.DOWNBEAT_CUT, TransitionType.EQUAL_POWER)])
        self.assertIn("[c0][c1]acrossfade=d=0.050000:curve1=qsin:curve2=qsin,asetpts=N/SR/TB[m1sum]", graph)

    def test_planner_styles_apply_to_equal_power_transitions_too(self) -> None:
        graph = self._pair_graph(TransitionDsp.FILTER_BLEND, TransitionType.EQUAL_POWER)
        self.assertIn("acrossover", graph)
        self.assertNotIn("qsin:curve2=qsin", graph)
        # ...while an unstyled EQUAL_POWER stays the exact legacy acrossfade.
        self.assertNotIn("acrossover", self._pair_graph(None, TransitionType.EQUAL_POWER))

    def test_mixed_chain_envelopes_each_side_with_its_own_style(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0),
                 _clip("c", "c", 106.0, 60.0), _clip("d", "d", 163.0, 60.0)]
        transitions = [
            _styled("a", "b", 52.0, 8.0, TransitionDsp.BASS_SWAP),
            _styled("b", "c", 106.0, 6.0, TransitionDsp.FILTER_BLEND),
            _styled("c", "d", 163.0, 3.0, TransitionDsp.SHORT_FADE),
        ]
        graph, label = build_filter_graph(clips, transitions)
        # b: bass-swap head, filter-blend tail.
        self.assertIn("[c1low]afade=t=in:st=2.800000:d=2.000000:curve=qsin,"
                      "afade=t=out:st=56.520000:d=0.720000:curve=qsin[c1lowe]", graph)
        # c: filter-blend head; its SHORT_FADE tail adds no band fade.
        self.assertIn("[c2low]afade=t=in:st=2.760000:d=0.720000:curve=qsin[c2lowe]", graph)
        self.assertNotIn("[c3pre]", graph)  # d only touches SHORT_FADE: no crossover
        self.assertIn("[m2][c3]acrossfade=d=3.000000:curve1=qsin:curve2=qsin,asetpts=N/SR/TB[m3sum]", graph)
        self.assertEqual(graph.count("alimiter="), 3)
        self.assertEqual(label, "m3")

    def test_fallback_renders_every_style_as_the_legacy_type_curve(self) -> None:
        clips = [_clip("a", "a", 0.0, 60.0), _clip("b", "b", 52.0, 60.0), _clip("c", "c", 106.0, 60.0)]
        transitions = [
            _styled("a", "b", 52.0, 8.0, TransitionDsp.VOCAL_SAFE_EQ),
            _styled("b", "c", 106.0, 6.0, TransitionDsp.FILTER_BLEND, TransitionType.EQUAL_POWER),
        ]
        graph, _label = build_filter_graph(clips, transitions, transition_dsp=False)
        self.assertNotIn("acrossover", graph)
        self.assertNotIn("alimiter", graph)
        self.assertIn("[c0][c1]acrossfade=d=8.000000:curve1=qsin:curve2=qsin[m1]", graph)
        self.assertIn("[m1][c2]acrossfade=d=6.000000:curve1=qsin:curve2=qsin[m2]", graph)


class BassSwapFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory(prefix="automix-fallback-")
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        for name in ("a", "b"):
            (self.directory / f"{name}.wav").write_bytes(b"\x00")  # never read; _run is mocked
        self.paths = {name: str(self.directory / f"{name}.wav") for name in ("a", "b")}
        self.plan = AudioRenderPlan(
            clips=(_clip("a", "a", 0.0, 20.0), _clip("b", "b", 12.0, 20.0)),
            transitions=(_beat_match("a", "b", 12.0, 8.0),),
        )

    def _render(self, pipeline: AutoMixAudioPipeline, fake_run, *, supported: bool = True) -> PreparedAudio:
        with (
            patch("app.automix.renderer.ffmpeg_supports_transition_dsp", return_value=supported),
            patch.object(pipeline, "_run", fake_run),
            patch.object(pipeline, "_probe_duration", return_value=32.0),
        ):
            return pipeline.render(self.plan, self.paths, self.directory / "out")

    def test_missing_filters_render_beat_match_as_qsin(self) -> None:
        pipeline = AutoMixAudioPipeline(Path("ffmpeg"))
        graphs: list[str] = []
        self._render(pipeline, lambda args, *_: graphs.append(args[args.index("-filter_complex") + 1]),
                     supported=False)
        self.assertEqual(len(graphs), 1)
        self.assertNotIn("acrossover", graphs[0])
        self.assertIn("acrossfade=d=8.000000:curve1=qsin:curve2=qsin", graphs[0])

    def test_dsp_render_failure_retries_once_with_qsin_and_same_timing(self) -> None:
        pipeline = AutoMixAudioPipeline(Path("ffmpeg"))
        graphs: list[str] = []
        output_path =self.directory / "out" / "automix_mix.nut"

        def fake_run(args, *_):
            graphs.append(args[args.index("-filter_complex") + 1])
            if len(graphs) == 1:
                output_path.write_bytes(b"partial")
                raise AutoMixRenderError("No such filter: 'acrossover'")
            self.assertFalse(output_path.exists())  # partial DSP output removed before retry

        rendered = self._render(pipeline, fake_run)
        self.assertEqual(len(graphs), 2)
        self.assertIn("acrossover", graphs[0])
        self.assertNotIn("acrossover", graphs[1])
        self.assertIn("acrossfade=d=8.000000:curve1=qsin", graphs[1])
        transition = rendered.audio_plan.transitions[0]
        self.assertIsNone(transition.dsp)
        self.assertIs(transition.type, TransitionType.EQUAL_POWER)
        self.assertTrue(dict(transition.details)["dsp_fallback"])
        self.assertEqual((transition.timeline_start, transition.duration), (12.0, 8.0))

    def test_missing_rubberband_records_the_actual_pitch_and_tape_fallback(self) -> None:
        edited = replace(self.plan.clips[0], tempo_ramp=TempoRamp(2.0, 12.0, 1.0, end_pitch=0.94))
        transition = replace(self.plan.transitions[0], dsp=TransitionDsp.TAPE_STOP,
                             details=(("key_shift_semitones", -1),))
        self.plan = AudioRenderPlan((edited, self.plan.clips[1]), (transition,))
        with patch("app.automix.renderer.ffmpeg_filter_names", return_value=frozenset()):
            rendered = self._render(AutoMixAudioPipeline(Path("ffmpeg")), lambda *_: None)
        self.assertEqual(rendered.audio_plan.clips[0].tempo_ramp.end_pitch, 1.0)
        transition = rendered.audio_plan.transitions[0]
        self.assertIs(transition.dsp, TransitionDsp.SHORT_FADE)
        self.assertIsNone(dict(transition.details)["key_shift_semitones"])
        self.assertTrue(dict(transition.details)["pitch_unavailable"])

    def test_cancellation_is_not_retried(self) -> None:
        pipeline = AutoMixAudioPipeline(Path("ffmpeg"))
        calls: list[int] = []

        def fake_run(*_):
            calls.append(1)
            raise AutoMixRenderCancelled("cancelled")

        with self.assertRaises(AutoMixRenderCancelled):
            self._render(pipeline, fake_run)
        self.assertEqual(len(calls), 1)

    def test_capability_probe_reads_the_filter_list(self) -> None:
        listing = "\n".join(f" .. {name}  A->A  x" for name in ("acrossover", "afade", "amix", "alimiter",
                                                               "asplit", "acrossfade"))
        ffmpeg_filter_names.cache_clear()
        self.addCleanup(ffmpeg_filter_names.cache_clear)
        with patch("app.automix.renderer.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout=listing)):
            self.assertTrue(ffmpeg_supports_transition_dsp("ffmpeg-full"))
        with patch("app.automix.renderer.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout=listing.replace("acrossover", "x"))):
            self.assertFalse(ffmpeg_supports_transition_dsp("ffmpeg-no-crossover"))
        with patch("app.automix.renderer.subprocess.run", side_effect=OSError("missing")):
            self.assertFalse(ffmpeg_supports_transition_dsp("ffmpeg-missing"))


class AutoMixMixIntermediateCodecTests(unittest.TestCase):
    """The intermediate mix must be lossless, not a second lossy AAC pass.

    prepare_playlist_audio() (app/renderer/ffmpeg_renderer.py) already
    re-encodes to AAC exactly once, after loudness normalization. If this
    intermediate render also encoded to AAC, every AutoMix/crossfade export
    would be lossy-to-lossy double-encoded regardless of the final bitrate
    the user picked.
    """

    def test_render_command_uses_pcm_in_nut_not_aac(self) -> None:
        with TemporaryDirectory(prefix="automix-codec-") as directory:
            directory = Path(directory)
            source = directory / "a.wav"
            source.write_bytes(b"\x00")  # never read; _run is mocked below
            plan = AudioRenderPlan(clips=(_clip("a", "a", 0.0, 10.0),), transitions=())
            pipeline = AutoMixAudioPipeline(Path("ffmpeg"))
            captured: list[list[str]] = []

            def fake_run(arguments, _cancel_event, _on_progress_seconds) -> None:
                captured.append(arguments)

            with (
                patch.object(pipeline, "_run", fake_run),
                patch.object(pipeline, "_probe_duration", return_value=10.0),
            ):
                result = pipeline.render(plan, {"a": str(source)}, directory)

        command = captured[0]
        self.assertNotIn("aac", command)
        self.assertNotIn("192k", command)
        self.assertIn("pcm_f32le", command)
        self.assertIn("nut", command)
        self.assertEqual(result.path.suffix, ".nut")


def _write_tone_wav(path: Path, frequency: float, duration: float, sample_rate: int = SAMPLE_RATE) -> None:
    t = np.linspace(0.0, duration, int(duration * sample_rate), endpoint=False)
    signal = 0.5 * np.sin(2 * np.pi * frequency * t)
    pcm16 = (signal * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(pcm16)}h", *pcm16.tolist()))


@unittest.skipUnless(
    os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(),
    "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg AutoMix render checks.",
)
class RealAutoMixRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        self._directory = TemporaryDirectory(prefix="automix-render-")
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def _pipeline(self) -> AutoMixAudioPipeline:
        return AutoMixAudioPipeline(self.executable)

    def test_two_track_crossfade_render_has_the_expected_duration(self) -> None:
        a = self.directory / "a.wav"
        b = self.directory / "b.wav"
        _write_tone_wav(a, 440.0, 20.0)
        _write_tone_wav(b, 660.0, 20.0)
        clips = (_clip("a", "a", 0.0, 20.0), _clip("b", "b", 16.0, 20.0))
        transitions = (AudioRenderTransition("a", "b", 16.0, 4.0, TransitionType.EQUAL_POWER),)
        plan = AudioRenderPlan(clips=clips, transitions=transitions)
        result = self._pipeline().render(plan, {"a": str(a), "b": str(b)}, self.directory / "out")
        self.assertTrue(result.path.is_file())
        self.assertAlmostEqual(result.duration_seconds, 36.0, delta=0.2)

    def test_three_track_cumulative_render_duration(self) -> None:
        paths = {}
        for track_id, freq in (("a", 440.0), ("b", 550.0), ("c", 660.0)):
            path = self.directory / f"{track_id}.wav"
            _write_tone_wav(path, freq, 20.0)
            paths[track_id] = str(path)
        clips = (
            _clip("a", "a", 0.0, 20.0),
            _clip("b", "b", 16.0, 20.0),
            _clip("c", "c", 33.0, 20.0),
        )
        transitions = (
            AudioRenderTransition("a", "b", 16.0, 4.0, TransitionType.EQUAL_POWER),
            AudioRenderTransition("b", "c", 33.0, 3.0, TransitionType.CROSSFADE),
        )
        plan = AudioRenderPlan(clips=clips, transitions=transitions)
        result = self._pipeline().render(plan, paths, self.directory / "out")
        self.assertAlmostEqual(result.duration_seconds, 53.0, delta=0.2)

    def test_a_long_dsp_playlist_renders_with_its_dsp_despite_the_command_line_limit(self) -> None:
        count = 40  # the band-DSP graph for this many tracks exceeds Windows' 32,767-character command line
        paths, clips, transitions = {}, [], []
        for index in range(count):
            track_id = f"t{index}"
            paths[track_id] = str(self.directory / f"{track_id}.wav")
            _write_tone_wav(Path(paths[track_id]), 220.0 + 10 * index, 12.0)
            clips.append(_clip(track_id, track_id, 8.0 * index, 12.0))
            if index:
                transitions.append(AudioRenderTransition(
                    f"t{index - 1}", track_id, 8.0 * index, 4.0, TransitionType.BEAT_MATCH, dsp=TransitionDsp.BASS_SWAP,
                ))
        plan = AudioRenderPlan(clips=tuple(clips), transitions=tuple(transitions))
        self.assertGreater(len(build_filter_graph(plan.clips, plan.transitions)[0]), 32767)
        with self.assertNoLogs("app.automix.renderer", level="WARNING"):  # no legacy-crossfade fallback
            result = self._pipeline().render(plan, paths, self.directory / "out")
        self.assertAlmostEqual(result.duration_seconds, 8.0 * (count - 1) + 12.0, delta=0.2)
        self.assertTrue((self.directory / "out" / "automix_graph.txt").is_file())

    def test_moved_vocal_handoff_keeps_exact_duration_and_headroom_at_a_changed_rate(self) -> None:
        a, b = self.directory / "a.wav", self.directory / "b.wav"
        _write_tone_wav(a, 440.0, 20.0)
        _write_tone_wav(b, 660.0, 22.0)
        clips = (_clip("a", "a", 0.0, 20.0), _clip("b", "b", 12.0, 22.0, rate=1.1))
        transition = AudioRenderTransition("a", "b", 12.0, 8.0, TransitionType.BEAT_MATCH,
                                           dsp=TransitionDsp.VOCAL_SAFE_EQ, vocal_handoff=0.75)
        result = self._pipeline().render(AudioRenderPlan(clips, (transition,)), {"a": str(a), "b": str(b)},
                                         self.directory / "out", container="flac")
        self.assertAlmostEqual(result.duration_seconds, 12.0 + 22.0 / 1.1, delta=0.05)
        raw = subprocess.run([str(self.executable), "-v", "error", "-i", str(result.path), "-f", "f32le", "pipe:1"],
                             capture_output=True, check=True).stdout
        self.assertLessEqual(float(np.max(np.abs(np.frombuffer(raw, dtype=np.float32)))), 0.98)

    def test_explicit_gap_produces_matching_total_duration(self) -> None:
        a = self.directory / "a.wav"
        b = self.directory / "b.wav"
        _write_tone_wav(a, 440.0, 10.0)
        _write_tone_wav(b, 440.0, 10.0)
        clips = (_clip("a", "a", 0.0, 10.0), _clip("b", "b", 15.0, 10.0))
        plan = AudioRenderPlan(clips=clips, transitions=())
        result = self._pipeline().render(plan, {"a": str(a), "b": str(b)}, self.directory / "out")
        self.assertAlmostEqual(result.duration_seconds, 25.0, delta=0.2)

    def test_tempo_adjusted_clip_renders_the_expected_shorter_duration(self) -> None:
        a = self.directory / "a.wav"
        _write_tone_wav(a, 440.0, 20.0)
        clip = _clip("a", "a", 0.0, 20.0, rate=1.25)
        plan = AudioRenderPlan(clips=(clip,), transitions=())
        result = self._pipeline().render(plan, {"a": str(a)}, self.directory / "out")
        self.assertAlmostEqual(result.duration_seconds, 16.0, delta=0.2)

    def test_cancellation_removes_incomplete_output(self) -> None:
        a = self.directory / "a.wav"
        b = self.directory / "b.wav"
        _write_tone_wav(a, 440.0, 20.0)
        _write_tone_wav(b, 440.0, 20.0)
        clips = (_clip("a", "a", 0.0, 20.0), _clip("b", "b", 16.0, 20.0))
        transitions = (AudioRenderTransition("a", "b", 16.0, 4.0, TransitionType.EQUAL_POWER),)
        plan = AudioRenderPlan(clips=clips, transitions=transitions)
        cancel_event = threading.Event()
        cancel_event.set()
        output_directory = self.directory / "out"
        with self.assertRaises(AutoMixRenderCancelled):
            self._pipeline().render(plan, {"a": str(a), "b": str(b)}, output_directory, cancel_event=cancel_event)
        self.assertFalse((output_directory / "automix_mix.nut").exists())

    def test_equal_power_crossfade_has_no_volume_hole_at_the_midpoint(self) -> None:
        a = self.directory / "a.wav"
        b = self.directory / "b.wav"
        _write_tone_wav(a, 440.0, 20.0)
        _write_tone_wav(b, 440.0, 20.0)
        clips = (_clip("a", "a", 0.0, 20.0), _clip("b", "b", 16.0, 20.0))
        transitions = (AudioRenderTransition("a", "b", 16.0, 4.0, TransitionType.EQUAL_POWER),)
        plan = AudioRenderPlan(clips=clips, transitions=transitions)
        result = self._pipeline().render(plan, {"a": str(a), "b": str(b)}, self.directory / "out")

        pcm_process = subprocess.run(
            [str(self.executable), "-hide_banner", "-loglevel", "error", "-i", str(result.path),
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
            capture_output=True, **hidden_process_kwargs(),
        )
        signal = np.frombuffer(pcm_process.stdout, dtype=np.float32)

        def rms(start_seconds: float, end_seconds: float) -> float:
            segment = signal[int(start_seconds * SAMPLE_RATE):int(end_seconds * SAMPLE_RATE)]
            return float(np.sqrt(np.mean(np.square(segment)))) if len(segment) else 0.0

        steady_state = rms(2.0, 10.0)
        midpoint = rms(17.9, 18.1)
        # A true volume hole would sound like a dip toward silence at the
        # crossfade center; equal-power curves keep it close to full level.
        self.assertGreater(midpoint, steady_state * 0.6)


def _write_stereo_wav(path: Path, signal: np.ndarray) -> None:
    """``signal`` is (samples, 2) float in [-1, 1]."""
    pcm16 = (np.clip(signal, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm16.tobytes())


def _tones(components: list[tuple[float, float]], duration: float) -> np.ndarray:
    t = np.arange(int(duration * SAMPLE_RATE)) / SAMPLE_RATE
    mono = sum(amplitude * np.sin(2 * np.pi * frequency * t) for frequency, amplitude in components)
    return np.stack([mono, 0.8 * mono], axis=1)  # unequal channels: stereo must survive


def _spectrum_level(segment: np.ndarray, frequency: float) -> float:
    window = np.hanning(len(segment))
    spectrum = np.abs(np.fft.rfft(segment * window)) * 2.0 / window.sum()  # tone amplitude, length-independent
    frequencies = np.fft.rfftfreq(len(segment), 1.0 / SAMPLE_RATE)
    return float(spectrum[np.argmin(np.abs(frequencies - frequency))])


@unittest.skipUnless(
    os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(),
    "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg bass-swap checks.",
)
class RealBassSwapRenderTests(unittest.TestCase):
    def test_dj_effect_auditions_match_the_full_mix_samples(self) -> None:
        from types import SimpleNamespace
        from app.automix.progressive import render_window

        paths = {"a": self._source("a", _tones([(100, 0.1), (2000, 0.1)], 8.0)),
                 "b": self._source("b", _tones([(700, 0.1)], 8.0))}
        clips = [_clip("a", "a", 0.0, 8.0), _clip("b", "b", 4.0, 8.0)]
        for style in (TransitionDsp.BEAT_ROLL, TransitionDsp.LOWPASS_OUT):
            with self.subTest(style=style):
                transition = replace(_styled("a", "b", 4.0, 4.0, style),
                                     beat_seconds=0.5, roll_beats=0.25, filter_cutoff_hz=350)
                full = self._render(clips, [transition], paths)
                plan = SimpleNamespace(audio=AudioRenderPlan(tuple(clips), (transition,)))
                window = render_window(plan, 0, 1.0, preroll=1.0, postroll=1.0)
                audition = self._render(window.plan.clips, window.plan.transitions, paths)
                first = round(window.origin * SAMPLE_RATE)
                self.assertLess(np.max(np.abs(audition - full[first:first + len(audition)])), 2e-5)

    def test_lowpass_out_removes_highs_and_preserves_the_solo_and_length(self) -> None:
        paths = {"a": self._source("a", _tones([(100, 0.1), (6000, 0.1)], 8.0)),
                 "b": self._source("b", np.zeros((8 * SAMPLE_RATE, 2)))}
        clips = [_clip("a", "a", 0.0, 8.0), _clip("b", "b", 4.0, 8.0)]
        transition = replace(_styled("a", "b", 4.0, 4.0, TransitionDsp.LOWPASS_OUT),
                             filter_cutoff_hz=350.0)
        audio = self._render(clips, [transition], paths)
        self._assert_exact_length(audio, 12.0)
        solo = audio[SAMPLE_RATE:2 * SAMPLE_RATE, 0]
        self.assertAlmostEqual(_spectrum_level(solo, 6000), 0.1, delta=0.005)
        closing = audio[7 * SAMPLE_RATE:int(7.5 * SAMPLE_RATE), 0]
        self.assertLess(_spectrum_level(closing, 6000), _spectrum_level(closing, 100) * 0.1)

    def test_beat_roll_repeats_the_cue_slice_and_preserves_length(self) -> None:
        original = _tones([(500, 0.1)], 8.0)
        original[int(4.25 * SAMPLE_RATE):] = _tones([(2500, 0.1)], 3.75)
        paths = {"a": self._source("a", original),
                 "b": self._source("b", np.zeros((8 * SAMPLE_RATE, 2)))}
        clips = [_clip("a", "a", 0.0, 8.0), _clip("b", "b", 4.0, 8.0)]
        transition = replace(_styled("a", "b", 4.0, 4.0, TransitionDsp.BEAT_ROLL),
                             beat_seconds=0.5, roll_beats=0.5)
        audio = self._render(clips, [transition], paths)
        self._assert_exact_length(audio, 12.0)
        loop = audio[5 * SAMPLE_RATE:6 * SAMPLE_RATE, 0]
        self.assertGreater(_spectrum_level(loop, 500), 0.05)
        self.assertLess(_spectrum_level(loop, 2500), 0.005)

    def setUp(self) -> None:
        self.executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        self._directory = TemporaryDirectory(prefix="automix-bass-swap-")
        self.directory = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.assertTrue(ffmpeg_supports_transition_dsp(str(self.executable)))

    def _source(self, name: str, signal: np.ndarray) -> str:
        path = self.directory / f"{name}.wav"
        _write_stereo_wav(path, signal)
        return str(path)

    def _render(self, clips, transitions, paths) -> np.ndarray:
        plan = AudioRenderPlan(clips=tuple(clips), transitions=tuple(transitions))
        result = AutoMixAudioPipeline(self.executable).render(plan, paths, self.directory / "out")
        decoded = subprocess.run(
            [str(self.executable), "-hide_banner", "-loglevel", "error", "-i", str(result.path),
             "-f", "f32le", "-ac", "2", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, check=True, **hidden_process_kwargs(),
        )
        return np.frombuffer(decoded.stdout, dtype=np.float32).reshape(-1, 2)

    def _assert_exact_length(self, audio: np.ndarray, seconds: float) -> None:
        # Sample count, not container duration: NUT's format duration omits
        # the final packet's length (a probe artifact shared with qsin).
        self.assertLessEqual(abs(len(audio) - seconds * SAMPLE_RATE), 2, len(audio) / SAMPLE_RATE)

    def _pair(self, rate_b: float = 1.0) -> tuple[list, list, dict]:
        paths = {
            "a": self._source("a", _tones([(100.0, 0.5), (2000.0, 0.2)], 30.0)),
            "b": self._source("b", _tones([(120.0, 0.5), (3000.0, 0.2)], 32.0)),
        }
        clips = [_clip("a", "a", 0.0, 30.0), _clip("b", "b", 22.0, 30.0 * rate_b, rate=rate_b)]
        return clips, [_beat_match("a", "b", 22.0, 8.0)], paths

    def test_two_track_bass_swap_has_exact_duration_and_band_handoff(self) -> None:
        clips, transitions, paths = self._pair()
        audio = self._render(clips, transitions, paths)
        self._assert_exact_length(audio, 52.0)

        def levels(start: float, end: float) -> dict[float, float]:
            segment = audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE), 0]
            return {f: _spectrum_level(segment, f) for f in (100.0, 120.0, 2000.0, 3000.0)}

        early, middle, late = levels(22.2, 24.6), levels(25.0, 26.6), levels(27.6, 29.8)
        self.assertGreater(early[100.0], 10 * early[120.0])  # outgoing bass only
        self.assertGreater(late[120.0], 10 * late[100.0])    # incoming bass only
        self.assertGreater(middle[100.0], 0.2 * early[100.0])  # a handoff, not a hole
        self.assertGreater(middle[120.0], 0.2 * late[120.0])
        # Mid/high is a full-window crossfade: both present mid-transition.
        self.assertGreater(middle[2000.0], 0.3 * early[2000.0])
        self.assertGreater(middle[3000.0], 0.3 * late[3000.0])
        self.assertGreater(early[3000.0], 0.1 * late[3000.0])  # incoming highs already fading in
        # Stereo image survives (sources are L = 1.25 x R).
        steady = audio[2 * SAMPLE_RATE:10 * SAMPLE_RATE]
        ratio = np.sqrt(np.mean(steady[:, 0] ** 2) / np.mean(steady[:, 1] ** 2))
        self.assertAlmostEqual(float(ratio), 1.25, delta=0.02)

    def test_echo_out_repeats_decay_beat_by_beat_after_the_dry_track_stops(self) -> None:
        paths = {"a": self._source("a", _tones([(1000.0, 0.5)], 30.0)),
                 "b": self._source("b", _tones([(3000.0, 0.5)], 12.0))}
        clips = [_clip("a", "a", 0.0, 30.0), _clip("b", "b", 26.0, 10.0, gain=0.0)]
        transition = AudioRenderTransition("a", "b", 26.0, 4.0, TransitionType.EQUAL_POWER,
                                           TransitionDsp.ECHO_OUT, beat_seconds=0.5)
        audio = self._render(clips, [transition], paths)
        self._assert_exact_length(audio, 36.0)
        levels = [_spectrum_level(audio[int(s * SAMPLE_RATE):int((s + 0.4) * SAMPLE_RATE), 0], 1000.0)
                  for s in (25.0, 27.05, 27.55, 28.05)]  # dry, then repeats 2-4 of the 26.0 s hit
        self.assertGreater(levels[1], 0.2 * levels[0])        # the echo is audible...
        self.assertLess(levels[1], levels[0])                  # ...below the dry track...
        self.assertLess(levels[2], 0.8 * levels[1])            # ...and dies away
        self.assertLess(levels[3], 0.8 * levels[2])

    def test_tape_stop_lowers_the_pitch_as_it_slows(self) -> None:
        if "rubberband" not in ffmpeg_filter_names(str(self.executable)):
            self.skipTest("FFmpeg has no rubberband")
        paths = {"a": self._source("a", _tones([(440.0, 0.5)], 30.0)),
                 "b": self._source("b", _tones([(3000.0, 0.5)], 12.0))}
        clips = [_clip("a", "a", 0.0, 30.0), _clip("b", "b", 28.0, 10.0, gain=0.0)]
        audio = self._render(clips, [_styled("a", "b", 28.0, 2.0, TransitionDsp.TAPE_STOP,
                                             TransitionType.EQUAL_POWER)], paths)
        self._assert_exact_length(audio, 38.0)
        segment = audio[int(29.3 * SAMPLE_RATE):int(29.5 * SAMPLE_RATE), 0]
        self.assertGreater(_spectrum_level(segment, 220.0), _spectrum_level(segment, 440.0))

    def test_failed_bass_swap_with_partial_output_retries_as_valid_qsin(self) -> None:
        clips, transitions, paths = self._pair()
        pipeline = AutoMixAudioPipeline(self.executable)
        real_run = pipeline._run
        graphs: list[str] = []

        def failing_first_run(arguments, cancel_event, on_progress):
            graphs.append(arguments[arguments.index("-filter_complex") + 1])
            real_run(arguments, cancel_event, on_progress)
            if len(graphs) == 1:
                # A real bass-swap mix, truncated mid-file, then reported as a failure.
                output = Path(arguments[-1])
                output.write_bytes(output.read_bytes()[:output.stat().st_size // 2])
                raise AutoMixRenderError("simulated bass-swap failure after partial output")

        plan = AudioRenderPlan(clips=tuple(clips), transitions=tuple(transitions))
        with patch.object(pipeline, "_run", failing_first_run):
            result = pipeline.render(plan, paths, self.directory / "out")

        self.assertEqual(len(graphs), 2)
        self.assertIn("acrossover", graphs[0])
        self.assertNotIn("acrossover", graphs[1])
        self.assertAlmostEqual(result.duration_seconds, 52.0, delta=0.15)
        decoded = subprocess.run(
            [str(self.executable), "-hide_banner", "-loglevel", "error", "-i", str(result.path),
             "-f", "f32le", "-ac", "2", "-"], capture_output=True, check=True, **hidden_process_kwargs(),
        )
        audio = np.frombuffer(decoded.stdout, dtype=np.float32).reshape(-1, 2)
        self._assert_exact_length(audio, 52.0)
        self.assertTrue(np.isfinite(audio).all())
        # qsin overlaps the bass lines from the start -- proof the retry's
        # graph, not the truncated DSP file, is what survived.
        early = audio[int(22.2 * SAMPLE_RATE):int(24.6 * SAMPLE_RATE), 0]
        self.assertGreater(_spectrum_level(early, 120.0), 0.05 * _spectrum_level(early, 100.0))

    def test_window_edges_are_click_free(self) -> None:
        clips, transitions, paths = self._pair()
        audio = self._render(clips, transitions, paths)[:, 0]
        steps = np.abs(np.diff(audio))
        typical = float(steps[5 * SAMPLE_RATE:15 * SAMPLE_RATE].max())
        for edge in (22.0, 30.0):
            around = steps[int((edge - 0.01) * SAMPLE_RATE):int((edge + 0.01) * SAMPLE_RATE)]
            self.assertLess(float(around.max()), 1.5 * typical, edge)

    def test_tempo_adjusted_incoming_keeps_the_planned_duration(self) -> None:
        for rate in (0.94, 1.06):
            with self.subTest(rate=rate):
                clips, transitions, paths = self._pair(rate_b=rate)
                bass_swap = self._render(clips, transitions, paths)
                with patch("app.automix.renderer.ffmpeg_supports_transition_dsp", return_value=False):
                    qsin = self._render(clips, transitions, paths)
                # atempo itself may round a few ms off a stretched clip's end
                # (identically on both paths); the DSP must add nothing to that.
                self.assertEqual(len(bass_swap), len(qsin))
                self.assertLess(abs(len(bass_swap) / SAMPLE_RATE - 52.0), 0.02)

    def test_three_track_chain_with_leading_silence(self) -> None:
        paths = {
            "a": self._source("a", _tones([(100.0, 0.5), (2000.0, 0.2)], 30.0)),
            "b": self._source("b", _tones([(120.0, 0.5), (3000.0, 0.2)], 32.0)),
            "c": self._source("c", _tones([(80.0, 0.5), (1500.0, 0.2)], 30.0)),
        }
        clips = [
            _clip("a", "a", 2.0, 30.0),
            _clip("b", "b", 24.0, 31.8, rate=1.06),  # 30 s of timeline
            _clip("c", "c", 48.0, 30.0),
        ]
        transitions = [_beat_match("a", "b", 24.0, 8.0), _beat_match("b", "c", 48.0, 6.0)]
        audio = self._render(clips, transitions, paths)
        self._assert_exact_length(audio, 78.0)
        self.assertLess(float(np.abs(audio[:int(1.9 * SAMPLE_RATE)]).max()), 1e-4)  # leading silence intact
        early = audio[int(48.2 * SAMPLE_RATE):int(50.0 * SAMPLE_RATE), 0]
        late = audio[int(52.0 * SAMPLE_RATE):int(53.8 * SAMPLE_RATE), 0]
        self.assertGreater(_spectrum_level(early, 120.0), 10 * _spectrum_level(early, 80.0))
        self.assertGreater(_spectrum_level(late, 80.0), 10 * _spectrum_level(late, 120.0))

    def test_constant_energy_material_has_no_hole_or_spike(self) -> None:
        rng = np.random.default_rng(7)

        def music_like(seed_offset: float) -> np.ndarray:
            noise = rng.standard_normal((30 * SAMPLE_RATE, 2)) * 0.08
            return noise + _tones([(90.0 + seed_offset, 0.3)], 30.0)

        paths = {"a": self._source("a", music_like(0.0)), "b": self._source("b", music_like(17.0))}
        audio = self._render([_clip("a", "a", 0.0, 30.0), _clip("b", "b", 22.0, 30.0)],
                             [_beat_match("a", "b", 22.0, 8.0)], paths)[:, 0]

        def rms(start: float) -> float:
            segment = audio[int(start * SAMPLE_RATE):int((start + 0.1) * SAMPLE_RATE)]
            return float(np.sqrt(np.mean(segment ** 2)))

        steady = np.mean([rms(t) for t in np.arange(5.0, 15.0, 0.1)])
        for t in np.arange(22.0, 29.9, 0.1):
            self.assertGreater(rms(t), 0.7 * steady, t)   # > -3.1 dB
            self.assertLess(rms(t), 1.42 * steady, t)     # < +3 dB

    def test_near_full_scale_inputs_do_not_clip(self) -> None:
        rng = np.random.default_rng(3)
        shared = rng.standard_normal((30 * SAMPLE_RATE, 2))
        shared *= 0.98 / np.abs(shared).max()
        # Identical (fully correlated) mid/high on both sides is the worst case
        # for the equal-power overlap: unlimited, it would peak near +3 dB.
        rotated = np.concatenate([shared[22 * SAMPLE_RATE:], shared[:22 * SAMPLE_RATE]])  # B head == A tail
        paths = {"a": self._source("a", shared), "b": self._source("b", rotated)}
        audio = self._render([_clip("a", "a", 0.0, 30.0), _clip("b", "b", 22.0, 30.0)],
                             [_beat_match("a", "b", 22.0, 8.0)], paths)
        self.assertTrue(np.isfinite(audio).all())
        window = audio[22 * SAMPLE_RATE:30 * SAMPLE_RATE]
        self.assertLessEqual(float(np.abs(window).max()), 0.98)
        self.assertLess(int(np.sum(np.abs(audio) >= 32767 / 32768)), 1)

    def test_unity_band_recombination_is_flat(self) -> None:
        from app.automix.renderer import _band_filters

        rng = np.random.default_rng(1)
        noise = rng.standard_normal((10 * SAMPLE_RATE, 2)) * 0.2
        source = self._source("noise", noise)
        graph = ";".join(["[0:a]aformat=sample_rates=48000:channel_layouts=stereo[x]",
                          *_band_filters("x", "y", 10.0, None, None)])
        processed = subprocess.run(
            [str(self.executable), "-hide_banner", "-loglevel", "error", "-i", source,
             "-filter_complex", graph, "-map", "[y]", "-f", "f32le", "-"],
            capture_output=True, check=True, **hidden_process_kwargs(),
        )
        output = np.frombuffer(processed.stdout, dtype=np.float32).reshape(-1, 2)[:, 0]
        original = np.clip(noise[:, 0], -1.0, 1.0)
        frequencies = np.fft.rfftfreq(len(original), 1.0 / SAMPLE_RATE)
        power_in = np.abs(np.fft.rfft(original)) ** 2
        power_out = np.abs(np.fft.rfft(output[:len(original)])) ** 2
        for low, high in ((30, 150), (150, 260), (260, 2000), (2000, 3000), (3000, 16000)):
            band = (frequencies >= low) & (frequencies < high)
            gain_db = 10 * np.log10(power_out[band].sum() / power_in[band].sum())
            self.assertLess(abs(gain_db), 0.5, (low, high))


@unittest.skipUnless(
    os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(),
    "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg transition-style checks.",
)
class RealTransitionStyleRenderTests(unittest.TestCase):
    """Every Phase 2 style in one real A->B->C->D->E chain, one tone per band per track."""

    setUp = RealBassSwapRenderTests.setUp
    _source = RealBassSwapRenderTests._source
    _render = RealBassSwapRenderTests._render
    _assert_exact_length = RealBassSwapRenderTests._assert_exact_length

    # (low, mid, high) Hz per track -- each track owns its three frequencies.
    TONES = {"a": (60, 500, 4000), "b": (80, 700, 5000), "c": (100, 900, 6000),
             "d": (120, 1100, 7000), "e": (150, 1300, 8000)}
    # clip start, (transition start, duration, style) into that clip
    CHAIN = [
        ("a", 0.0, None),
        ("b", 22.0, (22.0, 8.0, TransitionDsp.BASS_SWAP)),
        ("c", 44.0, (44.0, 8.0, TransitionDsp.VOCAL_SAFE_EQ)),
        ("d", 66.0, (66.0, 8.0, TransitionDsp.FILTER_BLEND)),
        ("e", 93.0, (93.0, 3.0, TransitionDsp.SHORT_FADE)),
    ]

    def _chain(self, sources=None) -> tuple[list, list, dict]:
        paths, clips, transitions = {}, [], []
        previous = None
        for track_id, start, transition in self.CHAIN:
            signal = sources[track_id] if sources else _tones(
                [(f, 0.25) for f in self.TONES[track_id]], 30.0)
            paths[track_id] = self._source(track_id, signal)
            clips.append(_clip(track_id, track_id, start, 30.0))
            if transition is not None:
                transitions.append(_styled(previous, track_id, *transition))
            previous = track_id
        return clips, transitions, paths

    def _levels(self, audio: np.ndarray, window: tuple[float, float, TransitionDsp], p0: float, p1: float,
                out_id: str, in_id: str) -> dict[str, tuple[float, float]]:
        start, duration, _style = window
        segment = audio[int((start + p0 * duration) * SAMPLE_RATE):int((start + p1 * duration) * SAMPLE_RATE), 0]
        return {band: (_spectrum_level(segment, self.TONES[out_id][i]), _spectrum_level(segment, self.TONES[in_id][i]))
                for i, band in enumerate(("low", "mid", "high"))}

    def test_mixed_style_chain_renders_each_style_in_its_own_window(self) -> None:
        clips, transitions, paths = self._chain()
        audio = self._render(clips, transitions, paths)
        self._assert_exact_length(audio, 123.0)
        self.assertTrue(np.isfinite(audio).all())
        windows = [transition for _id, _start, transition in self.CHAIN[1:]]

        # BASS_SWAP a->b: only the outgoing bass early, only the incoming late; mids overlap.
        early, mid, late = (self._levels(audio, windows[0], *p, "a", "b") for p in ((0.05, 0.3), (0.4, 0.55), (0.7, 0.95)))
        self.assertGreater(early["low"][0], 10 * early["low"][1])
        self.assertGreater(late["low"][1], 10 * late["low"][0])
        self.assertGreater(mid["mid"][0], 0.3 * mid["mid"][1])
        self.assertGreater(mid["mid"][1], 0.3 * mid["mid"][0])

        # VOCAL_SAFE_EQ b->c: mids are swapped too; highs still crossfade.
        early, late = (self._levels(audio, windows[1], *p, "b", "c") for p in ((0.05, 0.3), (0.75, 0.95)))
        self.assertGreater(early["mid"][0], 10 * early["mid"][1])
        self.assertGreater(late["mid"][1], 10 * late["mid"][0])
        self.assertGreater(early["high"][1], 0.1 * early["high"][0])  # incoming highs already in

        # FILTER_BLEND c->d: incoming highs lead; the lows swap briefly mid-window, never dropping out.
        early, handoff, late = (self._levels(audio, windows[2], *p, "c", "d") for p in ((0.05, 0.3), (0.46, 0.54), (0.6, 0.8)))
        self.assertGreater(early["high"][1], 0.2 * early["high"][0])
        self.assertGreater(early["low"][0], 10 * early["low"][1])
        self.assertGreater(max(handoff["low"]), 0.3 * early["low"][0])  # no bass hole
        self.assertLess(min(handoff["low"]), 0.7 * early["low"][0])     # and no full double bass
        self.assertGreater(late["high"][0], 0.2 * late["high"][1])   # outgoing highs linger
        self.assertLess(late["low"][0], 0.05 * late["low"][1])

        # SHORT_FADE d->e: full-band equal-power crossing, both tracks present mid-window.
        mid = self._levels(audio, windows[3], 0.35, 0.65, "d", "e")
        for band in ("low", "mid", "high"):
            self.assertGreater(min(mid[band]), 0.3 * max(mid[band]), band)

        # No clicks at any window edge.
        steps = np.abs(np.diff(audio[:, 0]))
        typical = float(steps[5 * SAMPLE_RATE:15 * SAMPLE_RATE].max())
        for start, duration, _style in windows:
            for edge in (start, start + duration):
                around = steps[int((edge - 0.01) * SAMPLE_RATE):int((edge + 0.01) * SAMPLE_RATE)]
                self.assertLess(float(around.max()), 2.5 * typical, edge)

    def test_every_style_keeps_correlated_full_scale_material_below_full_scale(self) -> None:
        rng = np.random.default_rng(11)
        shared = rng.standard_normal((200 * SAMPLE_RATE, 2))
        shared *= 0.98 / np.abs(shared).max()
        # Each incoming head is the exact audio its outgoing tail plays: worst-case correlation.
        starts = {track_id: start for track_id, start, _t in self.CHAIN}
        offsets = {"a": 0.0}  # where in ``shared`` each track's source begins
        for (previous, _s, _t), (track_id, _start, (t_start, _d, _style)) in zip(self.CHAIN, self.CHAIN[1:]):
            offsets[track_id] = offsets[previous] + t_start - starts[previous]
        sources = {track_id: shared[int(offset * SAMPLE_RATE):int(offset * SAMPLE_RATE) + 30 * SAMPLE_RATE]
                   for track_id, offset in offsets.items()}
        clips, transitions, paths = self._chain(sources)
        audio = self._render(clips, transitions, paths)
        self._assert_exact_length(audio, 123.0)
        self.assertTrue(np.isfinite(audio).all())
        for start, duration, style in (t for _i, _s, t in self.CHAIN[1:]):
            window = audio[int(start * SAMPLE_RATE):int((start + duration) * SAMPLE_RATE)]
            self.assertLessEqual(float(np.abs(window).max()), 0.98, style)
        self.assertEqual(int(np.sum(np.abs(audio) >= 32767 / 32768)), 0)

    def test_filter_blend_dip_is_a_bass_gap_not_a_hole(self) -> None:
        rng = np.random.default_rng(7)

        def music_like(offset: float) -> np.ndarray:
            return rng.standard_normal((30 * SAMPLE_RATE, 2)) * 0.08 + _tones([(90.0 + offset, 0.3)], 30.0)

        paths = {"a": self._source("a", music_like(0.0)), "b": self._source("b", music_like(17.0))}
        audio = self._render([_clip("a", "a", 0.0, 30.0), _clip("b", "b", 22.0, 30.0)],
                             [_styled("a", "b", 22.0, 8.0, TransitionDsp.FILTER_BLEND)], paths)[:, 0]

        def rms(start: float) -> float:
            segment = audio[int(start * SAMPLE_RATE):int((start + 0.1) * SAMPLE_RATE)]
            return float(np.sqrt(np.mean(segment ** 2)))

        steady = np.mean([rms(t) for t in np.arange(5.0, 15.0, 0.1)])
        levels = [rms(t) for t in np.arange(22.0, 29.9, 0.1)]
        # Phase 2's bass gap dipped this bass-heavy fixture to -7 dB; the staggered swap keeps it within -3 dB.
        self.assertGreater(min(levels), 10 ** (-3 / 20) * steady)
        self.assertLess(max(levels), 1.42 * steady)

    def test_filter_blend_bass_overlap_is_brief_and_below_bass_swap(self) -> None:
        paths = {"a": self._source("a", _tones([(90.0, 0.3)], 30.0)), "b": self._source("b", _tones([(107.0, 0.3)], 30.0))}

        def overlap(dsp: TransitionDsp) -> tuple[float, float]:
            """(peak of min(out bass, in bass) re steady, seconds both >= -12 dB) over the window."""
            audio = self._render([_clip("a", "a", 0.0, 30.0), _clip("b", "b", 22.0, 30.0)],
                                 [_styled("a", "b", 22.0, 8.0, dsp)], paths)[:, 0]
            steady = _spectrum_level(audio[5 * SAMPLE_RATE:int(5.2 * SAMPLE_RATE)], 90.0)
            peak, both = 0.0, 0.0
            for start in np.arange(22.0, 29.8, 0.05):
                segment = audio[int(start * SAMPLE_RATE):int((start + 0.2) * SAMPLE_RATE)]
                shared = min(_spectrum_level(segment, 90.0), _spectrum_level(segment, 107.0)) / steady
                peak = max(peak, shared)
                both += 0.05 if shared >= 10 ** (-12 / 20) else 0.0
            return peak, both

        blend_peak, blend_time = overlap(TransitionDsp.FILTER_BLEND)
        swap_peak, swap_time = overlap(TransitionDsp.BASS_SWAP)
        self.assertLess(blend_peak, 10 ** (-6 / 20))   # never two basses near full level
        self.assertLess(blend_peak, swap_peak)
        self.assertLess(blend_time, 0.5 * swap_time)


if __name__ == "__main__":
    unittest.main()
