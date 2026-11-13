"""Install the verified FFmpeg used by Windows CI integration tests.

Shared by the packaging and isolated Python test jobs. The retry policy and
GITHUB_ENV handoff are kept in one place so the two jobs use identical FFmpeg.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

from app.ffmpeg.managed_installer import FFmpegInstallError, ManagedFFmpegInstaller


def main() -> int:
    temporary = Path(os.environ["RUNNER_TEMP"])
    environment_file = Path(os.environ["GITHUB_ENV"])
    installation = None
    # The official FFmpeg mirror may return transient 502/503/504 errors.
    # Every attempt has its own directory; partial archives are not reused.
    for attempt in range(1, 4):
        try:
            destination = temporary / f"ffmpeg-tests-attempt-{attempt}"
            installation = ManagedFFmpegInstaller(destination).install_latest()
            break
        except FFmpegInstallError as error:
            if attempt == 3:
                raise RuntimeError("Verified FFmpeg download failed after 3 attempts") from error
            delay = (5, 20)[attempt - 1]
            print(f"FFmpeg download attempt {attempt}/3 failed: {error}; retrying", flush=True)
            time.sleep(delay)
    if installation is None:
        raise RuntimeError("Verified FFmpeg installation was not created")
    with environment_file.open("a", encoding="utf-8") as environment:
        environment.write(f"PLAYLIST_CANVAS_TEST_FFMPEG={installation.executable}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
