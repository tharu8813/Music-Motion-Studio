"""AutoMix editor audition: one transition's window, rendered alone, exactly as the full mix places it."""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.automix.planner import compile_automix  # noqa: E402
from app.automix.progressive import render_window  # noqa: E402
from app.automix.renderer import (  # noqa: E402
    BAND_ENVELOPES, AutoMixRenderError, PreparedAudio, build_filter_graph, transition_dsp_style,
)
from app.timeline.render_plan import TransitionDsp  # noqa: E402
from app.controllers import transition_audition_controller as audition  # noqa: E402
from tests.test_automix_manual_transitions import _analyses, _tracks  # noqa: E402
from tests.test_automix_planner import ENABLED  # noqa: E402


def _plan():
    tracks = _tracks()
    # 120 -> 124 -> 120 BPM: both junctions beat-match, so both outgoing clips carry a tempo ramp.
    return tracks, compile_automix(tracks, _analyses(tracks, (120.0, 124.0, 120.0)), ENABLED)


class RenderWindowTests(unittest.TestCase):
    def test_the_window_plays_the_full_mix_samples_and_stays_out_of_its_neighbours(self):
        _tracks_, plan = _plan()
        (first, second), clips = plan.audio.transitions, plan.audio.clips
        self.assertIsNotNone(clips[0].tempo_ramp)
        self.assertIsNotNone(clips[1].tempo_ramp)
        window, origin, start, end, resting = render_window(plan, 0, 0.5)
        head, tail = window.clips
        # Heard from no later than A's tempo ramp, stopping before B's ramp and the next transition.
        self.assertLessEqual(start, clips[0].timeline_at(clips[0].tempo_ramp.source_start) + 1e-9)
        self.assertLessEqual(end, clips[1].timeline_at(clips[1].tempo_ramp.source_start) + 1e-9)
        self.assertLessEqual(end, second.timeline_start)
        self.assertGreaterEqual(end, first.timeline_start + first.duration)
        # A ramping outgoing clip keeps its whole head, so its stretcher starts where the full mix's does.
        self.assertEqual(origin, clips[0].timeline_start)
        self.assertEqual(head.rate_segments(), clips[0].rate_segments())
        # File second t is timeline second origin + t, sample for sample.
        self.assertAlmostEqual(tail.timeline_end, end - origin, places=6)
        self.assertAlmostEqual(window.transitions[0].timeline_start, first.timeline_start - origin, places=9)
        self.assertEqual(window.transitions[0].duration, first.duration)
        for seconds in (start + 0.5, first.timeline_start - 0.1, first.timeline_start + 1.0):
            self.assertAlmostEqual(head.source_at(seconds - origin), clips[0].source_at(seconds), places=6)
        for seconds in (first.timeline_start + 0.5, end - 0.1):
            self.assertAlmostEqual(tail.source_at(seconds - origin), clips[1].source_at(seconds), places=6)
        self.assertIsNone(tail.tempo_ramp)
        self.assertEqual((head.gain, tail.gain), (0.5, 0.5))
        graph, _label = build_filter_graph(window.clips, window.transitions)
        self.assertIn(f"acrossfade=d={first.duration:.6f}", graph)
        # B's next junction filters B in the full mix; the window keeps that filter, at rest.
        style = transition_dsp_style(second)
        expected = (style in BAND_ENVELOPES, style is TransitionDsp.FILTER_SWEEP)
        self.assertEqual(resting, {tail.clip_id: expected} if any(expected) else {})
        self.assertNotIn(head.clip_id, resting)  # A's only other junction is none: the playlist starts

    def test_resting_filters_split_or_highpass_without_moving_anything(self):
        _tracks_, plan = _plan()
        clip = plan.audio.clips[2]
        plain, _label = build_filter_graph((clip,), ())
        split, _label = build_filter_graph((clip,), (), resting_dsp={clip.clip_id: (True, False)})
        swept, _label = build_filter_graph((clip,), (), resting_dsp={clip.clip_id: (False, True)})
        self.assertNotIn("acrossover", plain)
        self.assertIn("acrossover", split)
        self.assertNotIn("afade", split)  # no envelope: a band split sums back to an allpass
        self.assertIn("highpass@sweep0=f=10.00", swept)
        self.assertNotIn("asendcmd", swept)  # the cutoff never moves
        unchanged, _label = build_filter_graph((clip,), (), transition_dsp=False,
                                               resting_dsp={clip.clip_id: (True, True)})
        self.assertEqual(unchanged, plain)  # the legacy fallback drops DSP everywhere

    def test_the_second_window_starts_after_the_first_transition(self):
        _tracks_, plan = _plan()
        first = plan.audio.transitions[0]
        _window, _origin, start, end, _resting = render_window(plan, 1, 1.0)
        second = plan.audio.transitions[1]
        self.assertGreaterEqual(start, first.timeline_start + first.duration - 1e-9)
        self.assertAlmostEqual(end, second.timeline_start + second.duration + 3.0, places=6)  # the tail

    def test_without_a_tempo_ramp_only_the_window_is_rendered(self):
        from app.automix.overrides import TransitionOverride

        tracks = _tracks()
        plan = compile_automix(tracks, _analyses(tracks), ENABLED.with_overrides(
            {"a>b": TransitionOverride(150.0, 2.0, 10.0, "bass_swap", tempo_match=False)}))
        window, origin, start, end, _resting = render_window(plan, 0, 1.0)
        transition = plan.audio.transitions[0]
        self.assertEqual(origin, start)
        self.assertAlmostEqual(start, transition.timeline_start - 4.0, places=9)  # the run-up
        self.assertAlmostEqual(end, transition.timeline_start + transition.duration + 3.0, places=9)
        self.assertAlmostEqual(window.clips[1].timeline_end, end - start, places=6)


class _FakePipeline:
    """Stands in for FFmpeg: writes a file, records what it was asked to render."""

    calls: list = []
    gate: threading.Event | None = None
    fail: str = ""

    def __init__(self, _executable):
        pass

    def render(self, plan, track_paths, output_directory, *, cancel_event=None, container="nut", progress=None,
               resting_dsp=None, track_filters=None, workers=1):
        type(self).calls.append(plan)
        if type(self).gate is not None:
            type(self).gate.wait(5)
        if type(self).fail:
            raise AutoMixRenderError(type(self).fail)
        output_directory.mkdir(parents=True, exist_ok=True)
        path = output_directory / "automix_mix.flac"
        path.write_bytes(b"fLaC")
        return PreparedAudio(path, max(clip.timeline_end for clip in plan.clips))


class AuditionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _FakePipeline.calls, _FakePipeline.gate, _FakePipeline.fail = [], None, ""
        for target, value in (("AutoMixAudioPipeline", _FakePipeline), ("playlist_gain", lambda *_: 0.5)):
            patcher = patch.object(audition, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.controller = audition.TransitionAuditionController(Path("ffmpeg.exe"))
        self.addCleanup(self.controller.shutdown)
        self.ready, self.states = [], []
        self.controller.audio_ready.connect(lambda *args: self.ready.append(args))
        self.controller.state_changed.connect(lambda state, _detail: self.states.append(state))

    def _wait(self, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not condition():
            self.app.processEvents()
            time.sleep(0.01)
        return condition()

    def test_debounced_window_render_never_renders_the_playlist_and_is_cached(self):
        tracks, plan = _plan()
        with patch("app.renderer.ffmpeg_renderer.FFmpegRenderer.prepare_playlist_audio") as full_render:
            self.controller.request(plan, 0, tracks)
            self.controller.request(plan, 1, tracks)  # within the debounce: only this one renders
            self.assertEqual(self.controller.state, audition.WAITING)
            self.controller.flush()
            self.assertTrue(self._wait(lambda: self.ready))
            full_render.assert_not_called()
        self.assertEqual(self.controller.render_count, 1)
        (rendered,) = _FakePipeline.calls
        self.assertEqual(len(rendered.clips), 2)  # two songs, never the playlist
        self.assertEqual({clip.track_id for clip in rendered.clips}, {"b", "c"})
        self.assertEqual(self.ready[0][1:], render_window(plan, 1, 1.0)[1:4])  # (origin, start, end)
        self.controller.request(plan, 1, tracks)  # heard before: plays at once from the cache
        self.assertEqual(len(self.ready), 2)
        self.assertEqual(self.controller.render_count, 1)
        self.assertEqual(self.controller.state, audition.READY)

    def test_a_track_level_edit_invalidates_the_audition_even_with_the_same_geometry(self):
        tracks, plan = _plan()
        self.controller.request(plan, 0, tracks)
        self.controller.flush()
        self.assertTrue(self._wait(lambda: len(self.ready) == 1))
        tracks[1].volume_db = -12.0
        self.controller.request(plan, 0, tracks)
        self.controller.flush()
        self.assertTrue(self._wait(lambda: len(self.ready) == 2))
        self.assertEqual(self.controller.render_count, 2)

    def test_a_newer_request_makes_a_running_render_stale(self):
        tracks, plan = _plan()
        _FakePipeline.gate = threading.Event()
        self.controller.request(plan, 0, tracks)
        self.controller.flush()
        self.assertTrue(self._wait(lambda: _FakePipeline.calls))
        self.controller.request(plan, 1, tracks)
        self.controller.flush()
        _FakePipeline.gate.set()
        self.assertTrue(self._wait(lambda: self.ready))
        self._wait(lambda: False, 0.3)
        self.assertEqual(len(self.ready), 1)  # the first (stale) window never reached playback
        self.assertEqual(self.ready[0][1:], render_window(plan, 1, 1.0)[1:4])

    def test_a_failure_is_reported_and_can_be_retried(self):
        tracks, plan = _plan()
        _FakePipeline.fail = "FFmpeg could not render"
        self.controller.request(plan, 0, tracks)
        self.controller.flush()
        self.assertTrue(self._wait(lambda: self.controller.state == audition.FAILED))
        _FakePipeline.fail = ""
        self.controller.retry()
        self.assertTrue(self._wait(lambda: self.ready))
        self.assertEqual(self.controller.render_count, 2)

    def test_shutdown_deletes_the_rendered_windows(self):
        tracks, plan = _plan()
        self.controller.request(plan, 0, tracks)
        self.controller.flush()
        self.assertTrue(self._wait(lambda: self.ready))
        folder = Path(self.ready[0][0]).parent.parent
        self.controller.shutdown()
        self.assertFalse(folder.exists())
        self.controller.request(plan, 1, tracks)  # closed: ignored
        self.assertEqual(self.controller.render_count, 1)

    def test_without_ffmpeg_it_says_so(self):
        tracks, plan = _plan()
        controller = audition.TransitionAuditionController(None)
        self.addCleanup(controller.shutdown)
        controller.request(plan, 0, tracks)
        self.assertEqual(controller.state, audition.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
