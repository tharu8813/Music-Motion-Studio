from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import unittest
import wave

from PySide6.QtGui import QColor, QImage

from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import (
    ExportMetadata,
    FFmpegRenderer,
    PreparedStaticOverlayLayer,
    PreparedVideoInput,
    RenderCancelledError,
    RenderSettings,
    VisualizerOverlay,
    VideoClipOverlay,
)
from app.renderer.static_video_stream import StaticVideoStreamEncoder


class ExportFfmetadataTests(unittest.TestCase):
    """FFmetadata container tags and per-track chapters (no FFmpeg required)."""

    def _write(self, tracks, metadata, stem="video"):
        renderer = FFmpegRenderer.__new__(FFmpegRenderer)
        with TemporaryDirectory() as directory:
            path = renderer._write_export_ffmetadata(
                Path(directory), tracks, metadata, Path(f"/out/{stem}.mp4"),
            )
            return path.read_text(encoding="utf-8")

    def test_container_tags_and_contiguous_chapters(self) -> None:
        tracks = [
            PlaylistTrack("a.wav", "First; Song = intro", duration_seconds=90.0),
            PlaylistTrack("b.wav", "Track #2", duration_seconds=120.5),
            PlaylistTrack("c.wav", "", duration_seconds=45.0),
        ]
        text = self._write(tracks, ExportMetadata(
            title="My Mix", artist="DJ Test", comment="hello",
        ))
        self.assertIn("title=My Mix", text)
        self.assertIn("artist=DJ Test", text)
        self.assertIn("comment=hello", text)
        # reserved characters are escaped
        self.assertIn(r"title=First\; Song \= intro", text)
        self.assertIn(r"title=Track \#2", text)
        # empty title falls back to the audio filename
        self.assertIn("title=c.wav", text)
        self.assertEqual(text.count("[CHAPTER]"), 3)
        # chapters are contiguous and cover the whole timeline
        self.assertIn("START=0\nEND=90000", text)
        self.assertIn("START=90000\nEND=210500", text)
        self.assertIn("START=210500\nEND=255500", text)

    def test_single_track_has_no_chapters_and_derives_a_title(self) -> None:
        text = self._write(
            [PlaylistTrack("solo.wav", "Solo", duration_seconds=30.0)],
            ExportMetadata(), stem="my-export",
        )
        self.assertNotIn("[CHAPTER]", text)
        self.assertIn("title=my-export", text)
        self.assertIn("comment=Music Motion Studio", text)


class FFmpegStreamingIntegrationTests(unittest.TestCase):
    """Optional real-process checks for the streamed Canvas export boundary."""

    def test_real_ffmpeg_cancellation_terminates_promptly(self) -> None:
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
        if not configured:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
        executable = Path(configured)
        if not executable.is_file():
            self.fail(f"Configured FFmpeg does not exist: {executable}")

        cancel_event = threading.Event()
        cancel_timer = threading.Timer(0.25, cancel_event.set)
        started_at = monotonic()
        cancel_timer.start()
        try:
            with self.assertRaises(RenderCancelledError):
                FFmpegRenderer(executable)._run(
                    [
                        "-re", "-f", "lavfi", "-i",
                        "testsrc=size=64x64:rate=30", "-t", "60",
                        "-f", "null", "-",
                    ],
                    cancel_event=cancel_event,
                )
        finally:
            cancel_timer.cancel()
            cancel_timer.join(timeout=1.0)

        self.assertLess(monotonic() - started_at, 8.0)

    def test_real_ffmpeg_preserves_alpha_and_composites_dynamic_layers(self) -> None:
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
        if not configured:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
        executable = Path(configured)
        if not executable.is_file():
            self.fail(f"Configured FFmpeg does not exist: {executable}")

        fps = 10
        duration = 0.4
        width = height = 32
        with TemporaryDirectory(prefix="playlist-stream-integration-") as raw_directory:
            directory = Path(raw_directory)
            audio_path = directory / "silence.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48_000)
                audio.writeframes(b"\0\0\0\0" * round(48_000 * duration))

            base_image = QImage(width, height, QImage.Format.Format_RGB32)
            base_image.fill(QColor(0, 0, 255))
            base_encoder = StaticVideoStreamEncoder(
                executable, directory / "base.mkv", fps,
            )
            base_encoder.submit(base_image, duration)
            base = base_encoder.finish()

            overlay_image = QImage(
                width, height, QImage.Format.Format_ARGB32_Premultiplied,
            )
            overlay_image.fill(QColor(255, 0, 0, 128))
            overlay_encoder = StaticVideoStreamEncoder(
                executable, directory / "overlay.mkv", fps, preserve_alpha=True,
            )
            overlay_encoder.submit(overlay_image, duration)
            overlay = overlay_encoder.finish()

            output_path = directory / "result.mp4"
            renderer = FFmpegRenderer(executable)
            track = PlaylistTrack(
                str(audio_path), "Silence", duration_seconds=duration,
            )
            settings = RenderSettings(
                fps=fps, video_codec="libx264", crf=0, preset="ultrafast",
                output_width=width, output_height=height,
            )
            renderer.preflight_export([track], output_path, settings)
            renderer.render(
                PreparedVideoInput(
                    base.path, duration, base.width, base.height, base.fps,
                ),
                [track],
                output_path,
                settings,
                visualizers=[VisualizerOverlay(
                    0, 0, width, height, "bars", "#00FF00",
                    opacity=0.0, bar_count=8, rotation=45.0, z_index=1.0,
                )],
                static_layers=[PreparedStaticOverlayLayer(
                    2.0,
                    PreparedVideoInput(
                        overlay.path, duration, overlay.width, overlay.height,
                        overlay.fps,
                    ),
                )],
            )

            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)
            decoded = subprocess.run(
                [
                    str(executable), "-hide_banner", "-loglevel", "error",
                    "-i", str(output_path), "-frames:v", "1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ],
                capture_output=True,
                check=True,
            ).stdout

        self.assertEqual(len(decoded), width * height * 3)
        for x, y in ((width // 2, height // 2), (2, 2), (width - 3, 2)):
            with self.subTest(pixel=(x, y)):
                offset = (y * width + x) * 3
                red, _green, blue = decoded[offset:offset + 3]
                # The red alpha overlay must still reveal the blue base. An
                # opaque black rotate fill would reduce the blue channel to zero.
                self.assertGreater(red, 100)
                self.assertGreater(blue, 100)

    def test_real_ffmpeg_composites_scheduled_video_then_returns_to_base(self) -> None:
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
        if not configured:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
        executable = Path(configured)
        fps, width, height, duration = 10, 32, 32, 1.1
        with TemporaryDirectory(prefix="playlist-video-overlay-") as raw_directory:
            directory = Path(raw_directory)
            audio_path = directory / "silence.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48_000)
                audio.writeframes(b"\0\0\0\0" * round(48_000 * duration))
            base_image = QImage(width, height, QImage.Format.Format_RGB32)
            base_image.fill(QColor(0, 0, 255))
            encoder = StaticVideoStreamEncoder(executable, directory / "base.mkv", fps)
            encoder.submit(base_image, duration)
            base = encoder.finish()
            clip_path = directory / "red.mkv"
            renderer = FFmpegRenderer(executable)
            renderer._run([
                "-f", "lavfi", "-i", f"color=red:size={width}x{height}:rate={fps}",
                "-t", "0.2", "-c:v", "ffv1", "-y", str(clip_path),
            ])
            output = directory / "scheduled.mp4"
            settings = RenderSettings(
                fps=fps, video_codec="libx264", crf=0, preset="ultrafast",
                output_width=width, output_height=height,
            )
            renderer.render(
                PreparedVideoInput(base.path, duration, base.width, base.height, base.fps),
                [PlaylistTrack(str(audio_path), "Silence", duration_seconds=duration)],
                output, settings,
                video_clips=[
                    VideoClipOverlay(
                        clip_path, 0.2, 0.2, 0.0,
                        0, 0, width, height, 1.0,
                    ),
                    VideoClipOverlay(
                        clip_path, 0.6, 0.2, 0.0,
                        0, 0, width, height, 1.0,
                    ),
                ],
            )
            samples = []
            for timestamp in (0.1, 0.25, 0.5, 0.65, 0.95):
                samples.append(subprocess.run([
                    str(executable), "-hide_banner", "-loglevel", "error",
                    "-ss", str(timestamp), "-i", str(output), "-frames:v", "1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
                ], capture_output=True, check=True).stdout)
        center = (height // 2 * width + width // 2) * 3
        before, first, between, second, after = [
            sample[center:center + 3] for sample in samples
        ]
        self.assertGreater(before[2], before[0])
        self.assertGreater(first[0], first[2])
        self.assertGreater(between[2], between[0])
        self.assertGreater(second[0], second[2])
        self.assertGreater(after[2], after[0])

    def test_real_ffmpeg_matches_video_effect_units_and_rounded_clip(self) -> None:
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip()
        if not configured:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG to run real FFmpeg checks.")
        executable = Path(configured)
        fps, width, height, duration = 10, 64, 64, 0.3
        with TemporaryDirectory(prefix="playlist-video-properties-") as raw_directory:
            directory = Path(raw_directory)
            audio_path = directory / "silence.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48_000)
                audio.writeframes(b"\0\0\0\0" * round(48_000 * duration))
            base_image = QImage(width, height, QImage.Format.Format_RGB32)
            base_image.fill(QColor(0, 0, 255))
            encoder = StaticVideoStreamEncoder(
                executable, directory / "base.mkv", fps,
            )
            encoder.submit(base_image, duration)
            base = encoder.finish()
            clip_path = directory / "gray.mkv"
            renderer = FFmpegRenderer(executable)
            renderer._run([
                "-f", "lavfi", "-i", f"color=0x404040:size=32x32:rate={fps}",
                "-t", str(duration), "-c:v", "ffv1", "-y", str(clip_path),
            ])
            output = directory / "properties.mp4"
            settings = RenderSettings(
                fps=fps, video_codec="libx264", crf=0, preset="ultrafast",
                output_width=width, output_height=height,
            )
            renderer.render(
                PreparedVideoInput(
                    base.path, duration, base.width, base.height, base.fps,
                ),
                [PlaylistTrack(
                    str(audio_path), "Silence", duration_seconds=duration,
                )],
                output,
                settings,
                video_clips=[VideoClipOverlay(
                    clip_path, 0.0, duration, 0.0,
                    16, 16, 32, 32, 1.0,
                    brightness=50.0, border_radius=8.0,
                )],
            )
            decoded = subprocess.run([
                str(executable), "-hide_banner", "-loglevel", "error",
                "-i", str(output), "-frames:v", "1",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ], capture_output=True, check=True).stdout

        def pixel(x: int, y: int) -> bytes:
            offset = (y * width + x) * 3
            return decoded[offset:offset + 3]

        corner = pixel(16, 16)
        center = pixel(32, 32)
        self.assertGreater(corner[2], corner[0])
        self.assertGreater(center[0], 150)
        self.assertGreater(center[1], 150)
        self.assertGreater(center[2], 150)


if __name__ == "__main__":
    unittest.main()
