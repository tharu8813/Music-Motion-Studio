"""Audit the three saved banks through real workspace history and packaging."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

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
