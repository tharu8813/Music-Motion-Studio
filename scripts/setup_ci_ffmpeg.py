"""Install the verified FFmpeg used by Windows CI integration tests.

Shared by Windows deployment, isolated Python, native export and media
benchmark jobs. Keep retry, version and environment handoff in one place.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from app.ffmpeg.managed_installer import (
    FFmpegInstallError, FFmpegReleaseOption, ManagedFFmpegInstaller,
)

# CI must not float to another FFmpeg series when upstream rotates "latest".
# This official dated 9.0 archive was selected from the release asset list
# after 9.1 produced reproducible three-input pipe timeouts on 2026-11-28.
# A deliberate update requires the actual media/pipe suite to pass again.
CI_FFMPEG_TAG = "autobuild-2026-11-27-12-42"
CI_FFMPEG_BUILD = "n9.0.2-982-g836df4e858"
CI_FFMPEG_SERIES = "9.0"
CI_FFMPEG_ARCHIVE = f"ffmpeg-{CI_FFMPEG_BUILD}-win64-gpl-{CI_FFMPEG_SERIES}.zip"
CI_FFMPEG_BASE = f"https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/{CI_FFMPEG_TAG}"


def ci_release() -> FFmpegReleaseOption:
    return FFmpegReleaseOption(
        series=CI_FFMPEG_SERIES, build=CI_FFMPEG_BUILD,
        release_tag=CI_FFMPEG_TAG, release_name=CI_FFMPEG_TAG, published_at="2026-11-27",
        archive_name=CI_FFMPEG_ARCHIVE,
        archive_url=f"{CI_FFMPEG_BASE}/{CI_FFMPEG_ARCHIVE}",
        checksum_url=f"{CI_FFMPEG_BASE}/checksums.sha256",
        notes="Pinned Windows integration-test baseline", recommended=True,
    )


def main() -> int:
    temporary = Path(os.environ["RUNNER_TEMP"])
    environment_file = Path(os.environ["GITHUB_ENV"])
    installation = None
    # The official FFmpeg mirror may return transient 502/503/504 errors.
    # Every attempt has its own directory; partial archives are not reused.
    for attempt in range(1, 4):
        try:
            destination = temporary / f"ffmpeg-tests-attempt-{attempt}"
            installation = ManagedFFmpegInstaller(destination).install_release(ci_release())
            break
        except FFmpegInstallError as error:
            if attempt == 3:
                raise RuntimeError("Verified FFmpeg download failed after 3 attempts") from error
            delay = (5, 20)[attempt - 1]
            print(f"FFmpeg download attempt {attempt}/3 failed: {error}; retrying", flush=True)
            time.sleep(delay)
    if installation is None:
        raise RuntimeError("Verified FFmpeg installation was not created")
    print(f"Verified CI FFmpeg {installation.version} ({CI_FFMPEG_TAG})", flush=True)
    with environment_file.open("a", encoding="utf-8") as environment:
        environment.write(f"PLAYLIST_CANVAS_TEST_FFMPEG={installation.executable}\n")
    # Some native diagnostic tools discover FFmpeg through PATH instead.
    if github_path := os.environ.get("GITHUB_PATH"):
        with Path(github_path).open("a", encoding="utf-8") as paths:
            paths.write(f"{installation.executable.parent}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
