"""Worker-only resolved-lyrics cache: unchanged Canvas pixels, no editor staleness."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame


def _bytes(image: QImage) -> bytes:
    return bytes(image.constBits()[:image.sizeInBytes()])


class CanvasWorkerLyricCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _scene(self) -> tuple[CanvasScene, SourceItem, Source]:
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(
            SourceType.LYRICS, "Lyrics", x=30, y=30,
            width=260, height=105, font_size=22,
            subtitle_advanced_categories=["layout", "styles"],
            subtitle_advanced_settings={
                "subtitle_context_lines": 2,
                "subtitle_next_lines": 2,
                "subtitle_line_spacing": 18.0,
                "subtitle_role_styles": {
                    "previous": {"scale": 0.82, "opacity": 0.55},
                    "current": {"scale": 1.05, "opacity": 1.0},
                },
            },
        )
        item = SourceItem(source)
        scene.addItem(item)
        return scene, item, source

    def _capture(self, frozen: bool) -> tuple[list[bytes], int]:
        scene, item, source = self._scene()
        track = PlaylistTrack("sample.wav", "Example", duration_seconds=1.0)
        captured: list[bytes] = []
        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, None)],
            lambda image, duration, key: RenderFrame(image.copy(), duration),
            lambda: None, lambda *_: None,
            output_fps=60, frozen_worker_scene=frozen,
        )
        original_method = Source.resolved_lyrics
        count = 0

        def measured(obj: Source) -> Source:
            nonlocal count
            if obj is source and obj.subtitle_advanced_categories:
                count += 1
            return original_method(obj)

        with patch.object(Source, "resolved_lyrics", measured):
            for index in range(3):
                sample = ExportFrameSample(
                    track, 1, 0.0, 1.0 / 60.0,
                    index / 60.0, index / 60.0,
                )
                frame = capturer.capture_stream(sample, "base")
                self.assertIs(item.source, source)
                self.assertIsInstance(frame.image, QImage)
                captured.append(_bytes(frame.image))
        self.assertEqual(source.subtitle_advanced_categories, ["layout", "styles"])
        self.assertEqual(source.subtitle_context_lines, 1)
        scene.clear()
        return captured, count

    def test_worker_cache_matches_original_pixel_output_and_resolves_once(self) -> None:
        original, old_count = self._capture(frozen=False)
        cached, new_count = self._capture(frozen=True)
        self.assertEqual(original, cached)
        self.assertEqual(old_count, 3)
        self.assertEqual(new_count, 1)

    def test_live_editing_uses_fresh_settings_not_frozen_cache(self) -> None:
        scene, item, source = self._scene()
        track = PlaylistTrack("sample.wav", "Example", duration_seconds=1.0)
        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, None)],
            lambda image, duration, key: RenderFrame(image, duration),
            lambda: None, lambda *_: None,
        )
        self.assertIsNone(capturer._frozen_lyric_settings_cache)
        first = source.resolved_lyrics()
        source.subtitle_advanced_settings["subtitle_context_lines"] = 3
        second = source.resolved_lyrics()
        self.assertEqual(first.subtitle_context_lines, 2)
        self.assertEqual(second.subtitle_context_lines, 3)
        self.assertIs(item.source, source)
        scene.clear()

    def test_worker_cache_restores_original_item_after_failed_capture(self) -> None:
        scene, item, source = self._scene()
        track = PlaylistTrack("sample.wav", "Example", duration_seconds=1.0)
        cache: dict = {}
        kwargs = dict(
            elapsed_seconds=0, timeline_seconds=0,
            _frozen_lyric_settings_cache=cache,
        )
        with patch.object(CanvasSnapshot, "capture", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                CanvasSnapshot.capture_track(scene, track, 1, 1, 0, **kwargs)
        self.assertIs(item.source, source)
        self.assertEqual(len(cache), 1)
        # The cached copy has no advanced-category indirection.
        self.assertEqual(next(iter(cache.values()))[1].subtitle_advanced_categories, [])
        scene.clear()


if __name__ == "__main__":
    unittest.main()
