"""Precise timing drafts, Qt entry points and real project history boundaries."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest, QSignalSpy
from PySide6.QtWidgets import QApplication, QDialog

from app.timeline.precision_time_dialog import PrecisionTimeDialog, parse_time, time_text
from tests import test_visual_timeline as timeline_tests
from tests.main_window_base import MainWindowTestCase
from tests import test_keyframe_workspace as workspace_tests


class TimeInputTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def edit(self, dialog, key, text):
        field = dialog.fields[key]
        field.setText(text)
        field.textEdited.emit(text)
        return dialog.update_draft(key)

    def test_parser_preserves_milliseconds_and_rejects_invalid_or_negative_input(self):
        for text, value in (("12.50", 12.5), ("01:23.45", 83.45), ("01:23.450", 83.45),
                            ("01:02:03.500", 3723.5), ("0.000001", .000001)):
            self.assertEqual(parse_time(text), value)
            self.assertEqual(parse_time(time_text(value)), value)
        for text in ("", "-1", "nan", "inf", "1e3", "1:60", "1:02:60", "1:2", "1..0", "1:00:00:00", ".1"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_time(text)

    def test_source_rules_keep_duration_or_start_and_zero_means_playlist_end(self):
        dialog = PrecisionTimeDialog("Text", 5, 10, playlist_end=100)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(self.edit(dialog, "start", "01:23.450"))
        self.assertEqual(dialog.timing, (83.45, 10))
        self.assertTrue(self.edit(dialog, "end", "01:40.001"))
        self.assertEqual(dialog.timing, (83.45, 16.551))
        self.assertTrue(self.edit(dialog, "duration", "0.001"))
        self.assertEqual(dialog.timing, (83.45, .001))
        self.assertFalse(self.edit(dialog, "end", "1"))
        self.assertTrue(dialog.error.text())
        dialog.full.setChecked(True)
        self.assertEqual(dialog.timing, (83.45, 0))
        self.assertFalse(dialog.fields["end"].isEnabled())
        self.assertTrue(self.edit(dialog, "start", "120.123456"))
        self.assertEqual(dialog.timing, (120.123456, 0))
        self.assertEqual(dialog.fields["duration"].text(), "0")

    def test_native_focus_enter_escape_and_track_readonly_minimum(self):
        dialog = PrecisionTimeDialog("Track", 20, 10, track=True, minimum=20)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.fields["duration"].isReadOnly())
        self.assertTrue(dialog.fields["end"].isReadOnly())
        dialog.show()
        self.app.processEvents()
        field = dialog.fields["start"]
        field.selectAll()
        QTest.keyClicks(field, "19.999")
        QTest.keyClick(field, Qt.Key.Key_Return)
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        field.selectAll()
        QTest.keyClicks(field, "20.123456")
        QTest.keyClick(field, Qt.Key.Key_Tab)
        self.assertEqual(dialog.start, 20.123456)
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        other = PrecisionTimeDialog("Source", 5, 0, full=True, playlist_end=20)
        self.addCleanup(other.deleteLater)
        other.show()
        QTest.keyClick(other, Qt.Key.Key_Escape)
        self.assertEqual(other.result(), QDialog.DialogCode.Rejected)


class PreciseTimelineTests(timeline_tests.VisualTimelineTests):
    def test_f2_apply_preserves_selection_scroll_and_fractional_model(self):
        self.panel._select_visual_block("source", self.source.id)
        self.visual.zoom(5)
        self.visual.horizontalScrollBar().setValue(80)
        scroll = self.visual.horizontalScrollBar().value()
        commits = QSignalSpy(self.store.source_changed)
        def apply(dialog):
            self.assertEqual(dialog.timing, (5, 10))
            dialog.start = 5.123456
            return QDialog.DialogCode.Accepted
        with patch.object(PrecisionTimeDialog, "exec", apply):
            QTest.keyClick(self.visual, Qt.Key.Key_F2)
        self.app.processEvents()
        self.assertEqual(self.source.timeline_start, 5.123456)
        self.assertEqual(self.source.timeline_duration, 10)
        self.assertEqual(commits.count(), 1)
        self.assertEqual(self.visual.selected, ("source", self.source.id))
        self.assertEqual(self.visual.horizontalScrollBar().value(), scroll)

    def test_cancel_and_noop_never_publish_changes(self):
        self.panel._select_visual_block("source", self.source.id)
        saved = deepcopy(self.source.to_dict())
        changed = QSignalSpy(self.store.source_changed)
        for result in (QDialog.DialogCode.Rejected, QDialog.DialogCode.Accepted):
            with patch.object(PrecisionTimeDialog, "exec", return_value=result):
                QTest.keyClick(self.visual, Qt.Key.Key_F2)
        self.assertEqual(self.source.to_dict(), saved)
        self.assertEqual(changed.count(), 0)

    def test_drag_and_arrow_after_precise_entry_keep_original_fractional_duration(self):
        self.store.update(self.source.id, timeline_start=5.123456, timeline_duration=10.123456)
        self.app.processEvents()
        self.panel._select_visual_block("source", self.source.id)
        QTest.keyClick(self.visual, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.app.processEvents()
        self.assertEqual(self.source.timeline_start, 5.223456)
        self.assertEqual(self.source.timeline_duration, 10.123456)
        point = self.begin(self.block(self.source.id))
        from PySide6.QtCore import QPoint
        QTest.mouseMove(self.visual.viewport(), point+QPoint(40, 0))
        self.finish(point+QPoint(40, 0))
        self.assertEqual(self.source.timeline_duration, 10.123456)

    def test_context_precision_entry_and_locked_f2(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        point = self.visual.block_rect(self.block(self.source.id)).center().toPoint()
        menu, actions = Mock(), []
        def add_action(text):
            action = Mock()
            actions.append(action)
            return action
        menu.addAction.side_effect = add_action
        menu.exec.side_effect = lambda _position: actions[-1]
        with patch("app.timeline.visual_timeline.QMenu", return_value=menu), patch.object(
                PrecisionTimeDialog, "exec", return_value=QDialog.DialogCode.Rejected) as opened:
            self.visual.contextMenuEvent(SimpleNamespace(pos=lambda: point, globalPos=lambda: point))
            opened.assert_called_once()
        self.store.update(self.source.id, locked=True)
        self.app.processEvents()
        with patch.object(PrecisionTimeDialog, "exec") as opened:
            QTest.keyClick(self.visual, Qt.Key.Key_F2)
            opened.assert_not_called()

    def test_track_entry_uses_service_minimum_and_preserves_file_duration(self):
        self.panel._select_visual_block("track", self.tracks[1].id)
        def apply(dialog):
            self.assertEqual(dialog.minimum, self.playlist.minimum_start_time(self.tracks[1].id))
            dialog.start = 83.45
            return QDialog.DialogCode.Accepted
        with patch.object(PrecisionTimeDialog, "exec", apply):
            QTest.keyClick(self.visual, Qt.Key.Key_F2)
        self.assertEqual(self.tracks[1].start_time_seconds, 83.45)
        self.assertEqual(self.tracks[1].duration_seconds, 20)


class PrecisionHistoryTests(MainWindowTestCase):
    def test_each_apply_is_one_undo_step_and_cancel_does_not_dirty_project(self):
        source, _controller = workspace_tests.KeyframeWorkspaceTests.prepare(self)
        panel = self.window.timeline_panel
        panel.refresh()
        panel._select_visual_block("source", source.id)
        before = deepcopy(self.window._project_document().to_dict())
        serial = self.window.project_session.change_serial
        dirty = self.window.project_session.dirty
        with patch.object(PrecisionTimeDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            panel._edit_precise_timing("source", source.id)
        self.assertEqual(serial, self.window.project_session.change_serial)
        self.assertEqual(dirty, self.window.project_session.dirty)
        self.assertEqual(before, self.window._project_document().to_dict())
        for start in (1.234, 2.345):
            def apply(dialog):
                dialog.start = start
                return QDialog.DialogCode.Accepted
            with patch.object(PrecisionTimeDialog, "exec", apply):
                panel._edit_precise_timing("source", source.id)
        self.window._undo()
        self.assertEqual(self.window.store.get(source.id).timeline_start, 1.234)
        self.window._undo()
        self.assertEqual(self.window._project_document().to_dict(), before)
        self.window._redo()
        self.assertEqual(self.window.store.get(source.id).timeline_duration, 0)
        self.window._redo()
        self.assertEqual(self.window.store.get(source.id).timeline_start, 2.345)

    def test_track_timing_undo_and_packaged_source_time_roundtrip(self):
        from app.services.project_service import ProjectService
        source, _controller = workspace_tests.KeyframeWorkspaceTests.prepare(self)
        panel = self.window.timeline_panel
        track = self.window.playlist_service.tracks[0]
        panel._commit_visual_timing("track", track.id, 83.450001, track.duration_seconds)
        self.assertEqual(self.window.playlist_service.tracks[0].start_time_seconds, 83.450001)
        self.window._undo()
        self.assertIsNone(self.window.playlist_service.tracks[0].start_time_seconds)
        self.window._redo()
        panel._commit_visual_timing("source", source.id, 83.450001, .001001)
        with TemporaryDirectory() as raw:
            path = ProjectService.save(Path(raw)/"precise.pvsproj", self.window._project_document())
            restored = ProjectService.load(path)
        self.assertEqual(restored.sources[0].timeline_start, 83.450001)
        self.assertEqual(restored.sources[0].timeline_duration, .001001)
        self.assertEqual(restored.playlist[0].start_time_seconds, 83.450001)
