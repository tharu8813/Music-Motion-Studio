"""Window-local keyboard editing with native text-input priority."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractSlider, QApplication, QCheckBox, QComboBox,
    QLineEdit, QPlainTextEdit, QAbstractSpinBox, QTextEdit)


class MotionEditorShortcuts(QObject):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.space_down = False
        QApplication.instance().installEventFilter(self)

    def reset(self):
        self.space_down = False
        self.dialog.canvas.cancel_navigation()

    def close(self):
        self.reset()
        QApplication.instance().removeEventFilter(self)

    def action(self, key, modifiers):
        d = self.dialog
        K, M = Qt.Key, Qt.KeyboardModifier
        if modifiers == M.ControlModifier:
            return {K.Key_Z: d.undo_stack.undo, K.Key_Y: d.undo_stack.redo,
                K.Key_Left: lambda: d.navigate(-1) if d.advanced else d.navigate_common(-1),
                K.Key_Right: lambda: d.navigate(1) if d.advanced else d.navigate_common(1),
                K.Key_Home: lambda: d.seek(0), K.Key_End: lambda: d.seek(1),
                K.Key_0: d.canvas.fit_canvas, K.Key_1: d.canvas.zoom,
                K.Key_Backspace: d.reset_active_value}.get(key)
        if modifiers == (M.ControlModifier | M.ShiftModifier) and key == K.Key_Z:
            return d.undo_stack.redo
        if modifiers == M.ShiftModifier:
            return {K.Key_Left: lambda: d.seek(d.progress-.1/d.durations[d.edge]),
                K.Key_Right: lambda: d.seek(d.progress+.1/d.durations[d.edge]),
                K.Key_F1: lambda: d.show_help("motion_shortcuts"),
                K.Key_Plus: lambda: d.canvas.zoom(1.2)}.get(key)
        if modifiers != M.NoModifier:
            return None
        fields = tuple(d.lanes)
        if K.Key_1 <= key <= K.Key_5:
            return lambda: d.select_time(fields[key-K.Key_1], d.progress)
        if key in (K.Key_Up, K.Key_Down):
            direction = -1 if key == K.Key_Up else 1
            return lambda: d.select_time(fields[min(4, max(0, fields.index(d.active_field)+direction))], d.progress)
        return {
            K.Key_Home: lambda: d.seek(d.play_minimum), K.Key_End: lambda: d.seek(d.play_maximum),
            K.Key_Left: lambda: d.seek(d.progress-.01/d.durations[d.edge]),
            K.Key_Right: lambda: d.seek(d.progress+.01/d.durations[d.edge]),
            K.Key_K: lambda: d.add_point(d.progress) if d.advanced else d.add_common_point(d.progress),
            K.Key_Delete: lambda: d.delete_point(d.progress) if d.advanced else d.delete_common_point(d.progress),
            K.Key_Backspace: lambda: d.delete_point(d.progress) if d.advanced else d.delete_common_point(d.progress),
            K.Key_E: lambda: d.toggle_field(d.active_field, not d.motion.field_enabled(d.active_field)),
            K.Key_B: d.edit_graph, K.Key_P: lambda: d.open_presets(d.active_field),
            K.Key_L: lambda: d.loop.setChecked(not d.loop.isChecked()),
            K.Key_G: lambda: d.guides.setChecked(not d.guides.isChecked()),
            K.Key_O: lambda: d.original.setChecked(not d.original.isChecked()),
            K.Key_F: d.canvas.fit_canvas, K.Key_0: d.canvas.zoom,
            K.Key_Plus: lambda: d.canvas.zoom(1.2), K.Key_Equal: lambda: d.canvas.zoom(1.2),
            K.Key_Minus: lambda: d.canvas.zoom(1/1.2),
            K.Key_F1: d.show_help,
        }.get(key)

    def eventFilter(self, watched, event):
        d = self.dialog
        kind = event.type()
        if watched is d and kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide):
            self.reset()
        if kind not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if d._closed or QApplication.activeWindow() is not d or QApplication.activePopupWidget() is not None:
            return False
        key = event.key()
        focus = QApplication.focusWidget()
        typing = isinstance(focus, (QLineEdit, QAbstractSpinBox, QComboBox, QTextEdit, QPlainTextEdit))
        modifiers = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier |
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        if key == Qt.Key.Key_Space and self.space_down and kind == QEvent.Type.KeyRelease:
            if not event.isAutoRepeat():
                used = d.canvas.space_pan_used
                self.space_down = False
                d.canvas.set_space_pan(False)
                if not used and not typing:
                    d.toggle_play()
            return True
        if typing and key != Qt.Key.Key_F1:
            return False
        if isinstance(focus, QAbstractSlider) and key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
            return False
        if d._gesture_before is not None and key != Qt.Key.Key_Escape:
            # Do not change time or open a modal window halfway through one
            # drag transaction. Slider arrow input above remains native.
            if kind == QEvent.Type.ShortcutOverride:
                event.accept()
            return True
        if key == Qt.Key.Key_Space and modifiers == Qt.KeyboardModifier.NoModifier:
            if isinstance(focus, QCheckBox):
                return False
            if kind == QEvent.Type.ShortcutOverride:
                event.accept()
            elif kind == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self.space_down = True
                d.canvas.set_space_pan(True)
            return True
        if key == Qt.Key.Key_Escape and (self.space_down or d.canvas._panning):
            if kind == QEvent.Type.ShortcutOverride:
                event.accept()
            elif kind == QEvent.Type.KeyPress:
                self.reset()
            return True
        action = self.action(key, modifiers)
        if action is None:
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()
        elif kind == QEvent.Type.KeyPress:
            if not event.isAutoRepeat() or key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                action()
        return True
