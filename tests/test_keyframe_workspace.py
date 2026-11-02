"""Keyboard, history and workspace integration for keyed sources."""
from dataclasses import replace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tests.main_window_base import MainWindowTestCase
from app.animation.keyframes import Keyframe
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType


class KeyframeWorkspaceTests(MainWindowTestCase):
    def prepare(self):
        source = Source(SourceType.SHAPE, "Animated", x=100, y=100)
        self.window.set_project_open(True)
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.playlist_service.replace([PlaylistTrack("", "A", duration_seconds=30)])
        self.window.project_session.ready = True
        self.window._history_timer.stop()
        self.window.history.reset(self.window._project_document().to_dict())
        return source, self.window.timeline_panel.keyframe_controller

    def test_toggle_and_value_edits_have_distinct_undo_steps(self):
        source, controller = self.prepare()
        controller.toggle("x")
        controller.edit_value("x", 250)
        self.window._undo()
        self.assertEqual(controller.source.keyframes.global_curves["x"][0].value, 100)
        self.window._undo()
        self.assertFalse(controller.source.keyframes.animated)
        self.window._redo()
        self.window._redo()
        self.assertEqual(controller.source.keyframes.global_curves["x"][0].value, 250)

    def test_delete_key_cannot_remove_source_from_canvas(self):
        source, controller = self.prepare()
        controller.toggle("x")
        editor = self.window.timeline_panel.keyframe_editor
        self.window._show_bottom_panel(1)
        self.window.timeline_panel.view_combo.setCurrentIndex(2)
        self.window.show()
        editor.view.setFocus()
        self.application.processEvents()
        for item in editor.scene.items():
            if hasattr(item, "point"):
                item.setSelected(True)
        QTest.keyClick(editor.view, Qt.Key.Key_Delete)
        self.assertIsNotNone(self.window.store.get(source.id))
        self.assertFalse(controller.curves()["x"])

    def test_visual_view_changes_preserve_animation_time_basis_and_data(self):
        source, controller = self.prepare()
        controller.configure(mode="per_track")
        controller.choose_track(self.window.playlist_service.tracks[0].id)
        controller.toggle("scale")
        saved = source.to_dict()
        for index in (0, 1, 2, 0):
            self.window.timeline_panel.view_combo.setCurrentIndex(index)
        self.assertEqual(source.to_dict(), saved)

    def test_copy_paste_and_frame_nudge_are_one_transaction_per_edit(self):
        source, controller = self.prepare()
        controller.toggle("x")
        controller.seek(3)
        controller.toggle("rotation")
        editor = self.window.timeline_panel.keyframe_editor
        for item in editor.scene.items():
            if hasattr(item, "point"):
                item.setSelected(True)
        before = {name: list(points) for name, points in controller.curves().items()}
        QTest.keyClick(editor.view, Qt.Key.Key_Right)
        self.assertAlmostEqual(controller.curves()["x"][0].time, 1 / editor.fps, places=5)
        self.assertAlmostEqual(controller.curves()["rotation"][0].time, 3 + 1 / editor.fps, places=5)
        self.window._undo()
        self.assertEqual(controller.curves(), before)
        self.window._show_bottom_panel(1)
        self.window.timeline_panel.view_combo.setCurrentIndex(2)
        self.window.show()
        editor.view.setFocus()
        self.application.processEvents()
        for item in editor.scene.items():
            if hasattr(item, "point"):
                item.setSelected(True)
        QTest.keyClick(editor.view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        controller.seek(6)
        QTest.keyClick(editor.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual([point.time for point in controller.curves()["x"]], [0, 6])
        self.assertEqual([point.time for point in controller.curves()["rotation"]], [3, 9])

    def test_rejected_paste_preserves_existing_points(self):
        source, controller = self.prepare()
        controller.toggle("x")
        saved = source.to_dict()
        controller.copy()
        controller.paste()
        self.assertEqual(source.to_dict(), saved)

    def test_scrubbing_updates_slider_without_creating_an_undo_entry(self):
        source, controller = self.prepare()
        source.keyframes.global_curves = {"scale": [Keyframe(0, 1), Keyframe(10, 2)]}
        self.window.store.update(source.id, keyframes=source.keyframes)
        self.window.history_controller.flush_pending()
        serial = self.window.project_session.change_serial
        controller.seek_global(5)
        self.assertEqual(self.window.inspector.scale_spin.value(), 1.5)
        slider = self.window.inspector._linked_sliders[self.window.inspector.scale_spin]
        self.assertEqual(slider.value(), round((1.5 - .1) / .05))
        self.assertEqual(self.window.project_session.change_serial, serial)

    def test_crossfade_editor_uses_visual_owner_not_early_incoming_audio(self):
        source, controller = self.prepare()
        tracks = [PlaylistTrack("", "A", duration_seconds=10), PlaylistTrack("", "B", duration_seconds=10)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="crossfade", crossfade_seconds=2)
        source.keyframes.mode = "per_track"
        source.keyframes.per_track_curves = {tracks[0].id: {"x": [Keyframe(0, 100)]},
                                             tracks[1].id: {"x": [Keyframe(0, 300)]}}
        controller.seek_global(8.5)
        self.assertEqual(controller.evaluated()["x"], 100)
        controller.seek_global(9.5)
        self.assertEqual(controller.evaluated()["x"], 300)

    def test_minimum_window_does_not_overlap_lanes_and_numeric_controls(self):
        self.prepare()
        self.window._show_bottom_panel(1)
        self.window.timeline_panel.view_combo.setCurrentIndex(2)
        self.window.resize(960, 520)
        self.window.show()
        self.application.processEvents()
        editor = self.window.timeline_panel.keyframe_editor
        self.assertLess(editor.view.geometry().bottom(), editor.property.geometry().top())
        self.assertLessEqual(self.window.width(), 960)
