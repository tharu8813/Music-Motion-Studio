"""Visual combo popup contract: a grid UI without changing stored field values."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QListView

from app.widgets.visual_preset_combo import VisualPresetComboBox, _preview_icon
from app.models.source import Source, SourceType


class VisualPresetComboTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_combo(self, kind="entrance"):
        combo = VisualPresetComboBox(kind)
        self.addCleanup(combo.deleteLater)
        self.addCleanup(combo.close)
        for label, value in (
            ("None", "none"), ("Slide left", "slide_left"), ("Custom", "custom")
        ):
            combo.addItem(label, value)
        combo.setCurrentIndex(1)
        return combo

    def test_is_normal_combobox_and_preserves_item_data(self):
        combo = self.make_combo()
        self.assertIsInstance(combo, QComboBox)
        self.assertEqual(combo.currentData(), "slide_left")
        combo.setItemText(1, "왼쪽 슬라이드")
        self.assertEqual(combo.currentText(), "왼쪽 슬라이드")
        self.assertEqual(combo.itemData(1), "slide_left")

    def test_popup_has_grid_previews_and_commits_selection_once(self):
        combo = self.make_combo()
        changed = []
        activated = []
        text_activated = []
        combo.currentIndexChanged.connect(changed.append)
        combo.activated.connect(activated.append)
        combo.textActivated.connect(text_activated.append)
        combo.showPopup()
        view = combo._list
        self.assertIsInstance(view, QListView)
        self.assertEqual(view.viewMode(), QListView.ViewMode.IconMode)
        self.assertTrue(view.isWrapping())
        self.assertIs(view.model(), combo.model())
        self.assertEqual(view.currentIndex().row(), 1)
        custom = view.model().index(2, view.modelColumn(), view.rootIndex())
        self.assertEqual(custom.data(), "Custom")
        self.assertTrue(combo.itemIcon(2).isNull())
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(custom).center())
        self.assertEqual(combo.currentData(), "custom")
        self.assertEqual(changed, [2])
        self.assertEqual(activated, [2])
        self.assertEqual(text_activated, ["Custom"])
        self.assertFalse(combo._timer.isActive())
        self.assertIsNone(combo._list)

    def test_popup_respects_disabled_choices_and_activates_existing_selection(self):
        combo = self.make_combo()
        combo.model().item(2).setEnabled(False)
        activated = []
        combo.activated.connect(activated.append)
        combo.showPopup()
        view = combo._list
        disabled = view.model().index(2, view.modelColumn(), view.rootIndex())
        self.assertFalse(disabled.flags() & Qt.ItemFlag.ItemIsEnabled)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(disabled).center())
        self.assertEqual(combo.currentIndex(), 1)
        self.assertEqual(activated, [])
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(activated, [1])

    def test_popup_does_not_change_selection_when_dismissed(self):
        combo = self.make_combo("mask")
        combo.showPopup()
        self.assertIsNotNone(combo._popup)
        combo._popup.close()
        self.assertEqual(combo.currentData(), "slide_left")
        self.assertFalse(combo._timer.isActive())
        self.assertIsNone(combo._list)

    def test_five_option_grid_has_three_real_columns(self):
        combo = VisualPresetComboBox("progress")
        self.addCleanup(combo.deleteLater)
        self.addCleanup(combo.close)
        for value in ("rounded", "spotify", "apple", "youtube", "gradient"):
            combo.addItem(value, value)
        combo.show()
        self.app.processEvents()
        combo.showPopup()
        self.app.processEvents()
        view = combo._list
        self.assertIsNotNone(view)
        positions = [view.visualRect(view.model().index(i, view.modelColumn(), view.rootIndex()))
                     for i in range(5)]
        # Windows previously made a three-column popup with a two-column
        # viewport due to missing scrollbar width, wasting a whole strip.
        self.assertEqual(positions[0].top(), positions[2].top())
        self.assertGreater(positions[2].left(), positions[1].left())
        self.assertGreaterEqual(view.viewport().width(), view.gridSize().width() * 3)
        combo.hidePopup()

    def test_click_again_closes_and_subsequent_click_reopens(self):
        combo = self.make_combo()
        combo.show()
        self.app.processEvents()
        QTest.mouseClick(combo.windowHandle(), Qt.MouseButton.LeftButton, pos=combo.rect().center())
        self.assertIsNotNone(combo._popup)
        popup = combo._popup
        field_center = popup.mapFromGlobal(combo.mapToGlobal(combo.rect().center()))
        QTest.mouseClick(popup.windowHandle(), Qt.MouseButton.LeftButton, pos=field_center)
        self.app.processEvents()
        self.assertIsNone(combo._popup)
        QTest.mouseClick(combo.windowHandle(), Qt.MouseButton.LeftButton, pos=combo.rect().center())
        self.app.processEvents()
        self.assertIsNotNone(combo._popup)
        self.assertEqual(combo.currentIndex(), 1)
        combo.hidePopup()

    def test_keyboard_navigation_applies_only_on_confirmation(self):
        combo = self.make_combo()
        combo.show()
        changes = []
        combo.currentIndexChanged.connect(changes.append)
        QTest.keyClick(combo, Qt.Key.Key_F4)
        view = combo._list
        QTest.keyClick(view, Qt.Key.Key_Right)
        self.assertEqual(view.currentIndex().row(), 2)
        self.assertEqual(combo.currentIndex(), 1)
        QTest.keyClick(view, Qt.Key.Key_Escape)
        self.assertEqual(changes, [])
        self.assertIsNone(combo._popup)
        QTest.keyClick(combo, Qt.Key.Key_F4)
        view = combo._list
        QTest.keyClick(view, Qt.Key.Key_Right)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(changes, [2])
        self.assertIsNone(combo._popup)

    def test_mixed_value_stays_blank_until_first_enabled_option_is_confirmed(self):
        combo = self.make_combo()
        combo.setCurrentIndex(-1)
        combo.model().item(0).setEnabled(False)
        combo.showPopup()
        self.assertEqual(combo._list.currentIndex().row(), 1)
        self.assertEqual(combo.currentIndex(), -1)
        QTest.keyClick(combo._list, Qt.Key.Key_Space)
        self.assertEqual(combo.currentData(), "slide_left")

    def test_open_popup_tracks_inserted_options_and_live_labels(self):
        combo = self.make_combo("shape")
        combo.showPopup()
        view = combo._list
        view.setCurrentIndex(combo.model().index(2, 0))
        combo.insertItem(0, "New", "new")
        combo.setItemText(3, "사용자 지정")
        self.assertEqual(view.currentIndex().data(), "사용자 지정")
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(combo.currentData(), "custom")

    def test_removing_all_options_closes_popup(self):
        combo = self.make_combo()
        combo.showPopup()
        combo.clear()
        self.assertIsNone(combo._popup)
        self.assertFalse(combo._timer.isActive())

    def test_scrollable_grid_opens_above_near_bottom_and_reveals_current_choice(self):
        combo = self.make_combo("visualizer")
        for index in range(14):
            combo.addItem(f"Style {index}", index)
        combo.setCurrentIndex(combo.count() - 1)
        combo.show()
        self.app.processEvents()
        combo.showPopup()
        bounds = QRect(0, 0, 640, 480)
        anchor = QRect(510, 438, 124, 30)
        combo._position_popup(anchor, bounds)
        self.app.processEvents()
        view = combo._list
        view.scrollTo(view.currentIndex())
        self.assertTrue(bounds.contains(combo._popup.geometry()))
        self.assertLess(combo._popup.geometry().bottom(), anchor.top())
        self.assertTrue(view.viewport().rect().contains(view.visualRect(view.currentIndex())))
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        model = combo.model()
        self.assertEqual(view.visualRect(model.index(0, 0)).top(), view.visualRect(model.index(2, 0)).top())

    def test_disable_and_source_replacement_dismiss_without_applying(self):
        combo = self.make_combo()
        combo.set_preview_source(Source(SourceType.TEXT, "First"))
        combo.showPopup()
        combo.set_preview_source(Source(SourceType.TEXT, "Second"))
        self.assertIsNone(combo._popup)
        combo.showPopup()
        combo.setEnabled(False)
        self.assertIsNone(combo._popup)
        self.assertFalse(combo._timer.isActive())
        self.assertEqual(combo.currentIndex(), 1)

    def test_closed_combo_ignores_wheel_to_allow_inspector_scrolling(self):
        combo = self.make_combo()
        event = QWheelEvent(QPointF(8, 8), QPointF(8, 8), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(combo, event)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertFalse(event.isAccepted())

    def test_previews_use_selected_source_colors_and_real_styles(self):
        source = Source(SourceType.PROGRESS_BAR, "Example", fill_color="#44FF00")
        image = _preview_icon("progress", "rounded", 0, source).pixmap(112, 72).toImage()
        self.assertEqual(image.pixelColor(36, 35).name(), "#44ff00")
        self.assertNotEqual(
            _preview_icon("visualizer", "radial", .4).pixmap(112, 72).toImage(),
            _preview_icon("visualizer", "arc", .4).pixmap(112, 72).toImage(),
        )
        self.assertNotEqual(
            _preview_icon("now_playing", "minimal", 0).pixmap(112, 72).toImage(),
            _preview_icon("now_playing", "glass", 0).pixmap(112, 72).toImage(),
        )

    def test_motion_cards_remain_visible_for_transparent_and_dark_sources(self):
        for kind, preset in (("entrance", "fade"), ("entrance", "zoom"), ("exit", "zoom")):
            combo = self.make_combo(kind)
            for fill in ("#00000000", "#151718", "#44FF00"):
                with self.subTest(kind=kind, preset=preset, fill=fill):
                    combo.set_preview_source(Source(SourceType.TEXT, "Sample", fill_color=fill))
                    combo.showPopup()
                    delegate = combo._list.itemDelegate()
                    image = delegate._icon(combo, preset, 1.0, False).pixmap(112, 72).toImage()
                    sample = image.pixelColor(56, 27)
                    self.assertGreater(sample.lightness() - image.pixelColor(12, 12).lightness(), 35)
                    if fill == "#44FF00":
                        self.assertGreater(sample.green(), sample.blue() + 45)
            combo.hidePopup()

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
