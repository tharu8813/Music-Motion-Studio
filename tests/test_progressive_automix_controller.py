from __future__ import annotations

import math
import os
import subprocess
import threading
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.automix.progressive import RENDER_DEBOUNCE_SECONDS
from app.controllers import progressive_automix_controller as module
from app.controllers.preview_audio_controller import PreviewAudioController
from app.controllers.progressive_automix_controller import ProgressiveAutoMixController
from app.utils.subprocess_utils import hidden_process_kwargs
from tests.test_automix_planner import _analysis, _track

APP = QApplication.instance() or QApplication([])


class ProgressiveControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracks = [_track(f"t{index}", 240.0) for index in range(6)]
        self.analyses = {t.id: _analysis(t.id, 120.0, 240.0) for t in self.tracks}
        self.partials: list = []
        self.finals: list = []
        for target, value in (
            (patch.object(module._AnalysisWorker, "start"), None),
            (patch.object(module._PartialRenderWorker, "start",
                          lambda worker: self.partials.append(worker)), None),
            (patch.object(PreviewAudioController, "start",
                          lambda controller, *args, **kwargs: self.finals.append((controller, args, kwargs))), None),
            (patch("app.automix.structure.sonara.sonara_available", return_value=False), None),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.controller = ProgressiveAutoMixController(SimpleNamespace(executable=Path("ffmpeg")), korean=False)
        self.addCleanup(self.controller.shutdown)
        self.messages: list[str] = []
        self.controller.progress.connect(lambda _stage, _fraction, message: self.messages.append(message))
        self.controller.start(self.tracks, Path("unused"), "automix", 3.0)

    def analyze(self, *track_ids: str, generation: int | None = None) -> None:
        worker = self.controller._analysis_worker
        for track_id in track_ids:
            worker.rhythm_done.emit(self.controller._generation if generation is None else generation,
                                    track_id, self.analyses[track_id])

    def settle(self) -> None:
        QTest.qWait(round(RENDER_DEBOUNCE_SECONDS * 1000) + 250)

    def test_a_burst_of_analysis_events_starts_one_partial_render_of_the_latest_plan(self) -> None:
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1", "t2", "t3", "t4")  # t5 still analyzing
        self.assertEqual(self.partials, [])  # debounced
        self.settle()
        self.assertEqual(len(self.partials), 1)
        self.assertEqual(self.partials[0]._track_count, 5)
        self.assertEqual(len(self.partials[0]._plan.audio.transitions), 4)
        self.assertTrue(any("Analyzing beats & vocals · 5/6 done" in message
                            and "transitions planned 4/5" in message for message in self.messages), self.messages)

    def test_nothing_renders_until_preview_attaches(self) -> None:
        self.analyze("t0", "t1", "t2")
        self.settle()
        self.assertEqual(self.partials, [])

    def test_fully_cached_analysis_skips_partial_mixes_and_runs_the_export_pipeline_once(self) -> None:
        self.analyze(*(t.id for t in self.tracks))
        self.controller.report_playhead(0.0, True)
        self.settle()
        self.assertEqual(self.partials, [])
        self.assertEqual(len(self.finals), 1)
        self.assertEqual(self.finals[0][1][2], "automix")
        self.assertEqual(self.controller.render_count, 0)

    def test_partial_result_is_forwarded_and_stale_generations_are_dropped(self) -> None:
        received = []
        self.controller.progressive_ready.connect(lambda *args: received.append(args))
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1")
        self.settle()
        worker = self.partials[0]
        worker.ready.emit(worker._generation - 1, "stale.flac", worker._plan, 480.0, 0.5)
        self.assertEqual(received, [])
        worker.ready.emit(worker._generation, "mix.flac", worker._plan, 480.0, 0.5)
        self.assertEqual(received, [("mix.flac", worker._plan, 480.0, 0.5)])
        self.assertIn("AutoMix playable through track 2", self.messages[-1])

    def test_results_from_a_cancelled_run_never_reach_the_next_one(self) -> None:
        old_generation = self.controller._generation
        self.controller.start(self.tracks, Path("unused"), "automix", 3.0)
        self.analyze("t0", "t1", "t2", generation=old_generation)
        self.assertEqual(self.controller._state.frontier(), 0)

    def test_final_mix_is_emitted_after_analysis_completes(self) -> None:
        ready = []
        self.controller.audio_ready.connect(lambda path, plan: ready.append((path, plan)))
        self.analyze(*(t.id for t in self.tracks))
        final_controller = self.finals[0][0]
        final_controller.audio_ready.emit("final.m4a", "plan")
        self.assertEqual(ready, [("final.m4a", "plan")])

    def test_a_failed_partial_render_disables_partials_but_still_finalizes(self) -> None:
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1")
        self.settle()
        worker = self.partials[0]
        worker.failed.emit(worker._generation, "boom")
        self.analyze("t2", "t3")
        self.settle()
        self.assertEqual(len(self.partials), 1)
        self.assertFalse(self.controller._timer.isActive())  # no 10 ms re-arm loop while partials are off
        self.analyze("t4", "t5")
        self.assertEqual(len(self.finals), 1)

    def test_given_settings_plan_the_partial_mixes_and_reach_the_final_export_pipeline(self) -> None:
        from app.automix.renderer import _DSP_REQUIRED_FILTERS
        from app.automix.settings import AutoMixTransitionSettings

        energetic = AutoMixTransitionSettings(enabled=True, preferred_bars=4, max_transition_seconds=12.0)
        # This is a controller unit test with a dummy executable, not a probe
        # of whatever FFmpeg happens to be on the Windows runner's PATH.
        with patch("app.automix.renderer.ffmpeg_filter_names",
                   return_value=frozenset(_DSP_REQUIRED_FILTERS) | {"rubberband"}):
            self.controller.start(self.tracks, Path("unused"), "automix", 3.0, automix_settings=energetic)
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1")
        self.settle()
        transition = self.partials[-1]._plan.audio.transitions[0]
        self.assertAlmostEqual(transition.duration, 8.0)  # 4 bars at 120 BPM, Auto would pick 8 bars
        self.analyze(*(t.id for t in self.tracks[2:]))
        self.partials[-1].ready.emit(self.partials[-1]._generation, "mix.flac", self.partials[-1]._plan, 1.0, 1.0)
        self.assertIs(self.finals[-1][2]["automix_settings"], energetic)  # Preview final == Export settings

    def test_missing_ffmpeg_filters_keep_resolved_settings_for_final_mix(self) -> None:
        from dataclasses import replace
        from app.automix.settings import AutoMixTransitionSettings

        requested = AutoMixTransitionSettings(enabled=True, preferred_bars=4, max_transition_seconds=12.0)
        with patch("app.automix.renderer.ffmpeg_filter_names", return_value=frozenset()):
            self.controller.start(self.tracks, Path("unused"), "automix", 3.0, automix_settings=requested)
        expected = replace(requested, allow_pitch_shift=False, allow_tape_stop=False)
        self.assertEqual(self.controller._settings, expected)
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1")
        self.settle()
        worker = self.partials[-1]
        self.assertAlmostEqual(worker._plan.audio.transitions[0].duration, 8.0)
        self.analyze(*(t.id for t in self.tracks[2:]))
        worker.ready.emit(worker._generation, "mix.flac", worker._plan, 1.0, 1.0)
        self.assertIs(self.finals[-1][2]["automix_settings"], self.controller._settings)
        self.assertEqual(self.finals[-1][2]["automix_settings"], expected)

    def test_the_playlist_level_reaches_preview_before_any_mix(self) -> None:
        levels = []
        self.controller.level_ready.connect(levels.append)
        worker = self.controller._analysis_worker
        worker.level_done.emit(self.controller._generation - 1, 0.9)  # a cancelled run's
        worker.level_done.emit(self.controller._generation, 0.5)
        self.assertEqual(levels, [0.5])
        self.assertEqual(self.controller.playback_gain, 0.5)
        self.controller.report_playhead(0.0, True)
        self.analyze("t0", "t1")
        self.settle()
        self.assertEqual(self.partials[0]._gain, 0.5)  # partial mixes share the per-track level

    def test_a_restarted_run_ignores_the_old_final_mix(self) -> None:
        ready = []
        self.controller.audio_ready.connect(lambda path, plan: ready.append(path))
        self.analyze(*(t.id for t in self.tracks))
        old_final = self.finals[0][0]
        self.controller.start(self.tracks, Path("unused"), "automix", 3.0)
        self.messages.clear()
        old_final.progress.emit("Combining audio", 0.55, "Measuring loudness 5.0s / 10.0s · 50%")
        old_final.audio_ready.emit("old.flac", "plan")
        self.assertEqual(ready, [])
        self.assertFalse(any("loudness" in message for message in self.messages))

    def test_progress_is_monotonic_and_names_the_final_stages(self) -> None:
        fractions = []
        self.controller.progress.connect(lambda _stage, fraction, _message: fractions.append(fraction))
        self.analyze(*(t.id for t in self.tracks))
        final_controller = self.finals[0][0]
        final_controller.progress.emit("Combining audio", 0.54, "Measuring loudness 30.0s / 600.0s · 5%")
        self.settle()  # coalesced redraw
        self.assertIn("Final mix · Measuring the mix loudness · 00:30 / 10:00", self.messages[-1])
        final_controller.progress.emit("Combining audio", 0.64, "Normalizing loudness and saving audio 600.0s / 600.0s · 100%")
        self.settle()
        self.assertLess(fractions[-1], 1.0)  # not complete before the mix is playable
        from app.automix.planner import compile_automix
        from app.automix.settings import AUTOMIX_SETTINGS
        final_controller.audio_ready.emit("final.flac", compile_automix(self.tracks, self.analyses, AUTOMIX_SETTINGS))
        self.assertEqual(fractions, sorted(fractions))
        self.assertEqual(fractions[-1], 1.0)
        self.assertEqual(self.messages[-1], "AutoMix ready")

    def test_nothing_is_published_after_the_final_mix_is_ready(self) -> None:
        # A coalesced update still pending when the mix landed used to arrive
        # after audio_ready, reviving Preview's finished status-bar task.
        from app.timeline.compiler import compile_playlist

        self.analyze(*(t.id for t in self.tracks))
        final_controller = self.finals[0][0]
        final_controller.progress.emit("Combining audio", 0.5, "Normalizing loudness 1.0s / 600.0s · 1%")
        final_controller.progress.emit("Combining audio", 0.6, "Normalizing loudness 2.0s / 600.0s · 2%")
        self.assertTrue(self.controller._publish_timer.isActive())  # the second one was coalesced
        after_ready: list[str] = []
        self.controller.audio_ready.connect(
            lambda *_args: self.controller.progress.connect(lambda *update: after_ready.append(update)))
        final_controller.audio_ready.emit("final.flac", compile_playlist(self.tracks))
        self.settle()
        self.assertEqual(after_ready, [])

    def test_a_final_mix_without_transitions_is_not_reported_as_automix(self) -> None:
        from app.timeline.compiler import compile_playlist

        self.analyze(*(t.id for t in self.tracks))
        self.finals[0][0].audio_ready.emit("final.flac", compile_playlist(self.tracks))
        self.assertIn("back to back", self.controller.fallback_message)
        self.assertIn("back to back", self.messages[-1])

    def test_several_tracks_analyzing_at_once_are_all_shown(self) -> None:
        worker = self.controller._analysis_worker
        for track_id, step in (("t0", "vocals"), ("t1", "beat_model"), ("t2", "decode")):
            worker.rhythm_step.emit(self.controller._generation, track_id, step)
        worker.rhythm_step.emit(self.controller._generation, "t3", "cached")
        self.settle()
        self.assertIn("now: t0(vocals), t1(beat model) +1 more · 1 from cache", self.messages[-1])


def _loudness(executable: Path, path: Path, seconds: float) -> float:
    """Integrated LUFS of the first ``seconds`` of ``path``."""
    result = subprocess.run(
        [str(executable), "-hide_banner", "-nostats", "-t", f"{seconds:.3f}", "-i", str(path),
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **hidden_process_kwargs(),
    )
    return float(module._LUFS.findall(result.stderr)[-1])


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(), "Real FFmpeg is opt-in")
class RealProgressiveRenderTests(unittest.TestCase):
    """A real partial mix, then the real export pipeline, on the same playlist."""

    def test_partial_mix_duration_final_parity_and_level_match(self) -> None:
        import numpy as np

        from app.automix.analysis.service import AnalysisBatchResult
        from app.automix.planner import compile_automix
        from app.automix.progressive import ProgressiveAnalysis, partial_plan
        from app.automix.settings import AutoMixTransitionSettings
        from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings

        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="progressive-real-") as raw:
            directory = Path(raw)
            tracks = [_track(f"t{index}", 60.0) for index in range(4)]
            rng = np.random.default_rng(5)
            for index, (track, amplitude) in enumerate(zip(tracks, (0.5, 0.25, 0.4, 0.3))):
                track.file_path = str(directory / f"{track.id}.wav")
                t = np.arange(60 * 48000) / 48000
                mono = amplitude * (np.sin(2 * np.pi * (110 + 20 * index) * t) + 0.3 * rng.standard_normal(len(t)))
                with wave.open(track.file_path, "wb") as handle:
                    handle.setnchannels(2)
                    handle.setsampwidth(2)
                    handle.setframerate(48000)
                    handle.writeframes((np.clip(np.stack([mono, mono], 1), -1, 1) * 32767).astype("<i2").tobytes())
            analyses = {t.id: _analysis(t.id, 120.0, 60.0) for t in tracks}
            settings = AutoMixTransitionSettings(enabled=True)

            # Partial: first two tracks analyzed.
            state = ProgressiveAnalysis(tracks, structure_enabled=False)
            for track in tracks[:2]:
                state.record_rhythm(track.id, analyses[track.id])
            plan = partial_plan(tracks, state, settings)
            ready = []
            worker = module._PartialRenderWorker(
                executable, plan, 2, tracks, directory / "partial", 1, threading.Event(), None,
            )
            worker.ready.connect(lambda *args: ready.append(args))
            worker.run()  # synchronously, in this thread
            (_generation, path, _plan, covered_until, gain), = ready
            decoded = subprocess.run(
                [str(executable), "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-"],
                capture_output=True, check=True, **hidden_process_kwargs(),
            ).stdout
            samples = np.frombuffer(decoded, dtype=np.float32)
            self.assertTrue(np.isfinite(samples).all())
            self.assertLessEqual(abs(len(samples) / 2 / 48000 - covered_until), 0.03)

            # Final: the unmodified export pipeline, with structure analysis off for both.
            plans = []
            with (
                patch("app.automix.workflow.AutoMixWorkflow.analyze", return_value=AnalysisBatchResult(analyses, {})),
                patch("app.automix.structure.sonara.sonara_available", return_value=False),
            ):
                final_path = FFmpegRenderer(executable).prepare_playlist_audio(
                    tracks, directory / "final", RenderSettings(), "automix", plan_callback=plans.append,
                )
            for track in tracks[2:]:
                state.record_rhythm(track.id, analyses[track.id])
            self.assertEqual(partial_plan(tracks, state, settings), plans[0])
            self.assertEqual(plans[0], compile_automix(tracks, analyses, settings))

            # Same stretch of music, partial vs final level.
            partial_lufs = _loudness(executable, Path(path), covered_until)
            final_lufs = _loudness(executable, final_path, covered_until)
            print(f"\n    partial {partial_lufs:.2f} LUFS vs final {final_lufs:.2f} LUFS "
                  f"(gain {20 * math.log10(gain):+.2f} dB) over the first {covered_until:.1f}s")
            self.assertLess(abs(partial_lufs - final_lufs), 1.5)


if __name__ == "__main__":
    unittest.main()
