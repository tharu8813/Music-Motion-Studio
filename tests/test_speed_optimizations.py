from __future__ import annotations

from dataclasses import replace
import io
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument
from app.models.source import Source, SourceType
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample, ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.history_service import HistoryService
from app.services.project_service import ProjectLoadCancelled, ProjectService


class AudioOptimizationTests(unittest.TestCase):
    def test_non_spectral_layers_skip_fft_without_changing_pixels(self):
        renderer = PythonVisualizerRenderer(Path("unused"))
        cancel = threading.Event()
        samples = np.sin(np.arange(2400, dtype=np.float32) * 0.15) * 0.3
        stereo = np.column_stack((samples, samples * 0.6))
        overlay = VisualizerOverlay(0, 0, 16, 16, "bars", "#22CCFF", bar_count=8)
        for kind in ("particles", "level_meter", "waveform"):
            with self.subTest(kind=kind), patch.object(renderer, "_decode_mono_audio", return_value=samples), \
                 patch.object(renderer, "_decode_stereo_audio", return_value=stereo):
                selected = replace(overlay, kind=kind)
                mono = np.mean(stereo, axis=1, dtype=np.float32) if kind == "level_meter" else samples
                original_levels = renderer._analyze_levels(mono, 10, 8, cancel)
                with patch.object(renderer, "_analyze_levels", side_effect=AssertionError("Unused FFT")):
                    levels, waveform, meters = renderer._audio_levels(Path("unused"), [selected], 10, cancel, None)
                expected = waveform if kind == "waveform" else original_levels
                actual = waveform if kind == "waveform" else levels
                before = [bytes(image.constBits()) for image in renderer._layer_frames(
                    selected, expected, 10, cancel, None, meters)]
                after = [bytes(image.constBits()) for image in renderer._layer_frames(
                    selected, actual, 10, cancel, None, meters)]
                self.assertEqual(before, after)

    def test_frequency_visualizer_still_analyzes_the_audio(self):
        renderer = PythonVisualizerRenderer(Path("unused"))
        overlay = VisualizerOverlay(0, 0, 16, 16, "bars", "white")
        with patch.object(renderer, "_decode_mono_audio", return_value=np.zeros(8000, dtype=np.float32)), \
             patch.object(renderer, "_analyze_levels", wraps=renderer._analyze_levels) as fft:
            levels, _, _ = renderer._audio_levels(Path("unused"), [overlay], 30, threading.Event(), None)
        self.assertEqual(levels.shape, (30, 32))
        fft.assert_called_once()

    def test_pcm_scaling_preserves_signed_extremes(self):
        renderer = PythonVisualizerRenderer(Path("unused"))
        values = np.array([-32768, -1, 0, 1, 32767], dtype="<i2")
        process = SimpleNamespace(stdout=io.BytesIO(values.tobytes()), stderr=io.BytesIO(), wait=lambda: 0)
        with patch("app.renderer.python_visualizer.subprocess.Popen", return_value=process):
            decoded = renderer._decode_pcm(Path("unused"), 1, threading.Event())
        np.testing.assert_array_equal(decoded, values.astype(np.float32) / 32768.0)


class HistoryOptimizationTests(unittest.TestCase):
    def test_shared_history_branches_do_not_escape_or_follow_live_edits(self):
        history = HistoryService()
        payload = {"sources": [{"id": "text", "x": 0}], "playlist": [
            {"id": "song", "lyrics": [{"text": "before", "start": 0, "end": 1}]}]}
        history.reset(payload)
        payload["sources"][0]["x"] = 10
        history.commit(payload)
        self.assertIs(history._snapshots[0]["playlist"], history._snapshots[1]["playlist"])
        payload["playlist"][0]["lyrics"][0]["text"] = "after"
        history.commit(payload)
        previous = history.undo()
        self.assertEqual(previous["playlist"][0]["lyrics"][0]["text"], "before")
        previous["playlist"][0]["lyrics"][0]["text"] = "external mutation"
        self.assertEqual(history.undo()["playlist"][0]["lyrics"][0]["text"], "before")
        self.assertEqual(history.redo()["sources"][0]["x"], 10)
        self.assertEqual(history.redo()["playlist"][0]["lyrics"][0]["text"], "after")


class ProjectCacheOptimizationTests(unittest.TestCase):
    def test_reopen_reuses_verified_cache_and_repairs_same_size_corruption(self):
        with TemporaryDirectory() as raw, patch("app.services.project_service.gettempdir", return_value=raw):
            root = Path(raw)
            media = root / "song.wav"
            original = b"original audio" * 100
            media.write_bytes(original)
            package = ProjectService.save(root / "project.pvsproj", ProjectDocument(
                playlist=[PlaylistTrack(str(media), "Song", duration_seconds=1)]))
            try:
                cached = Path(ProjectService.load(package).playlist[0].file_path)
                stamp = cached.stat().st_mtime_ns
                original_open = zipfile.ZipFile.open
                reads = []

                def opened(archive, name, mode="r", *args, **kwargs):
                    filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
                    if filename.startswith("assets/"):
                        reads.append(filename)
                    return original_open(archive, name, mode, *args, **kwargs)

                with patch.object(zipfile.ZipFile, "open", opened), patch(
                    "app.services.project_service.shutil.disk_usage",
                    return_value=SimpleNamespace(free=0),
                ):
                    self.assertEqual(ProjectService.load(package).playlist[0].file_path, str(cached))
                self.assertEqual(reads, [])
                self.assertEqual(cached.stat().st_mtime_ns, stamp)
                cached.write_bytes(b"x" * len(original))
                with patch.object(zipfile.ZipFile, "open", opened):
                    ProjectService.load(package)
                self.assertEqual(len(reads), 1)
                self.assertEqual(cached.read_bytes(), original)
                cancel = threading.Event()
                cancel.set()
                with self.assertRaises(ProjectLoadCancelled):
                    ProjectService.load(package, cancel_event=cancel)
                cancel.clear()
                with self.assertRaises(ProjectLoadCancelled):
                    ProjectService.load(package, cancel_event=cancel,
                                        progress=lambda done, _total: cancel.set() if done else None)
                self.assertEqual(cached.read_bytes(), original)
            finally:
                ProjectService.release_cache_locks()


class LazyTimelineTests(unittest.TestCase):
    def test_long_entrance_animation_is_generated_incrementally(self):
        track = PlaylistTrack("long.wav", "Long", duration_seconds=3600)
        source = Source(SourceType.TEXT, "Title", animation_in="fade", animation_in_duration=1800)
        tracemalloc.start()
        try:
            samples = list(islice(ExportTimelinePlanner.iter_build([track], [source], 60), 3))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0].animation_progress, 0)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_long_ambient_gap_is_generated_incrementally(self):
        track = PlaylistTrack("long.wav", "Long", duration_seconds=1, start_time_seconds=3600)
        source = Source(SourceType.BACKGROUND, "Ambient", background_mode="album_art", background_ambient=True)
        tracemalloc.start()
        try:
            samples = list(islice(ExportTimelinePlanner.iter_build([track], [source], 60), 3))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(samples), 3)
        self.assertLess(samples[-1].timeline_seconds, 3600)
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_hour_long_schedule_retains_no_full_frame_list(self):
        track = PlaylistTrack("long.wav", "Long", duration_seconds=3600)
        source = Source(SourceType.TEXT, "Moving", loop_motion="float")
        tracemalloc.start()
        try:
            schedules = ExportTimelinePlanner.build_by_z_band(
                [track], [source], set(), [(None, None)], 60, lazy=True)
            self.assertTrue(schedules["base"])
            count = 0
            seconds = 0.0
            for sample in schedules["base"]:
                count += 1
                seconds += sample.duration_seconds
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 216000)
        self.assertAlmostEqual(seconds, 3600, places=6)
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertEqual(next(iter(schedules["base"])).elapsed_seconds, 0.0005)

    def test_coalescing_keeps_only_one_lookahead_sample(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=4)
        capturer = object.__new__(ExportCanvasCapturer)
        capturer._stream_state_key = lambda sample, _key: (sample.track_number,)
        seen = []

        def samples():
            for index, owner in enumerate((1, 1, 2, 3)):
                seen.append(index)
                yield ExportFrameSample(track, owner, 0, 1, index, index)

        merged = capturer.iter_coalesced_samples(samples(), "base")
        self.assertEqual(next(merged).duration_seconds, 2)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual([sample.duration_seconds for sample in merged], [1, 1])

    def test_long_coalescing_runs_remain_cancellable(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=1000)
        capturer = object.__new__(ExportCanvasCapturer)
        capturer._stream_state_key = lambda _sample, _key: ("unchanged",)
        cancel = threading.Event()

        def check_cancel():
            if cancel.is_set():
                raise RuntimeError("cancelled")

        capturer.ensure_not_cancelled = check_cancel
        samples = (ExportFrameSample(track, 1, 0, 1, index, index) for index in range(1000))
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            next(capturer.iter_coalesced_samples(samples, "base", cancel.set))


if __name__ == "__main__":
    unittest.main()
