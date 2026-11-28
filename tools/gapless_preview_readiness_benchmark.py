"""Measure the real embedded Preview from entry to playable continuous audio.

Synthetic input only. Qt settings and caches are isolated by the test harness.
Cold means no session soundtrack cache, not a flushed OS disk cache.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # noqa: F401 -- isolate application data before creating Qt objects

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from app.models.playlist import PlaylistTrack
from app.preview.gapless_audio_cache import GAPLESS_PREVIEW_CACHE
from tests.main_window_base import MainWindowTestCase


def run(executable: str, seconds: int, rounds: int, timeout: float) -> dict:
    def ff(*options) -> bytes:
        return subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, options)],
            check=True, capture_output=True, timeout=300,
        ).stdout

    version = ff("-version").decode(errors="replace").splitlines()[0]
    rows = []
    with TemporaryDirectory(prefix="gapless-readiness-", ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        tracks = []
        for i in range(8):
            source = root / f"track-{i}.{('wav', 'flac', 'mp3', 'm4a')[i % 4]}"
            ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.25:seed={10 + i}:r=48000:d={seconds}",
               "-ac", "2", source)
            tracks.append(PlaylistTrack(str(source), f"Track {i + 1}", duration_seconds=seconds))

        MainWindowTestCase.setUpClass()
        fixture = MainWindowTestCase()
        try:
            fixture.setUp()
            window, app = fixture.window, fixture.application
            window.settings_service.save(replace(window.settings_service.current, ffmpeg_path=executable))
            window.project_settings = replace(window.project_settings, transition_mode="none", audio_effects={})
            window._preview_backend_for_session = "cpu"
            window.playlist_service.replace(tracks)
            window.timeline_panel.refresh()
            window.show()
            app.processEvents()

            for repeat in range(rounds):
                GAPLESS_PREVIEW_CACHE.clear()
                for mode in ("cold", "warm"):
                    window._show_bottom_panel(1)
                    if window._inline_preview is not None:
                        raise RuntimeError("Previous preview did not close")
                    hits = []
                    restore = GAPLESS_PREVIEW_CACHE.restore

                    def record_restore(*args, **kwargs):
                        result = restore(*args, **kwargs)
                        hits.append(result is not None)
                        return result

                    with patch.object(GAPLESS_PREVIEW_CACHE, "restore", side_effect=record_restore):
                        started = perf_counter()
                        window._show_bottom_panel(2)
                        preview = window._inline_preview
                        if preview is None:
                            raise RuntimeError("Preview did not open")
                        entry_seconds = perf_counter() - started
                        while not preview.play_button.isEnabled():
                            if perf_counter() - started > timeout:
                                raise TimeoutError("Continuous preview audio did not become playable")
                            QTest.qWait(10)
                        ready_seconds = perf_counter() - started
                        soundtrack = preview._blended_audio_path
                        if soundtrack is None or not soundtrack.is_file():
                            raise RuntimeError("Preview fell back to individual tracks")
                        if (mode == "warm") != any(hits):
                            raise RuntimeError(f"Unexpected cache result for {mode}: {hits}")
                        digest = ff("-i", soundtrack, "-c:a", "pcm_f32le", "-f", "hash",
                                    "-hash", "sha256", "-").decode().strip()
                        row = {"round": repeat + 1, "mode": mode, "entry_seconds": entry_seconds,
                               "ready_seconds": ready_seconds, "cache_hit": any(hits),
                               "duration_seconds": preview._compiled_plan.duration_seconds,
                               "decoded_audio_hash": digest}
                        if mode == "warm":
                            cold = rows[-1]
                            if (cold["decoded_audio_hash"], cold["duration_seconds"]) != (digest, row["duration_seconds"]):
                                raise RuntimeError("Cache reuse changed decoded audio or timing")
                        rows.append(row)
                        print(json.dumps(row), flush=True)
                        window._show_bottom_panel(1)
                        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                        app.processEvents()
        finally:
            try:
                fixture.doCleanups()
                fixture.tearDown()
            finally:
                GAPLESS_PREVIEW_CACHE.clear()
                MainWindowTestCase.tearDownClass()
    return {"ffmpeg": version, "logical_cpus": os.cpu_count(), "track_count": 8,
            "seconds_per_track": seconds, "rounds": rounds, "runs": rows,
            "median_ready_seconds": {mode: statistics.median(r["ready_seconds"] for r in rows if r["mode"] == mode)
                                     for mode in ("cold", "warm")},
            "scope": "Offscreen CPU embedded Preview; readiness means enabled Play with continuous FLAC, not first audible sample. Cold means app cache miss, not cold OS disk cache."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg"))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--report", type=Path, default=Path("gapless-preview-readiness.json"))
    args = parser.parse_args()
    if not args.ffmpeg or min(args.seconds, args.rounds, args.timeout) <= 0:
        parser.error("FFmpeg and positive seconds, rounds and timeout are required")
    result = run(str(Path(args.ffmpeg).resolve()), args.seconds, args.rounds, args.timeout)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["median_ready_seconds"], indent=2), flush=True)
