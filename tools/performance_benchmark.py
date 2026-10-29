"""CPU planes, lyric capture, muted preview and export preparation benchmarks.

Run with the application's Python: python tools/performance_benchmark.py planes
Save baseline JSON before changing code, then run the same command again.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from math import ceil
import os
from pathlib import Path
import platform
import statistics
import sys
import subprocess
import tempfile
import threading
from time import perf_counter
import tracemalloc
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def plane_benchmark(iterations: int) -> dict:
    import numpy as np
    import PySide6
    from PySide6.QtCore import QSize
    from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
    from app.preview.gpu_texture_surface import GpuTexturePreviewSurface as Surface

    cases = []
    for width, height in ((1280, 720), (1920, 1080), (3840, 2160)):
        frame = QVideoFrame(QVideoFrameFormat(
            QSize(width, height), QVideoFrameFormat.PixelFormat.Format_NV12,
        ))
        if not frame.map(QVideoFrame.MapMode.WriteOnly):
            raise RuntimeError("Cannot map benchmark frame")
        for plane in range(2):
            bits = memoryview(frame.bits(plane)).cast("B")
            bits[:] = bytes([64 + plane * 64]) * len(bits)
        frame.unmap()
        if not frame.map(QVideoFrame.MapMode.ReadOnly):
            raise RuntimeError("Cannot map benchmark frame")
        try:
            for scale in (1.0, 0.65, 0.5):
                target_width, target_height = round(width * scale), round(height * scale)

                def prepare():
                    y = Surface._copy_scaled_video_plane(
                        frame, 0, width, height, target_width, target_height,
                    )
                    uv = Surface._copy_scaled_video_plane(
                        frame, 1, (width + 1) // 2, (height + 1) // 2,
                        (target_width + 1) // 2, (target_height + 1) // 2, channels=2,
                    )
                    return len(y) + len(uv)

                prepare()  # warm allocation/code paths outside measurement
                batches = []
                for _ in range(5):
                    started = perf_counter()
                    for _ in range(iterations):
                        byte_count = prepare()
                    batches.append((perf_counter() - started) * 1000 / iterations)
                tracemalloc.start()
                prepare()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                cases.append({
                    "width": width, "height": height, "scale": scale,
                    "median_ms": statistics.median(batches), "batches_ms": batches,
                    "output_bytes": byte_count, "python_peak_bytes": peak,
                })
        finally:
            frame.unmap()
    return {
        "benchmark": "nv12_cpu_plane_preparation", "iterations_per_batch": iterations,
        "python": platform.python_version(), "qt": PySide6.__version__,
        "numpy": np.__version__, "platform": platform.platform(),
        "implementation_sha256": hashlib.sha256((
            inspect.getsource(Surface._copy_video_plane)
            + inspect.getsource(Surface._copy_scaled_video_plane)
        ).encode()).hexdigest(), "cases": cases,
    }


def preview_benchmark(args) -> dict:
    """Use the real dialog/decoder; optional real analysis competes for the GIL."""
    os.environ.setdefault("QT_QPA_PLATFORM", args.platform)
    os.environ.setdefault("PLAYLIST_CANVAS_PROFILE", str(args.profile.resolve()))
    import numpy as np
    import PySide6
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import asdict
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.dialogs.export_preview_dialog import ExportPreviewDialog
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.renderer.ffmpeg_renderer import FFmpegRenderer
    from app.services.source_store import SourceStore
    from app.utils.i18n import Translator
    from app.utils.performance import PROFILE, resident_set_bytes
    from app.utils.subprocess_utils import background_work, hidden_process_kwargs

    executable = FFmpegRenderer.find_executable(args.ffmpeg)
    application = QApplication.instance() or QApplication([])
    application.setOrganizationName("Music Motion Studio Benchmark")
    application.setApplicationName("Performance Benchmark")
    with tempfile.TemporaryDirectory(prefix="pc-preview-bench-") as directory:
        root = Path(directory)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(root))
        QSettings().setValue("export/ffmpeg_path", str(executable))
        video = root / "video.mp4"
        generated = subprocess.run([
            str(executable), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", f"testsrc2=size={args.width}x{args.height}:rate={args.fps}",
            "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-y", str(video),
        ], capture_output=True, **hidden_process_kwargs())
        if generated.returncode:
            raise RuntimeError(generated.stderr.decode(errors="replace"))
        audio = root / "audio.wav"
        sample_rate = 22050
        times = np.arange(round((args.seconds + args.warmup + 10) * sample_rate)) / sample_rate
        signal = (0.12 * np.sin(2 * np.pi * 220 * times)
                  + 0.10 * np.sin(2 * np.pi * 330 * times)
                  + 0.20 * np.sin(2 * np.pi * 80 * times) * np.exp(-20 * (times % 0.5)))
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(sample_rate)
            stream.writeframes((signal * 32767).astype("<i2").tobytes())
        del times, signal
        store = SourceStore()
        source = Source(SourceType.VIDEO, "Benchmark video", width=args.width,
                        height=args.height, video_paths=[str(video)], video_repeat_mode="loop_one",
                        video_cycle_unlimited=True, image_fit_mode="stretch")
        store.add(source)
        item = SourceItem(source)
        scene = CanvasScene()
        from PySide6.QtCore import QRectF
        scene.artboard_rect = QRectF(0, 0, args.width, args.height)
        scene.addItem(item)
        track = PlaylistTrack(str(audio), "Benchmark", duration_seconds=args.seconds + args.warmup + 10)
        if args.lyrics:
            lyrics = Source(SourceType.LYRICS, "Bilingual karaoke", x=80, y=20,
                            width=max(1, args.width - 160), height=max(1, args.height - 40), font_size=32,
                            fill_color="#00000000", z_index=10, subtitle_animation="glow",
                            subtitle_karaoke_enabled=True, subtitle_karaoke_lines=[0])
            store.add(lyrics)
            scene.addItem(SourceItem(lyrics))
            track.lyrics = [{"start": i * 2, "end": i * 2 + 2,
                             "text": f"{i}: 음악이 흐르는 밤\nMusic brings us together"}
                            for i in range(ceil(track.duration_seconds / 2))]
        preview = ExportPreviewDialog(scene, [track], Translator(), source_store=store,
                                      preferred_backend=args.backend)
        preview.audio_output.setMuted(True)
        preview._video_duration_cache[str(video)] = 6.0
        preview.preview_fps = args.fps
        preview.play_timer.setInterval(round(1000 / args.fps))
        preview.resize(960, 700)
        preview.show()
        application.processEvents()
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark-analysis")
        cancellation = threading.Event()
        future = None
        from collections import deque
        presentation_times = deque(maxlen=36001)
        scene_times = deque(maxlen=36001)
        presentation_count = 0
        scene_count = 0
        last_scene_serial = -1
        # Count the existing presentation callback, preserving all normal behavior.
        original_presented = preview._record_presented_frame
        measuring = False

        def presented():
            nonlocal presentation_count, scene_count, last_scene_serial
            original_presented()
            if measuring:
                presentation_count += 1
                now = perf_counter()
                presentation_times.append(now)
                surface = preview.gpu_surface
                serial = surface._frame_serial if surface else presentation_count
                if serial != last_scene_serial:
                    scene_count += 1
                    scene_times.append(now)
                    last_scene_serial = serial
        preview._record_presented_frame = presented
        heartbeat = deque(maxlen=6001)
        pulse = QTimer()
        pulse.setInterval(100)
        pulse.timeout.connect(lambda: heartbeat.append(perf_counter()) if measuring else None)
        pulse.start()

        def analyze():
            from app.automix.analysis.basic import BasicAnalysisProvider
            from app.automix.analysis.service import AnalysisService
            from app.automix.settings import AutoMixAnalysisSettings
            tracks = []
            for index in range(4):
                path = root / f"analysis-{index}.wav"
                path.write_bytes(audio.read_bytes())
                tracks.append(PlaylistTrack(str(path), f"Analysis {index}", duration_seconds=track.duration_seconds))
            with background_work():
                return AnalysisService(BasicAnalysisProvider(executable), settings=AutoMixAnalysisSettings(
                    max_workers=args.analysis_workers, use_cache=False,
                )).analyze_tracks(tracks, cancel_event=cancellation)

        rss_start = rss_peak = rss_end = None
        frame_buffer_peak = backlog_peak = 0
        try:
            preview.play_button.setChecked(True)
            preview._toggle_playback(True)
            QTest.qWait(round(args.warmup * 1000))
            surface = preview.gpu_surface
            if args.backend != "cpu" and (surface is None or not surface.ready):
                raise RuntimeError("Requested GPU backend unavailable; use --platform windows on a desktop")
            gpu_start = asdict(surface.upload_stats) if surface else None
            last_scene_serial = surface._frame_serial if surface else -1
            decoder_start = asdict(item.video_decoder_stats())
            rss_start = rss_peak = resident_set_bytes()
            measuring = True
            started = perf_counter()
            if args.analysis_workers:
                future = pool.submit(analyze)
            deadline = started + args.seconds
            while perf_counter() < deadline:
                QTest.qWait(200)
                rss_end = resident_set_bytes()
                if rss_end is not None:
                    rss_peak = max(rss_peak or 0, rss_end)
                images = [preview._base_image, preview._image, *preview._dynamic_region_buffers.values()]
                images.extend(preview._overlay_cache.images())
                # Unique Qt cache keys avoid double-counting implicitly shared QImages.
                buffers = {int(image.cacheKey()): image.sizeInBytes() for image in images if not image.isNull()}
                frame_buffer_peak = max(frame_buffer_peak, sum(buffers.values()))
                backlog_peak = max(backlog_peak, int(surface.frame_pending) if surface else 0)
                if rss_end is not None:
                    PROFILE.observe("memory.rss_bytes", rss_end)
                PROFILE.observe("memory.frame_buffers_bytes", sum(buffers.values()))
            elapsed = perf_counter() - started
            decoder_end = asdict(item.video_decoder_stats())
            gpu_end = asdict(surface.upload_stats) if surface else None
            callbacks = list(presentation_times)
            pulses = list(heartbeat)
            scenes = list(scene_times)
            intervals = [b - a for a, b in zip(callbacks, callbacks[1:])]
            scene_intervals = [b - a for a, b in zip(scenes, scenes[1:])]
            pulse_intervals = [b - a for a, b in zip(pulses, pulses[1:])]
            analysis = future.result() if future is not None and future.done() else None
            result = {
                "benchmark": "real_video_preview", "backend": args.backend,
                "python": platform.python_version(), "qt": PySide6.__version__,
                "numpy": np.__version__, "platform": platform.platform(),
                "width": args.width, "height": args.height, "warmup_seconds": args.warmup,
                "lyrics": args.lyrics,
                "gpu_backend": surface.backend_info.label if surface and surface.backend_info else None,
                "seconds": elapsed, "target_fps": args.fps,
                "presentation_callbacks": presentation_count,
                "average_fps": presentation_count / elapsed,
                "presented_scene_fps": scene_count / elapsed,
                "scene_frame_time_mean_ms": statistics.mean(scene_intervals) * 1000 if scene_intervals else None,
                "scene_frame_time_p95_ms": sorted(scene_intervals)[max(0, int(len(scene_intervals) * .95) - 1)] * 1000 if scene_intervals else None,
                "late_scene_intervals": sum(interval > 1.5 / args.fps for interval in scene_intervals),
                "decoder_fps": (decoder_end["accepted_frames"] - decoder_start["accepted_frames"]) / elapsed,
                "decode_drops": decoder_end["dropped_frames"] - decoder_start["dropped_frames"],
                "frame_time_mean_ms": statistics.mean(intervals) * 1000 if intervals else None,
                "frame_time_p95_ms": sorted(intervals)[max(0, int(len(intervals) * .95) - 1)] * 1000 if intervals else None,
                "late_intervals": sum(interval > 1.5 / args.fps for interval in intervals),
                "heartbeat_max_ms": max(pulse_intervals, default=0) * 1000,
                "decoder_start": decoder_start, "decoder_end": decoder_end,
                "gpu_start": gpu_start, "gpu_end": gpu_end, "queue_backlog_peak": backlog_peak,
                "rss_start_bytes": rss_start, "rss_end_bytes": rss_end, "rss_peak_bytes": rss_peak,
                "frame_buffer_peak_bytes": frame_buffer_peak, "analysis_workers": args.analysis_workers,
                "analysis_finished_in_window": future.done() if future else None,
                "analysis_successes": len(analysis.analyses) if analysis else None,
                "analysis_failures": len(analysis.failures) if analysis else None,
            }
        finally:
            measuring = False
            pulse.stop()
            cancellation.set()
            preview._stop_preview()
            preview.close()
            application.processEvents()
            item.release_video_decoder()
            application.processEvents()
            pool.shutdown(wait=True, cancel_futures=True)
        result["profile"] = PROFILE.snapshot()
        return result


def export_start_benchmark(args) -> dict:
    """Measure real FFmpeg/FFT bass preparation and GUI heartbeat; no playback."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import numpy as np
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.preview.export_plan import ExportPlan
    from app.preview.export_session import ExportSession, PngStaging
    from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderSettings

    application = QApplication.instance() or QApplication([])
    renderer = FFmpegRenderer(args.ffmpeg)
    with tempfile.TemporaryDirectory(prefix="pc-export-start-") as directory:
        audio = Path(directory) / "bass.wav"
        time = np.arange(8000) / 8000
        block = ((0.25 * np.sin(2 * np.pi * 80 * time)
                  + 0.1 * np.sin(2 * np.pi * 220 * time)) * 32767).astype("<i2").tobytes()
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            for _ in range(args.audio_seconds):
                stream.writeframesraw(block)
        track = PlaylistTrack(str(audio), "Bass fixture", duration_seconds=args.audio_seconds)
        scene = CanvasScene()
        scene.addItem(SourceItem(Source(SourceType.BACKGROUND, "Bass", background_bass_reactive=True)))
        session = ExportSession(
            scene=scene, renderer=renderer,
            plan=ExportPlan([(None, None)], set(), False, False, {}, args.fps, args.audio_seconds),
            render_settings=RenderSettings(fps=args.fps), active_tracks=[track], stream_root=None,
            preparation_cancel=threading.Event(), korean=False,
            staging=PngStaging(lambda *_: None, lambda *_: None, lambda: None,
                               lambda: None, lambda: 0, 1),
            layer_worker_count=lambda _: 1, report_progress=lambda *_: None,
            pump_ui=application.processEvents,
        )
        pulses = []
        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: pulses.append(perf_counter()))
        timer.start()
        cancel_requests = []
        cancel_timer = QTimer()
        cancel_timer.setSingleShot(True)

        def cancel():
            cancel_requests.append(perf_counter())
            session._cancel.set()

        cancel_timer.timeout.connect(cancel)
        if args.cancel_after_ms:
            cancel_timer.start(args.cancel_after_ms)
        started = perf_counter()
        envelope = None
        cancelled = False
        try:
            envelope = session._prepare_bass_envelopes()[track.id]
        except RenderCancelledError:
            cancelled = True
        finally:
            finished = perf_counter()
            timer.stop()
            cancel_timer.stop()
        gaps = [b - a for a, b in zip([started, *pulses], [*pulses, finished])]
        return {
            "benchmark": "export_bass_preparation", "audio_seconds": args.audio_seconds,
            "fps": args.fps, "preparation_seconds": finished - started,
            "heartbeat_interval_ms": 20, "heartbeat_count": len(pulses),
            "max_heartbeat_gap_ms": max(gaps) * 1000,
            "envelope_frames": len(envelope) if envelope is not None else None,
            "envelope_sha256": hashlib.sha256(envelope.tobytes()).hexdigest() if envelope is not None else None,
            "cancelled": cancelled,
            "cancel_request_ms": (cancel_requests[0] - started) * 1000 if cancel_requests else None,
            "cancel_completion_ms": (finished - cancel_requests[0]) * 1000 if cancel_requests else None,
            "python": platform.python_version(), "platform": platform.platform(),
        }


def lyrics_benchmark(iterations: int) -> dict:
    """Compare full-frame capture with RGBA and coverage-only lyric blur."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.preview.canvas_snapshot import CanvasSnapshot

    application = QApplication.instance() or QApplication([])
    raster = SourceItem._blurred_raster

    def rgba_reference(*args, **kwargs):
        kwargs.pop("solid_color", None)
        return raster(*args, **kwargs)

    track = PlaylistTrack("", "Fixture", duration_seconds=60, lyrics=[
        {"start": i * 2, "end": i * 2 + 2, "text": f"Line {i + 1}: music brings us together"}
        for i in range(20)
    ])
    batches = {"rgba_reference": [], "coverage_only": []}
    final_pixels = {}
    for _ in range(3):
        for mode in batches:
            scene = CanvasScene()
            scene.set_artboard_size(1280, 720)
            item = SourceItem(Source(
                SourceType.LYRICS, "Lyrics", width=760, height=650, font_size=32,
                fill_color="#00000000", subtitle_current_scale=1.15,
                subtitle_context_lines=3, subtitle_next_lines=3,
                subtitle_role_styles={role: {"blur": 5, "opacity": 0.35} for role in ("previous", "next")},
                subtitle_previous_distance_fade=1, subtitle_next_distance_fade=1,
            ))
            scene.addItem(item)
            timings = []
            with patch.object(SourceItem, "_blurred_raster", side_effect=(
                rgba_reference if mode == "rgba_reference" else raster
            )):
                for frame_index in range(iterations):
                    started = perf_counter()
                    frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                        elapsed_seconds=10 + frame_index / 30, transparent=True)
                    timings.append((perf_counter() - started) * 1000)
            batches[mode].append(statistics.mean(timings))
            final_pixels[mode] = bytes(frame.constBits())
            scene.clear()
            application.processEvents()
    medians = {mode: statistics.median(values) for mode, values in batches.items()}
    return {
        "benchmark": "lyrics_full_frame_capture", "python": platform.python_version(),
        "frames_per_batch": iterations, "batches_mean_ms": batches, "median_mean_ms": medians,
        "improvement_percent": (1 - medians["coverage_only"] / medians["rgba_reference"]) * 100,
        "white_glyph_pixels_equal": final_pixels["rgba_reference"] == final_pixels["coverage_only"],
        "fixture": "1280x720, seven cues, 32pt, 5px blur, previous/next distance falloff",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("planes", "preview", "export-start", "lyrics"))
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", type=Path, default=Path("build/performance-profile.json"))
    parser.add_argument("--ffmpeg")
    parser.add_argument("--backend", choices=("cpu", "gpu_layers"), default="cpu")
    parser.add_argument("--platform", choices=("offscreen", "windows"), default="offscreen")
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--warmup", type=float, default=7)
    parser.add_argument("--fps", type=int, choices=(30, 60), default=30)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--analysis-workers", type=int, choices=(0, 1, 2, 4), default=0)
    parser.add_argument("--lyrics", action="store_true", help="Include bilingual Glow/Karaoke in the real preview")
    parser.add_argument("--audio-seconds", type=int, default=600)
    parser.add_argument("--cancel-after-ms", type=int, default=0)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if not 1 <= args.seconds <= 600 or not 0 <= args.warmup <= 60:
        parser.error("--seconds must be 1..600 and --warmup must be 0..60")
    if not 16 <= args.width <= 3840 or not 16 <= args.height <= 2160:
        parser.error("dimensions must be 16..3840 by 16..2160")
    if not 1 <= args.audio_seconds <= 3600:
        parser.error("--audio-seconds must be 1..3600")
    if args.cancel_after_ms < 0:
        parser.error("--cancel-after-ms cannot be negative")
    if args.mode == "planes":
        result = plane_benchmark(args.iterations)
    elif args.mode == "lyrics":
        result = lyrics_benchmark(args.iterations)
    elif args.mode == "preview":
        result = preview_benchmark(args)
    else:
        result = export_start_benchmark(args)
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
