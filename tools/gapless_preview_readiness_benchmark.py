"""Measure the real embedded Preview from entry to playable continuous audio.

Synthetic input only. Qt settings and caches are isolated by the test harness.
Cold means no session soundtrack cache, not a flushed OS disk cache.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from functools import wraps
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
from app.preview.gapless_audio_cache import GaplessPreviewCache
from app.renderer.ffmpeg_renderer import FFmpegRenderer
from tests.main_window_base import MainWindowTestCase


def run(executable: str, seconds: int, rounds: int, timeout: float, fixture_kind: str = "pink",
        *, cache_switch: bool = False) -> dict:
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
            signal = (f"anoisesrc=color=pink:amplitude=0.25:seed={10 + i}:r=48000:d={seconds}"
                      if fixture_kind == "pink" else
                      f"aevalsrc=0.015*sin(2*PI*{440 + i * 37}*t)+0.7*lt(mod(t\\,2)\\,0.0001):s=48000:d={seconds}")
            ff("-f", "lavfi", "-i", signal, "-ac", "2", source)
            tracks.append(PlaylistTrack(str(source), f"Track {i + 1}", duration_seconds=seconds))

        MainWindowTestCase.setUpClass()
        fixture = MainWindowTestCase()
        cache = GaplessPreviewCache()
        identities = {}
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

            # Reverse the order of capacity comparisons in alternate rounds.
            # Both capacities use identical files, track IDs and A/B ordering.
            # B is a reversed playlist, not a different project file on disk.
            schedule = []
            for repeat in range(rounds):
                capacities = ((1, 3) if repeat % 2 == 0 else (3, 1)) if cache_switch else (3,)
                for capacity in capacities:
                    steps = (("A1", "cold"), ("B", "cold"),
                             ("A2", "warm" if capacity == 3 else "cold")) if cache_switch else (
                                 ("A1", "cold"), ("A2", "warm"),
                             )
                    schedule.extend((repeat, capacity, step, mode) for step, mode in steps)

            for repeat, capacity, step, mode in schedule:
                window._show_bottom_panel(1)
                if window._inline_preview is not None:
                    raise RuntimeError("Previous preview did not close")
                if step == "A1":
                    cache.clear()
                    cache = GaplessPreviewCache(max_entries=capacity)
                window.playlist_service.replace(list(reversed(tracks)) if step == "B" else tracks)
                window.timeline_panel.refresh()
                app.processEvents()
                hits = []
                stages = {}
                loudness_path = None
                restore = cache.restore
                prepare = FFmpegRenderer.prepare_playlist_audio

                def record_prepare(renderer, *args, **kwargs):
                    # Instrument only the renderer instance preparing this
                    # soundtrack; no unrelated Qt worker or export calls.
                    nonlocal loudness_path

                    def timed_stage(name, method):
                        @wraps(method)
                        def measured(*a, **kw):
                            nonlocal loudness_path
                            before = perf_counter()
                            try:
                                result = method(*a, **kw)
                                if name == "loudness":
                                    loudness_path = ("none" if result is None else
                                                     "limiter" if "alimiter=" in result else
                                                     "linear" if result.startswith("volume=") else "other")
                                return result
                            finally:
                                stages[name] = stages.get(name, 0.0) + perf_counter() - before
                        return measured

                    original_run = renderer._run

                    def record_run(arguments, *a, **kw):
                        # Measurement and protocol preflight are timed by
                        # their outer methods, not double-counted here.
                        if "-c:a" in arguments and arguments[arguments.index("-c:a") + 1] == "flac":
                            return timed_stage("flac_encode", original_run)(arguments, *a, **kw)
                        return original_run(arguments, *a, **kw)

                    before = perf_counter()
                    try:
                        with ExitStack() as stack:
                            for name, method_name in (
                                ("decode", "_normalize_audio"),
                                ("virtual_input", "_virtual_gapless_pcm_input"),
                                ("physical_join", "_join_gapless_pcm"),
                                ("loudness", "_measure_loudness_filter"),
                            ):
                                stack.enter_context(patch.object(
                                    renderer, method_name, timed_stage(name, getattr(renderer, method_name))))
                            stack.enter_context(patch.object(renderer, "_run", record_run))
                            return prepare(renderer, *args, **kwargs)
                    finally:
                        stages["prepare_total"] = perf_counter() - before

                def record_restore(*args, **kwargs):
                    result = restore(*args, **kwargs)
                    hits.append(result is not None)
                    return result

                with (
                    patch("app.controllers.preview_audio_controller.GAPLESS_PREVIEW_CACHE", cache),
                    patch.object(cache, "restore", side_effect=record_restore),
                    patch.object(FFmpegRenderer, "prepare_playlist_audio", record_prepare),
                ):
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
                    if mode == "cold" and not {"decode", "loudness", "flac_encode", "prepare_total"} <= stages.keys():
                        raise RuntimeError(f"Missing stage measurements: {stages}")
                    if mode == "warm" and stages:
                        raise RuntimeError(f"Cache hit unexpectedly rendered audio: {stages}")
                    row = {"round": repeat + 1, "mode": mode, "capacity": capacity,
                           "step": step, "entry_seconds": entry_seconds,
                           "ready_seconds": ready_seconds, "cache_hit": any(hits),
                           "duration_seconds": preview._compiled_plan.duration_seconds,
                           "decoded_audio_hash": digest,
                           "stages_seconds": dict(stages), "loudness_path": loudness_path}
                    playlist = "B" if step == "B" else "A"
                    identity = (digest, row["duration_seconds"])
                    if identities.setdefault(playlist, identity) != identity:
                        raise RuntimeError("Cache policy changed decoded audio or timing")
                    if "A" in identities and "B" in identities and identities["A"][0] == identities["B"][0]:
                        raise RuntimeError("Reversed playlist did not change decoded audio")
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
                cache.clear()
                MainWindowTestCase.tearDownClass()
    return {"ffmpeg": version, "logical_cpus": os.cpu_count(), "track_count": 8,
            "seconds_per_track": seconds, "rounds": rounds, "fixture": fixture_kind, "runs": rows,
            "scenario": "A-B-A playlist switch" if cache_switch else "same playlist reopen",
            "median_return_seconds_by_capacity": {
                str(capacity): statistics.median(row["ready_seconds"] for row in rows
                                                 if row["capacity"] == capacity and row["step"] == "A2")
                for capacity in sorted({row["capacity"] for row in rows})
            },
            "median_cold_stages_seconds": {
                name: statistics.median(row["stages_seconds"][name] for row in rows
                                        if name in row["stages_seconds"])
                for name in sorted({name for row in rows for name in row["stages_seconds"]})
            },
            "median_ready_seconds": {mode: statistics.median(r["ready_seconds"] for r in rows if r["mode"] == mode)
                                     for mode in ("cold", "warm")},
            "scope": "Offscreen CPU embedded Preview; readiness means enabled Play with continuous FLAC, not first audible sample. Cold means app cache miss, not cold OS disk cache."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg"))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--fixture", choices=("pink", "peaky"), default="pink")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--cache-switch", action="store_true",
                        help="Compare one-entry and three-entry caches on identical A/B/A playlists")
    parser.add_argument("--report", type=Path, default=Path("gapless-preview-readiness.json"))
    args = parser.parse_args()
    if not args.ffmpeg or min(args.seconds, args.rounds, args.timeout) <= 0:
        parser.error("FFmpeg and positive seconds, rounds and timeout are required")
    result = run(str(Path(args.ffmpeg).resolve()), args.seconds, args.rounds, args.timeout, args.fixture,
                 cache_switch=args.cache_switch)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"ready": result["median_ready_seconds"],
                      "return_by_capacity": result["median_return_seconds_by_capacity"],
                      "cold_stages": result["median_cold_stages_seconds"]}, indent=2), flush=True)
