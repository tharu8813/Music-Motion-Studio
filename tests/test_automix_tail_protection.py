"""Audible endings and real beat matching must survive conservative analysis."""

from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
import wave

import numpy as np

from app.automix.planner import compile_automix
from app.automix.renderer import AutoMixAudioPipeline, band_fade_windows
from app.utils.subprocess_utils import hidden_process_kwargs
from app.timeline.models import TransitionType
from app.timeline.render_plan import TransitionDsp
from tests.test_automix_planner import ENABLED, _analysis, _structure, _track


class TailProtectionTests(unittest.TestCase):
    def test_fallback_after_a_rate_matched_track_keeps_exact_geometry(self):
        tracks = [_track(name, 60) for name in ('a', 'b', 'c')]
        for bpm in (90, None):
            with self.subTest(bpm=bpm):
                analyses = {name: _analysis(name, tempo, 60)
                            for name, tempo in zip(('a', 'b', 'c'), (120, 124, bpm))}
                plan = compile_automix(tracks, analyses, ENABLED)
                self.assertEqual(plan.audio.clips[1].playback_rate, 1.0)  # b plays at its own tempo
                self.assertAlmostEqual(plan.audio.clips[0].tempo_ramp.end_rate, 124 / 120)
                self.assertEqual(plan.audio.transitions[1].type, TransitionType.CROSSFADE)
                self.assertIs(plan.audio.transitions[1].dsp, TransitionDsp.SHORT_FADE)
                self.assertEqual(plan.audio.clips[0].source_out, 60)
                for clip, transition in zip(plan.audio.clips, plan.audio.transitions):
                    self.assertAlmostEqual(clip.timeline_end, transition.timeline_start + transition.duration)

    def test_structure_hint_cannot_discard_an_unknown_or_known_vocal_ending(self):
        tracks = [_track("a", 200.3), _track("b", 200.0)]
        for vocals in ((), ((185.0, 200.3),)):
            with self.subTest(vocals=vocals):
                analyses = {
                    "a": replace(_analysis("a", 120, 200.3), vocal_activity=vocals),
                    "b": _analysis("b", 120, 200),
                }
                plan = compile_automix(tracks, analyses, ENABLED,
                                       structures={"a": _structure("a", 200.3, outro_start=150)})
                clip, following = plan.audio.clips
                self.assertEqual(clip.source_out, 200.3)
                transition = plan.audio.transitions[0]
                self.assertAlmostEqual(clip.timeline_end, transition.timeline_start + transition.duration)
                self.assertLessEqual(transition.duration, ENABLED.max_transition_seconds)
                if vocals:  # sung to the very end: only a short handoff over the last bars
                    self.assertEqual(dict(transition.details)["bars"], 2)

    def test_uncertain_bars_do_not_disable_reliable_beat_tempo_matching(self):
        tracks = [_track("a", 60.3), _track("b", 60)]
        analyses = {"a": _analysis("a", 120, 60.3, meter_confidence=.3),
                    "b": _analysis("b", 124, 60, meter_confidence=.3)}
        plan = compile_automix(tracks, analyses, ENABLED)
        transition = plan.audio.transitions[0]
        outgoing, incoming = plan.audio.clips
        self.assertEqual(transition.type, TransitionType.BEAT_MATCH)
        self.assertEqual(incoming.playback_rate, 1.0)
        self.assertEqual(outgoing.source_out, 60.3)
        outgoing_cue = outgoing.source_at(transition.timeline_start)
        self.assertAlmostEqual(min(analyses["a"].beats, key=lambda b: abs(b - outgoing_cue)), outgoing_cue, delta=1e-5)
        self.assertAlmostEqual(min(analyses["b"].beats, key=lambda b: abs(b - incoming.source_in)),
                               incoming.source_in, delta=1e-5)
        out_times = [outgoing.timeline_at(b) for b in analyses["a"].beats if outgoing_cue - 1e-5 <= b < outgoing.source_out]
        in_times = [incoming.timeline_at(b) for b in analyses["b"].beats
                    if incoming.source_in - 1e-5 <= b < incoming.source_in + transition.duration]
        self.assertGreater(len(in_times), 8)
        for a, b in zip(out_times, in_times):
            self.assertAlmostEqual(a, b, delta=1e-4)

    def test_unknown_vocals_hand_the_outgoing_voice_band_over_mid_window(self):
        # Two singers must never share the whole blend: with vocals unknown the
        # outgoing mids (the voice) are gone well before the overlap ends.
        tracks = [_track("a", 60), _track("b", 60)]
        for fields in ({}, {"key": "F# major"}, {"energy": .9}):
            with self.subTest(fields=fields):
                analyses = {"a": replace(_analysis("a", 120, 60), key="C major", energy=.1),
                            "b": replace(_analysis("b", 120, 60), **fields)}
                plan = compile_automix(tracks, analyses, ENABLED)
                transition = plan.audio.transitions[0]
                clip = plan.audio.clips[0]
                fade, start, length = band_fade_windows(
                    "mid", clip.duration, None, (transition.dsp, transition.duration, transition.vocal_handoff)
                )[0]
                self.assertEqual(fade, "out")
                self.assertLess(start + length, clip.duration - 0.3 * transition.duration)
                self.assertTrue(any("unknown" in r for r in transition.dsp_reasons))


@unittest.skipUnless(os.environ.get('PLAYLIST_CANVAS_TEST_FFMPEG'), 'Requires real FFmpeg')
class RenderedBeatProtectionTests(unittest.TestCase):
    def test_real_render_matches_incoming_pulses_to_outgoing_beat_grid(self):
        executable = Path(os.environ['PLAYLIST_CANVAS_TEST_FFMPEG'])
        sample_rate = 48000
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name, bpm, channel in [('a', 120, 0), ('b', 124, 1)]:
                samples = np.zeros((sample_rate * 32, 2), dtype=np.float64)
                # Separate channels make each track's rendered pulse timing observable.
                pulse_t = np.arange(2400) / sample_rate
                pulse = .25 * np.sin(2 * np.pi * 1000 * pulse_t) * np.hanning(len(pulse_t))
                for beat in np.arange(0, 31.9, 60 / bpm):
                    start = round(beat * sample_rate)
                    samples[start:start + len(pulse), channel] += pulse
                path = root / f'{name}.wav'
                with wave.open(str(path), 'wb') as output:
                    output.setnchannels(2)
                    output.setsampwidth(2)
                    output.setframerate(sample_rate)
                    output.writeframes((samples * 32767).astype('<i2').tobytes())
                paths[name] = str(path)
            analyses = {name: _analysis(name, bpm, 32, meter_confidence=.3)
                        for name, bpm in [('a', 120), ('b', 124)]}
            plan = compile_automix([_track('a', 32), _track('b', 32)], analyses, ENABLED)
            with self.assertNoLogs('app.automix.renderer', level='WARNING'):
                rendered = AutoMixAudioPipeline(executable).render(plan.audio, paths, root / 'mix')
            decoded = subprocess.run(
                [str(executable), '-v', 'error', '-i', str(rendered.path), '-f', 'f32le',
                 '-ar', str(sample_rate), '-ac', '2', '-'], capture_output=True, check=True,
                **hidden_process_kwargs(),
            )
            audio = np.frombuffer(decoded.stdout, dtype='<f4').reshape(-1, 2)
            self.assertAlmostEqual(len(audio) / sample_rate, plan.duration_seconds, delta=.05)
            transition = plan.audio.transitions[0]
            # Check every incoming beat (b keeps its own 124 BPM) through the
            # overlap, excluding nearly silent fade endpoints.
            for offset in np.arange(4, transition.duration - 2, 60 / 124):
                start = round((transition.timeline_start + offset - .1) * sample_rate)
                window = audio[start:start + round(.25 * sample_rate)]
                # 5 ms energy bins resist carrier phase and atempo waveform changes.
                energy = (window ** 2).reshape(-1, 240, 2).mean(axis=1)
                self.assertTrue(np.all(energy.max(axis=0) > 1e-7))
                peaks = energy.argmax(axis=0) * .005
                self.assertLess(abs(peaks[0] - peaks[1]), .02)  # was .045 before the fitted grid + rubberband ramp


if __name__ == "__main__":
    unittest.main()
