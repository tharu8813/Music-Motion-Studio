"""Audit the three saved banks through real workspace history and packaging."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.animation.keyframes import Keyframe
from app.services.project_service import ProjectService
from tests.main_window_base import MainWindowTestCase
from tests import test_keyframe_workspace as workspace


class AuditHistoryTests(MainWindowTestCase):
    def test_mode_cycle_save_restore_and_undo_redo_preserve_all_banks(self):
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        track = self.window.playlist_service.tracks[0]
        controller.toggle("x")
        controller.configure(mode="single_track", target=track.id)
        controller.choose_track(track.id)
        controller.toggle("rotation")
        controller.configure(mode="per_track")
        controller.choose_track(track.id)
        controller.toggle("scale")
        controller.configure(mode="global")
        saved = deepcopy(controller.source.to_dict())
        with TemporaryDirectory() as raw:
            path = ProjectService.save(Path(raw) / "cycle.pvsproj", self.window._project_document())
            restored = ProjectService.load(path)
        self.assertEqual(restored.sources[0].to_dict(), saved)
        self.window._undo()
        self.assertEqual(controller.source.keyframes.mode, "per_track")
        self.window._redo()
        self.assertEqual(controller.source.to_dict(), saved)
        for _ in range(3):
            for mode in ("single_track", "per_track", "global"):
                controller.configure(mode=mode)
                self.window._undo()
                self.window._redo()
        self.assertEqual(controller.source.to_dict(), saved)

    def test_deleted_track_history_restores_its_id_and_independent_keys(self):
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        track = self.window.playlist_service.tracks[0]
        controller.configure(mode="single_track", target=track.id)
        controller.choose_track(track.id)
        controller.toggle("x")
        saved = deepcopy(controller.source.keyframes)
        self.window.history_controller.flush_pending()
        self.window.playlist_service.remove([track.id])
        self.window.history_controller.flush_pending()
        self.assertFalse(controller.editable)
        self.assertEqual(controller.source.keyframes, saved)
        self.window._undo()
        self.assertEqual(self.window.playlist_service.tracks[0].id, track.id)
        self.assertEqual(controller.source.keyframes, saved)
        self.assertTrue(controller.editable)

    def test_failed_multi_paste_does_not_add_history_or_partially_edit(self):
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        controller.replace_points((), [("x", Keyframe(0, 100)), ("x", Keyframe(5, 200))])
        controller.copy()
        controller.seek(29)
        saved = deepcopy(source.to_dict())
        controller.paste()
        self.assertEqual(controller.source.to_dict(), saved)
        self.window._undo()
        self.assertFalse(controller.source.keyframes.animated)

    def test_native_workspace_language_resize_selection_and_snapshot(self):
        from app.utils.i18n import Language
        from app.timeline.keyframe_editor import Diamond
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        controller.replace_points((), [("x", Keyframe(0, 100)), ("x", Keyframe(5, 200))])
        self.window._show_bottom_panel(1)
        self.window.timeline_panel.view_combo.setCurrentIndex(2)
        self.window.resize(960, 600)
        self.window.show()
        self.application.processEvents()
        QTest.qWait(300)
        editor = self.window.timeline_panel.keyframe_editor
        for language in (Language.ENGLISH, Language.KOREAN):
            self.window.translator.set_language(language)
            self.application.processEvents()
            editor.change_zoom(1.25)
            editor.change_zoom(.8)
        for item in editor.scene.items():
            if isinstance(item, Diamond):
                item.setSelected(True)
        saved = deepcopy(source.to_dict())
        QTest.keyClick(editor.view, Qt.Key.Key_Right)
        self.window._undo()
        self.assertEqual(controller.source.to_dict(), saved)
        self.assertLess(editor.view.geometry().bottom(), editor.property.geometry().top())
        self.window.timeline_panel.keyframe_scroll.ensureWidgetVisible(editor.add)
        self.application.processEvents()
        self.assertTrue(self.window.rect().contains(editor.add.mapTo(self.window, editor.add.rect().bottomRight())))
        import os
        if os.environ.get("MMS_AUDIT_SCREENSHOT"):
            self.assertTrue(self.window.grab().save(os.environ["MMS_AUDIT_SCREENSHOT"]))
