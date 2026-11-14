"""Visual combo popup contract: a grid UI without changing stored field values."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QListWidget

from app.widgets.visual_preset_combo import VisualPresetComboBox, _preview_icon


class VisualPresetComboTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_combo(self, kind="entrance"):
        combo = VisualPresetComboBox(kind)
        for label, value in (
            ("None", "none"), ("Slide left", "slide_left"), ("Custom", "custom")
        ):
            combo.addItem(label, value)
        combo.setCurrentIndex(1)
        return combo

    def test_is_normal_combobox_and_preserves_item_data(self):
        combo = self.make_combo()
        self.addCleanup(combo.deleteLater)
        self.assertIsInstance(combo, QComboBox)
        self.assertEqual(combo.currentData(), "slide_left")
        combo.setItemText(1, "왼쪽 슬라이드")
        self.assertEqual(combo.currentText(), "왼쪽 슬라이드")
        self.assertEqual(combo.itemData(1), "slide_left")

    def test_popup_has_grid_previews_and_commits_selection_once(self):
        combo = self.make_combo()
        self.addCleanup(combo.deleteLater)
        changed = []
        combo.currentIndexChanged.connect(changed.append)
        combo.showPopup()
        view = combo._list
        self.assertIsInstance(view, QListWidget)
        self.assertEqual(view.viewMode(), QListWidget.ViewMode.IconMode)
        self.assertTrue(view.isWrapping())
        self.assertEqual(view.count(), combo.count())
        self.assertEqual(view.currentRow(), 1)
        self.assertEqual(view.item(2).text(), "Custom")
        self.assertFalse(view.item(2).icon().isNull())
        combo._choose(view.item(2))
        self.assertEqual(combo.currentData(), "custom")
        self.assertEqual(changed, [2])
        self.assertFalse(combo._timer.isActive())
        self.assertIsNone(combo._list)

    def test_popup_does_not_change_selection_when_dismissed(self):
        combo = self.make_combo("mask")
        self.addCleanup(combo.deleteLater)
        combo.showPopup()
        self.assertIsNotNone(combo._popup)
        combo._popup.close()
        self.assertEqual(combo.currentData(), "slide_left")
        self.assertFalse(combo._timer.isActive())
        self.assertIsNone(combo._list)

    def test_previews_are_illustrations_and_have_valid_pixmaps(self):
        for kind, value in (
            ("entrance", "fade"), ("exit", "slide_left"), ("loop", "spin"),
            ("reactive", "bass_scale"), ("mask", "star"), ("shape", "heart"),
            ("visualizer", "radial"), ("particle", "neon"),
            ("intro", "ring"), ("intro_exit", "drift"),
            ("album_frame", "polaroid"), ("track_list", "cards"),
        ):
            with self.subTest(kind=kind):
                icon = _preview_icon(kind, value, .6)
                image = icon.pixmap(112, 72).toImage()
                self.assertFalse(image.isNull())
                self.assertTrue(any(
                    image.pixelColor(x, y).alpha() > 0
                    for x, y in ((15, 15), (56, 35), (80, 50))
                ))


if __name__ == "__main__":
    unittest.main()
