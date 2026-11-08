"""Cropped FFmpeg Z bands remain pixel-identical with opacity keyframes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.animation.keyframes import Keyframe, KeyframeAnimation
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.ffmpeg_renderer import RenderFrame
from app.utils.subprocess_utils import hidden_process_kwargs


class OpacityKeyframeCropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    @staticmethod
    def _animation(property_name: str = "opacity") -> KeyframeAnimation:
        return KeyframeAnimation(global_curves={
            property_name: [Keyframe(0.0, 0.0), Keyframe(1.0, 1.0)],
        })

    def _scene(self, keyframes: KeyframeAnimation) -> tuple[CanvasScene, Source]:
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(
            SourceType.SHAPE, "Foreground",
            x=115, y=120, width=80, height=32,
            z_index=1.0, fill_color="#EE6699",
            keyframes=keyframes,
        )
        scene.addItem(SourceItem(source))
        return scene, source

    def test_opacity_only_keyframes_allow_small_fixed_export_band(self) -> None:
        scene, source = self._scene(self._animation())
        self.assertTrue(source.keyframes.animated)
        self.assertFalse(CanvasSnapshot._keyframes_change_capture_bounds(source))
        rect = CanvasSnapshot.band_capture_envelope(scene, 1.0, None)
        self.assertIsNotNone(rect)
        self.assertLess(rect.width() * rect.height(), 320 * 180 * 0.2)
        preview_rect = CanvasSnapshot._dynamic_band_capture_rect(scene, 1.0, None)
        self.assertIsNotNone(preview_rect)

    def test_feature_flag_restores_previous_full_frame_behavior(self) -> None:
        scene, source = self._scene(self._animation())
        with patch.dict(os.environ, {
            "PLAYLIST_CANVAS_DISABLE_OPACITY_KEYFRAME_ROI": "1",
        }):
            self.assertTrue(CanvasSnapshot._keyframes_change_capture_bounds(source))
            self.assertIsNone(CanvasSnapshot.band_capture_envelope(scene, 1.0, None))
        self.assertIsNotNone(CanvasSnapshot.band_capture_envelope(scene, 1.0, None))

    def test_geometry_or_shadow_keyframes_keep_full_frame_fallback(self) -> None:
        for property_name in ("x", "y", "scale", "rotation", "width", "height",
                              "blur", "shadow.offset_x", "shadow.blur_radius"):
            with self.subTest(property_name=property_name):
                scene, source = self._scene(self._animation(property_name))
                self.assertTrue(CanvasSnapshot._keyframes_change_capture_bounds(source))
                self.assertIsNone(CanvasSnapshot.band_capture_envelope(scene, 1.0, None))

    def test_inactive_and_other_scope_geometry_curves_are_conservative(self) -> None:
        animation = KeyframeAnimation(
            mode="per_track",
            per_track_curves={"track-a": {
                "opacity": [Keyframe(0, 0.2), Keyframe(1, 0.9)],
            }},
        )
        scene, source = self._scene(animation)
        self.assertFalse(CanvasSnapshot._keyframes_change_capture_bounds(source))
        self.assertIsNotNone(CanvasSnapshot.band_capture_envelope(scene, 1.0, None))
        # A dormant curve in another mode may be reactivated by the editor.
        source.keyframes.global_curves["x"] = [Keyframe(0.0, 115.0)]
        self.assertTrue(CanvasSnapshot._keyframes_change_capture_bounds(source))
        self.assertIsNone(CanvasSnapshot.band_capture_envelope(scene, 1.0, None))

    def test_high_resolution_crop_aligns_to_even_output_pixels(self) -> None:
        scene, source = self._scene(self._animation())
        track = PlaylistTrack("fade.wav", "Fade", duration_seconds=1.0)
        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, 0.0), (1.0, None)],
            lambda image, duration, key: RenderFrame(image, duration),
            lambda: None, lambda *_: None,
            output_scale=1.5, output_fps=60,
        )
        x, y = capturer.stream_origin("layer:0")
        self.assertEqual(x % 2, 0)
        self.assertEqual(y % 2, 0)
        rect = capturer._stream_capture_rects["layer:0"]
        self.assertEqual(round(rect.width() * 1.5) % 2, 0)
        self.assertEqual(round(rect.height() * 1.5) % 2, 0)

    def test_cropped_opacity_fade_matches_full_canvas_at_multiple_timestamps(self) -> None:
        scene, source = self._scene(self._animation())
        rect = CanvasSnapshot.band_capture_envelope(scene, 1.0, None)
        self.assertIsNotNone(rect)
        track = PlaylistTrack("fade.wav", "Fade", duration_seconds=1.0)
        for elapsed in (0.0, 0.25, 0.5, 0.75, 1.0):
            with self.subTest(elapsed=elapsed):
                kwargs = dict(
                    elapsed_seconds=elapsed, timeline_seconds=elapsed,
                    z_min=1.0, z_max=None, transparent=True, output_scale=1.0,
                )
                full = CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0, **kwargs)
                cropped = CanvasSnapshot.capture_track(
                    scene, track, 1, 1, 0.0, capture_rect=rect, **kwargs,
                )
                self.assertLess(cropped.width() * cropped.height(),
                                full.width() * full.height() * 0.2)
                positioned = QImage(full.size(), full.format())
                positioned.fill(0)
                painter = QPainter(positioned)
                painter.drawImage(round(rect.left()), round(rect.top()), cropped)
                painter.end()
                self.assertEqual(
                    bytes(full.constBits()[:full.sizeInBytes()]),
                    bytes(positioned.constBits()[:positioned.sizeInBytes()]),
                )

    def test_ffmpeg_even_grid_crop_preserves_output_pixels(self) -> None:
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not executable:
            self.skipTest("Set PLAYLIST_CANVAS_TEST_FFMPEG for real FFmpeg overlay parity.")

        # Simulate an opacity animation over four 30FPS frames, with the same
        # pixels supplied either as full-frame RGBA or small fixed-ROI RGBA.
        width, height = 320, 180
        crop_x, crop_y, crop_width, crop_height = 112, 116, 88, 40
        full = bytearray()
        cropped = bytearray()
        for index in range(4):
            alpha = min(255, 64 * (index + 1))
            full_frame = bytearray(width * height * 4)
            crop_frame = bytearray(crop_width * crop_height * 4)
            for y in range(120, 152):
                for x in range(115, 195):
                    offset = (y * width + x) * 4
                    crop_offset = ((y - crop_y) * crop_width + x - crop_x) * 4
                    rgba = bytes((255, 0, 0, alpha))
                    full_frame[offset:offset + 4] = rgba
                    crop_frame[crop_offset:crop_offset + 4] = rgba
            full.extend(full_frame)
            cropped.extend(crop_frame)
        self.assertLess(len(cropped), len(full) // 10)

        with TemporaryDirectory(prefix="opacity-overlay-") as raw:
            directory = Path(raw)
            full_path = directory / "full.rgba"
            crop_path = directory / "crop.rgba"
            full_path.write_bytes(full)
            crop_path.write_bytes(cropped)

            def render(path: Path, size: str, x: int, y: int) -> bytes:
                result = subprocess.run([
                    executable, "-hide_banner", "-loglevel", "error",
                    "-filter_complex_threads", "1",
                    "-f", "lavfi", "-i",
                    f"color=blue:size={width}x{height}:rate=30",
                    "-f", "rawvideo", "-pix_fmt", "rgba",
                    "-video_size", size, "-framerate", "30",
                    "-i", str(path),
                    "-filter_complex",
                    f"[0:v][1:v]overlay={x}:{y}:eof_action=pass:eval=init[vout]",
                    "-map", "[vout]", "-frames:v", "4",
                    "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1",
                ], capture_output=True, check=True, timeout=20,
                    **hidden_process_kwargs())
                return result.stdout

            old = render(full_path, "320x180", 0, 0)
            new = render(crop_path, "88x40", crop_x, crop_y)
            self.assertEqual(len(old), 4 * width * height * 3 // 2)
            self.assertEqual(new, old)

    def test_capturer_stages_the_cropped_image_and_correct_origin(self) -> None:
        scene, source = self._scene(self._animation())
        track = PlaylistTrack("fade.wav", "Fade", duration_seconds=1.0)
        staged: list[QImage] = []

        def stage(image: QImage, duration: float, key: str) -> RenderFrame:
            self.assertEqual(key, "layer:0")
            staged.append(image.copy())
            return RenderFrame(image, duration)

        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, 0.0), (1.0, None)],
            stage, lambda: None, lambda *_: None, output_fps=60,
        )
        self.assertIn("layer:0", capturer._stream_capture_rects)
        rect = capturer._stream_capture_rects["layer:0"]
        self.assertEqual(
            capturer.stream_origin("layer:0"),
            (round(rect.left()), round(rect.top())),
        )
        self.assertEqual(capturer.stream_origin("layer:0")[0] % 2, 0)
        self.assertEqual(capturer.stream_origin("layer:0")[1] % 2, 0)
        from app.renderer.export_timeline import ExportFrameSample
        sample = ExportFrameSample(
            track, 1, 0.0, 1 / 60, 0.5, 0.5,
        )
        capturer.capture_stream(sample, "layer:0")
        self.assertEqual(len(staged), 1)
        self.assertEqual(
            (staged[0].width(), staged[0].height()),
            (round(rect.width()), round(rect.height())),
        )


if __name__ == "__main__":
    unittest.main()
