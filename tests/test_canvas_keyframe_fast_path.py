"""Fast static Canvas keyframe path must preserve pixels and restore editor state."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import TestCase
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app.animation.keyframes import Keyframe
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview import canvas_snapshot
from app.preview.canvas_snapshot import CanvasSnapshot


class CanvasStaticKeyframeFastPathTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def fixture():
        scene = CanvasScene()
        source = Source(
            SourceType.TEXT, "Static text",
            text="Unchanged 한글 + 123", x=22, y=15,
            width=240, height=70, font_size=28,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=10)
        return scene, source, item, track

    def test_static_sources_do_not_enter_noop_keyframe_context_and_pixels_match(self):
        scene, source, item, track = self.fixture()
        before = source.to_dict()
        baseline = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, elapsed_seconds=2.5,
            output_scale=0.25, timeline_seconds=2.5,
        )
        with patch(
            "app.preview.canvas_snapshot.keyframe_source",
            side_effect=AssertionError("empty scope should be skipped"),
        ):
            actual = CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=2.5,
                output_scale=0.25, timeline_seconds=2.5,
            )
        self.assertEqual((actual.width(), actual.height()),
                         (baseline.width(), baseline.height()))
        self.assertEqual(bytes(actual.constBits()), bytes(baseline.constBits()))
        self.assertEqual(source.to_dict(), before)
        self.assertIs(item.source, source)
        self.assertFalse(item._suppress_position_sync)

    def test_animated_sources_still_enter_keyframe_scope_and_restore(self):
        scene, source, item, track = self.fixture()
        source.keyframes.global_curves["x"] = [
            Keyframe(0.0, 22.0, "linear"),
            Keyframe(10.0, 122.0, "linear"),
        ]
        before = source.to_dict()
        old_pos = item.pos()
        original = canvas_snapshot.keyframe_source
        with patch(
            "app.preview.canvas_snapshot.keyframe_source",
            wraps=original,
        ) as scope:
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.0,
                output_scale=0.25, timeline_seconds=5.0,
            )
        self.assertEqual(scope.call_count, 1)
        self.assertIn("x", source.keyframes.global_curves)
        self.assertEqual(source.to_dict(), before)
        self.assertIs(item.source, source)
        self.assertEqual(item.pos(), old_pos)

    def test_editor_blur_values_still_enter_restoration_context(self):
        scene, source, item, track = self.fixture()
        # A keyed editor preview may temporarily retain an old blur value
        # even when the evaluated frame currently has no numeric curves.
        item._editor_keyframe_pose = True
        item._editor_keyframe_base = source
        item._editor_keyframe_values = {"blur": 5.0}
        item._editor_keyframe_opacity = 1.0
        original = canvas_snapshot.keyframe_source
        with patch(
            "app.preview.canvas_snapshot.keyframe_source",
            wraps=original,
        ) as scope:
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=2.5,
                output_scale=0.25, timeline_seconds=2.5,
            )
        self.assertEqual(scope.call_count, 1)
        self.assertIs(item.source, source)


if __name__ == "__main__":
    import unittest
    unittest.main()
