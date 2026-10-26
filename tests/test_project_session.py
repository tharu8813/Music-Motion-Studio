"""Transient editing policy and controller guards without MainWindow."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.controllers.autosave_controller import AutosaveController
from app.controllers.history_controller import HistoryController
from app.services.project_session import ProjectSession


class ProjectSessionTests(unittest.TestCase):
    def test_nested_restore_and_exception_keep_tracking_suspended_until_outer_exit(self):
        session = ProjectSession()
        self.assertFalse(session.tracking_changes)
        session.ready = True
        self.assertTrue(session.tracking_changes)
        with session.suspend_changes():
            with self.assertRaises(ValueError):
                with session.suspend_changes():
                    raise ValueError("inner restore failed")
            self.assertFalse(session.tracking_changes)
        self.assertTrue(session.tracking_changes)
        self.assertFalse(session.dirty)
        self.assertEqual(session.change_serial, 0)

    def test_background_save_does_not_hide_newer_edits(self):
        session = ProjectSession(ready=True)
        session.mark_changed()
        saved_serial = session.change_serial
        session.mark_changed()
        self.assertFalse(session.mark_saved(saved_serial))
        self.assertTrue(session.dirty)
        self.assertTrue(session.mark_saved(session.change_serial))
        self.assertFalse(session.dirty)

    def test_sessions_do_not_share_dirty_or_suspension_state(self):
        first, second = ProjectSession(ready=True), ProjectSession(ready=True)
        first.mark_changed()
        with first.suspend_changes():
            self.assertTrue(second.tracking_changes)
            self.assertFalse(second.dirty)

    def test_history_uses_injected_session_without_window_state_fields(self):
        session = ProjectSession()
        window = SimpleNamespace(
            _update_project_status=Mock(), _history_timer=Mock(), _autosave_debounce_timer=Mock(),
            history=Mock(), _project_document=Mock(),
        )
        controller = HistoryController(window, session)
        controller.schedule()
        self.assertEqual(session.change_serial, 0)
        session.ready = True
        with session.suspend_changes():
            controller.schedule()
            controller.commit()
        window.history.commit.assert_not_called()
        controller.schedule()
        controller.commit()
        self.assertEqual(session.change_serial, 1)
        self.assertTrue(session.dirty)
        window._history_timer.start.assert_called_once()
        window._autosave_debounce_timer.start.assert_called_once()
        window.history.commit.assert_called_once()

    def test_autosave_cannot_capture_a_partially_restored_document(self):
        session = ProjectSession(ready=True, dirty=True)
        # No UI/document methods are needed: restoration must return before capture.
        controller = AutosaveController(SimpleNamespace(), session)
        with session.suspend_changes(), patch("app.controllers.autosave_controller.AutosaveWorker") as worker:
            controller.autosave()
        worker.assert_not_called()
        self.assertIsNone(controller.worker)

    def test_failed_history_restore_releases_its_guard_but_preserves_outer_suspension(self):
        from app.models.project import ProjectDocument

        session = ProjectSession(ready=True)
        window = SimpleNamespace(
            setUpdatesEnabled=Mock(), _apply_project=Mock(side_effect=ValueError("bad restore")),
            translator=SimpleNamespace(is_korean=False), canvas=Mock(),
        )
        controller = HistoryController(window, session)
        with (
            session.suspend_changes(),
            patch("app.controllers.history_controller.QMessageBox.critical") as error_dialog,
            patch("app.controllers.history_controller.report_unexpected_error"),
            patch("app.controllers.history_controller.LOGGER.exception"),
        ):
            controller.restore_snapshot(ProjectDocument().to_dict())
            self.assertFalse(session.tracking_changes)
        self.assertTrue(session.tracking_changes)
        self.assertFalse(controller.applying)
        self.assertFalse(session.dirty)
        self.assertEqual(session.change_serial, 0)
        window.setUpdatesEnabled.assert_called_with(True)
        error_dialog.assert_called_once()


if __name__ == "__main__":
    unittest.main()
