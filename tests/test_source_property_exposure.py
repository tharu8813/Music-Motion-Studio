"""Regression tests for editable, type-specific source properties."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from app.dialogs.color_editor_dialog import ColorEditorDialog  # noqa: E402
from app.dialogs.video_source_dialog import VideoSourceDialog  # noqa: E402
from app.inspector.source_inspector import SourceInspector  # noqa: E402
from app.models.source import Gradient, Shadow, Source, SourceType  # noqa: E402
from app.services.source_store import SourceStore  # noqa: E402
from app.utils.i18n import Translator  # noqa: E402


class SourcePropertyExposureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.store = SourceStore()
        self.inspector = SourceInspector(self.store, Translator())
        self.addCleanup(self.inspector.deleteLater)

    def test_waveform_can_edit_rendered_point_count_and_line_width(self) -> None:
        waveform = Source(
            SourceType.AUDIO_WAVEFORM, "Waveform",
            visualizer_bars=48, visualizer_line_width=3.5,
        )
        self.store.add(waveform)
        self.assertTrue(self.inspector._field_visibility["waveform_style"])
        self.assertTrue(self.inspector._field_visibility["visualizer_bars"])
        self.assertTrue(self.inspector._field_visibility["visualizer_line_width"])
        self.assertEqual(self.inspector.visualizer.widgets["visualizer_bars"].value(), 48)
        self.assertEqual(self.inspector.visualizer.widgets["visualizer_line_width"].value(), 3.5)

        self.inspector.visualizer.widgets["visualizer_bars"].setValue(64)
        self.inspector.visualizer.widgets["visualizer_line_width"].setValue(5.0)
        self.assertEqual(waveform.visualizer_bars, 64)
        self.assertEqual(waveform.visualizer_line_width, 5.0)

        self.store.add(Source(SourceType.SHAPE, "Shape"))
        self.assertFalse(self.inspector._field_visibility["visualizer_bars"])
        self.assertFalse(self.inspector._field_visibility["visualizer_line_width"])

    def test_time_format_is_only_shown_for_time_sources_and_round_trips(self) -> None:
        time = Source(SourceType.TIME, "Clock", time_format="HH:mm")
        self.store.add(time)
        self.assertTrue(self.inspector._field_visibility["time_format"])
        self.assertEqual(self.inspector.time_format_combo.currentData(), "HH:mm")
        self.inspector.time_format_combo.setCurrentIndex(
            self.inspector.time_format_combo.findData("HH:mm:ss")
        )
        self.assertEqual(time.time_format, "HH:mm:ss")
        self.assertEqual(Source.from_dict(time.to_dict()).time_format, "HH:mm:ss")

        self.store.add(Source(SourceType.TEXT, "Title"))
        self.assertFalse(self.inspector._field_visibility["time_format"])

    def test_glyph_effect_rows_reappear_when_dependencies_are_enabled(self) -> None:
        text = Source(
            SourceType.TEXT, "Glyphs",
            shadow=Shadow(enabled=False), gradient=Gradient(enabled=False),
        )
        self.store.add(text)
        self.assertFalse(self.inspector._field_visibility["text_shadow_glyph"])
        self.assertFalse(self.inspector._field_visibility["text_gradient"])

        self.inspector.shadow_check.click()
        self.assertTrue(text.shadow.enabled)
        self.assertTrue(self.inspector._field_visibility["text_shadow_glyph"])

        self.inspector.gradient_check.click()
        self.assertTrue(text.gradient.enabled)
        self.assertTrue(self.inspector._field_visibility["text_gradient"])

    def test_nontext_shadow_artwork_color_is_editable(self) -> None:
        image = Source(SourceType.IMAGE, "Image", shadow=Shadow(enabled=True))
        self.store.add(image)

        def enable_personal_color(dialog: ColorEditorDialog) -> QDialog.DialogCode:
            self.assertTrue(dialog._show_personal_color)
            dialog.personal_check.setChecked(True)
            dialog.personal_hue.setValue(50)
            return QDialog.DialogCode.Accepted

        with patch.object(ColorEditorDialog, "exec", new=enable_personal_color):
            self.inspector._choose_shadow_color()
        self.assertTrue(image.shadow.personal_color_enabled)
        self.assertEqual(image.shadow.personal_color_hue_shift, 50.0)


class VideoShuffleSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_seed_is_visible_only_for_random_repeat_and_is_preserved(self) -> None:
        source = Source(
            SourceType.VIDEO, "Video", video_repeat_mode="random",
            video_random_seed=317, video_paths=["a.mp4", "b.mp4"],
        )
        before = source.to_dict()
        dialog = VideoSourceDialog(source, korean=True)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.random_seed.value(), 317)
        self.assertFalse(dialog.random_seed.isHidden())

        dialog.random_seed.setValue(814)
        self.assertEqual(dialog.values["video_random_seed"], 814)
        dialog.repeat.setCurrentIndex(dialog.repeat.findData("sequence"))
        self.assertTrue(dialog.random_seed.isHidden())
        self.assertEqual(dialog.values["video_random_seed"], 814)
        dialog.repeat.setCurrentIndex(dialog.repeat.findData("random"))
        self.assertFalse(dialog.random_seed.isHidden())

        dialog.reject()
        self.assertEqual(source.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
