"""Preview surface menus and reversible fullscreen presentation."""

from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QPoint, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLayout, QMenu, QPushButton,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid


_ACTIVITY_EVENTS = frozenset({QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel})
_PRESENTATION_EVENTS = _ACTIVITY_EVENTS | frozenset({
    QEvent.Type.MouseButtonDblClick, QEvent.Type.ContextMenu, QEvent.Type.Resize,
})


class FullscreenPreviewWindow(QWidget):
    exit_requested = Signal()

    def closeEvent(self, event) -> None:
        # Alt+F4 returns to the editor's player without closing its playback session.
        event.ignore()
        self.exit_requested.emit()


class PreviewPresentationController(QObject):
    """Move the existing renderer and controls together, preserving playback state."""

    def __init__(self, preview, surface_layout) -> None:
        super().__init__(preview)
        self.preview = preview
        self.surface_layout = surface_layout
        self.window = None
        self.controls = None
        self._home = []
        self._menu = None
        self._monitor_position = None
        self._cursor_snapshots = []
        self._surface_cursors = []
        self._last_pointer_position = None
        self._owns_cursor_override = False
        self._controls_shown = False
        self.controls_animation = None
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(2500)
        self.idle_timer.timeout.connect(self.hide_controls)
        # Native/OpenGL surfaces can consume mouse-move events. Track the
        # desktop pointer too so a hidden cursor always has a recovery path.
        self.pointer_timer = QTimer(self)
        self.pointer_timer.setInterval(80)
        self.pointer_timer.timeout.connect(self._track_pointer)
        self.monitor_action = QAction(self)
        self.monitor_action.setCheckable(True)
        self.monitor_action.toggled.connect(self._set_monitor_visible)
        preview.performance_monitor.closed.connect(lambda: self.monitor_action.setChecked(False))
        self.fullscreen_action = QAction(self)
        self.fullscreen_action.setShortcut(QKeySequence("F11"))
        self.fullscreen_action.triggered.connect(self.toggle_fullscreen)
        preview.preview_stack_host.addAction(self.fullscreen_action)
        preview.preview_stack_host.setMouseTracking(True)
        preview.preview_stack_host.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        preview.preview_label.setMouseTracking(True)
        self.retranslate()
        QApplication.instance().installEventFilter(self)

    @property
    def is_fullscreen(self) -> bool:
        return self.window is not None

    def retranslate(self) -> None:
        korean = self.preview.translator.is_korean
        self.monitor_action.setText(
            ("성능 모니터 끄기" if self.monitor_action.isChecked() else "성능 모니터 켜기")
            if korean else
            ("Hide performance monitor" if self.monitor_action.isChecked() else "Show performance monitor")
        )
        self.fullscreen_action.setText(
            ("전체화면 해제" if self.is_fullscreen else "전체화면") if korean else
            ("Exit fullscreen" if self.is_fullscreen else "Fullscreen")
        )
        if self.controls is not None:
            self.exit_button.setText("전체화면 해제" if korean else "Exit fullscreen")
            self.hint.setText(
                "더블클릭 또는 Esc로 돌아가기" if korean else "Double-click or press Esc to return"
            )

    def _set_monitor_visible(self, visible: bool) -> None:
        self.preview.performance_monitor.setVisible(visible)
        self.retranslate()
        if self.is_fullscreen:
            self.show_controls()

    def show_context_menu(self, position) -> None:
        if self.preview._closing or self._menu is not None:
            return
        self.retranslate()
        menu = QMenu(self.preview.preview_stack_host)
        menu.addAction(self.monitor_action)
        menu.addSeparator()
        menu.addAction(self.fullscreen_action)
        self._menu = menu
        self.show_controls()
        try:
            menu.exec(position)
        finally:
            self._menu = None
            menu.deleteLater()
            self.show_controls()

    def toggle_fullscreen(self) -> None:
        if self.is_fullscreen:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self) -> None:
        preview = self.preview
        if self.is_fullscreen or preview._closing:
            return
        host = preview.preview_stack_host
        # Record every index before removing anything from its original layout.
        self._home = [
            (widget, widget.parentWidget(), layout, layout.indexOf(widget),
             layout.stretch(layout.indexOf(widget)), layout.itemAt(layout.indexOf(widget)).alignment(),
             not widget.isHidden())
            for widget, layout in (
                (host, self.surface_layout),
                *((card, card.parentWidget().layout()) for card in (
                    preview.now_playing_card, preview.timeline_card, preview.transport_card,
                )),
                (preview.error_banner, preview.layout()),
            )
        ]
        self._monitor_position = preview.performance_monitor.pos()
        self._surface_cursors = [
            (widget, widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor), QCursor(widget.cursor()))
            for widget in (host, preview.preview_label, preview.gpu_surface) if widget is not None
        ]
        window = FullscreenPreviewWindow(preview, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        window.setObjectName("fullscreenPreviewWindow")
        window.setWindowTitle(preview.windowTitle())
        window.setScreen(preview.screen())
        window.exit_requested.connect(self.exit_fullscreen)
        self.window = window
        root = QVBoxLayout(window)
        root.setContentsMargins(0, 0, 0, 0)
        self.surface_layout.removeWidget(host)
        root.addWidget(host)
        host.show()

        controls = QFrame(host)
        controls.setObjectName("fullscreenPreviewControls")
        self.controls = controls
        controls.hide()
        self._controls_shown = False
        self.controls_animation = QPropertyAnimation(controls, b"pos", self)
        self.controls_animation.finished.connect(self._controls_animation_finished)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        header = QHBoxLayout()
        self.hint = QLabel()
        self.hint.setObjectName("mutedLabel")
        header.addWidget(self.hint, 1)
        self.exit_button = QPushButton()
        self.exit_button.clicked.connect(self.exit_fullscreen)
        header.addWidget(self.exit_button)
        layout.addLayout(header)
        for card, _parent, home_layout, *_rest in self._home[1:4]:
            home_layout.removeWidget(card)
            layout.addWidget(card)
            card.show()
        preview.layout().removeWidget(preview.error_banner)
        preview.error_banner.setParent(host)
        preview.error_banner.setVisible(self._home[4][-1])
        # Original window-scoped playback shortcuts now need a scope in this window.
        for shortcut in preview._shortcuts:
            relay = QShortcut(shortcut.key(), window)
            relay.activated.connect(self.show_controls)
            relay.activated.connect(shortcut.activated.emit)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), window)
        escape.activated.connect(self.exit_fullscreen)
        for child in (host, *host.findChildren(QWidget)):
            child.setMouseTracking(True)
        self.retranslate()
        window.showFullScreen()
        window.activateWindow()
        host.setFocus(Qt.FocusReason.OtherFocusReason)
        self._position_controls()
        self.show_controls()
        self.pointer_timer.start()
        QTimer.singleShot(0, preview, preview.refresh_preview)

    def exit_fullscreen(self) -> None:
        window = self.window
        if window is None:
            return
        self.idle_timer.stop()
        self.pointer_timer.stop()
        self.controls_animation.stop()
        self.controls_animation.deleteLater()
        self.controls_animation = None
        self._restore_cursor()
        if self._owns_cursor_override:
            QApplication.restoreOverrideCursor()
            self._owns_cursor_override = False
        for widget, explicit, cursor in self._surface_cursors:
            if isValid(widget):
                widget.setCursor(cursor) if explicit else widget.unsetCursor()
        self._surface_cursors = []
        self.window = None
        self.controls.hide()
        self._controls_shown = False
        window.hide()
        error_visible = not self.preview.error_banner.isHidden()
        for widget, parent, layout, index, stretch, alignment, visible in self._home:
            widget.setParent(parent)
            layout.insertWidget(index, widget, stretch, alignment)
            widget.setVisible(visible)
        self.preview.error_banner.setVisible(error_visible)
        self._ensure_normal_cursor()
        self._home = []
        self.controls.deleteLater()
        self.controls = None
        window.deleteLater()
        monitor = self.preview.performance_monitor
        monitor.move(self._monitor_position)
        monitor.constrain()
        self.retranslate()
        if not self.preview._closing:
            self.preview.window().activateWindow()
            self.preview.preview_stack_host.setFocus(Qt.FocusReason.OtherFocusReason)
            QTimer.singleShot(0, self.preview, self.preview.refresh_preview)

    def _position_controls(self) -> None:
        if self.controls is None:
            return
        host = self.preview.preview_stack_host
        width = max(1, host.width() - 32)
        self.controls.resize(width, self.controls.sizeHint().height())
        target = self._controls_position(self._controls_shown)
        if self.controls_animation.state() == QAbstractAnimation.State.Running:
            self.controls_animation.setEndValue(target)
        else:
            self.controls.move(target)
        banner = self.preview.error_banner
        banner.resize(width, banner.sizeHint().height())
        banner.move(16, 16)

    def _ensure_normal_cursor(self) -> None:
        """Never carry an inherited fullscreen blank cursor into the editor."""
        root = self.preview.window()
        for widget in (root, *root.findChildren(QWidget)):
            if widget.cursor().shape() == Qt.CursorShape.BlankCursor:
                widget.setCursor(Qt.CursorShape.ArrowCursor)
        # A native window can retain its own cursor after its child is reparented.
        handle = root.windowHandle()
        if handle is not None:
            handle.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def _controls_position(self, shown: bool) -> QPoint:
        height = self.preview.preview_stack_host.height()
        return QPoint(16, max(0, height - self.controls.height() - 16) if shown else height + 1)

    def _animate_controls(self, shown: bool) -> None:
        animation = self.controls_animation
        animation.stop()
        if shown:
            if self.controls.isHidden():
                self.controls.move(self._controls_position(False))
            self.controls.show()
            self.controls.raise_()
        self._controls_shown = shown
        animation.setDuration(200 if shown else 160)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic if shown else QEasingCurve.Type.InCubic)
        animation.setStartValue(self.controls.pos())
        animation.setEndValue(self._controls_position(shown))
        animation.start()

    def _controls_animation_finished(self) -> None:
        if self.controls is not None and not self._controls_shown:
            self.controls.hide()

    def _restore_cursor(self) -> None:
        for widget, explicit, cursor in self._cursor_snapshots:
            if isValid(widget):
                if explicit:
                    widget.setCursor(cursor)
                else:
                    widget.unsetCursor()
        self._cursor_snapshots = []

    def _hide_cursor(self) -> None:
        if self._cursor_snapshots:
            return
        host = self.preview.preview_stack_host
        for widget in (self.window, host, *host.findChildren(QWidget)):
            self._cursor_snapshots.append((
                widget, widget.testAttribute(Qt.WidgetAttribute.WA_SetCursor), QCursor(widget.cursor()),
            ))
            widget.setCursor(Qt.CursorShape.BlankCursor)

    def show_controls(self) -> None:
        if not self.is_fullscreen:
            return
        # Qt's application override also reaches native/OpenGL child windows,
        # whose cached blank cursor can outlive QWidget.setCursor().
        if not self._owns_cursor_override:
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.ArrowCursor))
            self._owns_cursor_override = True
        else:
            QApplication.changeOverrideCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self._restore_cursor()
        self.window.setCursor(Qt.CursorShape.ArrowCursor)
        host = self.preview.preview_stack_host
        host.setCursor(Qt.CursorShape.ArrowCursor)
        for surface in (self.preview.preview_label, self.preview.gpu_surface):
            if surface is not None:
                surface.setCursor(Qt.CursorShape.ArrowCursor)
        handle = self.window.windowHandle()
        if handle is not None:
            handle.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self._last_pointer_position = QCursor.pos()
        if not self._controls_shown:
            self._animate_controls(True)
        self.raise_overlays()
        self.idle_timer.start()

    def _track_pointer(self) -> None:
        if self.is_fullscreen and QCursor.pos() != self._last_pointer_position:
            self.show_controls()

    def raise_overlays(self) -> None:
        """Keep telemetry and controls above CPU and OpenGL surfaces alike."""
        for widget in (self.preview.performance_monitor, self.controls, self.preview.error_banner):
            if widget is not None and not widget.isHidden():
                widget.raise_()

    def hide_controls(self) -> None:
        if not self.is_fullscreen:
            return
        modal = QApplication.activeModalWidget()
        if (self._menu is not None or self.preview._scrubbing
                or QApplication.mouseButtons() != Qt.MouseButton.NoButton
                or (modal is not None and modal is not self.preview and modal is not self.window)):
            self.idle_timer.start()
            return
        self._hide_cursor()
        if self._owns_cursor_override:
            QApplication.changeOverrideCursor(QCursor(Qt.CursorShape.BlankCursor))
        handle = self.window.windowHandle()
        if handle is not None:
            handle.setCursor(QCursor(Qt.CursorShape.BlankCursor))
        if self._controls_shown:
            self._animate_controls(False)

    def watch_gpu_surface(self, surface) -> None:
        surface.setMouseTracking(True)
        # Reparenting a QOpenGLWidget can recreate its context. Re-submit even
        # a paused frame so its composed overlays return with the new context.
        surface.backend_ready.connect(self._surface_ready)

    def _surface_ready(self, _info) -> None:
        if not self.preview._closing:
            QTimer.singleShot(0, self.preview, self.preview.refresh_preview)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind not in _PRESENTATION_EVENTS:
            return False
        preview = self.preview
        host = preview.preview_stack_host
        surface = watched is host or watched is preview.preview_label or watched is preview.gpu_surface
        if not self.is_fullscreen and surface and kind in _ACTIVITY_EVENTS:
            self._ensure_normal_cursor()
        if watched is host and kind in {QEvent.Type.MouseButtonDblClick, QEvent.Type.ContextMenu}:
            # Labels and overlay margins can propagate their mouse events to
            # the surface host. Keep those interactions out of fullscreen gestures.
            position = event.globalPosition().toPoint() if kind == QEvent.Type.MouseButtonDblClick else event.globalPos()
            if any(
                widget is not None and not widget.isHidden()
                and widget.rect().contains(widget.mapFromGlobal(position))
                for widget in (preview.performance_monitor, self.controls, preview.error_banner)
            ):
                surface = False
        if surface and kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self.toggle_fullscreen()
            return True
        if surface and kind == QEvent.Type.ContextMenu:
            self.show_context_menu(event.globalPos())
            return True
        if self.is_fullscreen and isinstance(watched, QWidget) and (watched is self.window or host.isAncestorOf(watched) or watched is host):
            if kind == QEvent.Type.Resize and watched is host:
                self._position_controls()
            elif kind in _ACTIVITY_EVENTS:
                self.show_controls()
        return False

    def shutdown(self) -> None:
        if self._menu is not None:
            self._menu.close()
        self.exit_fullscreen()
        self.idle_timer.stop()
        self.pointer_timer.stop()
        self.preview.performance_monitor.close_monitor()
        QApplication.instance().removeEventFilter(self)
