"""Real-audio native packet experiment, without requiring an NVIDIA GPU."""
from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
import math
import os
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, NativeCanvasFrame
from app.timeline.compiler import compile_playlist
from tests.test_native_canvas_project_probe import project_fixture
from tools.native_canvas_dynamic_overlays import (
    analyze_reactive_canvas_tracks, build_dynamic_stack, canvas_reaction_at,
    interleave_dynamic_layers, real_audio_dynamic_frames,
)
from tools.native_canvas_project_probe import (
    capture_project, inspect_project_track_windows,
    main as project_probe_main, project_source_inventory,
    validate_project_for_gpu_capture,
)
from tools.native_canvas_timeline_probe import TimelineValidationError, load_timeline


def sample_wav(path: Path) -> None:
    """Short PCM sine; diagnostics analyze actual audio rather than sample UI bars."""
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"".join(
            struct.pack("<h", round(10000 * math.sin(2 * math.pi * 440 * i / 8000)))
            for i in range(8000)
        ))


def dynamic_project(path: Path):
    project = project_fixture()
    project.playlist = [PlaylistTrack(str(path), "Sine", duration_seconds=1.0)]
    project.sources.extend([
        Source(SourceType.AUDIO_VISUALIZER, "Actual spectrum",
               x=30, y=25, width=380, height=170, z_index=2),
        Source(SourceType.PARTICLE_OVERLAY, "Deterministic particles",
               x=40, y=40, width=400, height=260, z_index=6),
    ])
    return project


class NativeDynamicOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_dynamic_stacking_keeps_static_canvas_between_effects(self):
        project = dynamic_project(Path("not-needed.wav"))
        plan = build_dynamic_stack(project, 0.25)
        self.assertEqual(plan.source_ids, (project.sources[-2].id, project.sources[-1].id))
        self.assertEqual(plan.order, (
            ("canvas", 0), ("dynamic", 0), ("canvas", 1), ("dynamic", 1),
        ))
        self.assertEqual(plan.bands, ((None, 1.5), (2.5, 5.5)))
        self.assertEqual(validate_project_for_gpu_capture(
            project, scale=0.25, source_policy="dynamic-audio",
        ), [])
        with self.assertRaisesRegex(TimelineValidationError, "dynamic sources"):
            validate_project_for_gpu_capture(project, scale=0.25)

    def test_equal_z_and_unsupported_effects_fail_closed(self):
        project = dynamic_project(Path("not-needed.wav"))
        project.sources[-2].z_index = 3
        with self.assertRaisesRegex(ValueError, "equal-Z"):
            build_dynamic_stack(project, 0.25)
        project.sources[-2].z_index = 2
        project.sources.append(Source(SourceType.VIDEO, "Unsupported video", z_index=8))
        with self.assertRaisesRegex(TimelineValidationError, "unsupported project sources"):
            validate_project_for_gpu_capture(project, scale=0.25, source_policy="dynamic-audio")
        project.sources.pop()
        project.sources[-2].rotation = 15
        with self.assertRaisesRegex(TimelineValidationError, "rotation"):
            validate_project_for_gpu_capture(project, scale=0.25, source_policy="dynamic-audio")

    def test_music_reaction_uses_original_track_fft_and_source_profiles(self):
        import numpy as np

        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            background = project.sources[0]
            background.source_type = SourceType.BACKGROUND
            background.background_bass_reactive = True
            lyric = next(s for s in project.sources if s.source_type is SourceType.LYRICS)
            lyric.music_reactive_enabled = True
            lyric.music_reactive_attack = 0.0
            lyric.music_reactive_release = 0.0
            lyric.music_reactive_sensitivity = 1.0
            lyric.music_reactive_threshold = 0.0
            project.sources[-2].source_type = SourceType.AUDIO_WAVEFORM
            track = project.playlist[0]
            fps = 30
            levels = np.full((30, 24), 0.002, dtype=np.float32)
            levels[8:16, :4] = 0.9
            sample_data = np.zeros(8000, dtype=np.float32)
            with patch(
                "tools.native_canvas_dynamic_overlays.FFmpegRenderer",
                return_value=SimpleNamespace(executable=Path("unused-ffmpeg")),
            ), patch.object(
                PythonVisualizerRenderer, "_decode_mono_audio",
                return_value=sample_data,
            ) as decode, patch.object(
                PythonVisualizerRenderer, "_analyze_levels", return_value=levels,
            ) as fft:
                envelopes = analyze_reactive_canvas_tracks(
                    project, [track], fps=fps,
                )
            decode.assert_called_once()
            fft.assert_called_once()
            self.assertEqual(fft.call_args.args[2], 24)
            self.assertIn(track.id, envelopes)
            self.assertIn((track.id, lyric.music_reaction_profile), envelopes)
            expected_bass = PythonVisualizerRenderer.bass_envelope(levels, fps)
            expected_music = PythonVisualizerRenderer.music_envelope(
                levels, fps, lyric.music_reaction_profile,
            )
            np.testing.assert_allclose(envelopes[track.id], expected_bass)
            np.testing.assert_allclose(
                envelopes[(track.id, lyric.music_reaction_profile)],
                expected_music,
            )
            bass, per_source = canvas_reaction_at(
                track, 9 / fps, fps, [background, lyric], envelopes,
            )
            self.assertAlmostEqual(bass, float(expected_bass[9]))
            self.assertAlmostEqual(per_source[lyric.id], float(expected_music[9]))
            self.assertGreater(bass, 0.1)
            self.assertGreater(per_source[lyric.id], 0.1)
            self.assertEqual(
                canvas_reaction_at(track, 0, fps, [lyric], {}), (0.0, {}),
            )

    def test_real_reactive_canvas_included_in_gpu_packet_manifest(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg needed for music-reactive source integration")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.sources[0].source_type = SourceType.BACKGROUND
            project.sources[0].background_bass_reactive = True
            lyric = next(x for x in project.sources if x.source_type is SourceType.LYRICS)
            lyric.music_reactive_enabled = True
            self.assertEqual(
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                ), [],
            )
            inventory = project_source_inventory(project)
            self.assertEqual([
                x["dynamic_audio_status"] for x in inventory
                if x["source_id"] in {project.sources[0].id, lyric.id}
            ], ["canvas_audio_reactive_supported"] * 2)
            project_file = root / "project.json"
            project_file.write_text(json.dumps(project.to_dict()), encoding="utf8")
            timeline_file = capture_project(
                project_file, root / "out", source_policy="dynamic-audio",
                prepared_audio=audio, ffmpeg=ffmpeg,
                frames=3, fps=30, scale=0.25,
            )
            meta, _ = load_timeline(timeline_file)
            self.assertEqual(
                meta["canvas_audio_reactive_source_ids"],
                [project.sources[0].id, lyric.id],
            )
            self.assertEqual(meta["canvas_audio_reaction_analysis"], "per_track_export_fft24")
            self.assertFalse(meta["full_project_visual_parity_verified"])
            meta["canvas_audio_reactive_source_ids"] = []
            timeline_file.write_text(json.dumps(meta), encoding="utf8")
            with self.assertRaisesRegex(TimelineValidationError, "music-reactive Canvas"):
                load_timeline(timeline_file)

    def test_waveform_and_stereo_meter_are_supported_with_real_audio(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg needed for waveform and meter analysis")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.sources[-2].source_type = SourceType.AUDIO_WAVEFORM
            project.sources[-1].source_type = SourceType.AUDIO_LEVEL_METER
            self.assertEqual(
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                ), [],
            )
            with real_audio_dynamic_frames(
                project, compile_playlist(project.playlist),
                fps=30, scale=0.25, start_seconds=0,
                prepared_audio=audio, ffmpeg=ffmpeg,
            ) as (overlays, streams, origin):
                self.assertEqual(origin, "provided_prepared_audio")
                self.assertEqual([x.kind for x in overlays], ["waveform", "level_meter"])
                self.assertTrue(all(not next(stream).isNull() for stream in streams))
            saved = root / "project.json"
            saved.write_text(json.dumps(project.to_dict()), encoding="utf8")
            timeline = capture_project(
                saved, root / "output", source_policy="dynamic-audio",
                prepared_audio=audio, ffmpeg=ffmpeg,
                frames=3, fps=30, scale=0.25,
            )
            data, refs = load_timeline(timeline)
            self.assertEqual(len(refs), 3)
            self.assertEqual(
                {src["source_type"] for src in data["dynamic_sources"]},
                {"audio_waveform", "audio_level_meter"},
            )
            packet = json.loads((root / "output" / "frame-0000.json").read_text("utf8"))
            self.assertEqual(len(packet["layers"]), 4)

    def test_inventory_and_rejection_identify_exact_unsupported_sources(self):
        with TemporaryDirectory() as directory:
            project = dynamic_project(Path("unused.wav"))
            unsupported = Source(SourceType.VIDEO, "Video background", z_index=8)
            project.sources.append(unsupported)
            report = project_source_inventory(project)
            video = next(row for row in report if row["source_id"] == unsupported.id)
            self.assertEqual(video["dynamic_audio_status"], "unsupported_video")
            with self.assertRaisesRegex(
                TimelineValidationError, "Video background"
            ) as caught:
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                )
            self.assertIn(unsupported.id, str(caught.exception))
            project_path = Path(directory) / "project.json"
            project_path.write_text(json.dumps(project.to_dict()), encoding="utf8")
            buffer = StringIO()
            with redirect_stdout(buffer):
                status = project_probe_main([
                    "--project", str(project_path), "--inspect-sources",
                ])
            self.assertEqual(status, 0)
            result = json.loads(buffer.getvalue())
            self.assertEqual(result["status"], "inspected")
            self.assertEqual(len(result["track_windows"]), 1)
            self.assertEqual(result["track_windows"][0]["recommended_probe_frame"], 0)
            self.assertIn(unsupported.id, [
                entry["source_id"] for entry in result["sources"]
            ])
            self.assertFalse((Path(directory) / "capture").exists())

    def test_distant_frame_seek_is_pixel_identical_to_full_progression(self):
        import numpy as np

        renderer = PythonVisualizerRenderer(Path("ffmpeg-not-required"))
        levels = np.full((130, 24), 0.01, dtype=np.float32)
        levels[30:60, :6] = 0.85
        levels[95:, 6:18] = 0.55
        stereo = (
            np.linspace(0.02, 0.88, len(levels), dtype=np.float32),
            np.linspace(0.77, 0.03, len(levels), dtype=np.float32),
        )
        windows = ((0.0, 2.0), (2.0, 2.4))
        index = 74  # On the SECOND track, not near project start.
        for kind in ("visualizer", "particles", "waveform", "level_meter"):
            with self.subTest(kind=kind):
                overlay = VisualizerOverlay(
                    x=6, y=9, width=100, height=55, style="bars",
                    color="#FFFFFF", kind=kind,
                    personal_colors=("#FF2244", "#22CC88"),
                )
                original = renderer._layer_frames(
                    overlay, levels, 30, Event(), None, stereo, windows,
                )
                for _ in range(index):
                    next(original)
                expected = next(original).copy()
                expected_next = next(original).copy()
                with patch.object(
                    renderer, "_draw_frame", wraps=renderer._draw_frame,
                ) as painter:
                    sought = renderer._layer_frames(
                        overlay, levels, 30, Event(), None, stereo, windows,
                        start_frame=index,
                    )
                    actual = next(sought).copy()
                    actual_next = next(sought).copy()
                self.assertEqual(painter.call_count, 2)
                self.assertEqual(bytes(actual.constBits()), bytes(expected.constBits()))
                self.assertEqual(
                    bytes(actual_next.constBits()), bytes(expected_next.constBits()),
                )
        with self.assertRaisesRegex(ValueError, "start frame"):
            next(renderer._layer_frames(
                VisualizerOverlay(x=0,y=0,width=64,height=40,style="bars",
                                  color="#FFFFFF"),
                levels,30,Event(),None,start_frame=130,
            ))
        # Seek 45 seconds into a long playlist: no 2700-frame Qt warm-up.
        distant_levels = np.full((2703, 24), 0.05, dtype=np.float32)
        with patch.object(renderer, "_draw_frame", wraps=renderer._draw_frame) as paint:
            long_seek = renderer._layer_frames(
                VisualizerOverlay(
                    x=0, y=0, width=90, height=50,
                    style="bars", color="#FFFFFF", kind="particles",
                ), distant_levels, 60, Event(), None, start_frame=2700,
            )
            self.assertFalse(next(long_seek).isNull())
        self.assertEqual(paint.call_count, 1)

    def test_cross_track_seek_reuses_real_track_clocks_not_time_zero(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg needed to seek across real soundtrack track boundary")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.wav"
            second = root / "second.wav"
            joined = root / "two-tracks.wav"
            sample_wav(first)
            sample_wav(second)
            with wave.open(str(joined), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                for filename in (first, second):
                    with wave.open(str(filename), "rb") as part:
                        output.writeframes(part.readframes(part.getnframes()))
            project = dynamic_project(first)
            first_track = project.playlist[0]
            second_track = PlaylistTrack(str(second), "Next track", duration_seconds=1.0)
            project.playlist.append(second_track)
            windows = inspect_project_track_windows(project, 30)
            self.assertEqual(
                [row["recommended_probe_frame"] for row in windows], [0, 30],
            )
            self.assertEqual(
                [row["recommended_probe_start_seconds"] for row in windows], [0.0, 1.0],
            )
            project.sources[0].source_type = SourceType.BACKGROUND
            project.sources[0].background_bass_reactive = True
            lyrics = next(x for x in project.sources if x.source_type is SourceType.LYRICS)
            lyrics.music_reactive_enabled = True
            target = root / "project.json"
            target.write_text(json.dumps(project.to_dict()), encoding="utf8")
            timeline_file = capture_project(
                target, root / "after-boundary", source_policy="dynamic-audio",
                prepared_audio=joined, ffmpeg=ffmpeg,
                start_seconds=29 / 30, frames=5, fps=30, scale=0.25,
            )
            timeline, refs = load_timeline(timeline_file)
            self.assertEqual(len(refs), 5)
            frame_ids = [row["track_id"] for row in timeline["frames"]]
            self.assertEqual(frame_ids[:1], [first_track.id])
            self.assertEqual(frame_ids[1:], [second_track.id] * 4)
            self.assertEqual(timeline["frames"][1]["track_number"], 2)
            self.assertAlmostEqual(timeline["frames"][1]["track_elapsed_seconds"], 0.0)
            self.assertAlmostEqual(timeline["frames"][1]["timeline_seconds"], 1.0)
            self.assertEqual(len(timeline["dynamic_sources"]), 2)
            self.assertEqual(len(timeline["canvas_audio_reactive_source_ids"]), 2)

    def test_bgra_alpha_and_order_are_retained(self):
        project = dynamic_project(Path("not-needed.wav"))
        stack = build_dynamic_stack(project, 0.25)
        width, height = 320, 180
        base = NativeCanvasBgraLayer(
            "base", width, height, bytes((3, 2, 1, 255)) * (width * height),
        )
        foreground = NativeCanvasBgraLayer(
            "layer:0", width, height, bytes((0, 0, 0, 0)) * (width * height),
        )
        static = NativeCanvasFrame(width, height, (base, foreground))
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(255, 0, 0, 128))
        from types import SimpleNamespace
        overlays = [
            (SimpleNamespace(x=2, y=3), image),
            (SimpleNamespace(x=5, y=6), image),
        ]
        result = interleave_dynamic_layers(static, stack, overlays)
        self.assertEqual(len(result.layers), 4)
        self.assertEqual([x.stream_key for x in result.layers],
                         ["base", "layer:0", "layer:1", "layer:2"])
        self.assertEqual(result.layers[0].pixels[:4], bytes((3, 2, 1, 255)))
        offset = (3 * width + 2) * 4
        self.assertEqual(result.layers[1].pixels[offset:offset + 4],
                         bytes((0, 0, 255, 128)))
        self.assertEqual(result.layers[2].pixels[offset:offset + 4],
                         bytes((0, 0, 0, 0)))

    def test_real_audio_frames_and_gpu_manifests(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg needed for real audio analysis")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            plan = compile_playlist(project.playlist)
            with real_audio_dynamic_frames(
                project, plan, fps=30, scale=0.25, start_seconds=0,
                prepared_audio=audio, ffmpeg=ffmpeg,
            ) as (overlays, streams, origin):
                self.assertEqual(origin, "provided_prepared_audio")
                self.assertEqual([o.kind for o in overlays], ["visualizer", "particles"])
                # The production export helper clamps its scale to >=1.0,
                # whereas native diagnostics deliberately capture at 0.25x.
                self.assertEqual((overlays[0].x, overlays[0].y), (8, 6))
                self.assertEqual((overlays[0].width, overlays[0].height), (95, 42))
                self.assertTrue(all(not next(stream).isNull() for stream in streams))
            source = root / "project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            snapshot = source.read_bytes()
            output = root / "capture"
            timeline = capture_project(
                source, output, source_policy="dynamic-audio",
                prepared_audio=audio, ffmpeg=ffmpeg, frames=3, fps=30, scale=0.25,
            )
            meta, refs = load_timeline(timeline)
            self.assertEqual(len(refs), 3)
            self.assertEqual(meta["capture_scope"], "dynamic_overlay_audio_diagnostic")
            self.assertEqual(len(meta["dynamic_sources"]), 2)
            self.assertEqual(meta["dynamic_audio_origin"], "provided_prepared_audio")
            self.assertFalse(meta["full_project_visual_parity_verified"])
            self.assertEqual(source.read_bytes(), snapshot)
            packet = json.loads((output / "frame-0000.json").read_text("utf8"))
            self.assertEqual(len(packet["layers"]), 4)
            # The timeline validator must accept 4 actual project layers but
            # reject a reordered packet before hardware encoding.
            packet_path = output / "frame-0000.json"
            original_packet = packet_path.read_text(encoding="utf8")
            packet["layers"][1]["stream_key"] = "layer:99"
            packet_path.write_text(json.dumps(packet), encoding="utf8")
            with self.assertRaisesRegex(TimelineValidationError, "ordering"):
                load_timeline(timeline)
            packet_path.write_text(original_packet, encoding="utf8")
            load_timeline(timeline)
            # Reject a forged diagnostic that fails to disclose rendered sources.
            meta["dynamic_sources"] = []
            timeline.write_text(json.dumps(meta), encoding="utf8")
            with self.assertRaisesRegex(TimelineValidationError, "disclosure"):
                load_timeline(timeline)


if __name__ == "__main__":
    unittest.main()
