"""Fullscreen input, playback continuity, overlays, and reversible layout changes."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QContextMenuEvent, QEnterEvent, QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Language, Translator


class PreviewPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def make_preview(self, embedded=False):
        scene = CanvasScene()
        scene.setSceneRect(0, 0, 1920, 1080)
        translator = Translator()
        translator.set_language(Language.KOREAN)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            translator, embedded=embedded, preferred_backend="cpu",
        )
        if embedded:
            root = QWidget()
            layout = QVBoxLayout(root)
            layout.addWidget(preview, 1)
            layout.addWidget(preview.build_embedded_controls_page())
        else:
            root = preview
        root.resize(1200, 760)
        root.show()
        root.activateWindow()
        self.app.processEvents()

        def dispose():
            preview._stop_preview()
            root.close()
            root.deleteLater()
            self.app.processEvents()

        self.addCleanup(dispose)
        return preview, preview.presentation_controller

    @staticmethod
    def double_click(widget):
        QTest.mouseDClick(widget, Qt.MouseButton.LeftButton)
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton)

    def test_surface_context_menu_controls_monitor_without_a_header_button(self):
        preview, controller = self.make_preview()
        self.assertFalse(hasattr(preview, "performance_toggle"))
        self.assertFalse(any(button.text() == "성능 모니터" for button in preview.findChildren(QPushButton)))
        menus = []

        def select_monitor():
            menu = controller._menu
            menus.append([action.text() for action in menu.actions() if not action.isSeparator()])
            menu.actions()[0].trigger()
            menu.close()

        for expected_visible in (True, False):
            QTimer.singleShot(0, select_monitor)
            event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(100, 100),
                                      preview.preview_label.mapToGlobal(QPoint(100, 100)))
            self.app.sendEvent(preview.preview_label, event)
            self.assertEqual(not preview.performance_monitor.isHidden(), expected_visible)
        self.assertEqual(menus[0][0], "성능 모니터 켜기")
        self.assertEqual(menus[1][0], "성능 모니터 끄기")
        preview.performance_action.trigger()
        preview.performance_monitor.close_monitor()
        self.assertFalse(controller.monitor_action.isChecked())

    def test_double_click_round_trip_restores_both_layouts_without_touching_audio(self):
        for embedded in (False, True):
            with self.subTest(embedded=embedded):
                preview, controller = self.make_preview(embedded)
                widgets = (preview.preview_stack_host, preview.now_playing_card,
                           preview.timeline_card, preview.transport_card, preview.error_banner)
                parents = [widget.parentWidget() for widget in widgets]
                home_layouts = [controller.surface_layout, *(widget.parentWidget().layout() for widget in widgets[1:])]
                indices = [layout.indexOf(widget) for layout, widget in zip(home_layouts, widgets)]
                preview.timeline.setValue(725)
                preview.volume_slider.setValue(43)
                preview._playing = True
                preview.play_timer.setInterval(100000)
                preview.play_timer.start()
                with patch.object(preview.media_player, "stop") as stop, \
                     patch.object(preview.media_player, "setSource") as source:
                    for _ in range(3):
                        self.double_click(preview.preview_label)
                        self.app.processEvents()
                        self.assertTrue(controller.window.isFullScreen())
                        self.assertIs(preview.preview_label.window(), controller.window)
                        self.assertIs(preview.transport_card.parentWidget(), controller.controls)
                        self.assertIs(preview.performance_monitor.window(), controller.window)
                        self.assertIs(preview.error_banner.window(), controller.window)
                        self.double_click(preview.preview_label)
                        self.app.processEvents()
                        self.assertFalse(controller.is_fullscreen)
                        self.assertEqual([widget.parentWidget() for widget in widgets], parents)
                        self.assertEqual([layout.indexOf(widget) for layout, widget in zip(home_layouts, widgets)], indices)
                    stop.assert_not_called()
                    source.assert_not_called()
                self.assertEqual(preview.timeline.value(), 725)
                self.assertEqual(preview.volume_slider.value(), 43)
                self.assertTrue(preview._playing)
                self.assertTrue(preview.play_timer.isActive())

    def test_idle_hides_cursor_and_slides_controls_down_then_motion_slides_them_up(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        self.app.processEvents()
        self.assertNotEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.BlankCursor)
        controller.controls_animation.setCurrentTime(controller.controls_animation.duration())
        visible_y = controller.controls.y()
        controller.hide_controls()
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        # Native and QWidget cursor properties must never be left blank.
        self.assertNotEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertEqual(preview.performance_monitor.heading.cursor().shape(), Qt.CursorShape.SizeAllCursor)
        controller.controls_animation.setCurrentTime(80)
        self.assertGreater(controller.controls.y(), visible_y)
        controller.controls_animation.setCurrentTime(controller.controls_animation.duration())
        self.assertTrue(controller.controls.isHidden())
        position = QPoint(610, 90)
        self.app.sendEvent(preview.preview_label, QMouseEvent(
            QEvent.Type.MouseMove, QPointF(position), QPointF(preview.preview_label.mapToGlobal(position)),
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        ))
        self.assertNotEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertEqual(preview.performance_monitor.heading.cursor().shape(), Qt.CursorShape.SizeAllCursor)
        start_y = controller.controls.y()
        controller.controls_animation.setCurrentTime(100)
        self.assertLess(controller.controls.y(), start_y)
        controller.controls_animation.setCurrentTime(controller.controls_animation.duration())
        self.assertEqual(controller.controls.y(), visible_y)
        self.assertFalse(controller.controls.isHidden())

    def test_idle_timer_fires_while_cursor_is_stationary(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        self.app.processEvents()
        controller.idle_timer.start(20)
        QTest.qWait(70)
        self.assertFalse(controller._controls_shown)
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)

    def test_dragging_and_open_context_menu_keep_controls_available(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        preview._scrubbing = True
        controller.hide_controls()
        self.assertTrue(controller._controls_shown)
        preview._scrubbing = False

        observed = []

        def inspect():
            menu = controller._menu
            controller.hide_controls()
            observed.append(controller._controls_shown)
            menu.actions()[0].trigger()
            observed.append(preview.performance_monitor.isVisible())
            menu.close()

        QTimer.singleShot(0, inspect)
        controller.show_context_menu(QPoint(100, 100))
        self.assertEqual(observed, [True, True])
        preview.performance_monitor.heading.setFocus()
        self.double_click(preview.performance_monitor.heading)
        self.assertTrue(controller.is_fullscreen)

    def test_escape_f11_and_playback_shortcuts_work_in_fullscreen(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        self.app.processEvents()
        before = preview.volume_slider.value()
        QTest.keyClick(preview.preview_stack_host, Qt.Key.Key_Down)
        self.assertEqual(preview.volume_slider.value(), before - 5)
        QTest.keyClick(preview.preview_stack_host, Qt.Key.Key_Right)
        self.assertEqual(preview.timeline.value(), 500)
        QTest.keyClick(preview.preview_stack_host, Qt.Key.Key_Escape)
        self.assertFalse(controller.is_fullscreen)
        self.assertFalse(preview._closing)
        self.app.processEvents()
        QTest.keyClick(preview.preview_stack_host, Qt.Key.Key_F11)
        self.app.processEvents()
        self.assertTrue(controller.is_fullscreen)
        QTest.keyClick(preview.preview_stack_host, Qt.Key.Key_F11)
        self.assertFalse(controller.is_fullscreen)

    def test_monitor_and_composed_frame_survive_fullscreen_and_auto_hide(self):
        preview, controller = self.make_preview()
        preview.performance_action.trigger()
        preview.performance_monitor.set_field("fps_graph", False)
        frame = QImage(160, 90, QImage.Format.Format_ARGB32)
        frame.fill(Qt.GlobalColor.red)
        preview.preview_label.set_image(frame)
        controller.enter_fullscreen()
        self.assertTrue(preview.performance_monitor.isVisible())
        self.assertIs(preview.performance_monitor.parentWidget(), preview.preview_stack_host)
        self.assertEqual(preview.preview_label.image.pixelColor(80, 45), frame.pixelColor(80, 45))
        controller.hide_controls()
        controller.controls_animation.setCurrentTime(controller.controls_animation.duration())
        self.assertTrue(preview.performance_monitor.isVisible())
        self.assertFalse(preview.performance_monitor.enabled["fps_graph"])
        preview._gpu_backend_failed("simulated context loss")
        self.app.processEvents()
        self.assertTrue(preview.error_banner.isVisible())
        self.assertIs(preview.error_banner.window(), controller.window)
        hit = preview.preview_stack_host.childAt(preview.performance_monitor.mapTo(
            preview.preview_stack_host, QPoint(50, 250),
        ))
        self.assertTrue(hit is preview.performance_monitor or preview.performance_monitor.isAncestorOf(hit))
        controller.exit_fullscreen()
        self.assertTrue(preview.performance_action.isChecked())
        self.assertTrue(preview.error_banner.isVisible())

    def test_pointer_motion_restores_cursor_even_without_widget_mouse_move_events(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        self.app.processEvents()
        controller.hide_controls()
        controller.controls_animation.setCurrentTime(controller.controls_animation.duration())
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        # Offscreen/native backends may update the desktop pointer without
        # delivering a QWidget MouseMove. The polling fallback must still recover.
        QTest.mouseMove(preview.preview_label, QPoint(631, 97))
        QTest.qWait(130)
        self.assertEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertEqual(controller.window.windowHandle().cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertTrue(controller._controls_shown)

    def test_normal_window_cannot_retain_an_inherited_blank_cursor(self):
        preview, controller = self.make_preview(True)
        root = preview.window()
        root.setCursor(Qt.CursorShape.BlankCursor)
        controller.enter_fullscreen()
        controller.hide_controls()
        controller.exit_fullscreen()
        self.app.processEvents()
        self.assertEqual(root.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertEqual(root.windowHandle().cursor().shape(), Qt.CursorShape.ArrowCursor)
        for widget in (preview.preview_stack_host, preview.preview_label, preview.transport_card):
            self.assertNotEqual(widget.cursor().shape(), Qt.CursorShape.BlankCursor)

    def test_fullscreen_motion_overrides_native_blank_cursor_and_releases_override(self):
        preview, controller = self.make_preview()
        previous = QApplication.overrideCursor()
        controller.enter_fullscreen()
        controller.hide_controls()
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        QTest.mouseMove(preview.preview_label, QPoint(647, 113))
        QTest.qWait(140)
        self.assertEqual(QApplication.overrideCursor(), previous)
        controller.exit_fullscreen()
        self.assertEqual(QApplication.overrideCursor(), previous)

    def test_enter_or_pointer_poll_restores_cursor_without_widget_mouse_move(self):
        preview, controller = self.make_preview()
        previous = QApplication.overrideCursor()
        controller.enter_fullscreen()
        controller.hide_controls()
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)

        # Native child windows may send only Enter/Hover events, not MouseMove.
        # Make the remembered position stale to represent actual movement.
        controller._last_pointer_position = QPoint(-10000, -10000)
        global_pos = preview.preview_label.mapToGlobal(QPoint(10, 10))
        self.app.sendEvent(
            preview.preview_label,
            QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(global_pos)),
        )
        self.assertEqual(QApplication.overrideCursor(), previous)
        self.assertTrue(controller._controls_shown)

        controller.hide_controls()
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        controller._last_pointer_position = QPoint(-10000, -10000)
        controller._track_pointer()
        self.assertEqual(QApplication.overrideCursor(), previous)
        self.assertTrue(controller._controls_shown)
        controller.exit_fullscreen()

    def test_overlay_reparent_enter_does_not_reveal_idle_cursor(self):
        preview, controller = self.make_preview()
        controller.enter_fullscreen()
        self.app.processEvents()
        controller.hide_controls()
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        # The overlay disappearing may emit Enter while the mouse is still.
        controller.eventFilter(preview.preview_label, QEvent(QEvent.Type.Enter))
        self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertFalse(controller._controls_shown)

    def test_idle_cursor_override_is_balanced_over_repeated_fullscreen_sessions(self):
        preview, controller = self.make_preview()
        original = QApplication.overrideCursor()
        for _ in range(3):
            controller.enter_fullscreen()
            self.app.processEvents()
            controller.hide_controls()
            self.assertEqual(QApplication.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
            controller.show_controls()
            self.assertEqual(QApplication.overrideCursor(), original)
            self.assertNotEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.BlankCursor)
            controller.hide_controls()
            controller.exit_fullscreen()
            self.assertEqual(QApplication.overrideCursor(), original)

    def test_gpu_surface_gestures_and_new_context_rehydrate_paused_preview(self):
        class Surface(QWidget):
            backend_ready = Signal(object)

        preview, controller = self.make_preview()
        surface = Surface()
        preview.preview_stack.addWidget(surface)
        preview.preview_stack.setCurrentWidget(surface)
        preview.gpu_surface = surface
        controller.watch_gpu_surface(surface)
        self.double_click(surface)
        self.app.processEvents()
        self.assertTrue(controller.is_fullscreen)
        with patch.object(preview, "refresh_preview") as refresh:
            surface.backend_ready.emit(object())
            self.app.processEvents()
            refresh.assert_called_once()
        self.double_click(surface)
        self.assertFalse(controller.is_fullscreen)

    def test_closing_fullscreen_window_returns_and_stopping_preview_cleans_up(self):
        preview, controller = self.make_preview(True)
        controller.enter_fullscreen()
        controller.window.close()
        self.assertFalse(controller.is_fullscreen)
        self.assertFalse(preview._closing)
        controller.enter_fullscreen()
        controller.hide_controls()
        preview._stop_preview()
        self.assertFalse(controller.is_fullscreen)
        self.assertFalse(controller.idle_timer.isActive())
        self.assertNotEqual(preview.preview_label.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertTrue(preview._closing)
