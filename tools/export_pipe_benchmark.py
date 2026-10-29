"""Compare the live (piped) Canvas export with the intermediate-file export.

Builds a short project in an isolated settings profile -- a moving album-art
background, a progress bar, a text layer with fade animations, and a Python
visualizer between them -- exports it once each way through the real export
controller, and reports total time, peak temporary disk use, and PSNR between
the two results.

    python tools/export_pipe_benchmark.py --ffmpeg C:\\path\\to\\ffmpeg.exe
    python tools/export_pipe_benchmark.py --encoder h264_nvenc --seconds 60

The user's own settings and presets are never touched.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from time import monotonic
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from app.renderer.canvas_pipe import ENVIRONMENT_DISABLE  # noqa: E402


class _DiskPeak:
    """Poll the size of every export-owned temporary path."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _size(self) -> int:
        total = 0
        for root in self.roots:
            for directory, _dirs, files in os.walk(root):
                for name in files:
                    if not (
                        "playlist-video" in directory
                        or name.endswith(".rendering.mp4")
                    ):
                        continue
                    try:
                        total += os.lstat(os.path.join(directory, name)).st_size
                    except OSError:
                        pass
        return total

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self.peak = max(self.peak, self._size())

    def __enter__(self) -> "_DiskPeak":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()


def _make_audio(ffmpeg: str, directory: Path, seconds: float) -> Path:
    cover = directory / "cover.png"
    image = QImage(640, 640, QImage.Format.Format_RGB32)
    # A textured "photo": the worst case for a colour intermediate.
    for y in range(640):
        for x in range(640):
            image.setPixelColor(x, y, QColor(
                (x * 7 + y * 3) % 256, (x * y) % 256, (x ^ y) % 256,
            ))
    image.save(str(cover))
    audio = directory / "track.mp3"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            f"sine=frequency=220:duration={seconds}:sample_rate=48000",
            "-f", "lavfi", "-i",
            f"sine=frequency=330:duration={seconds}:sample_rate=48000",
            "-i", str(cover),
            "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]",
            "-map", "[a]", "-map", "2:v", "-c:a", "libmp3lame", "-b:a", "192k",
            "-c:v", "png", "-disposition:v", "attached_pic",
            "-id3v2_version", "3", str(audio),
        ],
        check=True,
    )
    return audio


def _export(window, output: Path, piped: bool, application: QApplication) -> float:
    if piped:
        os.environ.pop(ENVIRONMENT_DISABLE, None)
    else:
        os.environ[ENVIRONMENT_DISABLE] = "1"
    with (
        patch("app.controllers.export_controller.ExportSettingsDialog.exec",
              return_value=QDialog.DialogCode.Accepted),
        patch("app.controllers.export_controller.ExportSettingsDialog.output_path",
              new=output, create=True),
        patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
        patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes),
        patch.object(type(window.export_orchestrator), "show_complete_dialog"),
        patch.object(QMessageBox, "critical") as critical,
    ):
        started = monotonic()
        window._export_video()
        while window._render_worker is not None or window._export_dialog is not None:
            application.processEvents()
            QTest.qWait(20)
        elapsed = monotonic() - started
    if critical.called:
        raise RuntimeError(f"Export failed: {critical.call_args.args[2]}")
    return elapsed


def _psnr(ffmpeg: str, first: Path, second: Path) -> str:
    completed = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-i", str(first), "-i", str(second),
            "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    match = re.search(r"PSNR .*", completed.stderr)
    return match.group(0) if match else completed.stderr[-400:]


def _frames(ffmpeg: str, video: Path) -> int:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video), "-map", "0:v", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    matches = re.findall(r"frame=\s*(\d+)", completed.stderr)
    return int(matches[-1]) if matches else -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--encoder", default="libx264")
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--keep", type=Path, help="Keep both videos in this folder.")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance() or QApplication([])
    application.setOrganizationName("Music Motion Studio Benchmark")
    application.setApplicationName("Music Motion Studio Benchmark")
    preset_directory = tempfile.mkdtemp(prefix="pc-bench-presets-")
    os.environ["PLAYLIST_CANVAS_PRESET_DIR"] = preset_directory

    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.ui.main_window import MainWindow

    work = Path(tempfile.mkdtemp(prefix="pc-bench-"))
    try:
        audio = _make_audio(args.ffmpeg, work, args.seconds)
        window = MainWindow()
        window.settings_service.save(replace(
            window.settings_service.current,
            ffmpeg_path=args.ffmpeg, output_directory=str(work),
            fps=args.fps, video_codec=args.encoder, preset=args.preset,
            render_width=args.width, render_height=args.height,
        ))
        window.playlist_service.replace([PlaylistTrack(
            str(audio), "Benchmark", duration_seconds=args.seconds,
        )])
        for source in list(window.store.sources()):
            window.store.remove(source.id)
        artboard = window.canvas.scene_model.artboard_rect
        window.store.add(Source(
            SourceType.BACKGROUND, "Moving cover", background_mode="album_art",
            background_ambient=True, width=artboard.width(), height=artboard.height(),
            z_index=0,
        ))
        window.store.add(Source(
            SourceType.PROGRESS_BAR, "Progress", x=80, y=artboard.height() - 90,
            width=artboard.width() - 160, height=18, z_index=1,
        ))
        window.store.add(Source(
            SourceType.AUDIO_VISUALIZER, "Bars", x=80, y=artboard.height() - 320,
            width=artboard.width() - 160, height=200, z_index=2,
        ))
        window.store.add(Source(
            SourceType.TEXT, "Title", text="Direct final encode", x=80, y=60,
            width=900, height=120, font_size=64, animation_in="fade",
            animation_out="fade", animation_duration=1.0, z_index=3,
        ))
        application.processEvents()

        results = {}
        for label, piped in (("intermediate", False), ("live", True)):
            output = work / f"{label}.mp4"
            with _DiskPeak([Path(tempfile.gettempdir()), work]) as disk:
                elapsed = _export(window, output, piped, application)
            results[label] = {
                "seconds": round(elapsed, 2),
                "peak_temp_bytes": disk.peak,
                "frames": _frames(args.ffmpeg, output),
                "bytes": output.stat().st_size,
            }
        results["psnr_live_vs_intermediate"] = _psnr(
            args.ffmpeg, work / "intermediate.mp4", work / "live.mp4",
        )
        print(json.dumps(results, indent=2))
        if args.keep:
            args.keep.mkdir(parents=True, exist_ok=True)
            for label in ("intermediate", "live"):
                shutil.copy2(work / f"{label}.mp4", args.keep / f"{label}.mp4")
        window.project_session.dirty = False
        window.close()
        application.processEvents()
    finally:
        os.environ.pop(ENVIRONMENT_DISABLE, None)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(preset_directory, ignore_errors=True)
        QSettings().clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
