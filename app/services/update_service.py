"""Secure GitHub Releases update discovery and Setup download."""

from __future__ import annotations

from dataclasses import dataclass
import os
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading
from uuid import uuid4
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app import __version__
from app.services.maintenance_service import installation_root


REPOSITORY_URL = "https://github.com/tharu8813/Playlist-Canvas"
LATEST_RELEASE_API = "https://api.github.com/repos/tharu8813/Playlist-Canvas/releases/latest"
_VERSION_PATTERN = re.compile(
    r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$"
)
_SETUP_PATTERN = re.compile(r"playlist[\s._-]*canvas.*setup.*\.exe$", re.IGNORECASE)
_MAX_RELEASE_RESPONSE = 2 * 1024 * 1024
_MAX_SETUP_SIZE = 750 * 1024 * 1024

ProgressCallback = Callable[[float, str], None]


class UpdateError(RuntimeError):
    """A release could not be checked, validated, downloaded, or installed."""


class UpdateCancelled(UpdateError):
    """The user cancelled a Setup download."""


@dataclass(frozen=True)
class ReleaseInfo:
    """The stable GitHub release and its optional Windows Setup asset."""

    version: str
    tag_name: str
    name: str
    body: str
    published_at: str
    html_url: str
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    asset_sha256: str = ""

    @property
    def can_install(self) -> bool:
        return bool(self.asset_name and self.asset_url and self.asset_sha256)


def normalized_version(value: str) -> tuple[int, int, int, int]:
    """Return a comparable version, tolerating legacy four-part release tags."""
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise UpdateError(f"Unsupported release version: {value or '(empty)'}")
    major, minor, patch, revision = match.groups()
    return int(major), int(minor), int(patch), int(revision or 0)


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    """Compare stable application versions without a third-party dependency."""
    return normalized_version(candidate) > normalized_version(current)


def automatic_setup_arguments(installer: Path, recovery_path: Path, parent_pid: int, *, korean: bool) -> list[str]:
    """Update the running installation, then resume this exact workspace."""
    root = installation_root()
    return [
        "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
        "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS",
        "/AUTOMATICUPDATE=1", f"/UPDATEPID={parent_pid}",
        f"/UPDATERECOVERY={recovery_path.resolve()}",
        f"/UPDATEREADY={recovery_path.resolve().with_name(uuid4().hex + '.update-ready')}",
        *([f"/DIR={root}"] if root is not None else []),
        "/LANG=korean" if korean else "/LANG=english",
        f"/LOG={installer.with_suffix('.install.log').resolve()}",
    ]


def launch_setup(installer: Path, arguments: list[str]) -> bool:
    """Let Setup elevate itself, preserving its original user's unelevated token."""
    try:
        os.startfile(str(installer), "open", subprocess.list2cmdline(arguments))
    except OSError:
        return False
    return True


class GitHubUpdateService:
    """Read only the configured repository and download its verified Setup asset."""

    def fetch_latest_release(self) -> ReleaseInfo:
        request = Request(
            LATEST_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"PlaylistCanvas/{__version__}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read(_MAX_RELEASE_RESPONSE + 1)
        except HTTPError as error:
            if error.code == 404:
                raise UpdateError("No published GitHub release is available yet.") from error
            raise UpdateError(f"GitHub returned HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise UpdateError("The GitHub release service could not be reached.") from error
        if len(raw) > _MAX_RELEASE_RESPONSE:
            raise UpdateError("The GitHub release response was unexpectedly large.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("GitHub returned invalid release information.") from error
        return self.release_from_payload(payload)

    @staticmethod
    def release_from_payload(payload: object) -> ReleaseInfo:
        """Validate the latest-release response and select the Windows Setup asset."""
        if not isinstance(payload, dict):
            raise UpdateError("GitHub returned invalid release information.")
        if payload.get("draft") or payload.get("prerelease"):
            raise UpdateError("The latest GitHub release is not a stable public release.")
        tag_name = str(payload.get("tag_name", "")).strip()
        version = tag_name.removeprefix("v").removeprefix("V")
        normalized_version(version)
        html_url = str(payload.get("html_url", "")).strip()
        GitHubUpdateService._validate_release_page(html_url)

        selected: dict[str, object] | None = None
        assets = payload.get("assets", [])
        if isinstance(assets, list):
            candidates = [
                asset for asset in assets
                if isinstance(asset, dict)
                and str(asset.get("state", "uploaded")) == "uploaded"
                and _SETUP_PATTERN.search(str(asset.get("name", "")))
            ]
            if candidates:
                selected = candidates[0]

        asset_name = ""
        asset_url = ""
        asset_size = 0
        asset_sha256 = ""
        if selected is not None:
            asset_name = Path(str(selected.get("name", ""))).name
            asset_url = str(selected.get("browser_download_url", "")).strip()
            GitHubUpdateService._validate_asset_url(asset_url)
            try:
                asset_size = int(selected.get("size", 0) or 0)
            except (TypeError, ValueError) as error:
                raise UpdateError("The Setup asset has an invalid size.") from error
            digest = str(selected.get("digest", "") or "").strip().lower()
            if digest.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", digest[7:]):
                asset_sha256 = digest[7:]
        return ReleaseInfo(
            version=version,
            tag_name=tag_name,
            name=str(payload.get("name", "")).strip() or tag_name,
            body=str(payload.get("body", "") or "").strip(),
            published_at=str(payload.get("published_at", "") or "").strip(),
            html_url=html_url,
            asset_name=asset_name,
            asset_url=asset_url,
            asset_size=asset_size,
            asset_sha256=asset_sha256,
        )

    def download_setup(
        self,
        release: ReleaseInfo,
        target_directory: Path,
        progress: ProgressCallback | None,
        cancelled: threading.Event,
    ) -> Path:
        """Download, size-check, and SHA-256 verify a release Setup executable."""
        if not release.can_install:
            raise UpdateError("This release has no SHA-256-verified Playlist Canvas Setup asset.")
        self._validate_asset_url(release.asset_url)
        if release.asset_size < 1 or release.asset_size > _MAX_SETUP_SIZE:
            raise UpdateError("The Setup asset size is missing or outside the allowed range.")
        target_directory.mkdir(parents=True, exist_ok=True)
        destination = target_directory / Path(release.asset_name).name
        partial = destination.with_suffix(destination.suffix + ".download")
        request = Request(
            release.asset_url,
            headers={"User-Agent": f"PlaylistCanvas/{__version__}"},
        )
        digest = hashlib.sha256()
        downloaded = 0
        try:
            if partial.exists():
                partial.unlink()
            with urlopen(request, timeout=30) as response, partial.open("wb") as output:
                while True:
                    if cancelled.is_set():
                        raise UpdateCancelled("The update download was cancelled.")
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    downloaded += len(block)
                    if downloaded > release.asset_size or downloaded > _MAX_SETUP_SIZE:
                        raise UpdateError("The Setup download exceeded its declared size.")
                    output.write(block)
                    digest.update(block)
                    if progress:
                        progress(
                            downloaded / release.asset_size,
                            f"Downloaded {downloaded / 1024 / 1024:.1f} MB of "
                            f"{release.asset_size / 1024 / 1024:.1f} MB",
                        )
            if downloaded != release.asset_size:
                raise UpdateError("The Setup download size did not match the GitHub release.")
            if digest.hexdigest().lower() != release.asset_sha256.lower():
                raise UpdateError("The Setup SHA-256 verification failed.")
            partial.replace(destination)
        except UpdateCancelled:
            partial.unlink(missing_ok=True)
            raise
        except UpdateError:
            partial.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            partial.unlink(missing_ok=True)
            raise UpdateError("The Playlist Canvas Setup download failed.") from error
        if progress:
            progress(1.0, "Setup download and SHA-256 verification complete")
        return destination

    @staticmethod
    def _validate_release_page(url: str) -> None:
        parsed = urlsplit(url)
        expected = "/tharu8813/Playlist-Canvas/releases/"
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or expected not in parsed.path:
            raise UpdateError("GitHub returned an unexpected release page URL.")

    @staticmethod
    def _validate_asset_url(url: str) -> None:
        parsed = urlsplit(url)
        prefix = "/tharu8813/Playlist-Canvas/releases/download/"
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parsed.path.startswith(prefix):
            raise UpdateError("GitHub returned an unexpected Setup download URL.")
