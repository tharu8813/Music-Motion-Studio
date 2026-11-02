"""Commit a modal motion draft as one project history operation."""
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog


class CustomMotionController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        window.inspector.custom_motion_requested.connect(self.open_editor)

    def open_editor(self, identifier, edge):
        window = self.window
        source = window.store.get(identifier)
        if source is None or source.locked or len(window.store.selected_ids) != 1:
            return
        window.animation_preview_controller.cancel()
        from app.dialogs.custom_motion_dialog import CustomMotionDialog
        dialog = CustomMotionDialog(source, window.canvas.scene_model, window.playlist_service.tracks,
                                    window.translator, edge, window)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.changes:
            current = window.store.get(identifier)
            if current is not None and not current.locked:
                window.history_controller.flush_pending()
                window.store.update(identifier, **dialog.changes)
                window.history_controller.flush_pending()
        dialog.deleteLater()
