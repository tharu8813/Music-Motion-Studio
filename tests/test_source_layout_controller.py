"""Exercise editing operations without constructing MainWindow or its controllers."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import LiveCanvas
from app.controllers.source_layout_controller import SourceLayoutController
from app.models.source import Source, SourceType
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class SourceLayoutControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = SourceStore()
        self.canvas = LiveCanvas(self.store, Translator())
        self.layout = SourceLayoutController(self.store, self.canvas.source_content_bounds)
        self.sources = [
            Source(SourceType.SHAPE, str(index), x=x, y=x, width=80, height=100,
                   scale=scale, rotation=angle)
            for index, (x, scale, angle) in enumerate(((100, 1.5, 30), (290, 0.7, 70), (700, 1.2, 15)))
        ]
        self.store.replace(self.sources)

    def tearDown(self):
        self.canvas.close()
        self.canvas.deleteLater()
        self.app.processEvents()

    def test_center_uses_transformed_group_bounds_and_preserves_spacing(self):
        for horizontal in (True, False):
            before = [(source.x, source.y) for source in self.sources]
            self.layout.center(self.sources, self.canvas.scene_model.artboard_rect, horizontal)
            bounds = self.canvas.source_content_bounds(self.sources[0].id)
            for source in self.sources[1:]:
                bounds = bounds.united(self.canvas.source_content_bounds(source.id))
            self.assertAlmostEqual(
                bounds.center().x() if horizontal else bounds.center().y(),
                self.canvas.scene_model.artboard_rect.center().x() if horizontal
                else self.canvas.scene_model.artboard_rect.center().y(),
            )
            deltas = [(source.x - x, source.y - y) for source, (x, y) in zip(self.sources, before)]
            for delta in deltas[1:]:
                self.assertAlmostEqual(delta[0], deltas[0][0])
                self.assertAlmostEqual(delta[1], deltas[0][1])

    def test_missing_and_locked_sources_are_not_modified(self):
        first, second, _third = self.sources
        self.store.update(first.id, locked=True)
        missing = Source(SourceType.SHAPE, "Missing")
        self.assertIsNone(self.canvas.source_content_bounds(missing.id))
        before = first.to_dict()
        self.layout.align([first, second, missing], "left")
        self.layout.distribute([first, second, missing], "horizontal")
        self.layout.center([first, missing], self.canvas.scene_model.artboard_rect, True)
        self.layout.nudge([first, second], 3, -4)
        self.assertEqual(first.to_dict(), before)
        self.assertEqual((second.x, second.y), (293, 286))


if __name__ == "__main__":
    unittest.main()
