"""Live telemetry and overlay appearance must not change its settings window."""
import os
import unittest
import tempfile
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QLabel
from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator
from app.widgets.performance_monitor import FIELDS, PRESETS, PerformanceMonitor
from PySide6.QtCore import QSettings


class PerformanceMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def setUp(self):
        settings_dir = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(settings_dir.cleanup)
        self.settings = QSettings(os.path.join(settings_dir.name, 'monitor.ini'), QSettings.Format.IniFormat)
        settings_patch = patch('app.widgets.performance_monitor.QSettings', return_value=self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.scene = CanvasScene()
        self.preview = ExportPreviewDialog(self.scene,
            [PlaylistTrack('missing.wav', 'Evening Walk', duration_seconds=120)],
            Translator(), preferred_backend='cpu')
        self.addCleanup(self.preview.deleteLater)
        self.addCleanup(self.preview._stop_preview)
        self.monitor = self.preview.performance_monitor

    def test_preset_tab_and_every_preset(self):
        m = self.monitor
        m.configure()
        self.assertEqual(
            [m.settings_tabs.tabText(i) for i in range(m.settings_tabs.count())],
            [m.tr('표시 항목', 'Metrics'), m.tr('모양', 'Appearance'), m.tr('프리셋', 'Presets')],
        )
        self.assertNotIn('none', m.preset_buttons)
        from PySide6.QtWidgets import QPushButton
        texts = [button.text() for button in m.options.findChildren(QPushButton)]
        self.assertNotIn(m.tr('기본 항목', 'Essentials'), texts)
        self.assertNotIn('전체 해제', texts)
        appearance = (m.font_size, m.background_opacity, m.text_opacity)
        for mode, ko, en, desc_ko, desc_en, selected in PRESETS:
            m.preset_buttons[mode].click()
            expected = {f[0]: f[4] if selected is None else f[0] in selected for f in FIELDS}
            self.assertEqual(m.enabled, expected)
            self.assertEqual((m.font_size, m.background_opacity, m.text_opacity), appearance)

    def test_settings_restore_in_new_monitor(self):
        m = self.monitor
        m.configure()
        m.preset_buttons['video'].click()
        m.checks['track'].setChecked(True)
        m.font_spin.setValue(17)
        m.opacity_sliders['background_opacity'].setValue(35)
        m.opacity_sliders['text_opacity'].setValue(15)
        restored = PerformanceMonitor(self.preview, self.preview.preview_stack_host)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.enabled, m.enabled)
        self.assertEqual(restored.font_size, 17)
        self.assertEqual(restored.background_opacity, 65)
        self.assertEqual(restored.text_opacity, 85)
        restored.configure()
        self.assertTrue(restored.checks['track'].isChecked())
        self.assertEqual(restored.font_spin.value(), 17)

    def test_invalid_saved_settings_are_safe(self):
        self.settings.setValue('performance_monitor/settings', '{broken')
        restored = PerformanceMonitor(self.preview, self.preview.preview_stack_host)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.enabled, {f[0]: f[4] for f in FIELDS})

    def test_overlay_geometry_is_saved_and_constrained_on_restore(self):
        from PySide6.QtCore import QPoint, QSize
        m = self.monitor
        self.preview.resize(1400, 900)
        self.preview.show()
        m.show()
        self.app.processEvents()
        m.move(40, 45)
        m._preferred_size = QSize(360, 300)
        m.constrain()
        self._drag_border(QPoint(357, 297), QPoint(20, 20))
        restored = PerformanceMonitor(self.preview, self.preview.preview_stack_host)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored._preferred_size, QSize(380, 320))
        self.assertEqual(restored.pos(), m.pos())
        restored.show()
        self.app.processEvents()
        self.assertEqual(restored.size(), QSize(380, 320))
        parent = restored.parentWidget()
        original_size = parent.size()
        parent.resize(240, 160)
        self.app.processEvents()
        self.assertTrue(parent.rect().contains(restored.geometry()))
        parent.resize(original_size)
        self.app.processEvents()
        self.assertEqual(restored.size(), QSize(380, 320))

    def test_saved_horizontal_position_survives_temporary_narrow_layout(self):
        import json
        from PySide6.QtCore import QPoint
        self.settings.setValue('performance_monitor/settings', json.dumps({
            'position': [500, 60], 'size': [300, 200],
        }))
        parent = self.preview.preview_stack_host
        parent.resize(320, 300)
        restored = PerformanceMonitor(self.preview, parent)
        self.addCleanup(restored.deleteLater)
        restored.constrain()
        self.assertEqual(restored.x(), 20)
        restored.change_appearance('font_size', 14)
        saved = json.loads(self.settings.value('performance_monitor/settings'))
        self.assertEqual(saved['position'], [500, 60])
        parent.resize(1000, 700)
        restored.constrain()
        self.assertEqual(restored.pos(), QPoint(500, 60))

    def test_font_and_opacity_do_not_leak_into_settings(self):
        m = self.monitor
        m.configure()
        self.app.processEvents()
        size = m.font_spin.fontMetrics().height()
        check_size = m.checks['fps'].fontMetrics().height()
        for value in (9, 24, 12):
            m.font_spin.setValue(value)
            self.app.processEvents()
            self.assertEqual(m.font_size, value)
            self.assertEqual(m.font_spin.fontMetrics().height(), size)
            self.assertEqual(m.checks['fps'].fontMetrics().height(), check_size)
        m.opacity_sliders['background_opacity'].setValue(100)
        self.assertEqual(m.background_opacity, 0)
        m.opacity_sliders['text_opacity'].setValue(40)
        self.assertEqual(m.text_opacity, 60)
        m.options.hide()

    def test_presets_and_unavailable_gpu_data(self):
        m = self.monitor
        self.assertLess(m.background_opacity, 100)
        m.configure()
        m.select_preset('all')
        m.refresh()
        self.assertTrue(all(m.enabled.values()))
        self.assertGreater(len(m.fields), 30)
        self.assertIn('—', m.fields['gpu'][2].text())
        self.assertIsNone(m.fields['latency_graph'][2].values[-1])
        m.select_preset('default')
        self.assertEqual(m.enabled, {f[0]: f[4] for f in FIELDS})
        m.options.hide()

    def test_close_stops_sampling_and_hides_settings(self):
        self.preview.show()
        self.monitor.show()
        self.monitor.configure()
        self.app.processEvents()
        self.assertTrue(self.monitor.timer.isActive())
        self.monitor.close_monitor()
        self.assertFalse(self.monitor.timer.isActive())
        self.assertTrue(self.monitor.options.isHidden())

    def test_gpu_and_decoder_counters_use_real_stat_fields(self):
        from types import SimpleNamespace
        from app.preview.gpu_texture_surface import GpuUploadStats
        from app.video.preview_decoder import VideoDecoderStats
        p = self.preview
        original = (p.gpu_preview_enabled, p.gpu_surface, p._cached_video_items, p._playing)
        try:
            p.gpu_preview_enabled = True
            p.gpu_surface = SimpleNamespace(upload_stats=GpuUploadStats(
                allocated_bytes=1048576, texture_budget_bytes=2097152,
                submitted_frames=12, presented_frames=10, dropped_pending_frames=2))
            p._cached_video_items = [SimpleNamespace(_video_timeline_preview_active=True,
                video_decoder_stats=lambda: VideoDecoderStats(accepted_frames=23, dropped_frames=3))]
            p._playing = True
            self.monitor.refresh()
            self.assertEqual(self.monitor.fields['gpu'][2].text(), '1.0 MiB / 2.0 MiB')
            self.assertEqual(self.monitor.fields['gpu_frames'][2].text(), '12 / 10 / 2')
            self.assertEqual(self.monitor.fields['decoded'][2].text(), '23 / 3')
            self.assertEqual(self.monitor.fields['memory_graph'][2].values[-1], 1.0)
        finally:
            p.gpu_preview_enabled, p.gpu_surface, p._cached_video_items, p._playing = original

    def _drag_border(self, start, delta):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtTest import QTest
        m = self.monitor
        QTest.mousePress(m, Qt.MouseButton.LeftButton, pos=start)
        target = start+delta
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(target),
            QPointF(m.mapToGlobal(target)), Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(m, event)
        QTest.mouseRelease(m, Qt.MouseButton.LeftButton, pos=target)
        self.app.processEvents()

    def test_all_borders_and_corners_resize_with_matching_cursor(self):
        from PySide6.QtCore import QPoint, QRect, Qt
        from PySide6.QtTest import QTest
        self.preview.resize(1400, 900)
        self.preview.show()
        self.monitor.show()
        self.app.processEvents()
        m = self.monitor
        for horizontal, vertical in ((-1, 0), (1, 0), (0, -1), (0, 1),
                                     (-1, -1), (1, 1), (-1, 1), (1, -1)):
            with self.subTest(edge=(horizontal, vertical)):
                m.setGeometry(60, 60, 360, 300)
                self.app.processEvents()
                start = QPoint(2 if horizontal < 0 else 357 if horizontal else 180,
                               2 if vertical < 0 else 297 if vertical else 150)
                QTest.mouseMove(m, start)
                if horizontal and vertical:
                    expected = (Qt.CursorShape.SizeFDiagCursor if horizontal == vertical
                                else Qt.CursorShape.SizeBDiagCursor)
                else:
                    expected = Qt.CursorShape.SizeHorCursor if horizontal else Qt.CursorShape.SizeVerCursor
                self.assertEqual(m.cursor().shape(), expected)
                self._drag_border(start, QPoint(horizontal*20, vertical*20))
                self.assertEqual(m.width(), 380 if horizontal else 360)
                self.assertEqual(m.height(), 320 if vertical else 300)
                self.assertTrue(m.parentWidget().rect().contains(m.geometry()))

    def test_resize_limits_and_preferred_size_survive_parent_resize(self):
        from PySide6.QtCore import QPoint, QSize
        self.preview.resize(1400, 900)
        self.preview.show()
        self.monitor.show()
        self.app.processEvents()
        m = self.monitor
        m.setGeometry(20, 20, 360, 300)
        self._drag_border(QPoint(357, 297), QPoint(-1000, -1000))
        self.assertEqual(m.size(), QSize(280, 180))
        m._preferred_size = QSize(450, 350)
        m.constrain()
        m.hide()
        m.show()
        self.assertEqual(m.size(), QSize(450, 350))
        parent = m.parentWidget()
        original_size = parent.size()
        parent.resize(240, 160)
        self.app.processEvents()
        self.assertTrue(parent.rect().contains(m.geometry()))
        parent.resize(original_size)
        self.app.processEvents()
        self.assertEqual(m.size(), QSize(450, 350))
