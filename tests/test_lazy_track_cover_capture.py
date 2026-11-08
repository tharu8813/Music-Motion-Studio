"""Album-art Canvas frames must remain identical with lazy cover materialization."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview import canvas_snapshot as snapshot


def pixels(image: QImage) -> bytes:
    return memoryview(image.constBits()).cast("B").tobytes()


def scene_with_cover(*, personal_only: bool = False) -> CanvasScene:
    scene = CanvasScene()
    if personal_only:
        source = Source(
            SourceType.SHAPE, "Personal color", x=16, y=12,
            width=110, height=80, fill_color="#226688",
            personal_color_enabled=True,
        )
        scene.addItem(SourceItem(source))
        return scene
    scene.addItem(SourceItem(Source(
        SourceType.ALBUM_COVER, "Artwork", x=10, y=10,
        width=120, height=120, z_index=1,
    )))
    scene.addItem(SourceItem(Source(
        SourceType.BACKGROUND, "Artwork background", background_mode="album_art",
        width=160, height=100, z_index=0,
    )))
    return scene


class LazyTrackCoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        snapshot._FILTERED_TRACK_PIXMAP_CACHE.clear()
        self.track = PlaylistTrack("fake-song.wav", "Track", duration_seconds=8.0)
        self.art = QPixmap(256, 256)
        self.art.fill(QColor("#3C85BF"))

    def tearDown(self):
        snapshot._FILTERED_TRACK_PIXMAP_CACHE.clear()

    def _capture(self, scene: CanvasScene, *, lazy: bool, when: float = 1.0) -> bytes:
        with patch.dict(os.environ, {
            "PLAYLIST_CANVAS_DISABLE_LAZY_TRACK_COVER": "0" if lazy else "1",
        }):
            frame = snapshot.CanvasSnapshot.capture_track(
                scene, self.track, 1, 1, 0.0,
                elapsed_seconds=when, timeline_seconds=when,
            )
        return pixels(frame)

    def test_one_capture_fetches_shared_artwork_once_for_two_sources(self):
        scene = scene_with_cover()
        with patch.object(snapshot, "extract_track_cover", return_value=self.art) as extract:
            first = self._capture(scene, lazy=True)
            self.assertEqual(extract.call_count, 1)
            second = self._capture(scene, lazy=True)
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(first, second)

    def test_opt_out_uses_original_eager_cover_path_with_identical_pixels(self):
        scene = scene_with_cover()
        with patch.object(snapshot, "extract_track_cover", return_value=self.art) as extract:
            lazy = self._capture(scene, lazy=True)
            self.assertEqual(extract.call_count, 1)
            eager = self._capture(scene, lazy=False)
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(lazy, eager)
            self._capture(scene, lazy=False)
            self.assertEqual(extract.call_count, 3)

    def test_personal_color_without_album_art_does_not_load_cover(self):
        scene = scene_with_cover(personal_only=True)
        with patch.object(snapshot, "extract_track_cover", return_value=self.art) as extract, patch.object(
            snapshot, "extract_track_personal_color", return_value=QColor("#80A0C0")
        ):
            lazy = self._capture(scene, lazy=True)
            extract.assert_not_called()
            eager = self._capture(scene, lazy=False)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(lazy, eager)


if __name__ == "__main__":
    unittest.main()
