from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import unittest
from unittest.mock import patch
import wave

import numpy as np
from PySide6.QtGui import QColor, QImage

from app.models.playlist import PlaylistTrack
from app.renderer.canvas_pipe import piped_export_supported
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer, RenderCancelledError, RenderError, RenderSettings, VisualizerOverlay,
)
from app.renderer.python_visualizer import PythonVisualizerError, PythonVisualizerRenderer
from app.timeline.compiler import compile_playlist
from app.utils.subprocess_utils import hidden_process_kwargs


def _threads() -> set[str]:
    return {thread.name for thread in threading.enumerate()
            if thread.name.startswith(("visualizer-stream-", "canvas-pipe-visualizer"))}


@unittest.skipUnless(piped_export_supported(), "named pipes unavailable")
class VisualizerStreamingTests(unittest.TestCase):
    def setUp(self):
        self.renderer = PythonVisualizerRenderer(Path("unused-ffmpeg"))
        self.levels = np.linspace(0, 1, 120, dtype=np.float32).reshape(15, 8)
        self.overlay = VisualizerOverlay(0, 0, 16, 16, "bars", "#FF0000", bar_count=8)

    def test_multiple_inputs_preserve_every_rgba_frame_without_movies(self):
        overlays = [
            replace(self.overlay, opacity=0.5, timeline_start=0.2,
                    personal_colors=("#00FF00", "#0000FF")),
            replace(self.overlay, kind="waveform"),
            replace(self.overlay, kind="level_meter"),
            replace(self.overlay, kind="particles"),
        ]
        windows = ((0, 0.7), (0.7, 1.5))
        expected = [b"".join(bytes(frame.constBits()) for frame in self.renderer._layer_frames(
            overlay, self.levels, 10, threading.Event(), None, None, windows,
        )) for overlay in overlays]
        received = [bytearray() for _ in overlays]
        readers = []

        def read(index, path):
            with open(path, "rb") as stream:
                while block := stream.read(65536):
                    received[index].extend(block)

        with TemporaryDirectory() as raw, patch.object(
            self.renderer, "_audio_levels", return_value=(self.levels, self.levels, None),
        ), patch.object(self.renderer, "_encode_layer", side_effect=AssertionError("No MOV encoder needed")):
            with self.renderer.stream_layers(
                Path("unused-audio"), overlays, 10, Path(raw), threading.Event(), track_windows=windows,
            ) as inputs:
                for index, arguments in enumerate(inputs):
                    reader = threading.Thread(target=read, args=(index, arguments[-1]))
                    reader.start()
                    readers.append(reader)
            for reader in readers:
                reader.join(timeout=5)
            self.assertEqual(list(Path(raw).iterdir()), [])
        self.assertTrue(all(not reader.is_alive() for reader in readers))
        self.assertEqual([bytes(value) for value in received], expected)
        self.assertEqual(_threads(), set())

    def test_abandoned_encoder_stops_blocked_writers(self):
        started = monotonic()
        with TemporaryDirectory() as raw, patch.object(
            self.renderer, "_audio_levels", return_value=(self.levels, None, None),
        ):
            with self.assertRaisesRegex(RenderError, "encoder failed"):
                with self.renderer.stream_layers(
                    Path("unused"), [self.overlay] * 3, 10, Path(raw), threading.Event(),
                ):
                    raise RenderError("encoder failed")
            self.assertEqual(list(Path(raw).iterdir()), [])
        self.assertLess(monotonic() - started, 5)
        self.assertEqual(_threads(), set())

    def test_cancel_before_encoder_connects_stops_all_producers(self):
        cancel = threading.Event()
        started = monotonic()
        with TemporaryDirectory() as raw, patch.object(
            self.renderer, "_audio_levels", return_value=(self.levels, None, None),
        ):
            with self.assertRaises(PythonVisualizerError):
                with self.renderer.stream_layers(Path("unused"), [self.overlay] * 3, 10, Path(raw), cancel):
                    cancel.set()
        self.assertLess(monotonic() - started, 5)
        self.assertEqual(_threads(), set())


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") and piped_export_supported(),
                     "Set PLAYLIST_CANVAS_TEST_FFMPEG for real encoding")
class VisualizerStreamingIntegrationTests(unittest.TestCase):
    def test_cancel_while_encoder_reads_keeps_existing_output(self):
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b"\0" * 48000 * 4)
            tracks = [PlaylistTrack(str(audio_path), "Track", duration_seconds=1)]
            output = root / "existing.mp4"
            output.write_bytes(b"original")
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor("blue"))
            cancel = threading.Event()

            def cancelled_frames(*args, **kwargs):
                for index in range(24):
                    # More than the bounded queues hold: FFmpeg must have
                    # started consuming frames before cancellation is requested.
                    if index == 10:
                        cancel.set()
                    yield image

            started = monotonic()
            with patch.object(PythonVisualizerRenderer, "_layer_frames", cancelled_frames):
                with self.assertRaises(RenderCancelledError):
                    FFmpegRenderer(executable).render(
                        image, tracks, output,
                        RenderSettings(output_width=16, output_height=16, fps=24),
                        visualizers=[VisualizerOverlay(0, 0, 16, 16, "bars", "white")],
                        compiled_plan=compile_playlist(tracks), prepared_audio_path=audio_path,
                        cancel_event=cancel,
                    )
            self.assertLess(monotonic() - started, 5)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(root.glob("*.rendering.mp4")), [])
        self.assertEqual(_threads(), set())

    def test_final_video_and_audio_match_movie_path(self):
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b"\x00\x10\x00\x20" * 48000)
            tracks = [PlaylistTrack(str(audio_path), "Track", duration_seconds=1)]
            canvas = QImage(64, 64, QImage.Format.Format_RGB32)
            canvas.fill(QColor("#203040"))
            overlay = VisualizerOverlay(8, 8, 48, 48, "bars", "#22CCFF", opacity=0.6,
                                        animation_in="fade", animation_in_duration=0.3)
            overlays = [overlay, replace(overlay, kind="particles", z_index=2),
                        replace(overlay, kind="waveform", z_index=3)]
            decoded = []
            for movie in (True, False):
                output = root / f"{movie}.mp4"
                renderer = FFmpegRenderer(executable)
                with (patch("app.renderer.python_visualizer.piped_export_supported", return_value=False)
                      if movie else nullcontext()):
                    result = renderer.render(
                        canvas, tracks, output,
                        RenderSettings(output_width=64, output_height=64, fps=24, crf=0, preset="ultrafast"),
                        visualizers=overlays,
                    )
                self.assertTrue(result.validation.passed, result.validation)
                hashes = []
                for stream in ("0:v", "0:a"):
                    completed = subprocess.run(
                        [str(executable), "-v", "error", "-i", str(output), "-map", stream,
                         "-f", "framemd5", "-"], capture_output=True, check=True, **hidden_process_kwargs(),
                    )
                    hashes.append(completed.stdout)
                decoded.append(hashes)
            self.assertEqual(decoded[0], decoded[1])
        self.assertEqual(_threads(), set())

    def test_drawing_failure_keeps_existing_output_and_releases_streams(self):
        executable = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(b"\0" * 48000 * 4)
            tracks = [PlaylistTrack(str(audio_path), "Track", duration_seconds=1)]
            output = root / "existing.mp4"
            output.write_bytes(b"original")
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor("blue"))

            def broken_frames(*args, **kwargs):
                yield image
                raise PythonVisualizerError("drawing failed")

            started = monotonic()
            with patch.object(PythonVisualizerRenderer, "_layer_frames", broken_frames):
                with self.assertRaises(RenderError):
                    FFmpegRenderer(executable).render(
                        image, tracks, output,
                        RenderSettings(output_width=16, output_height=16, fps=24),
                        visualizers=[VisualizerOverlay(0, 0, 16, 16, "bars", "white")],
                        compiled_plan=compile_playlist(tracks), prepared_audio_path=audio_path,
                    )
            self.assertLess(monotonic() - started, 10)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(root.glob("*.rendering.mp4")), [])
        self.assertEqual(_threads(), set())


if __name__ == "__main__":
    unittest.main()
