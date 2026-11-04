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
from time import perf_counter, process_time
import tracemalloc
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests  # Isolate preview caches, presets and settings from the running app.


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
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import asdict
    from PySide6.QtCore import QSettings, QTimer
    from PySide6.QtGui import QImage
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
        if args.video_count:
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
        video_items = []
        scene = CanvasScene()
        scene.set_artboard_size(args.width, args.height)
        if args.video_count:
            store.add(source)
            item = SourceItem(source)
            video_items.append(item)
            scene.addItem(item)
        else:
            backdrop = Source(SourceType.SHAPE, "Backdrop", width=args.width,
                              height=args.height, fill_color="#24334a")
            store.add(backdrop)
            scene.addItem(SourceItem(backdrop))
        for index in range(1, args.video_count):
            from dataclasses import replace
            duplicate = replace(source, id=f"video-{index}", z_index=index,
                                x=index * args.width / 6, y=index * args.height / 6,
                                width=args.width / 2, height=args.height / 2)
            if args.styled:
                duplicate.brightness = 12
                duplicate.blur = 4
                duplicate.opacity = 0.8
                duplicate.rotation = 8
            store.add(duplicate)
            duplicate_item = SourceItem(duplicate)
            video_items.append(duplicate_item)
            scene.addItem(duplicate_item)
        track = PlaylistTrack(str(audio), "Benchmark", duration_seconds=args.seconds + args.warmup + 10)
        if args.lyrics or args.integrated or args.static_lyrics:
            lyrics = Source(SourceType.LYRICS, "Bilingual karaoke", x=80, y=20,
                            width=max(1, args.width - 160), height=max(1, args.height - 40), font_size=32,
                            fill_color="#00000000", z_index=10, subtitle_animation="glow",
                            subtitle_karaoke_enabled=True, subtitle_karaoke_lines=[0])
            if args.static_lyrics:
                lyrics.subtitle_animation = "none"
                lyrics.subtitle_karaoke_enabled = False
                lyrics.subtitle_intro_enabled = False
                lyrics.subtitle_context_lines = lyrics.subtitle_next_lines = 0
            store.add(lyrics)
            scene.addItem(SourceItem(lyrics))
            track.lyrics = [{"start": i * 2, "end": i * 2 + 2,
                             "text": f"{i}: 음악이 흐르는 밤\nMusic brings us together"}
                            for i in range(ceil(track.duration_seconds / 2))]
        tracks = [track]
        preview_options = {}
        if args.static_lyrics:
            track.lyrics = [{"start": 0.0, "end": track.duration_seconds,
                             "text": "음악이 흐르는 밤\nMusic brings us together"}]
            cover_image = QImage(256, 256, QImage.Format.Format_RGB32)
            cover_image.fill(0xff456688)
            cover_path = root / "cover.png"
            cover_image.save(str(cover_path))
            track.cover_path = str(cover_path)
            cover = Source(SourceType.ALBUM_COVER, "Cover", x=24, y=24, width=180, height=180, z_index=11)
            store.add(cover)
            scene.addItem(SourceItem(cover))
        if args.integrated:
            from dataclasses import replace
            from app.animation.custom_motion import CustomMotion
            from app.renderer.ffmpeg_renderer import VisualizerOverlay

            cover = root / "album.png"
            cover_image = QImage(256, 256, QImage.Format.Format_RGB32)
            cover_image.fill(0xff456688)
            if not cover_image.save(str(cover)):
                raise RuntimeError("Could not write the integrated fixture cover")
            track.cover_path = str(cover)
            for index in range(1, 3):
                next_audio = root / f"audio-{index}.wav"
                shutil.copyfile(audio, next_audio)
                tracks.append(PlaylistTrack(str(next_audio), f"Benchmark {index + 1}",
                              duration_seconds=track.duration_seconds, cover_path=str(cover),
                              lyrics=[dict(cue) for cue in track.lyrics]))
            for index, video_item in enumerate(video_items[1:], start=1):
                video_copy = root / f"video-{index}.mp4"
                shutil.copyfile(video, video_copy)
                video_item.source.video_paths = [str(video_copy)]
                video_item.apply_source()
            scale = args.width / 1920
            cover_size = round(180 * scale)
            title = Source(SourceType.NOW_PLAYING, "Now Playing", x=cover_size + 48, y=24,
                           width=round(480 * scale), height=round(120 * scale), z_index=12,
                           animation_in="custom", custom_intro_keyframes=CustomMotion.default())
            if args.transport:
                from app.animation.keyframes import Keyframe
                title.keyframes.global_curves = {
                    "rotation": [Keyframe(0, 0), Keyframe(20, 8), Keyframe(40, -8)],
                    "opacity": [Keyframe(0, 1), Keyframe(20, .65), Keyframe(40, 1)],
                }
            for added in (
                Source(SourceType.ALBUM_COVER, "Cover", x=24, y=24, width=cover_size, height=cover_size, z_index=11),
                title,
                Source(SourceType.TEXT, "Audio reaction", text="Music Motion Studio", x=48, y=args.height - 180,
                       width=round(420 * scale), height=round(100 * scale), music_reactive_enabled=True, z_index=13),
            ):
                store.add(added)
                scene.addItem(SourceItem(added))
            bars = VisualizerOverlay(32, args.height - 130, round(540 * scale), 96, "bars", "#7dd3fc", z_index=8)
            overlays = [bars]
            if not args.video_count:
                progress = Source(SourceType.PROGRESS_BAR, "Progress", x=80, y=args.height - 80,
                                  width=args.width - 160, height=20, z_index=14)
                store.add(progress)
                scene.addItem(SourceItem(progress))
            if args.video_count >= 3:
                overlays.append(replace(bars, kind="particles", style="dust", x=0, y=0,
                                        width=args.width, height=args.height, z_index=9))
            # Match the editor: every rendered overlay also has a scene source
            # at that Z value, which the Canvas capture hides before composition.
            for overlay in overlays:
                reactive = Source(
                    SourceType.PARTICLE_OVERLAY if overlay.kind == "particles" else SourceType.AUDIO_VISUALIZER,
                    "Audio visualization", x=overlay.x, y=overlay.y,
                    width=overlay.width, height=overlay.height, z_index=overlay.z_index,
                )
                store.add(reactive)
                scene.addItem(SourceItem(reactive))
            preview_options = dict(overlays=overlays, ffmpeg_executable=executable, transition_mode="automix")
            if args.known_beats:
                from app.automix.models import TrackAnalysis
                from app.automix.planner import compile_automix
                from app.automix.renderer import AutoMixAudioPipeline
                from app.automix.settings import AUTOMIX_SETTINGS

                # The fixture WAV already contains exactly one pulse every
                # half-second. Use that ground truth to isolate playback from
                # rhythm-model startup, then render the actual AutoMix audio.
                beats = tuple(index / 2 for index in range(ceil(track.duration_seconds * 2)))
                analyses = {
                    song.id: TrackAnalysis(
                        song.id, song.file_path, song.duration_seconds,
                        bpm=120, bpm_confidence=1, beats=beats, downbeats=beats[::4],
                        meter_numerator=4, meter_denominator=4, meter_confidence=1,
                    )
                    for song in tracks
                }
                plan = compile_automix(tracks, analyses, AUTOMIX_SETTINGS)
                mix = AutoMixAudioPipeline(executable).render(
                    plan.audio, {song.id: song.file_path for song in tracks},
                    root / "mix", container="flac",
                )
                preview_options["preloaded_blended_audio"] = (mix.path, plan)
        from unittest.mock import patch
        from app.video.preview_proxy import PreviewProxyCache
        with (
            patch.object(PreviewProxyCache, "default_root", return_value=root / "preview-proxies"),
            patch("app.dialogs.export_preview_dialog.load_preview_quality", return_value=args.quality),
        ):
            preview = ExportPreviewDialog(scene, tracks, Translator(), source_store=store,
                                          preferred_backend=args.backend, **preview_options)
        preview.audio_output.setMuted(True)
        preview._video_duration_cache.update({path: 6.0 for item in video_items for path in item.source.video_paths})
        preview.preview_fps = args.fps
        if not args.display_refresh:
            preview._presentation_fps_override = args.fps
        preview._sync_presentation_cadence()
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
        measuring = False

        def presented():
            nonlocal presentation_count, scene_count, last_scene_serial
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
        if preview.gpu_surface is not None:
            preview.gpu_surface.frame_presented.connect(presented)
        else:
            preview.preview_label.frame_presented.connect(presented)
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
        gpu_samples = []
        stop_gpu_sampling = threading.Event()
        nvidia_smi = shutil.which("nvidia-smi") if args.sample_nvidia else None

        def sample_gpu():
            while not stop_gpu_sampling.is_set():
                try:
                    probe = subprocess.run(
                        [nvidia_smi, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=2, **hidden_process_kwargs(),
                    )
                    if probe.returncode == 0:
                        rows = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
                        if len(rows) == 1:
                            gpu_samples.append(tuple(map(int, rows[0].split(","))))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    return
                stop_gpu_sampling.wait(1.0)

        gpu_sampler = threading.Thread(target=sample_gpu, daemon=True) if nvidia_smi else None
        try:
            if args.integrated:
                mix_deadline = perf_counter() + args.mix_wait
                while (
                    preview._blended_audio_path is None
                    and preview._blended_audio_controller is not None
                    and preview._blended_audio_controller._worker is not None
                    and perf_counter() < mix_deadline
                ):
                    QTest.qWait(100)
                transitions = preview._compiled_plan.audio.transitions
                if transitions:
                    preview._seek_to_seconds(max(0.0, transitions[0].timeline_start - args.warmup - 1))
            preview.play_button.setChecked(True)
            preview._toggle_playback(True)
            if args.transport:
                preview.play_button.setChecked(False)
                preview._seek_to_seconds(preview._playhead_seconds + 0.35)
                preview.presentation_controller.enter_fullscreen()
                QTest.qWait(250)
                preview.presentation_controller.exit_fullscreen()
                QTest.qWait(250)
                preview.play_button.setChecked(True)
            QTest.qWait(round(args.warmup * 1000))
            surface = preview.gpu_surface
            if args.backend != "cpu" and (surface is None or not surface.ready):
                raise RuntimeError("Requested GPU backend unavailable; use --platform windows on a desktop")
            gpu_start = asdict(surface.upload_stats) if surface else None
            last_scene_serial = surface._frame_serial if surface else -1
            decoder_starts = [asdict(video_item.video_decoder_stats()) for video_item in video_items]
            decoder_start = decoder_starts[0] if decoder_starts else {}
            ordered_start = (preview._ordered_cache_hits, preview._ordered_cache_renders)
            static_band_start = (preview._static_band_cache_hits, preview._static_band_cache_misses)
            ordered_invalidation_start = getattr(preview, "_ordered_cache_invalidations", 0)
            scale_min = scale_max = preview._active_render_scale
            if hasattr(PROFILE, "clear"):
                PROFILE.clear()
            rss_start = rss_peak = resident_set_bytes()
            measuring = True
            started = perf_counter()
            cpu_started = process_time()
            if gpu_sampler:
                gpu_sampler.start()
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
                images.extend(preview._gpu_ordered_static_cache.values())
                images.extend(preview._gpu_static_band_cache.values())
                scratch = getattr(preview, "_gpu_ordered_scratch", None)
                if scratch is not None:
                    images.append(scratch)
                scale_min = min(scale_min, preview._active_render_scale)
                scale_max = max(scale_max, preview._active_render_scale)
                # Unique Qt cache keys avoid double-counting implicitly shared QImages.
                buffers = {int(image.cacheKey()): image.sizeInBytes() for image in images if not image.isNull()}
                frame_buffer_peak = max(frame_buffer_peak, sum(buffers.values()))
                backlog_peak = max(backlog_peak, int(surface.frame_pending) if surface else 0)
                if rss_end is not None:
                    PROFILE.observe("memory.rss_bytes", rss_end)
                PROFILE.observe("memory.frame_buffers_bytes", sum(buffers.values()))
            elapsed = perf_counter() - started
            cpu_elapsed = process_time() - cpu_started
            stop_gpu_sampling.set()
            decoder_ends = [asdict(video_item.video_decoder_stats()) for video_item in video_items]
            decoder_end = decoder_ends[0] if decoder_ends else {}
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
                "video_count": args.video_count,
                "preview_quality": args.quality, "render_path": preview._preview_render_path,
                "transport_exercised": args.transport,
                "canvas_scale_min": scale_min, "canvas_scale_max": scale_max,
                "ordered_cache_hits": preview._ordered_cache_hits - ordered_start[0],
                "ordered_band_renders": preview._ordered_cache_renders - ordered_start[1],
                "ordered_cache_invalidations": getattr(preview, "_ordered_cache_invalidations", 0) - ordered_invalidation_start,
                "legacy_band_cache_hits": preview._static_band_cache_hits - static_band_start[0],
                "legacy_band_cache_misses": preview._static_band_cache_misses - static_band_start[1],
                "lyrics": args.lyrics or args.integrated or args.static_lyrics,
                "integrated": args.integrated,
                "known_fixture_beats": args.known_beats,
                "track_count": len(tracks),
                "proxy_count": len(preview._video_proxy_paths),
                "blended_audio_ready": preview._blended_audio_path is not None,
                "mix_preparation_stage": (
                    preview._blended_audio_controller.last_progress
                    if preview._blended_audio_controller is not None else None
                ),
                "transitions": [dict(type=transition.type.value, dsp=transition.dsp.value if transition.dsp else None)
                                for transition in preview._compiled_plan.audio.transitions],
                "gpu_backend": surface.backend_info.label if surface and surface.backend_info else None,
                "gpu_active_at_end": preview.gpu_preview_enabled,
                "seconds": elapsed, "target_fps": preview.presentation_fps,
                "process_cpu_seconds": cpu_elapsed,
                "mean_parent_cpu_percent_of_one_core": 100 * cpu_elapsed / elapsed,
                "presentation_callbacks": presentation_count,
                "repeat_presentation_callbacks": presentation_count - scene_count,
                "average_fps": presentation_count / elapsed,
                "presented_scene_fps": scene_count / elapsed,
                "scene_frame_time_mean_ms": statistics.mean(scene_intervals) * 1000 if scene_intervals else None,
                "scene_frame_time_p95_ms": sorted(scene_intervals)[max(0, int(len(scene_intervals) * .95) - 1)] * 1000 if scene_intervals else None,
                "scene_frame_time_p50_ms": sorted(scene_intervals)[max(0, ceil(len(scene_intervals) * .5) - 1)] * 1000 if scene_intervals else None,
                "scene_frame_time_p99_ms": sorted(scene_intervals)[max(0, ceil(len(scene_intervals) * .99) - 1)] * 1000 if scene_intervals else None,
                "late_scene_intervals": sum(interval > 1.5 / args.fps for interval in scene_intervals),
                "decoder_fps": (decoder_end.get("accepted_frames", 0) - decoder_start.get("accepted_frames", 0)) / elapsed,
                "decode_drops": sum(end["dropped_frames"] - start["dropped_frames"]
                                    for start, end in zip(decoder_starts, decoder_ends, strict=True)),
                "decoder_sources": [
                    {name: end[name] - start[name] for name in (
                        "accepted_frames", "dropped_frames", "pressure_drops", "seek_count",
                        "received_frames", "mailbox_drops",
                    ) if name in end}
                    for start, end in zip(decoder_starts, decoder_ends, strict=True)
                ],
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
            stop_gpu_sampling.set()
            pulse.stop()
            cancellation.set()
            preview._stop_preview()
            preview.close()
            application.processEvents()
            for video_item in video_items:
                video_item.release_video_decoder()
            application.processEvents()
            pool.shutdown(wait=True, cancel_futures=True)
            if gpu_sampler and gpu_sampler.ident is not None:
                gpu_sampler.join(timeout=3)
        result["nvidia_device_samples_gpu_percent_and_vram_mib"] = gpu_samples
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


def automix_benchmark(args) -> dict:
    """Measure the Basic provider and its cache on a repeatable synthetic track."""
    import numpy as np
    import shutil
    from unittest.mock import patch
    from app.controllers import progressive_automix_controller as progressive
    from app.automix.analysis.basic import BasicAnalysisProvider
    from app.automix.analysis.service import AnalysisService
    from app.automix.cache import AnalysisCache
    from app.automix.settings import AutoMixAnalysisSettings
    from app.models.playlist import PlaylistTrack
    from app.renderer.ffmpeg_renderer import FFmpegRenderer

    executable = FFmpegRenderer.find_executable(args.ffmpeg)
    provider = BasicAnalysisProvider(executable)
    with tempfile.TemporaryDirectory(prefix="pc-automix-bench-") as directory:
        root = Path(directory)
        audio = root / "beat.wav"
        rate = 22050
        # Stream repeated bars instead of allocating a full song of float64 PCM.
        times = np.arange(2 * rate) / rate
        signal = .22 * np.sin(2 * np.pi * 90 * times) * np.exp(-25 * (times % .5))
        signal += .09 * np.sin(2 * np.pi * 261.6256 * times)
        block = (signal * 32767).astype("<i2").tobytes()
        with wave.open(str(audio), "wb") as stream:
            stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            for _ in range(args.audio_seconds // 2):
                stream.writeframesraw(block)
            if args.audio_seconds % 2:
                stream.writeframesraw(block[:2 * rate])
        tracks = [PlaylistTrack(str(audio), f"Beat fixture {index}", duration_seconds=args.audio_seconds)
                  for index in range(2)]
        runs = []
        loudness_runs = []
        for index in range(3):
            cache = AnalysisCache(root / f"cache-{index}", analyzer_id=provider.provider_id,
                                  analyzer_version=provider.version)
            service = AnalysisService(provider, cache=cache, settings=AutoMixAnalysisSettings(max_workers=1))
            started = perf_counter()
            cold = service.analyze_tracks(tracks)
            cold_seconds = perf_counter() - started
            started = perf_counter()
            warm = service.analyze_tracks(tracks)
            warm_seconds = perf_counter() - started
            if cold.failures or warm.failures or len(cold.analyses) != len(tracks):
                raise RuntimeError(f"AutoMix analysis did not complete: {cold.failures or warm.failures}")
            if len(warm.analyses) != len(tracks):
                raise RuntimeError("Cached AutoMix results were incomplete")
            runs.append(dict(cache_miss_seconds=cold_seconds, cache_hit_seconds=warm_seconds,
                             bpm=[item.bpm for item in warm.analyses.values()]))
            loudness_audio = root / f"loudness-{index}.wav"
            shutil.copyfile(audio, loudness_audio)
            repeated = [PlaylistTrack(str(loudness_audio), f"Copy {i}", duration_seconds=args.audio_seconds,
                                      volume_db=float(-i)) for i in range(4)]
            with patch.object(progressive, "measure_loudness", wraps=progressive.measure_loudness) as scans:
                started = perf_counter()
                gain = progressive.playlist_gain(executable, repeated, threading.Event())
                cold_loudness = perf_counter() - started
                cold_scans = scans.call_count
                # Reordering keeps the per-track weights and changes the list only.
                started = perf_counter()
                reordered = progressive.playlist_gain(executable, list(reversed(repeated)), threading.Event())
                warm_loudness = perf_counter() - started
                loudness_runs.append(dict(cold_seconds=cold_loudness, warm_seconds=warm_loudness,
                                          cold_scans=cold_scans, warm_scans=scans.call_count - cold_scans,
                                          gain=gain, reordered_gain=reordered))
    return dict(benchmark="automix_basic_analysis_and_cache", audio_seconds=args.audio_seconds,
                track_entries=2, unique_files=1, runs=runs,
                cache_miss_median_seconds=statistics.median(run["cache_miss_seconds"] for run in runs),
                cache_hit_median_seconds=statistics.median(run["cache_hit_seconds"] for run in runs),
                duplicate_loudness_runs=loudness_runs,
                limitation="Basic provider and preview loudness. Synthetic audio; excludes Beat This/ONNX and mix planning.")


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
    parser.add_argument("mode", choices=("planes", "preview", "export-start", "lyrics", "automix"))
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
    parser.add_argument("--video-count", type=int, choices=(0, 1, 2, 3, 4), default=1)
    parser.add_argument("--quality", choices=("resolution", "frames", "power", "quality"), default="resolution")
    parser.add_argument("--static-lyrics", action="store_true", help="Use a still bilingual cue with a cover")
    parser.add_argument("--styled", action="store_true", help="Apply color, blur, opacity and rotation to extra videos")
    parser.add_argument("--transport", action="store_true", help="Exercise pause, seek and fullscreen before measuring")
    parser.add_argument("--sample-nvidia", action="store_true", help="Sample device-wide NVIDIA usage and VRAM")
    parser.add_argument("--integrated", action="store_true", help="Use multiple songs, AutoMix and layered effects in Preview")
    parser.add_argument("--mix-wait", type=float, default=20, help="Seconds to wait for the integrated audio mix")
    parser.add_argument("--known-beats", action="store_true", help="Render AutoMix from the synthetic WAV's known beat grid")
    parser.add_argument("--display-refresh", action="store_true", help="Use the screen's natural preview cadence")
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
    elif args.mode == "automix":
        result = automix_benchmark(args)
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
