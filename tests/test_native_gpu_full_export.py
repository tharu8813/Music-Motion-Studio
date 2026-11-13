"""Exact complete-project Qt frame generator checks (GPU mock is explicit)."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
from threading import Event
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor, QPainter

from app.animation.keyframes import Keyframe
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.native_canvas_layer_packets import NativeCanvasParticleLayer, straight_bgra_bytes
from app.renderer.export_timeline import ExportTimelinePlanner
from app.renderer.cfr_sample_pruning import iter_cfr_emitting_samples
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderFrame, RenderResult, RenderSettings
from app.renderer.native_dynamic_layers import prepare_project_frame_audio
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.timeline.compiler import compile_playlist
from tests.test_native_canvas_dynamic_overlays import dynamic_project, sample_wav
from tools.native_gpu_full_export import (
    export_complete_project, make_short_smoke_project, produce_complete_project_frames,
)


class NativeGpuFullProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        # These tests mock the native encoder, not a real development export.
        gate = patch("app.renderer.native_project_export._require_developer_runtime")
        gate.start()
        self.addCleanup(gate.stop)
        hardware = patch("app.renderer.native_project_export.probe_native_gpu", return_value=17)
        hardware.start()
        self.addCleanup(hardware.stop)

    def test_production_frames_match_existing_canvas_capture_for_music_lyrics_and_particles(self):
        ffmpeg = FFmpegRenderer.find_executable(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"))
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "tone.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.canvas.width, project.canvas.height = 320, 180
            cover = QImage(128, 128, QImage.Format.Format_RGB32)
            cover.fill(QColor("#154266"))
            painter = QPainter(cover)
            painter.fillRect(0, 0, 50, 128, QColor("#e36b34"))
            painter.end()
            cover.save(str(root / "cover.png"))
            track = project.playlist[0]
            track.cover_path = str(root / "cover.png")
            track.lyrics = [{"start": 0, "end": 1, "text": "Current lyric"}]
            project.sources = [
                Source(SourceType.BACKGROUND, "Bass", width=320, height=180, z_index=0,
                       background_mode="album_art", background_bass_reactive=True),
                Source(SourceType.LYRICS, "Lyrics", x=20, y=20, width=280, height=130,
                       z_index=1, font_size=24, music_reactive_enabled=True,
                       animation_in="fade", animation_in_duration=0.3,
                       animation_out="fade", animation_out_duration=0.3,
                       subtitle_animation="none", subtitle_intro_enabled=False,
                       subtitle_intro_midtrack=False, subtitle_context_lines=0, subtitle_next_lines=0),
                Source(SourceType.PARTICLE_OVERLAY, "Particles", width=320, height=180,
                       z_index=2, particle_density=12, opacity=0.18),
            ]
            plan = compile_playlist(project.playlist)
            analysis = prepare_project_frame_audio(project, plan, fps=15, scale=1,
                prepared_audio=audio, ffmpeg=str(ffmpeg), cancel_event=Event())
            self.assertGreater(max(analysis.envelopes[track.id]), 0)
            self.assertGreater(max(analysis.envelopes[(track.id, project.sources[1].music_reaction_profile)]), 0)
            scene = CanvasScene()
            scene.set_artboard_size(320, 180)
            self.addCleanup(scene.clear)
            for source in deepcopy(project.sources):
                scene.addItem(SourceItem(source))
            reference = ExportCanvasCapturer(scene, [track], 1, {project.sources[-1].id},
                [(None, 1.5)], lambda image, duration, _key: RenderFrame(image, duration),
                lambda: None, lambda *_: None, bass_envelopes=analysis.envelopes,
                bass_fps=15, compiled_plan=plan)
            particles = PythonVisualizerRenderer(ffmpeg)._layer_frames(
                analysis.overlays[0], analysis.levels, 15, Event(), None,
            )
            native = produce_complete_project_frames(project, fps=15, scale=1,
                prepared_audio=audio, ffmpeg=str(ffmpeg), analysis=analysis,
                optimizations=NativeGpuOptimizations.for_profile("automatic"))
            self.addCleanup(native.close)
            self.addCleanup(particles.close)
            schedule = iter(iter_cfr_emitting_samples(ExportTimelinePlanner.iter_build(
                [track], project.sources[:-1], 15, plan,
            ), 15))
            sample = next(schedule)
            for index, packet in enumerate(native):
                packet = packet.resolve() if hasattr(packet, "resolve") else packet
                while sample.cfr_target_frame_count <= index:
                    sample = next(schedule)
                image = reference.capture_stream(sample, "base").image
                self.assertEqual(packet.layers[0].pixels, straight_bgra_bytes(image))
                self.assertEqual(packet.layers[1].pixels, straight_bgra_bytes(next(particles)))
            self.assertEqual(index + 1, 15)

    def test_automatic_gpu_offloads_neon_particles_without_reordering_layers(self):
        ffmpeg = FFmpegRenderer.find_executable(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"))
        with TemporaryDirectory() as directory:
            audio = Path(directory) / "tone.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            particle = project.sources[-1]
            particle.particle_style = "neon"
            particle.particle_density = 12
            project.sources.append(Source(SourceType.SHAPE, "Above particles", width=180,
                                          height=90, z_index=8))
            automatic = NativeGpuOptimizations.for_profile("automatic")

            def frame(optimizations):
                frames = produce_complete_project_frames(
                    project, fps=12, scale=.25, prepared_audio=audio, ffmpeg=str(ffmpeg),
                    optimizations=optimizations,
                )
                try:
                    packet = next(frames)
                    return packet.resolve() if hasattr(packet, "resolve") else packet
                finally:
                    frames.close()

            qt = frame(replace(automatic, gpu_particles=False))
            gpu = frame(automatic)
            sprite = gpu.layers[3]
            self.assertEqual((qt.schema, gpu.schema), (2, 3))
            self.assertEqual([layer.stream_key for layer in qt.layers],
                             [layer.stream_key for layer in gpu.layers])
            self.assertEqual(qt.layers[:3], gpu.layers[:3])
            self.assertEqual(qt.layers[4:], gpu.layers[4:])
            self.assertIsInstance(sprite, NativeCanvasParticleLayer)
            self.assertEqual((sprite.clip, len(sprite.pixels)), ((10, 10, 110, 75), 24 * 16))

            particle.mask_shape = "ellipse"
            self.assertEqual(frame(automatic), frame(replace(automatic, gpu_particles=False)))

    def test_automatic_gpu_reuses_held_lyric_samples_and_keeps_particle_frames(self):
        ffmpeg = FFmpegRenderer.find_executable(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"))
        with TemporaryDirectory() as temp:
            audio = Path(temp) / "tone.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.canvas.width, project.canvas.height = 640, 480
            project.playlist.append(PlaylistTrack(str(audio), "Second", duration_seconds=1.0))
            project.playlist[0].lyrics = [
                {"start": 0, "end": 0.5, "text": "First cue"},
                {"start": 0.5, "end": 1, "text": "Next cue"},
            ]
            project.playlist[1].lyrics = [{"start": 0, "end": 1, "text": "Second track"}]
            project.sources = [
                Source(SourceType.SHAPE, "Base", width=640, height=480, z_index=0),
                Source(SourceType.PARTICLE_OVERLAY, "Particles", width=640, height=480,
                       z_index=1, particle_density=12),
                Source(SourceType.LYRICS, "Lyrics", x=40, y=40, width=560, height=300,
                       z_index=2, font_size=32, text_stroke_width=4,
                       subtitle_animation="none", subtitle_intro_enabled=False,
                       subtitle_context_lines=0, subtitle_next_lines=0),
            ]
            plan = compile_playlist(project.playlist)
            fps = 12
            prepared = FFmpegRenderer(ffmpeg).prepare_playlist_audio(
                project.playlist, Path(temp), RenderSettings(fps=fps),
                transition_mode="none", cancel_event=Event(),
            )
            analysis = prepare_project_frame_audio(
                project, plan, fps=fps, scale=1, prepared_audio=prepared,
                ffmpeg=str(ffmpeg), cancel_event=Event(),
            )
            automatic = NativeGpuOptimizations.for_profile("automatic")
            reference_metrics, reuse_metrics = {}, {}
            common = dict(fps=fps, scale=1, prepared_audio=prepared,
                          ffmpeg=str(ffmpeg), analysis=analysis)
            reference = produce_complete_project_frames(
                project, **common, raster_counters=reference_metrics,
                optimizations=replace(automatic, reuse_canvas_samples=False),
            )
            candidate = produce_complete_project_frames(
                project, **common, raster_counters=reuse_metrics, optimizations=automatic,
            )
            self.addCleanup(reference.close)
            self.addCleanup(candidate.close)
            previous = None
            frames_compared = 0
            particle_changes_during_hold = 0
            for index, (expected, actual) in enumerate(zip(reference, candidate, strict=True)):
                expected = expected.resolve() if hasattr(expected, "resolve") else expected
                actual = actual.resolve() if hasattr(actual, "resolve") else actual
                self.assertEqual(actual, expected, f"frame {index}")
                if previous is not None:
                    if index in (6, 12):
                        self.assertNotEqual(actual.layers[2], previous.layers[2])
                    else:
                        self.assertEqual(actual.layers[2], previous.layers[2])
                        particle_changes_during_hold += actual.layers[1] != previous.layers[1]
                previous = actual
                frames_compared += 1
            self.assertEqual(frames_compared, 2 * fps)
            origins = reuse_metrics["frame_layer_source_types"]
            self.assertEqual(
                set(origins),
                {"base"} | {f"layer:{i}" for i in range(len(previous.layers) - 1)},
            )
            captured_types = {
                source_type
                for descriptor in origins.values()
                for source_type in descriptor["source_types"]
            }
            self.assertTrue({"shape", "lyrics", "particle_overlay"} <= captured_types)
            self.assertGreater(particle_changes_during_hold, 0)
            self.assertEqual(reuse_metrics["sample_reuse_hits"], 2 * fps - 3)
            self.assertEqual(reuse_metrics["qt_canvas_band_timing"]["layer:0"]["captures"], 3)
            self.assertEqual(
                reference_metrics["rasterizations"] - reuse_metrics["rasterizations"],
                reuse_metrics["sample_reuse_hits"],
            )
            self.assertLessEqual(reuse_metrics["peak_retained_bytes"], 2 * 640 * 480 * 4)
            self.assertEqual(reuse_metrics["retained_bytes"], 0)

    def test_gpu_translation_reuses_image_and_shape_pixels_with_exact_qt_fallback(self):
        with TemporaryDirectory() as temp:
            image_path = Path(temp) / "alpha.png"
            image = QImage(31, 19, QImage.Format.Format_ARGB32)
            image.fill(QColor("#803b9df4"))
            painter = QPainter(image)
            painter.fillRect(0, 0, 13, 9, QColor("#e2f27a30"))
            painter.end()
            self.assertTrue(image.save(str(image_path)))
            automatic = NativeGpuOptimizations.for_profile("automatic")
            baseline = replace(automatic, gpu_canvas_translation=False)
            for kind in (SourceType.IMAGE, SourceType.SHAPE):
                with self.subTest(kind=kind):
                    project = dynamic_project(Path(temp) / "unused.wav")
                    project.canvas.width, project.canvas.height = 320, 180
                    moving = Source(
                        kind, "Moving layer", x=28.25, y=24, width=52, height=31, z_index=1,
                        opacity=0.7, border_radius=5, outline_width=2, content_path=str(image_path),
                    )
                    curves = {
                        "x": [Keyframe(i / 8, x, "hold") for i, x in enumerate(
                            (28.25, 32.25, 36.5, -16, 44.25, 48.25, 52.25, 56.25)
                        )],
                        "y": [Keyframe(0, 24, "linear"), Keyframe(1, 40)],
                    }
                    if kind is SourceType.IMAGE:
                        moving.keyframes.global_curves = curves
                    else:
                        moving.keyframes.mode = "per_track"
                        moving.keyframes.per_track_curves[project.playlist[0].id] = curves
                    project.sources = [
                        Source(SourceType.SHAPE, "Base", width=320, height=180, z_index=0),
                        moving,
                    ]
                    before = project.to_dict()
                    metrics = {}
                    params = dict(fps=8, scale=1.5, prepared_audio=Path(temp) / "unused.wav",
                                  ffmpeg="unused")
                    reference = produce_complete_project_frames(project, **params, optimizations=baseline)
                    candidate = produce_complete_project_frames(
                        project, **params, optimizations=automatic, raster_counters=metrics,
                    )
                    self.addCleanup(reference.close)
                    self.addCleanup(candidate.close)
                    texture = None
                    for index, (expected, actual) in enumerate(zip(reference, candidate, strict=True)):
                        expected = expected.resolve() if hasattr(expected, "resolve") else expected
                        actual = actual.resolve() if hasattr(actual, "resolve") else actual
                        self.assertEqual(actual.layers[0], expected.layers[0])
                        layer = actual.layers[1]
                        if index in (2, 3):
                            self.assertEqual(layer, expected.layers[1])
                            continue
                        self.assertIsNotNone(layer.inverse_transform, f"frame {index}")
                        self.assertLess(layer.width, actual.width)
                        if texture is None:
                            texture = layer.pixels
                        self.assertIs(layer.pixels, texture)
                        x, y, right, bottom = layer.clip
                        expanded = bytearray(actual.width * actual.height * 4)
                        stride = layer.width * 4
                        self.assertEqual(layer.inverse_transform, (1, 0, 0, 1, -x, -y))
                        self.assertEqual((right - x, bottom - y), (layer.width, layer.height))
                        for row in range(layer.height):
                            offset = ((y + row) * actual.width + x) * 4
                            expanded[offset:offset + stride] = layer.pixels[row * stride:(row + 1) * stride]
                        self.assertEqual(expanded, expected.layers[1].pixels, f"frame {index}")
                    self.assertEqual(metrics["gpu_canvas_translation_frames"], 6)
                    self.assertEqual(metrics["gpu_canvas_translation_textures"], 1)
                    self.assertEqual(metrics["qt_canvas_band_timing"]["layer:0"]["captures"], 3)
                    self.assertEqual(metrics["retained_bytes"], 0)
                    self.assertEqual(project.to_dict(), before)

    def test_album_cover_motion_keeps_qt_fades_and_refreshes_texture_per_track(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = dynamic_project(root / "unused.wav")
            project.canvas.width, project.canvas.height = 320, 180
            cover = Source(
                SourceType.ALBUM_COVER, "Cover", x=15, y=25, width=60, height=60,
                z_index=2, scale=1.1, mask_shape="circle", animation_in="swing",
                animation_in_duration=.125, loop_motion="spin", loop_motion_period=2,
            )
            project.sources = [cover]
            project.playlist = [
                PlaylistTrack("", title, duration_seconds=.5, cover_path=str(root / f"{title}.png"))
                for title in ("Red", "Blue")
            ]
            for track in project.playlist:
                art = QImage(64, 64, QImage.Format.Format_RGB32)
                art.fill(QColor(track.title))
                self.assertTrue(art.save(track.cover_path))
            before = project.to_dict()
            automatic = NativeGpuOptimizations.for_profile("automatic")
            common = dict(fps=16, scale=1, prepared_audio=root / "unused.wav", ffmpeg="unused")
            baseline = list(produce_complete_project_frames(
                project, **common, optimizations=replace(automatic, gpu_album_cover_motion=False),
            ))
            metrics = {}
            frames = list(produce_complete_project_frames(
                project, **common, optimizations=automatic, raster_counters=metrics,
            ))
            self.assertEqual(len(frames), len(baseline))
            self.assertEqual(len(frames), 16)
            self.assertTrue(all(len(frame.layers) == 2 for frame in frames))
            self.assertTrue(all(frame.layers[0].pixels is frames[0].layers[0].pixels for frame in frames))
            self.assertEqual(frames[0].layers[0].pixels, baseline[0].layers[0].pixels)
            self.assertTrue(all(v == 0 for v in frames[0].layers[1].pixels[3::4]))
            self.assertIs(frames[4].layers[1].pixels, frames[6].layers[1].pixels)
            self.assertNotEqual(frames[4].layers[1].inverse_transform, frames[6].layers[1].inverse_transform)
            self.assertNotEqual(frames[4].layers[1].pixels, frames[12].layers[1].pixels)
            self.assertEqual(metrics["gpu_album_cover_texture_captures"], 2)
            self.assertGreater(metrics["gpu_album_cover_frames"], 0)
            self.assertLess(metrics["gpu_album_cover_frames"], len(frames))
            self.assertEqual(metrics["retained_bytes"], 0)
            self.assertEqual(project.to_dict(), before)

            project.sources.append(Source(
                SourceType.TIME, "Clock", x=180, y=24, width=90, height=40, z_index=3,
            ))
            metrics = {}
            mixed_reference = list(produce_complete_project_frames(
                project, **common, optimizations=replace(automatic, gpu_album_cover_motion=False),
            ))
            mixed = list(produce_complete_project_frames(
                project, **common, optimizations=automatic, raster_counters=metrics,
            ))
            self.assertEqual(mixed, mixed_reference)
            self.assertEqual(metrics["gpu_album_cover_frames"], 0)

    def test_complete_project_yields_all_real_qt_and_audio_layers_lazily(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for real-audio full project frames")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.sources[0].background_bass_reactive = True
            before = project.to_dict()
            frames = produce_complete_project_frames(
                project, fps=30, scale=0.25,
                prepared_audio=audio, ffmpeg=str(ffmpeg),
            )
            self.assertEqual(compile_playlist(project.playlist).duration_seconds, 1.0)
            count = 0
            for frame in frames:
                count += 1
                self.assertEqual((frame.width, frame.height), (320, 180))
                self.assertEqual(len(frame.layers), 4)
                self.assertEqual(
                    [layer.stream_key for layer in frame.layers],
                    ["base", "layer:0", "layer:1", "layer:2"],
                )
            self.assertEqual(count, 30)
            self.assertEqual(project.to_dict(), before)

    def test_full_export_invokes_real_bounded_stream_and_mp4_mux_after_audio_prep(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for real-audio export orchestration")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.settings.title = "Native audio project"
            source = root / "project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            original_bytes = source.read_bytes()
            target = root / "full.mp4"
            collected = []

            def mock_stream(_renderer, source_frames, **kwargs):
                for frame in source_frames:
                    collected.append((frame.width, frame.height, len(frame.layers)))
                self.assertEqual(kwargs["settings"].fps, 30)
                self.assertEqual(kwargs["settings"].video_codec, "h264_nvenc")
                self.assertEqual(kwargs["metadata"].title, "Native audio project")
                self.assertEqual(kwargs["compiled_plan"].duration_seconds, 1.0)
                return RenderResult(target, 1)

            with patch(
                "app.renderer.native_project_export.FFmpegRenderer.prepare_playlist_audio",
                return_value=audio,
            ) as prepare, patch(
                "app.renderer.native_project_export.render_native_gpu_playlist",
                side_effect=mock_stream,
            ) as render, patch("app.renderer.native_project_export.resolve_native_executable", return_value=root / "native.exe"):
                result = export_complete_project(
                    source, target, native_exe=root / "native.exe",
                    fps=30, scale=0.25, ffmpeg=str(ffmpeg),
                )
            self.assertEqual(result, target)
            self.assertEqual(len(collected), 30)
            self.assertEqual(collected[0], (320, 180, 4))
            prepare.assert_called_once()
            render.assert_called_once()
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_smoke_project_is_detached_and_keeps_source_layout(self):
        with TemporaryDirectory() as temp:
            project = dynamic_project(Path(temp) / "song.wav")
            project.playlist[0].start_time_seconds = 7.0
            project.playlist[0].duration_seconds = 3.0
            original = project.to_dict()
            clipped = make_short_smoke_project(project, 1.0, fps=30)
            self.assertEqual(project.to_dict(), original)
            self.assertIsNot(clipped, project)
            self.assertEqual(clipped.playlist[0].id, project.playlist[0].id)
            self.assertEqual(clipped.playlist[0].duration_seconds, 1.0)
            self.assertIsNone(clipped.playlist[0].start_time_seconds)
            self.assertEqual(len(clipped.playlist), 1)
            self.assertEqual(clipped.sources[0].id, project.sources[0].id)
            self.assertEqual(compile_playlist(clipped.playlist).duration_seconds, 1.0)
            self.assertIn("smoke clip", clipped.settings.title)

    def test_qt_paint_profiler_flag_is_dev_only_and_preserves_automatic_default(self):
        self.assertFalse(
            NativeGpuOptimizations.for_profile("automatic").profile_canvas_source_paint
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "project.json"
            source.write_text("{}", encoding="utf8")
            for profile in (None, "standard", "accelerated", "cpu_parallel"):
                with self.subTest(profile=profile), self.assertRaisesRegex(
                    ValueError, "--gpu-profile-qt-paint requires --gpu-profile automatic",
                ):
                    export_complete_project(
                        source, root / "out.mp4", gpu_profile=profile,
                        gpu_profile_qt_paint=True,
                    )

    def test_native_qt_source_paint_report_counts_real_rendered_types(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for actual Canvas frame profiling")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            snapshot = project.to_dict()
            metrics = {}
            opts = replace(
                NativeGpuOptimizations.for_profile("automatic"),
                profile_canvas_source_paint=True,
            )
            frames = produce_complete_project_frames(
                project, fps=30, scale=0.25, prepared_audio=audio,
                ffmpeg=str(ffmpeg), optimizations=opts,
                raster_counters=metrics,
            )
            seen = 0
            for frame in frames:
                seen += 1
                self.assertEqual((frame.width, frame.height), (320, 180))
                if seen == 1:
                    # Diagnostics are export-final, not expensive per-frame
                    # copies of evolving nested Qt paint dictionaries.
                    self.assertEqual(metrics, {})
            self.assertEqual(seen, 30)
            self.assertEqual(project.to_dict(), snapshot)
            self.assertEqual(metrics["retained_bytes"], 0)
            self.assertIn("qt_canvas_band_timing", metrics)
            self.assertIn("qt_canvas_source_type_paint", metrics)
            by_type = metrics["qt_canvas_source_type_paint"]
            self.assertTrue(by_type)
            self.assertGreater(sum(row["calls"] for row in by_type.values()), 0)
            self.assertTrue(all(row["seconds"] > 0 for row in by_type.values()))
            self.assertLessEqual(
                sum(row["seconds"] for row in by_type.values()),
                metrics["qt_canvas_paint_stage_seconds"] + 0.2,
            )
            self.assertIn("qt_canvas_text_stage_costs", metrics)
            stage_costs = metrics["qt_canvas_text_stage_costs"]
            self.assertTrue(stage_costs)
            self.assertGreater(
                sum(row["calls"] for stages in stage_costs.values()
                    for row in stages.values()), 0,
            )
            self.assertTrue(all(
                row["seconds"] > 0
                for stages in stage_costs.values() for row in stages.values()
            ))
            # Inclusive substage timings overlap. Every substage remains
            # bounded by that category's measured outer paint() duration.
            for category, stages in stage_costs.items():
                self.assertIn(category, by_type)
                for stage, entry in stages.items():
                    with self.subTest(category=category, stage=stage):
                        self.assertLessEqual(
                            entry["seconds"],
                            by_type[category]["seconds"] + 0.2,
                        )

    def test_invalid_smoke_seconds_fail_before_any_gpu_or_audio_work(self):
        with TemporaryDirectory() as temp:
            project = dynamic_project(Path(temp) / "song.wav")
            for value in [0, 0.2, -1, 31, 1.1, float("nan"), float("inf")]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    make_short_smoke_project(project, value, fps=30)
            with self.assertRaisesRegex(ValueError, "frame boundary"):
                make_short_smoke_project(project, 0.52, fps=30)

    def test_smoke_export_generates_30_real_qt_audio_frames_for_a_1s_mp4(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for short real-project frame generation")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "song.wav"
            sample_wav(audio)
            original = dynamic_project(audio)
            original.playlist[0].duration_seconds = 3.0
            path = root / "project.json"
            path.write_text(json.dumps(original.to_dict()), encoding="utf8")
            original_bytes = path.read_bytes()
            target = root / "smoke.mp4"
            completed = []

            def fake_native(_renderer, source_frames, **kwargs):
                completed.extend(
                    (frame.width, frame.height, len(frame.layers))
                    for frame in source_frames
                )
                self.assertEqual(kwargs["compiled_plan"].duration_seconds, 1.0)
                self.assertEqual(len(kwargs["tracks"]), 1)
                self.assertEqual(kwargs["tracks"][0].duration_seconds, 1.0)
                self.assertIn("short GPU smoke clip", kwargs["metadata"].title)
                self.assertEqual(
                    kwargs["optimizations"],
                    NativeGpuOptimizations.for_profile("automatic"),
                )
                return RenderResult(target, 1.0)

            with patch(
                "app.renderer.native_project_export.FFmpegRenderer.prepare_playlist_audio",
                return_value=audio,
            ), patch(
                "app.renderer.native_project_export.render_native_gpu_playlist",
                side_effect=fake_native,
            ), patch("app.renderer.native_project_export.resolve_native_executable", return_value=root / "native.exe"):
                actual = export_complete_project(
                    path, target, native_exe=root / "native.exe",
                    fps=30, scale=0.25, ffmpeg=str(ffmpeg),
                    smoke_seconds=1.0, gpu_profile="automatic",
                )
            self.assertEqual(actual, target)
            self.assertEqual(len(completed), 30)
            self.assertEqual(completed[0], (320, 180, 4))
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_refuses_to_overwrite_user_video_before_audio_or_gpu_start(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "full.mp4"
            existing.write_bytes(b"USER-ORIGINAL-VIDEO")
            source = root / "project.json"
            source.write_text(json.dumps(dynamic_project(root / "test.wav").to_dict()),
                              encoding="utf8")
            with self.assertRaisesRegex(ValueError, "will not be overwritten"):
                export_complete_project(
                    source, existing, native_exe=root / "native.exe",
                )
            self.assertEqual(existing.read_bytes(), b"USER-ORIGINAL-VIDEO")


if __name__ == "__main__":
    unittest.main()
