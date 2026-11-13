"""The CI FFmpeg helper never skips verification or forgets GITHUB_ENV."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.ffmpeg.managed_installer import FFmpegInstallError
from scripts import setup_ci_ffmpeg


class TestCiFFmpegSetupTests(unittest.TestCase):
    def test_writes_path_of_verified_installation(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            environment_file = root / "github-env"
            with patch.dict(os.environ, {"RUNNER_TEMP":raw, "GITHUB_ENV":str(environment_file)}), \
                    patch.object(setup_ci_ffmpeg, "ManagedFFmpegInstaller") as installer:
                installer.return_value.install_latest.return_value = SimpleNamespace(
                    executable=root / "ffmpeg" / "bin" / "ffmpeg.exe",
                )
                self.assertEqual(setup_ci_ffmpeg.main(), 0)
            self.assertEqual(
                environment_file.read_text(encoding="utf-8"),
                f"PLAYLIST_CANVAS_TEST_FFMPEG={root / 'ffmpeg' / 'bin' / 'ffmpeg.exe'}\n",
            )
            installer.assert_called_once_with(root / "ffmpeg-tests-attempt-1")

    def test_retry_uses_fresh_destination(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            environment_file = root / "github-env"
            with patch.dict(os.environ, {"RUNNER_TEMP":raw, "GITHUB_ENV":str(environment_file)}), \
                    patch.object(setup_ci_ffmpeg, "ManagedFFmpegInstaller") as installer, \
                    patch.object(setup_ci_ffmpeg.time, "sleep") as sleep:
                installer.return_value.install_latest.side_effect = [
                    FFmpegInstallError("transient"),
                    SimpleNamespace(executable=root / "ffmpeg.exe"),
                ]
                self.assertEqual(setup_ci_ffmpeg.main(), 0)
            self.assertEqual(
                [call.args[0] for call in installer.call_args_list],
                [root / "ffmpeg-tests-attempt-1", root / "ffmpeg-tests-attempt-2"],
            )
            sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
