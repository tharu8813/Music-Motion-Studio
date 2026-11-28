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
            with patch.dict(os.environ, {"RUNNER_TEMP": raw, "GITHUB_ENV": str(environment_file), "GITHUB_PATH": ""}), \
                    patch.object(setup_ci_ffmpeg, "ManagedFFmpegInstaller") as installer:
                installer.return_value.install_release.return_value = SimpleNamespace(
                    executable=root / "ffmpeg" / "bin" / "ffmpeg.exe", version="9.0.2",
                )
                self.assertEqual(setup_ci_ffmpeg.main(), 0)
            self.assertEqual(
                environment_file.read_text(encoding="utf-8"),
                f"PLAYLIST_CANVAS_TEST_FFMPEG={root / 'ffmpeg' / 'bin' / 'ffmpeg.exe'}\n",
            )
            installer.assert_called_once_with(root / "ffmpeg-tests-attempt-1")
            installer.return_value.install_release.assert_called_once_with(setup_ci_ffmpeg.ci_release())

    def test_writes_verified_bin_to_optional_github_path(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            env = root / "github-env"
            github_path = root / "github-path"
            binary = root / "bin" / "ffmpeg.exe"
            with patch.dict(os.environ, {"RUNNER_TEMP": raw, "GITHUB_ENV": str(env),
                                         "GITHUB_PATH": str(github_path)}), \
                    patch.object(setup_ci_ffmpeg, "ManagedFFmpegInstaller") as installer:
                installer.return_value.install_release.return_value = SimpleNamespace(
                    executable=binary, version="9.0.2")
                self.assertEqual(setup_ci_ffmpeg.main(), 0)
            self.assertEqual(github_path.read_text(encoding="utf-8"), f"{binary.parent}\n")

    def test_ci_release_uses_one_dated_series_and_checksum_manifest(self) -> None:
        release = setup_ci_ffmpeg.ci_release()
        self.assertEqual(release.series, "9.0")
        self.assertNotEqual(release.release_tag, "latest")
        self.assertIn("/" + release.release_tag + "/", release.archive_url)
        self.assertTrue(release.archive_url.endswith(release.archive_name))
        self.assertEqual(release.checksum_url.rsplit("/", 1)[0],
                         release.archive_url.rsplit("/", 1)[0])
        self.assertTrue(release.checksum_url.endswith("/checksums.sha256"))

    def test_retry_uses_fresh_destination(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            environment_file = root / "github-env"
            with patch.dict(os.environ, {"RUNNER_TEMP": raw, "GITHUB_ENV": str(environment_file), "GITHUB_PATH": ""}), \
                    patch.object(setup_ci_ffmpeg, "ManagedFFmpegInstaller") as installer, \
                    patch.object(setup_ci_ffmpeg.time, "sleep") as sleep:
                installer.return_value.install_release.side_effect = [
                    FFmpegInstallError("transient"),
                    SimpleNamespace(executable=root / "ffmpeg.exe", version="9.0.2"),
                ]
                self.assertEqual(setup_ci_ffmpeg.main(), 0)
            self.assertEqual(
                [call.args[0] for call in installer.call_args_list],
                [root / "ffmpeg-tests-attempt-1", root / "ffmpeg-tests-attempt-2"],
            )
            sleep.assert_called_once_with(5)
            self.assertEqual(installer.return_value.install_release.call_count, 2)
            self.assertTrue(all(call.args == (setup_ci_ffmpeg.ci_release(),)
                                for call in installer.return_value.install_release.call_args_list))


if __name__ == "__main__":
    unittest.main()
