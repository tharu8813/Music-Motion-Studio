from __future__ import annotations

import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.models.project import ProjectSettings

from app.controllers.export_controller import ExportOrchestrator


class _Window:
    def __init__(self) -> None:
        self.project_settings = ProjectSettings(
            transition_mode="crossfade",
            crossfade_seconds=2.0,
        )
        self._export_storage_monitor = None
        self._active_export_output_path = None
        self.orchestrator = ExportOrchestrator(self)
        self.orchestrator.frames.staging = TemporaryDirectory(
            prefix="playlist-test-frames-before-"
        )

    def _stop_export_storage_monitor(self) -> None:
        self._export_storage_monitor = None

    def _cancel_export_png_pipeline(self) -> None:
        self.orchestrator.cancel_png_pipeline()

    def _clear_export_frame_staging(self) -> None:
        self.orchestrator.clear_frame_staging()

    @staticmethod
    def _format_bytes(count: int) -> str:
        return str(count)

    def cleanup(self) -> None:
        self.orchestrator.clear_audio_staging()
        self.orchestrator.clear_frame_staging()


class ExportAudioStagingRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = _Window()

    def tearDown(self) -> None:
        self.window.cleanup()

    def test_transition_audio_survives_frame_staging_recreation(self) -> None:
        old_frame_directory = Path(self.window.orchestrator.frames.staging.name)
        observed_directory: list[Path] = []

        def fake_prepare(
            _renderer, _tracks, directory, mode, seconds,
            _settings, _cancel_event, _progress, automix_settings=None, *, audio_effects=None,
        ):
            self.assertEqual(mode, "crossfade")
            self.assertEqual(seconds, 2.0)
            directory = Path(directory)
            observed_directory.append(directory)
            path = directory / "playlist_audio.m4a"
            path.write_bytes(b"prepared-audio")
            return path, SimpleNamespace(duration_seconds=10.0)

        with patch(
            "app.controllers.export_controller.prepare_audio_for_ui",
            side_effect=fake_prepare,
        ):
            audio_path, plan = self.window.orchestrator.prepare_transition_audio(
                object(), [object()], object(), threading.Event(),
            )

        self.assertIsNotNone(audio_path)
        self.assertEqual(plan.duration_seconds, 10.0)
        self.assertEqual(len(observed_directory), 1)
        self.assertNotEqual(observed_directory[0], old_frame_directory)
        self.assertTrue(audio_path.is_file())

        # This is the operation that triggered the real regression:
        # prepare_staging_space destroys/recreates frame staging.
        settings = SimpleNamespace(output_width=16, output_height=16, fps=1)
        with patch(
            "app.controllers.export_controller.shutil.disk_usage",
            return_value=SimpleNamespace(free=10**15),
        ):
            self.assertTrue(
                self.window.orchestrator.prepare_staging_space(
                    settings, 1.0, 1, False, korean=False,
                )
            )

        self.assertFalse(old_frame_directory.exists())
        self.assertTrue(
            audio_path.is_file(),
            "prepared AutoMix/crossfade audio must outlive frame staging relocation",
        )
        self.assertNotEqual(
            Path(self.window.orchestrator.frames.staging.name),
            observed_directory[0],
        )

        self.window.orchestrator.clear_audio_staging()
        self.assertFalse(audio_path.exists())

    def test_none_mode_does_not_prepare_blended_audio(self) -> None:
        self.window.project_settings.transition_mode = "none"
        with patch(
            "app.controllers.export_controller.prepare_audio_for_ui"
        ) as prepare:
            result = self.window.orchestrator.prepare_transition_audio(
                object(), [object()], object(), threading.Event(),
            )

        self.assertEqual(result, (None, None))
        prepare.assert_not_called()

    def test_effects_prepare_blended_audio_even_without_transitions(self) -> None:
        self.window.project_settings = ProjectSettings(audio_effects={"eight_d": {"enabled": True}})
        with patch("app.controllers.export_controller.prepare_audio_for_ui") as prepare:
            self.window.orchestrator.prepare_transition_audio(object(), [object()], object(), threading.Event())
        self.assertEqual(prepare.call_args.args[3], "none")
        self.assertTrue(prepare.call_args.kwargs["audio_effects"]["eight_d"]["enabled"])


if __name__ == "__main__":
    unittest.main()
