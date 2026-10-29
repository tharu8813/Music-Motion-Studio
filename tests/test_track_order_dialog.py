from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

from app.dialogs.track_order_dialog import TrackOrderDialog
from app.models.playlist import PlaylistTrack
from app.services.playlist_service import PlaylistService
from app.utils.i18n import Language, Translator
from app.widgets.playlist_editor import PlaylistEditor


class TrackOrderDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.translator = Translator()
        self.translator.set_language(Language.KOREAN)
        self.tracks = [
            PlaylistTrack("a.mp3", "Alpha", "AR", "AL", duration_seconds=60.0),
            PlaylistTrack("b.mp3", "Bravo", "AR", "AL", duration_seconds=60.0),
            PlaylistTrack("c.mp3", "Charlie", "AR", "AL", duration_seconds=60.0),
        ]

    def _titles(self, dialog: TrackOrderDialog) -> list[str]:
        by_id = {t.id: t.title for t in self.tracks}
        return [by_id[i] for i in dialog._current_ids()]

    def test_move_buttons_reorder_the_working_copy(self) -> None:
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            dialog.list_widget.item(2).setSelected(True)
            dialog._move_selected("top")
            self.assertEqual(self._titles(dialog), ["Charlie", "Alpha", "Bravo"])
            dialog.list_widget.clearSelection()
            dialog.list_widget.item(0).setSelected(True)
            dialog._move_selected(1)
            self.assertEqual(self._titles(dialog), ["Alpha", "Charlie", "Bravo"])
        finally:
            dialog.deleteLater()

    def test_transport_uses_icons_and_accessible_playback_labels(self):
        from PySide6.QtMultimedia import QMediaPlayer
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            for button in (dialog.prev_button, dialog.play_button, dialog.next_button):
                self.assertFalse(button.icon().isNull())
                self.assertEqual(button.text(), "")
                self.assertTrue(button.accessibleName())
            self.assertFalse(dialog.volume_label.pixmap().isNull())
            dialog._on_playback_state(QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(dialog.play_button.accessibleName(), "일시정지")
            dialog._on_playback_state(QMediaPlayer.PlaybackState.PausedState)
            self.assertEqual(dialog.play_button.accessibleName(), "재생")
        finally:
            dialog.deleteLater()

    def test_drag_drop_reorders_and_rebuilds_rows(self) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QDropEvent

        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            list_widget = dialog.list_widget

            class _InternalDrop(QDropEvent):
                def source(self_inner):  # noqa: N805
                    return list_widget

            list_widget.item(0).setSelected(True)  # Alpha
            target = list_widget.item(2)           # drop onto Charlie
            rect = list_widget.visualItemRect(target)
            point = QPointF(rect.center().x(), rect.bottom() - 1)
            drop = _InternalDrop(
                point, Qt.DropAction.MoveAction, list_widget.model().mimeData(
                    [list_widget.indexFromItem(list_widget.item(0))]
                ),
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            list_widget.dropEvent(drop)
            self.assertEqual(self._titles(dialog), ["Bravo", "Charlie", "Alpha"])
            # Rows keep their metadata widgets after the rebuild.
            row = list_widget.itemWidget(list_widget.item(2))
            self.assertIn("Alpha", row.meta_label.text())
        finally:
            dialog.deleteLater()

    def test_now_playing_row_shows_marker_and_green_state(self) -> None:
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            dialog._play_track(self.tracks[1].id)
            row = dialog.list_widget.itemWidget(dialog.list_widget.item(1))
            self.assertIn("▶", row.meta_label.text())
            self.assertTrue(row.property("nowPlaying"))
            other = dialog.list_widget.itemWidget(dialog.list_widget.item(0))
            self.assertNotIn("▶", other.meta_label.text())
        finally:
            dialog.deleteLater()

    def test_save_without_changes_rejects(self) -> None:
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            with patch.object(QMessageBox, "exec") as message:
                dialog._save()
            message.assert_not_called()
            self.assertEqual(dialog.result(), TrackOrderDialog.DialogCode.Rejected)
        finally:
            dialog.deleteLater()

    def test_save_with_changes_shows_summary_then_accepts_on_continue(self) -> None:
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            dialog.list_widget.item(0).setSelected(True)
            dialog._move_selected("bottom")
            self.assertEqual(self._titles(dialog), ["Bravo", "Charlie", "Alpha"])

            def accept_continue(self: QMessageBox) -> int:
                return 0

            with patch.object(QMessageBox, "exec", accept_continue), patch.object(
                QMessageBox, "clickedButton",
                lambda self: self.buttons()[0],  # the "Continue" button
            ):
                dialog._save()

            self.assertEqual(
                dialog.result(), TrackOrderDialog.DialogCode.Accepted,
            )
            titles = {t.id: t.title for t in self.tracks}
            self.assertEqual(
                [titles[i] for i in dialog.new_order],
                ["Bravo", "Charlie", "Alpha"],
            )
        finally:
            dialog.deleteLater()

    def test_save_summary_lists_only_moved_tracks(self) -> None:
        dialog = TrackOrderDialog(self.tracks, self.translator)
        try:
            dialog.list_widget.item(0).setSelected(True)
            dialog._move_selected(1)  # Alpha and Bravo swap; Charlie stays
            captured: list[str] = []

            def capture(self: QMessageBox) -> int:
                captured.append(self.text())
                return 0

            with patch.object(QMessageBox, "exec", capture), patch.object(
                QMessageBox, "clickedButton", lambda self: self.buttons()[1],
            ):
                dialog._save()
            self.assertIn("Alpha", captured[0])
            self.assertIn("Bravo", captured[0])
            self.assertNotIn("Charlie", captured[0])
            self.assertEqual(
                dialog.result(), TrackOrderDialog.DialogCode.Rejected,
            )
        finally:
            dialog.deleteLater()


class PlaylistEditorReorderButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_reorder_button_opens_dialog_and_applies_new_order(self) -> None:
        translator = Translator()
        translator.set_language(Language.KOREAN)
        service = PlaylistService()
        service.add_tracks([
            PlaylistTrack("a.mp3", "Alpha", duration_seconds=10.0),
            PlaylistTrack("b.mp3", "Bravo", duration_seconds=10.0),
        ])
        editor = PlaylistEditor(service, translator)
        try:
            self.assertTrue(editor.order_editor_button.isEnabled())
            ids = [t.id for t in service.tracks]

            class FakeDialog:
                DialogCode = TrackOrderDialog.DialogCode

                def __init__(self, *_a, **_k) -> None:
                    self.new_order = list(reversed(ids))

                def exec(self) -> int:
                    return TrackOrderDialog.DialogCode.Accepted

                def deleteLater(self) -> None:  # noqa: N802
                    pass

            with patch(
                "app.dialogs.track_order_dialog.TrackOrderDialog", FakeDialog,
            ):
                editor._open_order_editor()
            self.assertEqual([t.title for t in service.tracks], ["Bravo", "Alpha"])
        finally:
            editor.deleteLater()

    def test_reorder_button_disabled_below_two_tracks(self) -> None:
        translator = Translator()
        service = PlaylistService()
        service.add_tracks([PlaylistTrack("a.mp3", "Alpha", duration_seconds=10.0)])
        editor = PlaylistEditor(service, translator)
        try:
            self.assertFalse(editor.order_editor_button.isEnabled())
        finally:
            editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
