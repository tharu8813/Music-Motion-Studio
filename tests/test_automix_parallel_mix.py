"""The AutoMix mix rendered in parts on several FFmpegs: same samples, same loudness."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from app.automix.parallel_mix import (
    RUN_UP_SECONDS,
    SEAM_MARGIN_SECONDS,
    finish_parts,
    integrated_loudness,
    plan_seams,
    render_parts,
)
from app.automix.renderer import SAMPLE_RATE, AutoMixAudioPipeline
from app.timeline.models import TransitionType
from app.timeline.render_plan import AudioRenderPlan, AudioRenderTransition, TransitionDsp
from app.utils.subprocess_utils import hidden_process_kwargs
from tests.test_automix_renderer import _clip, _tones, _write_stereo_wav


def _plan() -> AudioRenderPlan:
    """Three 70 s tracks, joined by two limited windows in a row and a bass swap."""
    clips = [_clip("a", "a", 0.0, 70.0), _clip("b", "b", 67.0, 70.0), _clip("c", "c", 129.0, 70.0)]
    return AudioRenderPlan(tuple(clips), (
        AudioRenderTransition("a", "b", 67.0, 3.0, TransitionType.EQUAL_POWER, TransitionDsp.SHORT_FADE),
        AudioRenderTransition("b", "c", 129.0, 8.0, TransitionType.BEAT_MATCH, TransitionDsp.BASS_SWAP),
    ))


class LoudnessTests(unittest.TestCase):
    def test_gating_matches_bs1770(self) -> None:
        self.assertAlmostEqual(integrated_loudness([-20.0] * 50), -20.0, places=6)
        # Silence under the absolute gate and a quiet stretch under the relative one do not count.
        self.assertAlmostEqual(integrated_loudness([-20.0] * 50 + [-80.0] * 50 + [-35.0] * 50), -20.0, places=6)
        self.assertEqual(integrated_loudness([-90.0] * 10), float("-inf"))


class SeamTests(unittest.TestCase):
    def test_seams_sit_in_a_clip_body_away_from_its_transitions(self) -> None:
        plan = _plan()
        seams = plan_seams(plan, 3)
        self.assertTrue(seams)
        for index, local in seams:
            clip = plan.clips[index]
            into = [t for t in plan.transitions if t.clip_b == clip.clip_id]
            out_of = [t for t in plan.transitions if t.clip_a == clip.clip_id]
            if into:
                self.assertGreaterEqual(clip.timeline_start + local, into[0].timeline_start + into[0].duration
                                        + SEAM_MARGIN_SECONDS + RUN_UP_SECONDS - 1e-9)
            if out_of:
                self.assertLessEqual(clip.timeline_start + local, out_of[0].timeline_start - SEAM_MARGIN_SECONDS)
        self.assertEqual(plan_seams(plan, 1), [])  # one worker: one part


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(),
                     "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
class RealPartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        self._directory = TemporaryDirectory(prefix="automix-parts-")
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        self.paths = {}
        for track, frequency in (("a", 220.0), ("b", 330.0), ("c", 440.0)):
            path = self.directory / f"{track}.wav"
            _write_stereo_wav(path, _tones([(frequency, 0.4), (frequency * 4.5, 0.2)], 70.0))
            self.paths[track] = str(path)
        self.pipeline = AutoMixAudioPipeline(self.executable)

    def _decode(self, path: Path) -> np.ndarray:
        result = subprocess.run([str(self.executable), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2",
                                 "-ar", str(SAMPLE_RATE), "-"], capture_output=True, check=True,
                                **hidden_process_kwargs())
        return np.frombuffer(result.stdout, np.float32)

    def test_parts_join_into_the_one_graph_mix_and_normalize_to_the_target(self) -> None:
        plan = _plan()
        whole = self._decode(self.pipeline.render(plan, self.paths, self.directory / "whole").path)
        clipwise = self._decode(self.pipeline.render(plan, self.paths, self.directory / "clipwise", workers=3).path)
        parts = render_parts(self.pipeline, plan, self.paths, self.directory / "parts", workers=3)
        self.assertGreater(len(parts), 1)
        joined = np.concatenate([np.fromfile(part.path, np.float32)[part.first * 2:(part.first + part.count) * 2]
                                 for part in parts])
        self.assertEqual(len(joined) // 2, round(199.0 * SAMPLE_RATE))  # two limited windows in a row: none lost
        # The same clip renders, folded in parts or at once: the very same samples.
        self.assertEqual(len(joined), len(clipwise))
        self.assertEqual(float(np.abs(joined - clipwise).max()), 0.0)
        # One graph for everything differs only in float rounding (FFmpeg picks other sample formats there).
        self.assertEqual(len(joined), len(whole))
        self.assertLess(float(np.abs(joined - whole).max()), 1e-4)

        output = self.directory / "mix.flac"
        chain = finish_parts(self.pipeline, parts, output, ["-c:a", "flac", "-ar", "48000", "-ac", "2"], workers=3)
        self.assertIsNotNone(chain)
        check = subprocess.run([str(self.executable), "-hide_banner", "-nostats", "-i", str(output),
                                "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", **hidden_process_kwargs()).stderr
        summary = check[check.rfind("Summary:"):]
        integrated = float(summary.split("I:")[1].split("LUFS")[0])
        peak = float(summary.split("Peak:")[1].split("dBFS")[0])
        self.assertAlmostEqual(integrated, -14.0, delta=0.3)
        self.assertLessEqual(peak, -1.4)
        self.assertEqual(len(self._decode(output)), len(whole))
        self.assertFalse(any(part.path.exists() for part in parts))  # intermediates are gone

    def test_export_parts_report_the_crossfade_when_effect_filters_are_unavailable(self) -> None:
        rendered = []
        plan = _plan()
        with patch("app.automix.parallel_mix.ffmpeg_supports_transition_dsp", return_value=False):
            parts = render_parts(self.pipeline, plan, self.paths, self.directory / "fallback", workers=2,
                                 plan_callback=rendered.append)
        self.assertEqual(sum(part.count for part in parts), round(199.0 * SAMPLE_RATE))
        self.assertEqual(rendered[0].clips, plan.clips)
        for original, actual in zip(plan.transitions, rendered[0].transitions, strict=True):
            self.assertEqual((actual.timeline_start, actual.duration), (original.timeline_start, original.duration))
            self.assertIsNone(actual.dsp)
            self.assertTrue(dict(actual.details)["dsp_fallback"])


if __name__ == "__main__":
    unittest.main()
