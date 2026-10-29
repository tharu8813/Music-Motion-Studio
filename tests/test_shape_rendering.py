from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter, QRegion, QTransform
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.canvas.live_canvas import CanvasScene
from app.inspector.source_inspector import SourceInspector
from app.models.source import SHAPE_KINDS, Shadow, Source, SourceType
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class ShapeRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_moving_shapes_with_effects_clears_old_pixels_using_item_bounds(self) -> None:
        # MinimalViewportUpdate clears the item's old/new bounding regions.
        # Compare that partial repaint with a fresh frame to detect trails.
        background = QColor("#202733")
        for kind in (*SHAPE_KINDS, "line"):
            for selected in (False, True):
                for rotation, scale, flipped in ((0, 1, False), (25, 1.3, False), (25, 1.3, True)):
                    with self.subTest(kind=kind, selected=selected, rotation=rotation):
                        source = Source(
                            SourceType.SHAPE, "Moving shape", shape_kind=kind,
                            flip_horizontal=flipped, flip_vertical=flipped,
                            width=90, height=60, fill_color="#F08040", opacity=0.7,
                            outline_width=80, outline_color="#10D090",
                            shadow=Shadow(enabled=True, offset_x=70, offset_y=-45,
                                          blur_radius=30, color="#4080FF", opacity=0.8),
                        )
                        item = SourceItem(source)
                        item.setSelected(selected)
                        old = QTransform().translate(130, 150).rotate(rotation).scale(scale, scale)
                        new = QTransform().translate(420, 270).rotate(rotation).scale(scale, scale)
                        dirty = QRegion(old.mapRect(item.boundingRect()).toAlignedRect()).united(
                            QRegion(new.mapRect(item.boundingRect()).toAlignedRect())
                        )
                        image = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
                        image.fill(background)
                        painter = QPainter(image)
                        try:
                            painter.setWorldTransform(old)
                            item.paint(painter, None)
                        finally:
                            painter.end()
                        painter = QPainter(image)
                        try:
                            painter.setClipRegion(dirty)
                            painter.fillRect(image.rect(), background)
                            painter.setWorldTransform(new)
                            item.paint(painter, None)
                        finally:
                            painter.end()
                        expected = QImage(image.size(), image.format())
                        expected.fill(background)
                        painter = QPainter(expected)
                        try:
                            painter.setWorldTransform(new)
                            item.paint(painter, None)
                        finally:
                            painter.end()
                        self.assertEqual(image, expected, "Partial repaint left shape trails or clipped effects")
                        item.deleteLater()

    def test_shape_choices_include_new_shapes_and_preserve_hidden_legacy_line(self) -> None:
        store = SourceStore()
        source = Source.from_dict(Source(SourceType.SHAPE, "Legacy", shape_kind="line").to_dict())
        store.add(source)
        inspector = SourceInspector(store, Translator())
        try:
            inspector.set_source(source)
            combo = inspector.shape_kind_combo
            self.assertEqual([combo.itemData(i) for i in range(combo.count())], list(SHAPE_KINDS))
            self.assertEqual(combo.currentIndex(), -1)
            self.assertEqual(source.shape_kind, "line")
            combo.setCurrentIndex(combo.findData("circle"))
            self.assertEqual(source.shape_kind, "circle")
        finally:
            inspector.close()
            inspector.deleteLater()

    def test_shape_effects_do_not_shift_alignment_targets(self) -> None:
        scene = CanvasScene()
        source = Source(SourceType.SHAPE, "Alignment", x=250, y=180,
                        width=90, height=60, outline_width=80,
                        shadow=Shadow(enabled=True, offset_x=70, offset_y=-45))
        item = SourceItem(source)
        scene.addItem(item)
        for selected in (False, True):
            item.setSelected(selected)
            x, y = scene._build_alignment_candidates(set())
            self.assertEqual(x, sorted({0, 640, 1280, 250, 295, 340}))
            self.assertEqual(y, sorted({0, 360, 720, 180, 210, 240}))


if __name__ == "__main__":
    unittest.main()
