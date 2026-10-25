"""Color controls must not alter unrelated channels or source-wide policies."""

from copy import deepcopy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDialog

from app.canvas.source_item import SourceItem
from app.canvas.live_canvas import CanvasScene
from app.dialogs.color_editor_dialog import ColorEditorDialog
from app.inspector.source_inspector import SourceInspector
from app.models.source import Shadow, Source, SourceType
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class ColorEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, **kwargs):
        source = kwargs.pop("source", Source(SourceType.TEXT, "Text"))
        dialog = ColorEditorDialog(QColor("#2E7FAD"), source, Translator(), "Color", **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_personal_color_locks_manual_inputs_and_reset_without_losing_fallback(self):
        dialog = self.dialog()
        original = dialog.selected_color
        dialog.personal_check.setChecked(True)
        for widget in (dialog.hex_edit, dialog.hue_slider, dialog.alpha_slider, dialog.reset_button):
            self.assertFalse(widget.isEnabled())
        self.assertTrue(dialog.personal_brightness.isEnabled())
        dialog.personal_check.setChecked(False)
        self.assertTrue(dialog.hex_edit.isEnabled())
        self.assertTrue(dialog.reset_button.isEnabled())
        self.assertEqual(dialog.selected_color, original)

    def test_hidden_personal_controls_never_lock_independent_colors_or_return_policy(self):
        dialog = self.dialog(source=Source(SourceType.TEXT, "Text", personal_color_enabled=True),
                             show_personal_color=False)
        self.assertTrue(dialog.hex_edit.isEnabled())
        self.assertEqual(dialog.personal_settings(), {})

    def test_alpha_preserves_rgb_and_hue_uses_360_degrees(self):
        dialog = self.dialog()
        original = dialog.selected_color.name()
        dialog.alpha_slider.setValue(50)
        self.assertEqual(dialog.selected_color.name(), original)
        dialog.hue_slider.setValue(120)
        self.assertAlmostEqual(dialog.selected_color.hsvHueF() * 360, 120, delta=0.02)

    def test_accept_validates_pending_hex_and_keeps_invalid_input_open(self):
        dialog = self.dialog()
        dialog.hex_edit.setText("not hex")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.hex_edit.setText("#40123456")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.selected_color, QColor("#40123456"))

    def test_independent_colors_preserve_personal_policy_on_every_selected_source(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.deleteLater)
        sources = [Source(SourceType.TEXT, "Text", personal_color_enabled=True,
                          personal_color_strength=0.6755, personal_color_brightness=18.4),
                   Source(SourceType.TEXT, "Other", personal_color_enabled=False)]
        store.replace(sources)
        store.select_many([source.id for source in sources], sources[0].id)
        for target, field in (("shadow", "color"), ("gradient", "start_color"), ("source", "fill_color")):
            before = [deepcopy(source.to_dict()) for source in sources]
            def edit(dialog):
                if target == "shadow":
                    self.assertFalse(dialog.personal_settings()["personal_color_enabled"])
                else:
                    self.assertEqual(dialog.personal_settings(), {})
                dialog._set_color(QColor("#123456"))
                return QDialog.DialogCode.Accepted
            with patch.object(ColorEditorDialog, "exec", new=edit):
                if target == "shadow":
                    inspector._choose_shadow_color()
                elif target == "gradient":
                    inspector._choose_gradient_color(field, inspector.gradient_start_button)
                else:
                    inspector._choose_color(field, inspector.fill_color_button)
            for source, original in zip(sources, before):
                if target == "source":
                    original[field] = "#123456"
                else:
                    original[target][field] = "#123456"
                self.assertEqual(source.to_dict(), original)

    def test_transparent_shadow_is_invisible_for_box_and_glyph_rendering(self):
        def render(source):
            item = SourceItem(source)
            image = QImage(320, 160, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            return image
        source = Source(SourceType.TEXT, "Text", text="Color", width=280, height=120,
                        fill_color="#00000000", outline_color="#FFFFFF",
                        shadow=Shadow(enabled=False, color="#00FF0000"))
        original = render(source)
        source.shadow.enabled = True
        for glyph in (False, True):
            with self.subTest(glyph=glyph):
                source.text_shadow_glyph = glyph
                self.assertEqual(render(source), original)

    def test_shadow_personal_color_edits_are_independent_for_all_selected_text_sources(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.deleteLater)
        sources = [Source(SourceType.TEXT, "Text", personal_color_enabled=True, personal_color_strength=0.6755),
                   Source(SourceType.LYRICS, "Lyrics", personal_color_enabled=False)]
        store.replace(sources)
        store.select_many([source.id for source in sources], sources[0].id)
        before = [deepcopy(source.to_dict()) for source in sources]
        def enable_shadow(dialog):
            self.assertTrue(dialog._show_personal_color)
            self.assertFalse(dialog.personal_check.isChecked())
            dialog._set_color(QColor("#80123456"))
            dialog.personal_check.setChecked(True)
            dialog.personal_hue.setValue(120)
            dialog.personal_strength.setValue(70)
            self.assertFalse(dialog.hex_edit.isEnabled())
            return QDialog.DialogCode.Accepted
        with patch.object(ColorEditorDialog, "exec", new=enable_shadow):
            inspector._choose_shadow_color()
        for source, original in zip(sources, before):
            original["shadow"].update(color="#80123456", personal_color_enabled=True,
                                      personal_color_hue_shift=120.0, personal_color_strength=0.7)
            self.assertEqual(source.to_dict(), original)
        unchanged = [deepcopy(source.to_dict()) for source in sources]
        with patch.object(ColorEditorDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            inspector._choose_shadow_color()
        self.assertEqual([source.to_dict() for source in sources], unchanged)

    def test_shadow_personal_settings_round_trip_and_old_projects_keep_fixed_shadows(self):
        source = Source(SourceType.TEXT, "Text", shadow=Shadow(
            enabled=True, personal_color_enabled=True, personal_color_hue_shift=120,
            personal_color_strength=0.7, personal_color_brightness=-20,
        ))
        self.assertEqual(Source.from_dict(source.to_dict()).to_dict(), source.to_dict())
        legacy = source.to_dict()
        for name in list(legacy["shadow"]):
            if name.startswith("personal_color_"):
                legacy["shadow"].pop(name)
        self.assertFalse(Source.from_dict(legacy).shadow.personal_color_enabled)
        for field, value in (("personal_color_enabled", "true"), ("personal_color_strength", 1.1),
                             ("personal_color_brightness", float("nan")), ("personal_color_hue_shift", True)):
            invalid = source.to_dict()
            invalid["shadow"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                Source.from_dict(invalid)

    def test_shadow_only_personal_color_changes_captured_pixels_and_restores_saved_colors(self):
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(SourceType.TEXT, "Text", x=20, y=20, width=280, height=120, text="Color",
                        fill_color="#00000000", outline_color="#123456",
                        shadow=Shadow(enabled=True, color="#80000000", opacity=1, blur_radius=0,
                                      personal_color_enabled=True, personal_color_hue_shift=120))
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("song.wav", "Song", duration_seconds=2)
        capture = CanvasSnapshot.capture
        for glyph in (False, True):
            source.text_shadow_glyph = glyph
            before = deepcopy(source.to_dict())
            def observe(*args, **kwargs):
                color = QColor(source.shadow.color)
                self.assertEqual(color.getRgb(), (0, 255, 0, 128))
                self.assertEqual(source.outline_color, "#123456")
                return capture(*args, **kwargs)
            with patch("app.preview.canvas_snapshot.extract_track_personal_color", return_value=QColor("#FF0000")), \
                 patch.object(CanvasSnapshot, "capture", side_effect=observe):
                linked = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, transparent=True)
            self.assertEqual(source.to_dict(), before)
            with patch("app.preview.canvas_snapshot.extract_track_personal_color", return_value=QColor()):
                fallback = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, transparent=True)
            self.assertNotEqual(linked, fallback)
            self.assertEqual(source.to_dict(), before)

    def test_export_does_not_freeze_shadow_personal_color_across_tracks(self):
        scene = CanvasScene()
        source = Source(SourceType.TEXT, "Fixed text", text="Color", fill_color="#00000000",
                        shadow=Shadow(enabled=True, opacity=1, personal_color_enabled=True))
        scene.addItem(SourceItem(source))
        tracks = [PlaylistTrack(path, "Track", duration_seconds=2) for path in ("red.wav", "blue.wav")]
        samples = [ExportFrameSample(track, index + 1, index * 2, 2, 0, index * 2)
                   for index, track in enumerate(tracks)]
        capturer = ExportCanvasCapturer(scene, tracks, 4, set(), [(None, None)],
                                       lambda image, seconds, _key: RenderFrame(image, seconds),
                                       lambda: None, lambda _track, _key: None)
        self.assertNotIn("base", capturer.invariant_stream_keys)
        self.assertEqual(len(capturer.coalesce_samples(samples, "base")), 2)
        def artwork(path, _cover):
            return QColor("#FF0000" if path == "red.wav" else "#0000FF")
        with patch("app.preview.canvas_snapshot.extract_track_personal_color", side_effect=artwork):
            first = capturer.capture_stream(samples[0], "base").image
            second = capturer.capture_stream(samples[1], "base").image
        self.assertNotEqual(first, second)
