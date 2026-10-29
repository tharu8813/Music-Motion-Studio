from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.services.update_service import (
    GitHubUpdateService,
    LATEST_RELEASE_API,
    REPOSITORY_URL,
    ReleaseInfo,
    UpdateError,
    is_newer_version,
    normalized_version,
    automatic_setup_arguments,
    launch_setup,
)


class UpdateServiceTests(unittest.TestCase):
    def _payload(self, data: bytes = b"signed setup bytes") -> dict[str, object]:
        return {
            "tag_name": "v1.2.0",
            "name": "Music Motion Studio 1.2.0",
            "body": "## Changes\n\n- Faster export",
            "published_at": "2026-08-12T00:00:00Z",
            "html_url": "https://github.com/tharu8813/Music-Motion-Studio/releases/tag/v1.2.0",
            "draft": False,
            "prerelease": False,
            "assets": [{
                "name": "Music Motion Studio-1.2.0-setup.exe",
                "browser_download_url": (
                    "https://github.com/tharu8813/Music-Motion-Studio/releases/download/"
                    "v1.2.0/Music.Motion.Studio-1.2.0-setup.exe"
                ),
                "state": "uploaded",
                "size": len(data),
                "digest": f"sha256:{sha256(data).hexdigest()}",
            }],
        }

    def test_new_app_discovers_releases_only_from_current_repository(self) -> None:
        import json

        self.assertEqual(REPOSITORY_URL, "https://github.com/tharu8813/Music-Motion-Studio")
        self.assertEqual(
            LATEST_RELEASE_API,
            "https://api.github.com/repos/tharu8813/Music-Motion-Studio/releases/latest",
        )
        with patch(
            "app.services.update_service.urlopen",
            return_value=BytesIO(json.dumps(self._payload()).encode("utf-8")),
        ) as opener:
            release = GitHubUpdateService().fetch_latest_release()
        self.assertEqual(release.version, "1.2.0")
        self.assertTrue(release.can_install)
        self.assertEqual(opener.call_args.args[0].full_url, LATEST_RELEASE_API)

    def test_legacy_repository_redirect_result_is_rejected(self) -> None:
        legacy = "https://github.com/tharu8813/Playlist-Canvas/releases/"
        payload = self._payload()
        payload["html_url"] = legacy + "tag/v1.2.0"
        with self.assertRaises(UpdateError):
            GitHubUpdateService.release_from_payload(payload)

        payload = self._payload()
        payload["assets"][0]["browser_download_url"] = (
            legacy + "download/v1.2.0/Music.Motion.Studio-1.2.0-setup.exe"
        )
        with self.assertRaises(UpdateError):
            GitHubUpdateService.release_from_payload(payload)

    def test_similar_repository_name_is_not_trusted(self) -> None:
        payload = self._payload()
        payload["html_url"] = (
            "https://github.com/tharu8813/Music-Motion-Studio-fake/releases/tag/v1.2.0"
        )
        with self.assertRaises(UpdateError):
            GitHubUpdateService.release_from_payload(payload)

    def test_stable_versions_are_compared_numerically(self) -> None:
        self.assertEqual(normalized_version("v1.10.2"), (1, 10, 2, 0))
        self.assertEqual(normalized_version("1.0.0.0"), normalized_version("1.0.0"))
        self.assertTrue(is_newer_version("1.10.0", "1.9.9"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))

    def test_release_notes_and_verified_setup_asset_are_parsed(self) -> None:
        release = GitHubUpdateService.release_from_payload(self._payload())
        self.assertEqual(release.version, "1.2.0")
        self.assertIn("Faster export", release.body)
        self.assertTrue(release.can_install)
        self.assertEqual(len(release.asset_sha256), 64)

    def test_foreign_release_asset_is_rejected(self) -> None:
        payload = self._payload()
        assets = payload["assets"]
        assert isinstance(assets, list) and isinstance(assets[0], dict)
        assets[0]["browser_download_url"] = (
            "https://example.com/tharu8813/Playlist-Canvas/releases/download/evil.exe"
        )
        with self.assertRaises(UpdateError):
            GitHubUpdateService.release_from_payload(payload)

    def test_renamed_setup_assets_keep_the_same_security_checks(self) -> None:
        for stem in ("Music Motion Studio", "Music.Motion.Studio", "Music-Motion-Studio"):
            with self.subTest(stem=stem):
                payload = self._payload()
                asset = payload["assets"][0]
                asset["name"] = f"{stem}-1.2.0-setup.exe"
                release = GitHubUpdateService.release_from_payload(payload)
                self.assertTrue(release.can_install)
                self.assertEqual(release.asset_name, asset["name"])
                asset["browser_download_url"] = "https://example.com/setup.exe"
                with self.assertRaises(UpdateError):
                    GitHubUpdateService.release_from_payload(payload)

    def test_setup_download_requires_exact_size_and_sha256(self) -> None:
        data = b"verified Music Motion Studio Setup"
        release = GitHubUpdateService.release_from_payload(self._payload(data))
        updates: list[tuple[float, str]] = []
        with TemporaryDirectory(prefix="playlist-canvas-update-test-") as raw_directory:
            with patch(
                "app.services.update_service.urlopen",
                return_value=BytesIO(data),
            ):
                destination = GitHubUpdateService().download_setup(
                    release,
                    Path(raw_directory),
                    lambda fraction, message: updates.append((fraction, message)),
                    threading.Event(),
                )
            self.assertEqual(destination.read_bytes(), data)
            self.assertFalse(destination.with_suffix(".exe.download").exists())
        self.assertEqual(updates[-1][0], 1.0)

    def test_automatic_install_has_no_prompt_or_machine_restart_and_resumes_exact_snapshot(self) -> None:
        installer = Path("한글 경로/Playlist Canvas-setup.exe").resolve()
        snapshot = Path("다른 작업/unsaved.recovery.json").resolve()
        root = Path("Program Files/Playlist Canvas").resolve()
        with patch("app.services.update_service.installation_root", return_value=root):
            arguments = automatic_setup_arguments(installer, snapshot, 1234, korean=True)
        for flag in ("/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                     "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS", "/AUTOMATICUPDATE=1"):
            self.assertIn(flag, arguments)
        self.assertIn(f"/UPDATERECOVERY={snapshot}", arguments)
        self.assertIn("/UPDATEPID=1234", arguments)
        self.assertIn(f"/DIR={root}", arguments)
        self.assertFalse(any("localappdata" in arg for arg in arguments))
        self.assertIn("/LANG=korean", arguments)

    def test_setup_retains_original_user_and_reports_launch_errors(self) -> None:
        with patch("app.services.update_service.os.startfile", create=True) as start:
            self.assertTrue(launch_setup(Path("setup.exe"), ["/DIR=C:\\Program Files\\Playlist Canvas"]))
            start.assert_called_once_with("setup.exe", "open", '"/DIR=C:\\Program Files\\Playlist Canvas"')
            start.side_effect = OSError("UAC cancelled")
            self.assertFalse(launch_setup(Path("setup.exe"), []))


if __name__ == "__main__":
    unittest.main()
