from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from PySide6.QtGui import QColor, QImage

from app.automix.renderer import AutoMixRenderError
from app.models.audio_effects import AUDIO_EFFECTS
from app.models.playlist import PlaylistTrack
from app.renderer import loudness
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.utils.subprocess_utils import hidden_process_kwargs


def _ffprobe_for(ffmpeg_executable: Path) -> Path:
    name = "ffprobe.exe" if ffmpeg_executable.suffix.lower() == ".exe" else "ffprobe"
    return ffmpeg_executable.with_name(name)


def _audio_codec(ffmpeg_executable: Path, path: Path) -> str:
    """The audio stream's codec name, via ffprobe (e.g. "aac", "pcm_s16le")."""
    result = subprocess.run(
        [str(_ffprobe_for(ffmpeg_executable)), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, **hidden_process_kwargs(),
    )
    return json.loads(result.stdout)["streams"][0]["codec_name"]


def _measured_lufs(ffmpeg_executable: Path, path: Path) -> float:
    """Re-measure a file's own integrated loudness (LUFS) via loudnorm's own analysis pass."""
    result = subprocess.run(
        [str(ffmpeg_executable), "-hide_banner", "-loglevel", "info", "-i", str(path),
         "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, **hidden_process_kwargs(),
    )
    text = result.stderr
    start, end = text.rfind("{"), text.rfind("}")
    return float(json.loads(text[start:end + 1])["input_i"])


def _write_tone_wav(path: Path, frequency: float, duration: float, sample_rate: int = 48000) -> None:
    t = np.linspace(0.0, duration, int(duration * sample_rate), endpoint=False)
    signal = 0.5 * np.sin(2 * np.pi * frequency * t)
    pcm16 = (signal * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{len(pcm16)}h", *pcm16.tolist()))


def _renderer() -> FFmpegRenderer:
    renderer = FFmpegRenderer.__new__(FFmpegRenderer)
    renderer.executable = Path("ffmpeg")
    return renderer


class RenderAutomixAudioSegmentsUnitTests(unittest.TestCase):
    """No real FFmpeg needed: exercises the fallback/degradation logic only."""

    def test_missing_dependency_returns_none(self) -> None:
        renderer = _renderer()
        with patch.dict(sys.modules, {"app.automix.analysis.basic": None}):
            result = renderer._render_automix_audio_segments(
                [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)],
                Path("."), 30.0, None, __import__("threading").Event(),
            )
        self.assertIsNone(result)

    def test_render_failure_falls_back_to_none_not_an_exception(self) -> None:
        renderer = _renderer()
        with patch(
            "app.automix.renderer.AutoMixAudioPipeline.render",
            side_effect=AutoMixRenderError("boom"),
        ):
            result = renderer._render_automix_audio_segments(
                [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)],
                Path("."), 30.0, None, __import__("threading").Event(),
            )
        self.assertIsNone(result)

    def test_cancellation_propagates_instead_of_falling_back(self) -> None:
        import threading
        from app.renderer.ffmpeg_renderer import RenderCancelledError

        renderer = _renderer()
        cancel_event = threading.Event()
        cancel_event.set()
        with patch(
            "app.automix.renderer.AutoMixAudioPipeline.render",
            side_effect=AutoMixRenderError("cancelled"),
        ):
            with self.assertRaises(RenderCancelledError):
                renderer._render_automix_audio_segments(
                    [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)],
                    Path("."), 30.0, None, cancel_event,
                )

    def test_uses_the_auto_provider_policy(self) -> None:
        """Regression: Export/Preview rendering must resolve the analyzer
        with the same "auto" policy as the interactive playlist-badge
        analysis path (AutoMixAnalysisController), never a different one --
        see app/automix/analysis/registry.py."""
        import threading

        renderer = _renderer()
        with patch(
            "app.automix.analysis.registry.create_analysis_provider",
            side_effect=ValueError("stop here"),
        ) as create_provider:
            result = renderer._render_automix_audio_segments(
                [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)],
                Path("."), 30.0, None, threading.Event(),
            )
        self.assertIsNone(result)  # ValueError from provider selection degrades, not raises
        create_provider.assert_called_once()
        provider_id = create_provider.call_args.args[0]
        self.assertEqual(provider_id, "auto")

    def test_structure_analysis_feeds_compile_automix_when_sonara_is_available(self) -> None:
        """Commit C: Preview/Export must pass real structure data (from
        StructureAnalysisService + its own persistent cache, not
        MainWindow's UI-session dict) into compile_automix()."""
        import threading

        from app.automix.analysis.service import AnalysisBatchResult
        from app.automix.renderer import AutoMixRenderError

        renderer = _renderer()
        tracks = [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)]
        fake_structures = {"fake": "structures"}

        class _FakeStructureResult:
            analyses = fake_structures

        with (
            patch("app.automix.analysis.registry.create_analysis_provider"),
            patch(
                "app.automix.workflow.AutoMixWorkflow.analyze",
                return_value=AnalysisBatchResult(analyses={}, failures={}),
            ),
            patch("app.automix.structure.sonara.sonara_available", return_value=True),
            patch(
                "app.automix.structure.service.StructureAnalysisService.analyze_tracks",
                return_value=_FakeStructureResult(),
            ) as analyze_structure,
            patch(
                "app.automix.planner.compile_automix", side_effect=AutoMixRenderError("stop"),
            ) as compile_automix,
        ):
            result = renderer._render_automix_audio_segments(
                tracks, Path("."), 30.0, None, threading.Event(),
            )
        self.assertIsNone(result)
        analyze_structure.assert_called_once()
        compile_automix.assert_called_once()
        self.assertEqual(compile_automix.call_args.kwargs.get("structures"), fake_structures)

    def test_structure_analysis_is_skipped_when_sonara_is_unavailable(self) -> None:
        import threading

        from app.automix.analysis.service import AnalysisBatchResult
        from app.automix.renderer import AutoMixRenderError

        renderer = _renderer()
        tracks = [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)]

        with (
            patch("app.automix.analysis.registry.create_analysis_provider"),
            patch(
                "app.automix.workflow.AutoMixWorkflow.analyze",
                return_value=AnalysisBatchResult(analyses={}, failures={}),
            ),
            patch("app.automix.structure.sonara.sonara_available", return_value=False),
            patch(
                "app.automix.structure.service.StructureAnalysisService.analyze_tracks",
            ) as analyze_structure,
            patch(
                "app.automix.planner.compile_automix", side_effect=AutoMixRenderError("stop"),
            ) as compile_automix,
        ):
            renderer._render_automix_audio_segments(
                tracks, Path("."), 30.0, None, threading.Event(),
            )
        analyze_structure.assert_not_called()
        self.assertEqual(compile_automix.call_args.kwargs.get("structures"), {})


class RenderFixedCrossfadeAudioSegmentsUnitTests(unittest.TestCase):
    """No real FFmpeg needed: exercises the fallback/degradation logic only."""

    def test_missing_dependency_returns_none(self) -> None:
        renderer = _renderer()
        with patch.dict(sys.modules, {"app.automix.renderer": None}):
            result = renderer._render_fixed_crossfade_audio_segments(
                [PlaylistTrack("a.mp3", "A", duration_seconds=30.0)],
                Path("."), 30.0, 3.0, None, __import__("threading").Event(),
            )
        self.assertIsNone(result)

    def test_render_failure_falls_back_to_none_not_an_exception(self) -> None:
        renderer = _renderer()
        with patch(
            "app.automix.renderer.AutoMixAudioPipeline.render",
            side_effect=AutoMixRenderError("boom"),
        ):
            result = renderer._render_fixed_crossfade_audio_segments(
                [
                    PlaylistTrack("a.mp3", "A", duration_seconds=30.0),
                    PlaylistTrack("b.mp3", "B", duration_seconds=30.0),
                ],
                Path("."), 60.0, 3.0, None, __import__("threading").Event(),
            )
        self.assertIsNone(result)

    def test_cancellation_propagates_instead_of_falling_back(self) -> None:
        import threading
        from app.renderer.ffmpeg_renderer import RenderCancelledError

        renderer = _renderer()
        cancel_event = threading.Event()
        cancel_event.set()
        with patch(
            "app.automix.renderer.AutoMixAudioPipeline.render",
            side_effect=AutoMixRenderError("cancelled"),
        ):
            with self.assertRaises(RenderCancelledError):
                renderer._render_fixed_crossfade_audio_segments(
                    [
                        PlaylistTrack("a.mp3", "A", duration_seconds=30.0),
                        PlaylistTrack("b.mp3", "B", duration_seconds=30.0),
                    ],
                    Path("."), 60.0, 3.0, None, cancel_event,
                )


@unittest.skipUnless(
    os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip(),
    "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg AutoMix export checks.",
)
class RealAutomixExportIntegrationTests(unittest.TestCase):
    def test_playlist_effects_keep_duration_and_match_lossless_audio_export(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        renderer = FFmpegRenderer(executable)

        def decode(path: Path) -> np.ndarray:
            result = subprocess.run(
                [str(executable), "-v", "error", "-i", str(path), "-f", "f32le", "-"],
                capture_output=True, check=True, **hidden_process_kwargs(),
            )
            return np.frombuffer(result.stdout, np.float32).reshape(-1, 2)

        with TemporaryDirectory(prefix="project-audio-effects-") as raw_directory:
            directory = Path(raw_directory)
            path = directory / "tone.wav"
            _write_tone_wav(path, 440, 2.0)
            tracks = [PlaylistTrack(str(path), "Tone", duration_seconds=2.0)]
            settings = RenderSettings()
            dry = decode(renderer.prepare_playlist_audio(tracks, directory, settings, audio_codec="flac"))
            effects = {effect.key: {"enabled": True} for effect in AUDIO_EFFECTS}
            wet = decode(renderer.prepare_playlist_audio(
                tracks, directory, settings, audio_codec="flac", audio_effects=effects,
            ))
            self.assertEqual(dry.shape, (96000, 2))
            self.assertEqual(wet.shape, dry.shape)
            self.assertGreater(np.max(np.abs(wet[:, 0] - wet[:, 1])), 0.02)
            output = directory / "export.flac"
            renderer.render_audio(tracks, output, settings, audio_effects=effects)
            np.testing.assert_array_equal(decode(output), wet)

    def test_automix_export_uses_the_actual_mix_duration(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="automix-export-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 20.0)
            _write_tone_wav(b_path, 440.0, 20.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=20.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=20.0),
            ]
            renderer = FFmpegRenderer(executable)
            sequential_duration = renderer._timeline_duration(tracks)
            self.assertEqual(sequential_duration, 40.0)

            plans = []
            parts = renderer._render_automix_audio_segments(
                tracks, directory, sequential_duration, None, __import__("threading").Event(),
                plan_callback=plans.append,
            )
            self.assertIsNotNone(parts)
            self.assertEqual(len(plans), 1)
            self.assertLess(plans[0].duration_seconds, sequential_duration)
            self.assertAlmostEqual(sum(part.count for part in parts) / 48000, plans[0].duration_seconds, delta=0.05)
            for part in parts:
                # The intermediate AutoMix mix must be lossless, not a second
                # lossy AAC encode -- the final combine step already encodes to
                # AAC exactly once, after loudness normalization. Raw float32,
                # so blending/stretching over full scale is not clipped.
                self.assertTrue(part.path.is_file())
                self.assertEqual(part.path.suffix, ".f32")
                self.assertGreaterEqual(part.path.stat().st_size, (part.first + part.count) * 8)

    def test_full_render_with_automix_enabled_uses_mix_duration(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="automix-export-full-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 10.0)
            _write_tone_wav(b_path, 440.0, 10.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=10.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=10.0),
            ]
            image = QImage(32, 32, QImage.Format.Format_RGB32)
            image.fill(QColor(10, 20, 30))
            settings = RenderSettings(
                fps=5, video_codec="libx264", crf=30, preset="ultrafast",
                output_width=32, output_height=32,
            )
            renderer = FFmpegRenderer(executable)
            output_path = directory / "out.mp4"
            from app.automix.planner import compile_automix

            plans = []
            def record_plan(*args, **kwargs):
                plan = compile_automix(*args, **kwargs)
                plans.append(plan)
                return plan
            with patch("app.automix.planner.compile_automix", side_effect=record_plan):
                result = renderer.render(
                    [image, image], tracks, output_path, settings, transition_mode="automix",
                )
            self.assertTrue(output_path.is_file())
            self.assertEqual(len(plans), 1)
            self.assertLess(plans[0].duration_seconds, 20.0)
            self.assertAlmostEqual(result.validation.duration_seconds, plans[0].duration_seconds, delta=0.5)
            # The final mux still encodes audio exactly once, to AAC.
            self.assertEqual(_audio_codec(executable, output_path), "aac")
            # And loudness normalization must actually have run for this
            # ordinary, several-second real clip -- not silently skipped
            # the way it was before -loglevel info was added to the
            # measurement pass (which made loudnorm's own JSON stats
            # invisible in stderr, so every measurement looked like a
            # failure and normalization silently never applied).
            self.assertAlmostEqual(_measured_lufs(executable, output_path), loudness.TARGET_LUFS, delta=1.0)

    def test_crossfade_export_uses_the_actual_mix_duration(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="crossfade-export-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 20.0)
            _write_tone_wav(b_path, 440.0, 20.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=20.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=20.0),
            ]
            renderer = FFmpegRenderer(executable)
            sequential_duration = renderer._timeline_duration(tracks)
            self.assertEqual(sequential_duration, 40.0)

            segments = renderer._render_fixed_crossfade_audio_segments(
                tracks, directory, sequential_duration, 3.0, None, __import__("threading").Event(),
            )
            self.assertIsNotNone(segments)
            segment_paths, segment_durations = segments
            self.assertAlmostEqual(sum(segment_durations), 37.0, delta=0.05)
            # A 3s crossfade shortens the mix by 3s versus plain concatenation.
            self.assertAlmostEqual(segment_durations[0], 37.0, delta=0.1)
            # Lossless intermediate, same as AutoMix -- see the equivalent
            # assertion in test_automix_export_uses_the_actual_mix_duration.
            self.assertEqual(_audio_codec(executable, segment_paths[0]), "pcm_f32le")

    def test_crossfade_preserves_an_explicit_gap(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="crossfade-gap-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 10.0)
            _write_tone_wav(b_path, 440.0, 10.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=10.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=10.0, start_time_seconds=15.0),
            ]
            renderer = FFmpegRenderer(executable)
            sequential_duration = renderer._timeline_duration(tracks)
            self.assertEqual(sequential_duration, 25.0)

            segments = renderer._render_fixed_crossfade_audio_segments(
                tracks, directory, sequential_duration, 3.0, None, __import__("threading").Event(),
            )
            self.assertIsNotNone(segments)
            _segment_paths, segment_durations = segments
            # No overlap should have been applied across the explicit gap.
            self.assertAlmostEqual(sum(segment_durations), 25.0, delta=0.05)

    def test_full_render_with_crossfade_mode_uses_mix_duration(self) -> None:
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="crossfade-export-full-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 10.0)
            _write_tone_wav(b_path, 440.0, 10.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=10.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=10.0),
            ]
            image = QImage(32, 32, QImage.Format.Format_RGB32)
            image.fill(QColor(10, 20, 30))
            settings = RenderSettings(
                fps=5, video_codec="libx264", crf=30, preset="ultrafast",
                output_width=32, output_height=32,
            )
            renderer = FFmpegRenderer(executable)
            output_path = directory / "out.mp4"
            result = renderer.render(
                [image, image], tracks, output_path, settings,
                transition_mode="crossfade", crossfade_seconds=2.0,
            )
            self.assertTrue(output_path.is_file())
            self.assertAlmostEqual(result.validation.duration_seconds, 18.0, delta=0.5)
            self.assertEqual(_audio_codec(executable, output_path), "aac")
            self.assertAlmostEqual(_measured_lufs(executable, output_path), loudness.TARGET_LUFS, delta=1.0)

    def test_full_render_with_sequential_mode_encodes_audio_once_too(self) -> None:
        """transition_mode="none" must keep working exactly like before --
        this pipeline change only touches the AutoMix/crossfade intermediate,
        never the legacy sequential path."""
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"].strip())
        with TemporaryDirectory(prefix="sequential-export-full-") as raw_directory:
            directory = Path(raw_directory)
            a_path = directory / "a.wav"
            b_path = directory / "b.wav"
            _write_tone_wav(a_path, 440.0, 10.0)
            _write_tone_wav(b_path, 440.0, 10.0)
            tracks = [
                PlaylistTrack(str(a_path), "A", duration_seconds=10.0),
                PlaylistTrack(str(b_path), "B", duration_seconds=10.0),
            ]
            image = QImage(32, 32, QImage.Format.Format_RGB32)
            image.fill(QColor(10, 20, 30))
            settings = RenderSettings(
                fps=5, video_codec="libx264", crf=30, preset="ultrafast",
                output_width=32, output_height=32,
            )
            renderer = FFmpegRenderer(executable)
            output_path = directory / "out.mp4"
            result = renderer.render(
                [image, image], tracks, output_path, settings, transition_mode="none",
            )
            self.assertTrue(output_path.is_file())
            self.assertAlmostEqual(result.validation.duration_seconds, 20.0, delta=0.5)
            self.assertEqual(_audio_codec(executable, output_path), "aac")
            self.assertAlmostEqual(_measured_lufs(executable, output_path), loudness.TARGET_LUFS, delta=1.0)


if __name__ == "__main__":
    unittest.main()
