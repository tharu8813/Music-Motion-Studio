"""Opt-in real-media preview smoke and soak tests.

Set ``PLAYLIST_CANVAS_TEST_FFMPEG`` to an FFmpeg executable for the short CPU
decode check. Also set ``PLAYLIST_CANVAS_RUN_PREVIEW_SOAK=1`` for the 60-second
GPU-layer soak; its duration, backend and RSS budget have dedicated environment
overrides below.
"""

from __future__ import annotations

import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QAudioBufferOutput, QMediaPlayer
from PySide6.QtWidgets import QApplication, QStackedWidget, QVBoxLayout, QWidget

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.gpu_texture_surface import GpuTexturePreviewSurface
from app.services.source_store import SourceStore
from app.utils.i18n import Translator
from app.utils.subprocess_utils import hidden_process_kwargs


MIB = 1024 * 1024


def _configured_ffmpeg() -> Path:
    configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
    if not configured:
        raise unittest.SkipTest(
            "Set PLAYLIST_CANVAS_TEST_FFMPEG to run real video preview checks."
        )
    executable = Path(configured)
    if not executable.is_file():
        raise AssertionError(f"Configured FFmpeg does not exist: {executable}")
    return executable


def _write_silence(path: Path, duration_seconds: float) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48_000)
        audio.writeframes(b"\0\0\0\0" * round(48_000 * duration_seconds))


def _generate_test_video(
    ffmpeg: Path, output: Path, duration_seconds: float,
) -> None:
    common = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-t", f"{duration_seconds:.3f}", "-pix_fmt", "yuv420p",
    ]
    attempts = (
        ["-c:v", "libx264", "-preset", "ultrafast"],
        ["-c:v", "mpeg4", "-q:v", "3"],
    )
    errors: list[str] = []
    for codec_args in attempts:
        result = subprocess.run(
            [*common, *codec_args, "-movflags", "+faststart", "-y", str(output)],
            capture_output=True,
            text=True,
            check=False,
            **hidden_process_kwargs(),
        )
        if result.returncode == 0 and output.is_file() and output.stat().st_size:
            return
        errors.append(result.stderr.strip())
    raise AssertionError("Could not generate the real preview fixture: " + " | ".join(errors))


def _resident_set_bytes() -> int | None:
    """Return current RSS without adding a psutil test dependency."""
    if sys.platform == "win32":
        size_t = ctypes.c_size_t

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", size_t),
                ("WorkingSetSize", size_t),
                ("QuotaPeakPagedPoolUsage", size_t),
                ("QuotaPagedPoolUsage", size_t),
                ("QuotaPeakNonPagedPoolUsage", size_t),
                ("QuotaNonPagedPoolUsage", size_t),
                ("PagefileUsage", size_t),
                ("PeakPagefileUsage", size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.WorkingSetSize) if ok else None
    if sys.platform.startswith("linux"):
        try:
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    return None


class RealVideoPreviewPerformanceTests(unittest.TestCase):
    """Decode generated MP4 files through the complete interactive preview path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_audio_returns_after_natural_end_and_rewind_for_raw_and_blended_preview(self):
        import numpy as np

        with TemporaryDirectory(prefix="playlist-replay-") as directory:
            audio = Path(directory) / "tone.wav"
            samples = (np.sin(np.arange(57_600) * (2 * np.pi * 440 / 48_000)) * 8_000).astype("<i2")
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
                stream.writeframes(samples.tobytes())
            for blended in (False, True):
                with self.subTest(blended=blended):
                    scene = CanvasScene()
                    scene.set_artboard_size(320, 180)
                    tracks = [PlaylistTrack(str(audio), "Tone", duration_seconds=1.2)]
                    if blended:
                        tracks = [PlaylistTrack(str(audio), title, duration_seconds=0.6) for title in ("A", "B")]
                    preview = ExportPreviewDialog(scene, tracks, Translator(), preferred_backend="cpu")
                    if blended:
                        preview._blended_audio_path = audio
                    preview.audio_output.setMuted(True)
                    preview.media_player.setAudioOutput(None)
                    output = QAudioBufferOutput(preview)
                    preview.media_player.setAudioBufferOutput(output)
                    decoded = []
                    output.audioBufferReceived.connect(
                        lambda buffer: decoded.append(buffer.byteCount())
                        if buffer.isValid() and any(buffer.constData()) else None
                    )
                    try:
                        for cycle in range(3):
                            decoded.clear()
                            if cycle != 1:
                                if cycle == 2:
                                    # Also exercise the exact-end silent boundary:
                                    # stop() retains the loaded URL in Qt.
                                    preview._start_audio_at_playhead()
                                    self.assertEqual(preview._active_track_index, -1)
                                preview.timeline.setValue(0)
                            preview.play_button.setChecked(True)
                            deadline = monotonic() + 6
                            while preview._playing and monotonic() < deadline:
                                QTest.qWait(20)
                            self.assertFalse(preview._playing, f"Playback did not finish on cycle {cycle}")
                            self.assertEqual(preview.timeline.value(), preview.timeline.maximum())
                            self.assertTrue(decoded, f"No audible PCM on cycle {cycle}")
                    finally:
                        preview._stop_preview()
                        preview.deleteLater()
                        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            # Qt can release its decoder handle after DeferredDelete has completed.
            deadline = monotonic() + 2
            while True:
                try:
                    audio.unlink()
                    break
                except PermissionError:
                    if monotonic() >= deadline:
                        raise
                    QTest.qWait(20)

    def _create_preview(
        self, directory: Path, *, timeline_seconds: float, backend: str,
        parent: QWidget | None = None,
    ) -> tuple[ExportPreviewDialog, SourceItem]:
        ffmpeg = _configured_ffmpeg()
        video_path = directory / "moving-pattern.mp4"
        audio_path = directory / "silence.wav"
        _generate_test_video(ffmpeg, video_path, min(6.0, timeline_seconds))
        _write_silence(audio_path, timeline_seconds)

        source = Source(
            SourceType.VIDEO,
            "Real decoder fixture",
            width=640,
            height=360,
            video_paths=[str(video_path)],
            video_repeat_mode="loop_one",
            video_cycle_unlimited=True,
            image_fit_mode="stretch",
        )
        item = SourceItem(source)
        scene = CanvasScene()
        scene.addItem(item)
        store = SourceStore()
        store.add(source)
        track = PlaylistTrack(
            str(audio_path), "Long preview fixture",
            duration_seconds=timeline_seconds,
        )
        # Duration probing remains separately covered; this test isolates
        # actual decode, scheduling and composition cost. The probe answers
        # with the fixture's length from before the dialog exists: its first
        # refresh queues a probe on a worker thread, and a real probe in this
        # process (no app settings, so no configured FFprobe) returned 0.0 a
        # few seconds in and switched the looping video off.
        fixture_seconds = min(6.0, timeline_seconds)
        probe = patch(
            "app.dialogs.export_preview_dialog.PlaylistService._probe_duration",
            return_value=fixture_seconds,
        )
        probe.start()
        self.addCleanup(probe.stop)
        preview = ExportPreviewDialog(
            scene, [track], Translator(), source_store=store,
            preferred_backend=backend, parent=parent, embedded=parent is not None,
        )
        preview.audio_output.setMuted(True)
        preview._video_duration_cache[str(video_path)] = fixture_seconds
        preview.show()
        self.application.processEvents()
        return preview, item

    def _run_until_frames(
        self, preview: ExportPreviewDialog, item: SourceItem,
        minimum_frames: int, timeout_seconds: float,
    ) -> None:
        preview.play_button.setChecked(True)
        preview._toggle_playback(True)
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if item.video_decoder_stats().accepted_frames >= minimum_frames:
                return
            QTest.qWait(20)
        stats = item.video_decoder_stats()
        self.fail(
            "The real video decoder did not deliver enough preview frames "
            f"within {timeout_seconds:.1f}s: {stats}"
        )

    def test_real_video_and_karaoke_drag_preserves_playback_intent(self) -> None:
        with TemporaryDirectory(prefix="playlist-preview-drag-") as directory:
            preview, video = self._create_preview(Path(directory), timeline_seconds=8, backend="cpu")
            preview.scene.set_artboard_size(640, 360)
            source = Source(SourceType.LYRICS, "Bilingual karaoke", x=20, y=20, width=600,
                            height=320, font_size=18, fill_color="#00000000", z_index=10,
                            subtitle_karaoke_enabled=True, subtitle_karaoke_lines=[0],
                            subtitle_animation="glow", subtitle_glow_radius=6)
            preview.scene.addItem(SourceItem(source))
            preview.source_store.add(source)
            preview.tracks[0].lyrics = [
                {"start": index * 2, "end": index * 2 + 2,
                 "text": f"{index}: 음악이 흐르는 밤\nMusic brings us together"}
                for index in range(4)
            ]
            try:
                preview.show()
                preview.play_button.setChecked(True)
                QTest.qWait(500)
                preview.timeline.setSliderDown(True)
                for seconds in (1, 2, 4):
                    preview.timeline.setValue(seconds * 100)
                    QTest.qWait(100)
                self.assertEqual(preview.timeline.value(), 400)
                self.assertNotEqual(preview.media_player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
                self.assertFalse(preview.play_timer.isActive())
                preview.timeline.setSliderDown(False)
                QTest.qWait(500)
                self.assertTrue(preview._playing)
                self.assertTrue(preview.play_timer.isActive())
                self.assertGreater(preview.timeline.value(), 400)
                self.assertGreater(video.video_decoder_stats().accepted_frames, 0)
                self.assertFalse(preview._image.isNull())
                preview.play_button.setChecked(False)
                preview.timeline.setSliderDown(True)
                preview.timeline.setValue(100)
                preview.timeline.setSliderDown(False)
                QTest.qWait(200)
                self.assertEqual(preview.timeline.value(), 100)
                self.assertFalse(preview._playing)
                self.assertFalse(preview.play_timer.isActive())
            finally:
                preview._stop_preview()
                preview.close()
                preview.deleteLater()
                self.application.processEvents()
                QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self._release_editor_item(video)

    def test_real_mp4_advances_decoder_and_preview_timeline(self) -> None:
        _configured_ffmpeg()
        with TemporaryDirectory(prefix="playlist-real-preview-") as raw_directory:
            preview, item = self._create_preview(
                Path(raw_directory), timeline_seconds=12.0, backend="cpu",
            )
            try:
                self._run_until_frames(preview, item, 8, 10.0)
                stats = item.video_decoder_stats()
                self.assertGreaterEqual(stats.accepted_frames, 8)
                self.assertGreater(preview.timeline.value(), 0)
                self.assertTrue(item._video_timeline_preview_active)
                self.assertFalse(preview._last_preview_error_title)
            finally:
                preview._stop_preview()
                preview.close()
                self.application.processEvents()
                self._release_editor_item(item)

    @staticmethod
    def _release_editor_item(item: SourceItem) -> None:
        """Drop the canvas element's decoder before its video file is deleted.

        Closing Preview returns a video element to its editor poster frame,
        which keeps its QMediaPlayer paused on the file for as long as the
        element exists (by design). The app releases it when the element is
        removed; the temporary fixture must do the same, or Windows refuses to
        delete the still-open file (WinError 32).
        """
        item.release_video_decoder()
        QApplication.processEvents()

    def test_long_real_mp4_preview_remains_bounded_and_responsive(self) -> None:
        if os.environ.get("PLAYLIST_CANVAS_RUN_PREVIEW_SOAK", "").strip() != "1":
            self.skipTest("Set PLAYLIST_CANVAS_RUN_PREVIEW_SOAK=1 for the long soak test.")
        _configured_ffmpeg()
        soak_seconds = min(
            600.0,
            max(15.0, float(os.environ.get("PLAYLIST_CANVAS_PREVIEW_SOAK_SECONDS", "60"))),
        )
        maximum_growth = max(
            32.0,
            float(os.environ.get("PLAYLIST_CANVAS_PREVIEW_SOAK_MAX_RSS_MB", "192")),
        ) * MIB
        backend = os.environ.get(
            "PLAYLIST_CANVAS_PREVIEW_SOAK_BACKEND", "gpu_layers",
        ).strip()
        if backend not in {"cpu", "gpu_layers"}:
            self.fail(f"Unsupported soak backend: {backend}")

        with TemporaryDirectory(prefix="playlist-preview-soak-") as raw_directory:
            preview, item = self._create_preview(
                Path(raw_directory),
                timeline_seconds=soak_seconds + 15.0,
                backend=backend,
            )
            heartbeat_count = 0
            heartbeat_times: list[float] = []

            def heartbeat() -> None:
                nonlocal heartbeat_count
                heartbeat_count += 1
                heartbeat_times.append(monotonic())

            heartbeat_timer = QTimer()
            heartbeat_timer.setInterval(100)
            heartbeat_timer.timeout.connect(heartbeat)
            heartbeat_timer.start()
            try:
                self._run_until_frames(preview, item, 12, 12.0)
                # One generated clip is six seconds. Warming through a complete
                # loop keeps lazy decoder/GPU allocations out of the leak budget.
                warmup_deadline = monotonic() + 6.5
                while monotonic() < warmup_deadline:
                    QTest.qWait(25)
                start_stats = item.video_decoder_stats()
                surface = preview.gpu_surface
                if backend == "gpu_layers":
                    self.assertIsNotNone(surface)
                    self.assertTrue(surface.ready, "The GPU soak must use a real OpenGL context.")
                    self.assertTrue(preview.gpu_preview_enabled)
                gpu_start = asdict(surface.upload_stats) if surface else None
                start_timeline = preview.timeline.value()
                baseline_rss = _resident_set_bytes()
                peak_rss = baseline_rss
                heartbeat_start = heartbeat_count
                last_progress_check = monotonic()
                last_frames = start_stats.accepted_frames
                loop_wraps = 0
                last_video_position = item._video_player.position()
                deadline = monotonic() + soak_seconds
                while monotonic() < deadline:
                    QTest.qWait(25)
                    current_rss = _resident_set_bytes()
                    if current_rss is not None:
                        peak_rss = max(peak_rss or current_rss, current_rss)
                    position = item._video_player.position()
                    if position < last_video_position - 1000:
                        loop_wraps += 1
                    last_video_position = position
                    if monotonic() - last_progress_check >= 6.0:
                        frames = item.video_decoder_stats().accepted_frames
                        self.assertGreater(frames, last_frames, "Video stalled during a loop.")
                        last_frames = frames
                        last_progress_check = monotonic()

                end_stats = item.video_decoder_stats()
                accepted = end_stats.accepted_frames - start_stats.accepted_frames
                pressure_drops = end_stats.pressure_drops - start_stats.pressure_drops
                seeks = end_stats.seek_count - start_stats.seek_count
                heartbeats = heartbeat_count - heartbeat_start
                timeline_advance = (
                    preview.timeline.value() - start_timeline
                ) / 100.0

                self.assertGreaterEqual(accepted, int(soak_seconds * 3))
                self.assertGreaterEqual(heartbeats, int(soak_seconds * 5))
                self.assertGreaterEqual(timeline_advance, soak_seconds * 0.75)
                self.assertLessEqual(seeks, int(soak_seconds * 2) + 5)
                self.assertLessEqual(
                    pressure_drops, accepted * 3 + 60,
                    "Decoder pressure drops grew without a corresponding frame flow.",
                )
                self.assertFalse(preview._last_preview_error_title)
                self.assertGreaterEqual(loop_wraps, int(soak_seconds / 6) - 2)
                if backend == "gpu_layers":
                    self.assertTrue(preview.gpu_preview_enabled, "GPU preview fell back during the soak.")
                measured_pulses = heartbeat_times[heartbeat_start:]
                gaps = [b - a for a, b in zip(measured_pulses, measured_pulses[1:])]
                print("PREVIEW_SOAK " + json.dumps({
                    "backend": backend, "seconds": soak_seconds, "loop_wraps": loop_wraps,
                    "accepted_frames": accepted, "pressure_drops": pressure_drops,
                    "seek_count": seeks, "heartbeats": heartbeats,
                    "heartbeat_max_ms": max(gaps, default=0) * 1000,
                    "rss_baseline_bytes": baseline_rss, "rss_peak_bytes": peak_rss,
                    "rss_end_bytes": _resident_set_bytes(),
                    "gpu_start": gpu_start,
                    "gpu_end": asdict(surface.upload_stats) if surface else None,
                }), flush=True)
                if baseline_rss is not None and peak_rss is not None:
                    self.assertLessEqual(
                        peak_rss - baseline_rss,
                        maximum_growth,
                        "Long preview RSS grew beyond the configured budget: "
                        f"{(peak_rss - baseline_rss) / MIB:.1f} MiB > "
                        f"{maximum_growth / MIB:.1f} MiB",
                    )
            finally:
                heartbeat_timer.stop()
                print("PREVIEW_STATE " + json.dumps({
                    "backend": backend, "timeline_seconds": preview.timeline.value() / 100,
                    "timeline_max_seconds": preview.timeline.maximum() / 100,
                    "playing": preview._playing, "timer_active": preview.play_timer.isActive(),
                    "video_active": item._video_timeline_preview_active,
                    "video_suppressed": item._video_preview_suppressed,
                    "video_position": item._video_player.position(),
                    "video_state": item._video_player.playbackState().name,
                    "video_status": item._video_player.mediaStatus().name,
                    "video_error": item._video_player.errorString(),
                    "duration_cache": preview._video_duration_cache,
                    "decoder": asdict(item.video_decoder_stats()),
                    "gpu_enabled": preview.gpu_preview_enabled,
                    "gpu_health": asdict(preview._gpu_health.stats),
                }), flush=True)
                preview._stop_preview()
                preview.close()
                self.application.processEvents()
                self._release_editor_item(item)

    def test_repeated_open_pause_seek_resume_close_releases_preview(self) -> None:
        if os.environ.get("PLAYLIST_CANVAS_RUN_PREVIEW_SOAK", "").strip() != "1":
            self.skipTest("Set PLAYLIST_CANVAS_RUN_PREVIEW_SOAK=1 for repeated real-media playback.")
        backend = os.environ.get("PLAYLIST_CANVAS_PREVIEW_SOAK_BACKEND", "gpu_layers").strip()
        baseline_rss = None
        peak_rss = 0
        rss_samples = []
        # Match MainWindow's stable canvas host and its hidden GL anchor.
        host = QWidget()
        stack = QStackedWidget(host)
        QVBoxLayout(host).addWidget(stack)
        stack.addWidget(QWidget())
        if backend == "gpu_layers":
            stack.addWidget(GpuTexturePreviewSurface())
        host.resize(960, 700)
        host.show()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        with TemporaryDirectory(prefix="playlist-preview-repeat-") as directory:
            preview, item = self._create_preview(
                Path(directory), timeline_seconds=12.0, backend=backend, parent=host,
            )
            scene, tracks, store = item.scene(), preview.tracks, preview.source_store
            self.addCleanup(scene.deleteLater)
            for cycle in range(10):
                if cycle:
                    preview = ExportPreviewDialog(
                        scene, tracks, Translator(), source_store=store,
                        preferred_backend=backend, parent=host, embedded=True,
                    )
                    preview.audio_output.setMuted(True)
                    preview.show()
                    self.application.processEvents()
                stack.addWidget(preview)
                stack.setCurrentWidget(preview)
                try:
                    before = item.video_decoder_stats().accepted_frames
                    self._run_until_frames(preview, item, before + 8, 10.0)
                    if backend == "gpu_layers":
                        self.assertTrue(preview.gpu_preview_enabled)
                        self.assertTrue(preview.gpu_surface.ready)
                    preview.play_button.setChecked(False)
                    self.assertFalse(preview.play_timer.isActive())
                    preview._seek_to_seconds(7.0)
                    before = item.video_decoder_stats().accepted_frames
                    self._run_until_frames(preview, item, before + 5, 10.0)
                    self.assertGreater(preview.timeline.value(), 700)
                    self.assertFalse(preview._last_preview_error_title)
                finally:
                    surface = preview.gpu_surface
                    preview.close()
                    if surface is not None:
                        self.assertEqual(surface.upload_stats.allocated_bytes, 0)
                    self.application.processEvents()
                    self._release_editor_item(item)
                self.assertTrue(preview._closing)
                self.assertFalse(preview.play_timer.isActive())
                self.assertFalse(preview._gpu_watchdog.isActive())
                self.assertEqual(preview.media_player.source(), QUrl())
                self.assertFalse(item._video_timeline_preview_active)
                self.assertIsNone(item._video_player)
                self.assertIsNone(preview._video_probe_worker)
                self.assertIsNone(preview._video_proxy_worker)
                self.assertIsNone(preview.gpu_surface)
                stack.setCurrentIndex(0)
                stack.removeWidget(preview)
                preview.deleteLater()
                self.application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.application.processEvents()
                current_rss = _resident_set_bytes()
                rss_samples.append(current_rss)
                if current_rss is not None:
                    if cycle == 0:
                        baseline_rss = current_rss
                    peak_rss = max(peak_rss, current_rss)
        print("PREVIEW_REPEAT " + json.dumps({
            "backend": backend, "embedded": True, "cycles": 10, "rss_baseline_bytes": baseline_rss,
            "rss_peak_bytes": peak_rss, "rss_end_bytes": _resident_set_bytes(),
            "rss_samples_bytes": rss_samples,
        }), flush=True)
        if baseline_rss is not None:
            self.assertLessEqual(peak_rss - baseline_rss, 64 * MIB)


if __name__ == "__main__":
    unittest.main()
