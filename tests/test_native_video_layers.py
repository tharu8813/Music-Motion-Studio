"""CFR video requests and optional end-to-end DXVA/NVENC verification."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from app.models.playlist import PlaylistTrack
from app.models.project import CanvasSettings, ProjectDocument
from app.models.source import Source, SourceType
from app.preview.native_canvas_layer_packets import (
    NativeCanvasBgraLayer, NativeCanvasFrame, NativeCanvasVideoLayer,
)
from app.renderer.native_frame_transport import frame_chunks, FrameReuseState, HEADER
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.renderer.native_project_export import export_project_document
from app.renderer.native_project_support import TimelineValidationError, validate_project_for_gpu_capture
from app.renderer.native_project_video import prepare_native_video_sources
from app.timeline.compiler import compile_playlist


WORKER = os.environ.get("PLAYLIST_NATIVE_VIDEO_WORKER")


def project_for_video(video, audio, *, loop=True):
    return ProjectDocument(
        canvas=CanvasSettings(320, 180),
        playlist=[PlaylistTrack(str(audio), "Soundtrack", duration_seconds=2.5)],
        sources=[
            Source(SourceType.BACKGROUND, "Canvas", width=320, height=180, fill_color="#14233f", z_index=0),
            Source(SourceType.VIDEO, "Video", x=40, y=20, width=240, height=120, z_index=1,
                   video_paths=[str(video)], image_fit_mode="stretch", video_speed=1.25,
                   video_repeat_mode="loop_one" if loop else "once", timeline_start=.25, timeline_duration=2),
            Source(SourceType.SHAPE, "Above video", x=70, y=40, width=40, height=24, z_index=2,
                   fill_color="#ffb400"),
        ],
    )


class NativeVideoLayerTests(unittest.TestCase):
    def test_gpu_video_timestamps_follow_offset_speed_end_and_repeat(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.touch()
            for loop in (False, True):
                project = project_for_video(path, Path("unused.wav"), loop=loop)
                plan = compile_playlist(project.playlist)
                with (
                    patch("app.renderer.native_project_video._find_ffprobe", return_value=Path("ffprobe")),
                    patch("app.renderer.native_project_video._probe_video", return_value=1.0),
                ):
                    video, = prepare_native_video_sources(project, plan, 1.0, None, Event()).values()
                self.assertIsNone(video.layer_at(.24).media_time)
                self.assertEqual(video.layer_at(.25).media_time, 0)
                self.assertEqual(video.layer_at(.65).media_time, 5_000_000)
                self.assertEqual(video.layer_at(1.05).media_time, 0 if loop else None)
                self.assertIsNone(video.layer_at(2.25).media_time)
        validate_project_for_gpu_capture(project, scale=1, source_policy="dynamic-audio", gpu_video=True)
        project.sources[1].mask_shape = "ellipse"
        with self.assertRaisesRegex(TimelineValidationError, "native_video_frame_provider_unavailable"):
            validate_project_for_gpu_capture(project, scale=1, source_policy="dynamic-audio", gpu_video=True)

    def test_video_packets_keep_commands_and_send_no_video_pixels(self):
        base = NativeCanvasBgraLayer("base", 64, 64, bytes([0, 0, 0, 255]) * 64 * 64)
        video = NativeCanvasVideoLayer("layer:0", 1, 1, b"", clip=(2, 4, 62, 60),
                                       asset=1, media_time=0, crop=True, border_radius=3, saturation=.6)
        with self.assertRaises(ValueError):
            NativeCanvasFrame(64, 64, (base, video), 4)
        reuse = FrameReuseState()
        for i, timestamp in enumerate((0, 100, None)):
            frame = NativeCanvasFrame(64, 64, (base, replace(video, media_time=timestamp)), 5)
            chunks = list(frame_chunks(frame, i, reuse_state=reuse, v3_wire=True))
            self.assertEqual(HEADER.unpack(chunks[0])[-1], len(base.pixels) if i == 0 else 0)
            command = json.loads(chunks[1])["layers"][1]
            self.assertEqual((command["kind"], command["bytes"], command["wire_bytes"], command["media_time"]),
                             ("video_source", 0, 0, timestamp))

    @unittest.skipUnless(WORKER and shutil.which("ffmpeg"), "Set PLAYLIST_NATIVE_VIDEO_WORKER for actual DXVA/NVENC")
    def test_real_gpu_export_preserves_repeat_z_order_mask_and_audio(self):
        app = QApplication.instance() or QApplication([])
        if not QFontDatabase.families():
            self.skipTest("Run the GPU export test with the Windows Qt platform")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video, audio, output = (root / name for name in ("video.mp4", "audio.wav", "gpu.mp4"))

            def ffmpeg(*args):
                return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *args], check=True,
                                      capture_output=True, timeout=45).stdout

            ffmpeg("-f", "lavfi", "-i", "color=red:s=320x180:r=20:d=0.5",
                   "-f", "lavfi", "-i", "color=green:s=320x180:r=20:d=0.5",
                   "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                   "-c:v", "libx264", "-g", "10", "-bf", "2", "-colorspace", "bt709", str(video))
            ffmpeg("-f", "lavfi", "-i", "sine=f=550:r=48000:d=2.5", str(audio))
            project = project_for_video(video, audio)
            project.sources[1].border_radius = 15
            project.sources[1].video_saturation = .85
            result = export_project_document(
                project, output, native_exe=Path(WORKER), fps=20, scale=1,
                optimizations=replace(NativeGpuOptimizations.for_profile("automatic"), gpu_video=True),
            )
            report = result.native_report["d3d11_video_sources"]
            self.assertEqual(report["composited_frames"], 40)
            self.assertEqual((report["cpu_pixel_bytes"], report["cpu_readbacks"]), (0, 0))
            self.assertGreater(report["seeks"], 0)
            decoded = np.frombuffer(ffmpeg("-i", str(output), "-map", "0:v:0", "-pix_fmt", "rgb24",
                                           "-f", "rawvideo", "pipe:1"), dtype=np.uint8).reshape(-1, 180, 320, 3)
            self.assertEqual(len(decoded), 50)
            self.assertGreater(int(decoded[3, 80, 150, 2]), int(decoded[3, 80, 150, 0]))
            self.assertGreater(decoded[7, 80, 150, 0], 140)
            self.assertGreater(decoded[17, 80, 150, 1], 75)
            self.assertGreater(decoded[22, 80, 150, 0], 140)
            self.assertGreater(decoded[7, 49, 85, 1], 110)
            self.assertGreater(decoded[7, 21, 41, 2], decoded[7, 21, 41, 0])
            self.assertGreater(decoded[46, 80, 150, 2], decoded[46, 80, 150, 0])
            prepared = FFmpegRenderer().prepare_playlist_audio(
                project.playlist, root, RenderSettings(fps=20), transition_mode="none", cancel_event=Event(),
            )
            pcm_args = ("-map", "0:a:0", "-t", "2.5", "-f", "s16le", "pipe:1")
            self.assertEqual(ffmpeg("-i", str(output), *pcm_args), ffmpeg("-i", str(prepared), *pcm_args))
