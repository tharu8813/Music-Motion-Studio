"""Verified per-user FFmpeg installer for supported Windows desktops."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import uuid
import zipfile
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from app import __version__
from app.utils.subprocess_utils import hidden_process_kwargs


class FFmpegInstallError(RuntimeError):
    """Raised when a managed FFmpeg installation cannot be completed safely."""


class FFmpegInstallCancelled(FFmpegInstallError):
    """Raised after a user cancels a managed installation."""


@dataclass(frozen=True, slots=True)
class ManagedFFmpegInstallation:
    """Verified executable and version returned by a completed installation."""

    executable: Path
    version: str
    series: str = ""
    release_tag: str = ""


@dataclass(frozen=True, slots=True)
class FFmpegReleaseOption:
    """One selectable, checksum-verified Windows GPL build."""

    series: str
    build: str
    release_tag: str
    release_name: str
    published_at: str
    archive_name: str
    archive_url: str
    checksum_url: str
    notes: str
    recommended: bool = False

    @property
    def install_key(self) -> str:
        return f"{self.series}-{self.build}"


ProgressCallback = Callable[[str, float, str], None]


class ManagedFFmpegInstaller:
    """Downloads BtbN's GPL Windows build and verifies its release checksum."""

    release_api_url = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
    releases_url = "https://github.com/BtbN/FFmpeg-Builds/releases"
    # Compatibility baseline selected for the original 1.0.x releases. Change this
    # deliberately with an app patch after the render suite passes against a
    # newer stable branch.
    recommended_series = "9.0"
    archive_name = "ffmpeg-master-latest-win64-gpl.zip"
    checksum_name = "checksums.sha256"
    max_text_bytes = 16 * 1024 * 1024
    max_archive_bytes = 2 * 1024 * 1024 * 1024
    max_extracted_bytes = 8 * 1024 * 1024 * 1024
    max_archive_members = 100_000

    def __init__(self, install_root: Path | None = None) -> None:
        self.install_root = install_root or self.default_install_root()

    @staticmethod
    def default_install_root() -> Path:
        """Return the app-only Windows installation folder without requiring admin rights."""
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "PlaylistCanvas" / "tools" / "ffmpeg"

    def current_installation(self) -> ManagedFFmpegInstallation | None:
        """Read the latest completed managed installation, if it still validates."""
        installation = self.recorded_installation()
        return installation if installation and self._is_runnable(installation.executable) else None

    def recorded_installation(self) -> ManagedFFmpegInstallation | None:
        """Read the activation manifest even when its executable is damaged.

        Keeping this separate from :meth:`current_installation` lets Settings
        offer a safe Delete/Reinstall recovery path for an incomplete install.
        """
        manifest = self.install_root / "current.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            executable = Path(str(data["executable"]))
            version = str(data["version"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return ManagedFFmpegInstallation(
            executable, version,
            str(data.get("series", "")), str(data.get("release_tag", "")),
        )

    def available_releases(
        self, cancel_event: threading.Event | None = None,
    ) -> list[FFmpegReleaseOption]:
        """Return every stable/master win64 GPL build in the latest manifest."""
        cancelled = cancel_event or threading.Event()
        release = self._read_json(self.release_api_url, cancelled)
        tag = str(release.get("tag_name", "latest") or "latest").strip()
        name = str(release.get("name", tag) or tag).strip()
        published_at = str(
            release.get("published_at", release.get("updated_at", "")) or ""
        ).strip()
        body = str(release.get("body", "") or "")
        raw_assets = release.get("assets", [])
        if not isinstance(raw_assets, list):
            raise FFmpegInstallError("The FFmpeg release asset list is invalid.")
        assets = [asset for asset in raw_assets if isinstance(asset, dict)]
        checksum_url = next((
            str(asset.get("browser_download_url", ""))
            for asset in assets if asset.get("name") == self.checksum_name
        ), "")
        if not checksum_url:
            raise FFmpegInstallError("The FFmpeg release has no checksum manifest.")
        options: list[FFmpegReleaseOption] = []
        for asset in assets:
            archive_name = str(asset.get("name", ""))
            parsed = self._parse_archive_version(archive_name, body)
            archive_url = str(asset.get("browser_download_url", ""))
            if parsed is None or not archive_url:
                continue
            series, build = parsed
            options.append(FFmpegReleaseOption(
                series=series,
                build=build,
                release_tag=tag,
                release_name=name,
                published_at=published_at,
                archive_name=archive_name,
                archive_url=archive_url,
                checksum_url=checksum_url,
                notes=self._release_notes(body, series, build),
                recommended=series == self.recommended_series,
            ))
        if not options:
            raise FFmpegInstallError("No supported Windows GPL FFmpeg builds were found.")
        return sorted(options, key=self._release_sort_key)

    @classmethod
    def _parse_archive_version(
        cls, archive_name: str, release_body: str,
    ) -> tuple[str, str] | None:
        """Accept static win64 GPL archives, excluding shared/debug/LTO builds."""
        if not archive_name.endswith(".zip") or "-win64-gpl" not in archive_name:
            return None
        if any(marker in archive_name for marker in ("-shared", "-debug", "-lto")):
            return None
        stable = re.search(r"-win64-gpl-(\d+\.\d+)\.zip$", archive_name)
        series = stable.group(1) if stable else "master"
        build = "latest"
        if series == "master":
            match = re.search(r"master\s+`([^`]+)`", release_body, re.IGNORECASE)
        else:
            match = re.search(
                rf"(?:^|\n)\s*{re.escape(series)}\s+`([^`]+)`",
                release_body,
            )
        if match:
            build = match.group(1).strip()
        return series, build

    @staticmethod
    def _release_notes(body: str, series: str, build: str) -> str:
        """Keep a readable upstream release excerpt for confirmation dialogs."""
        normalized = "\n".join(line.rstrip() for line in body.splitlines()).strip()
        heading = f"FFmpeg {series} · {build}"
        if not normalized:
            return heading
        # GitHub's generated body can contain large asset tables. Preserve a
        # bounded excerpt so the UI remains responsive and Detailed Text stays useful.
        return f"{heading}\n\n{normalized[:6000]}"

    @classmethod
    def _release_sort_key(cls, option: FFmpegReleaseOption) -> tuple[int, tuple[int, ...]]:
        if option.recommended:
            return (0, ())
        if option.series == "master":
            return (2, ())
        try:
            parts = tuple(-int(part) for part in option.series.split("."))
        except ValueError:
            parts = ()
        return (1, parts)

    def install_latest(self, progress: ProgressCallback | None = None,
                       cancel_event: threading.Event | None = None) -> ManagedFFmpegInstallation:
        """Download, checksum-verify, extract, test, and atomically activate FFmpeg."""
        cancelled = cancel_event or threading.Event()
        self._report(progress, "Preparing download", 0.02, "Reading the verified release manifest")
        try:
            releases = self.available_releases(cancelled)
            release = next((entry for entry in releases if entry.recommended), releases[0])
        except FFmpegInstallError:
            self._report(
                progress, "Preparing download", 0.04,
                "GitHub API unavailable; using the official recommended release links",
            )
            tag = "latest"
            base = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
            archive_name = (
                f"ffmpeg-n{self.recommended_series}-latest-win64-gpl-"
                f"{self.recommended_series}.zip"
            )
            archive_url = f"{base}/{archive_name}"
            checksum_url = f"{base}/{self.checksum_name}"
            release = FFmpegReleaseOption(
                self.recommended_series, "latest", tag, tag, "",
                archive_name, archive_url, checksum_url,
                f"FFmpeg {self.recommended_series} latest compatible build", True,
            )
        return self.install_release(release, progress, cancelled)

    def install_release(
        self, release: FFmpegReleaseOption,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None, *, force: bool = False,
    ) -> ManagedFFmpegInstallation:
        """Install one selected release and atomically activate it."""
        cancelled = cancel_event or threading.Event()
        tag = release.release_tag
        archive_url = release.archive_url
        checksum_url = release.checksum_url

        self.install_root.mkdir(parents=True, exist_ok=True)
        target = self.install_root / "versions" / self._safe_version(release.install_key)
        if target.exists():
            executable = self._find_executable(target)
            if not force and executable and self._is_runnable(executable):
                installation = ManagedFFmpegInstallation(
                    executable, release.build, release.series, tag,
                )
                self._activate(installation)
                self._report(progress, "Complete", 1.0, "Using the existing verified FFmpeg version")
                return installation
            if not force:
                raise FFmpegInstallError(
                "An incomplete FFmpeg version folder already exists. Remove it manually before retrying."
                )

        with TemporaryDirectory(prefix="ffmpeg-download-", dir=self.install_root) as temporary:
            temporary_directory = Path(temporary)
            self._report(progress, "Preparing download", 0.05, "Downloading the checksum manifest")
            checksum_text = self._download_text(checksum_url, cancelled)
            expected_hash = self._checksum_for_archive(checksum_text, release.archive_name)
            if expected_hash is None:
                raise FFmpegInstallError("The release checksum manifest does not list the FFmpeg archive.")
            self._report(progress, "Preparing download", 0.07, "Checksum found; starting FFmpeg download")
            archive = temporary_directory / release.archive_name
            self._download_file(archive_url, archive, progress, cancelled)
            self._raise_if_cancelled(cancelled)
            actual_hash = self._sha256(archive)
            if actual_hash.lower() != expected_hash.lower():
                raise FFmpegInstallError("FFmpeg archive checksum verification failed; no files were installed.")
            self._report(progress, "Extracting", 0.86, "Checksum verified; extracting archive safely")
            extracted = temporary_directory / "extracted"
            self._safe_extract(archive, extracted)
            executable = self._find_executable(extracted)
            if executable is None or not self._is_runnable(executable):
                raise FFmpegInstallError("The verified archive did not contain a runnable ffmpeg.exe.")
            self._raise_if_cancelled(cancelled)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.parent / f".{target.name}-{uuid.uuid4().hex}.installing"
            shutil.move(str(executable.parent.parent), str(staging))
            backup = target.parent / f".{target.name}-{uuid.uuid4().hex}.backup"
            try:
                if target.exists():
                    target.replace(backup)
                staging.replace(target)
            except OSError as error:
                shutil.rmtree(staging, ignore_errors=True)
                if backup.exists() and not target.exists():
                    backup.replace(target)
                raise FFmpegInstallError("Could not activate the verified FFmpeg installation.") from error
            final_executable = self._find_executable(target)
            if final_executable is None or not self._is_runnable(final_executable):
                shutil.rmtree(target, ignore_errors=True)
                if backup.exists():
                    backup.replace(target)
                raise FFmpegInstallError("FFmpeg failed its final execution check after installation.")
            shutil.rmtree(backup, ignore_errors=True)
            probed_version = self._probe_version(final_executable) or release.build
            installation = ManagedFFmpegInstallation(
                final_executable, probed_version, release.series, tag,
            )
            self._activate(installation)
        self._report(progress, "Complete", 1.0, "FFmpeg was installed and verified")
        return installation

    def uninstall_current(self) -> ManagedFFmpegInstallation | None:
        """Remove only the active app-managed version and its activation manifest."""
        # A broken executable must still be removable through Settings. The
        # resolved-path guard below keeps deletion scoped to our versions root.
        installation = self.recorded_installation()
        if installation is None:
            return None
        versions_root = (self.install_root / "versions").resolve()
        executable = installation.executable.resolve()
        try:
            relative = executable.relative_to(versions_root)
        except ValueError as error:
            raise FFmpegInstallError(
                "The configured FFmpeg is not an app-managed installation."
            ) from error
        if not relative.parts:
            raise FFmpegInstallError("The managed FFmpeg path is invalid.")
        version_root = versions_root / relative.parts[0]
        try:
            shutil.rmtree(version_root)
            (self.install_root / "current.json").unlink(missing_ok=True)
        except OSError as error:
            raise FFmpegInstallError("Could not remove the managed FFmpeg installation.") from error
        return installation

    @staticmethod
    def _probe_version(executable: Path) -> str:
        try:
            result = subprocess.run(
                [str(executable), "-version"], capture_output=True, text=True,
                timeout=10, check=False, **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        match = re.match(r"ffmpeg version\s+(\S+)", first_line, re.IGNORECASE)
        return match.group(1) if match else ""

    def _release_assets(
        self, cancelled: threading.Event, progress: ProgressCallback | None = None,
    ) -> tuple[str, str, str]:
        """Resolve release assets, falling back to BtbN's stable latest URLs."""
        try:
            release = self._read_json(self.release_api_url, cancelled)
            tag = str(release.get("tag_name", "")).strip()
            assets = {
                str(asset.get("name", "")): str(asset.get("browser_download_url", ""))
                for asset in release.get("assets", []) if isinstance(asset, dict)
            }
            archive_url = assets.get(self.archive_name, "")
            checksum_url = assets.get(self.checksum_name, "")
            if tag and archive_url and checksum_url:
                return tag, archive_url, checksum_url
        except FFmpegInstallCancelled:
            raise
        except FFmpegInstallError:
            pass

        # The floating `latest` release is maintained by the same upstream and
        # contains both the archive and checksum. This also avoids GitHub API
        # rate-limit failures while retaining end-to-end SHA-256 verification.
        self._report(
            progress, "Preparing download", 0.04,
            "GitHub API unavailable; using the official latest release links",
        )
        base = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
        return "latest", f"{base}/{self.archive_name}", f"{base}/{self.checksum_name}"

    def _read_json(self, url: str, cancelled: threading.Event) -> dict[str, object]:
        self._raise_if_cancelled(cancelled)
        try:
            return json.loads(self._download_text(url, cancelled))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise FFmpegInstallError("Could not retrieve the FFmpeg release manifest.") from error

    def _download_text(self, url: str, cancelled: threading.Event) -> str:
        self._raise_if_cancelled(cancelled)
        request = Request(url, headers={"User-Agent": f"PlaylistCanvas/{__version__}"})
        try:
            with urlopen(request, timeout=30) as response:
                declared_length = self._content_length(response.headers.get("Content-Length"))
                if declared_length > self.max_text_bytes:
                    raise FFmpegInstallError("The FFmpeg release manifest is unexpectedly large.")
                data = response.read(self.max_text_bytes + 1)
                if len(data) > self.max_text_bytes:
                    raise FFmpegInstallError("The FFmpeg release manifest is unexpectedly large.")
        except OSError as error:
            raise FFmpegInstallError("The FFmpeg download could not be reached.") from error
        self._raise_if_cancelled(cancelled)
        return data.decode("utf-8")

    def _download_file(self, url: str, destination: Path, progress: ProgressCallback | None,
                       cancelled: threading.Event) -> None:
        request = Request(url, headers={"User-Agent": f"PlaylistCanvas/{__version__}"})
        try:
            with urlopen(request, timeout=30) as response, destination.open("wb") as stream:
                length = self._content_length(response.headers.get("Content-Length"))
                if length > self.max_archive_bytes:
                    raise FFmpegInstallError("The FFmpeg archive is unexpectedly large.")
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    self._raise_if_cancelled(cancelled)
                    downloaded += len(chunk)
                    if downloaded > self.max_archive_bytes:
                        raise FFmpegInstallError("The FFmpeg archive exceeded the safe download limit.")
                    stream.write(chunk)
                    fraction = 0.08 + 0.72 * downloaded / length if length else 0.12
                    self._report(progress, "Downloading FFmpeg", min(0.80, fraction),
                                 f"Downloaded {downloaded / 1024 / 1024:.1f} MB")
                if length and downloaded != length:
                    raise FFmpegInstallError("The FFmpeg archive download ended before it was complete.")
        except OSError as error:
            raise FFmpegInstallError("The FFmpeg archive download failed.") from error

    @staticmethod
    def _content_length(value: object) -> int:
        try:
            return max(0, int(str(value))) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _checksum_for_archive(manifest: str, archive_name: str) -> str | None:
        for line in manifest.splitlines():
            fields = line.strip().replace("*", "").split()
            if len(fields) >= 2 and fields[1] == archive_name and len(fields[0]) == 64:
                return fields[0]
        return None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _safe_extract(self, archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as package:
                root = destination.resolve()
                members = package.infolist()
                if len(members) > self.max_archive_members:
                    raise FFmpegInstallError("The FFmpeg archive contains too many files.")
                total_size = 0
                for member in members:
                    total_size += member.file_size
                    if total_size > self.max_extracted_bytes:
                        raise FFmpegInstallError("The FFmpeg archive expands beyond the safe size limit.")
                    resolved = (destination / member.filename).resolve()
                    if not resolved.is_relative_to(root):
                        raise FFmpegInstallError("Unsafe path found in FFmpeg archive.")
                package.extractall(destination)
        except (OSError, zipfile.BadZipFile) as error:
            raise FFmpegInstallError("The FFmpeg archive could not be extracted.") from error

    @staticmethod
    def _find_executable(directory: Path) -> Path | None:
        return next(directory.rglob("ffmpeg.exe"), None)

    @staticmethod
    def _is_runnable(executable: Path) -> bool:
        if not executable.is_file():
            return False
        try:
            return subprocess.run(
                [str(executable), "-version"], capture_output=True, timeout=10, check=False,
                **hidden_process_kwargs(),
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _activate(self, installation: ManagedFFmpegInstallation) -> None:
        manifest = self.install_root / "current.json"
        temporary = manifest.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps({
                "version": installation.version,
                "executable": str(installation.executable),
                "series": installation.series,
                "release_tag": installation.release_tag,
                "source": "BtbN/FFmpeg-Builds",
                "license": "GPL-3.0-or-later",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(manifest)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _safe_version(value: str) -> str:
        return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)

    @staticmethod
    def _raise_if_cancelled(cancelled: threading.Event) -> None:
        if cancelled.is_set():
            raise FFmpegInstallCancelled("FFmpeg installation was cancelled.")

    @staticmethod
    def _report(callback: ProgressCallback | None, stage: str, fraction: float, message: str) -> None:
        if callback:
            callback(stage, fraction, message)
