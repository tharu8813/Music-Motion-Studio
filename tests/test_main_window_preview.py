"""Main window Preview: embedded playback, GPU layers, blended/AutoMix audio swaps."""

from __future__ import annotations

import math
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QMessageBox, QWidget
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.canvas.source_item import SourceItem
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.dialogs.export_preview_dialog import (
    GPU_TEXTURE_SURFACE_AVAILABLE,
    TIMELINE_SCALE,
    ExportPreviewDialog,
    OverlayFrameWorker,
    VideoDurationProbeWorker,
    _BLENDED_AUDIO_TRACK_INDEX,
    _LIVE_MIX_TRACK_INDEX,
    _transition_display_regions,
)
from app.controllers.progressive_automix_controller import ProgressiveAutoMixController
from app.controllers.preview_audio_controller import PreviewAudioController
from app.timeline.render_plan import AudioRenderTransition
from app.timeline.models import TransitionType
from app.services.app_settings_service import AppSettings
from app.services.playlist_service import PlaylistService
from app.utils.i18n import Language
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.gpu_texture_surface import (
    GpuColorFilter,
    GpuPreviewLayer,
    GpuTexturePreviewSurface,
)
from app.preview.album_art import create_cached_ambient_background, extract_track_cover
from app.video.preview_proxy import PreviewProxyCache
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer,
    PreparedStaticOverlayLayer,
    VisualizerOverlay,
)
from app.renderer.static_video_stream import StaticVideoStreamResult
from tests.main_window_base import MainWindowTestCase


class MainWindowPreviewTests(MainWindowTestCase):
    def test_missing_ffmpeg_notice_explains_reactions_and_opens_install_settings(self) -> None:
        from app.renderer.ffmpeg_renderer import FFmpegNotFoundError
        with patch("app.ui.main_window.FFmpegRenderer.find_executable", side_effect=FFmpegNotFoundError("missing")), \
             patch.object(self.window, "_show_settings") as settings:
            self.assertFalse(self.window.check_ffmpeg_availability())
            notice = self.window._ffmpeg_notice
            self.assertIn("음악에 맞춰 반응", notice.informativeText())
            self.assertFalse(self.window.check_ffmpeg_availability())
            self.assertIs(self.window._ffmpeg_notice, notice)
            install = next(button for button in notice.buttons() if notice.buttonRole(button) == QMessageBox.ButtonRole.AcceptRole)
            install.click()
            settings.assert_called_once_with(True)
            self.assertIsNone(self.window._ffmpeg_notice)

    def test_available_ffmpeg_needs_no_notice(self) -> None:
        with patch("app.ui.main_window.FFmpegRenderer.find_executable", return_value=Path("ffmpeg.exe")):
            self.assertTrue(self.window.check_ffmpeg_availability())
            self.assertIsNone(self.window._ffmpeg_notice)

    def test_visual_timeline_preview_button_opens_player_at_cursor(self) -> None:
        self.window.playlist_service.add_tracks([PlaylistTrack("preview.wav", "Song", duration_seconds=20)])
        self.window.timeline_panel.refresh()
        self.window._show_bottom_panel(1)
        self.window.timeline_panel.visual.set_playhead(7.25)

        class StubPreview(QDialog):
            def __init__(self, *_args, **_kwargs):
                super().__init__()
                self.controls_page = QWidget()
                self.track_list_panel = QFrame()
                self.position = None

            def build_embedded_controls_page(self):
                return self.controls_page

            def _seek_to_seconds(self, seconds):
                self.position = seconds

            def _stop_preview(self):
                pass

        with patch("app.controllers.preview_controller.ExportPreviewDialog", StubPreview):
            self.window.timeline_panel.preview_button.click()
        self.assertEqual(self.window.bottom_tabs.currentIndex(), 2)
        self.assertEqual(self.window._inline_preview.position, 7.25)
        self.window._finish_inline_preview()

    @staticmethod
    def _automix_plan(track_a: PlaylistTrack, track_b: PlaylistTrack):
        """A CompiledRenderPlan shaped like a real AutoMix result: two audio
        clips overlapping 0-100/90-180, but non-overlapping chapter
        ownership 0-90/90-180 -- the exact shape the reported bug (timeline
        ownership computed from overlapping clip/window ends) needs."""
        from app.timeline.render_plan import (
            AudioRenderClip, AudioRenderPlan, MetadataChapter, MetadataPlan,
            PresentationPlan, PresentationWindow, CompiledRenderPlan,
        )
        clip_a = AudioRenderClip(
            clip_id="a", track_id=track_a.id, timeline_start=0.0, source_in=0.0, source_out=100.0,
        )
        clip_b = AudioRenderClip(
            clip_id="b", track_id=track_b.id, timeline_start=90.0, source_in=0.0, source_out=90.0,
        )
        transition = AudioRenderTransition(
            clip_a="a", clip_b="b", timeline_start=90.0, duration=10.0, type=TransitionType.AUTOMIX,
        )
        windows = (
            PresentationWindow(track_id=track_a.id, timeline_start=0.0, timeline_end=100.0),
            PresentationWindow(track_id=track_b.id, timeline_start=90.0, timeline_end=180.0),
        )
        chapters = (
            MetadataChapter(track_id=track_a.id, start=0.0, end=90.0),
            MetadataChapter(track_id=track_b.id, start=90.0, end=180.0),
        )
        return CompiledRenderPlan(
            audio=AudioRenderPlan(clips=(clip_a, clip_b), transitions=(transition,)),
            presentation=PresentationPlan(windows=windows),
            metadata=MetadataPlan(chapters=chapters),
            duration_seconds=180.0,
        )

    def _open_progressive_preview(self, directory: str):
        """Preview adopting a (not started) progressive controller, plus partial plans."""
        from app.automix.progressive import ProgressiveAnalysis, partial_plan
        from tests.test_automix_planner import ENABLED, _analysis

        tracks = [PlaylistTrack(f"{name}.wav", name.upper(), duration_seconds=120.0) for name in "abc"]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        fake_ffmpeg = Path(directory) / "ffmpeg.exe"
        fake_ffmpeg.touch()
        self.window.settings_service.save(replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)))

        with patch.object(ProgressiveAutoMixController, "start"):
            self.window.preview_controller.show_export_preview(tracks)
        preview = self.window._inline_preview
        self.addCleanup(self.window._finish_inline_preview)
        state = ProgressiveAnalysis(tracks, structure_enabled=False)
        plans = {}
        for count in (2, 3):
            for track in tracks[:count]:
                state.record_rhythm(track.id, _analysis(track.id, 120.0, 120.0))
            plans[count] = partial_plan(tracks, state, ENABLED)
        files = {}
        for name in ("p2", "p3", "final"):
            files[name] = Path(directory) / f"{name}.flac"
            files[name].touch()
        return preview, tracks, plans, files

    def _media_patches(self, preview):
        player = preview.media_player
        return (
            patch.object(player, "setSource"), patch.object(player, "setPosition"),
            patch.object(player, "play"), patch.object(player, "pause"),
            patch.object(player, "mediaStatus", return_value=QMediaPlayer.MediaStatus.LoadingMedia),
            patch.object(player, "isSeekable", return_value=False),
        )

    def _play_at(self, preview, seconds: float, playing: bool = True) -> None:
        preview._playing = playing
        preview._advancing_playhead = True  # position only, not a user seek
        try:
            preview.timeline.setValue(round(seconds * TIMELINE_SCALE))
        finally:
            preview._advancing_playhead = False
        preview._playhead_seconds = seconds

    def test_settings_dialog_shows_and_clears_the_automix_analysis_cache(self) -> None:
        with TemporaryDirectory(prefix="automix-cache-ui-") as directory:
            root = Path(directory)
            (root / "entry.json").write_text("{}" * 600)
            with patch("app.automix.cache.cache_directories", return_value=(root,)):
                dialog = SettingsDialog(
                    self.window.settings_service.current, self.window.translator.language,
                    self.window.theme_service.preference, self.window.translator, self.window,
                )
                try:
                    self.assertTrue(dialog.automix_cache_clear_button.isEnabled())
                    self.assertIn("1", dialog.automix_cache_usage_label.text())
                    dialog.automix_cache_clear_button.click()
                    self.assertFalse((root / "entry.json").exists())
                    self.assertFalse(dialog.automix_cache_clear_button.isEnabled())
                    self.assertIn("0.0 MB", dialog.automix_cache_usage_label.text())
                finally:
                    dialog.close()

    def test_new_lyrics_source_has_room_for_preview_context_lines(self) -> None:
        self.window._add_source(SourceType.LYRICS)
        source = self.window.store.selected
        self.assertIsNotNone(source)
        self.assertIs(source.source_type, SourceType.LYRICS)
        self.assertGreaterEqual(source.width, 600)
        self.assertGreaterEqual(source.height, 200)

    def test_preview_tab_embeds_canvas_controls_and_restores_editing_tab(self) -> None:
        track = PlaylistTrack(
            "preview.wav", "Preview", duration_seconds=10.0,
        )
        self.window.playlist_service.add_tracks([track])
        total_width = sum(self.window.main_splitter.sizes())
        self.window.main_splitter.setSizes([
            355, max(300, total_width - 735), 380,
        ])
        self.application.processEvents()
        expected_sidebar_width = self.window.main_splitter.sizes()[0]

        class StubPreview(QDialog):
            def __init__(self, *_args, **kwargs) -> None:
                super().__init__(kwargs.get("parent"))
                self.stopped = False
                self.controls_page = QWidget()
                self.track_list_panel = QFrame()
                self.preferred_backend = kwargs.get("preferred_backend")

            def build_embedded_controls_page(self) -> QWidget:
                return self.controls_page

            def _stop_preview(self) -> None:
                self.stopped = True

        with patch(
            "app.controllers.preview_controller.ExportPreviewDialog", StubPreview,
        ):
            self.window.preview_action.trigger()
            self.application.processEvents()
            self.assertFalse(self.window.left_workspace.isHidden())
            # The sidebar collapses over a 190 ms animation; under a loaded full
            # run a fixed 230 ms wait was sometimes too short.
            for _ in range(60):
                QTest.qWait(50)
                if self.window.left_workspace.isHidden():
                    break

        preview = self.window._inline_preview
        self.assertIsNotNone(preview)
        self.assertEqual(
            preview.preferred_backend,
            self.window._preview_backend_for_session,
        )
        self.assertIs(self.window.canvas_stack.currentWidget(), preview)
        self.assertEqual(self.window.bottom_tabs.currentIndex(), 2)
        self.assertIs(
            self.window.bottom_workspace_stack.currentWidget(),
            self.window.bottom_tabs,
        )
        self.assertIs(
            preview.controls_page.parentWidget(), self.window.preview_tab_page,
        )
        self.assertIs(
            preview.track_list_panel.parentWidget(),
            self.window.preview_track_inspector,
        )
        self.assertIs(
            self.window.inspector_stack.currentWidget(),
            self.window.preview_track_inspector,
        )
        self.assertFalse(self.window.canvas.isEnabled())
        self.assertTrue(self.window.left_workspace.isHidden())
        self.assertTrue(self.window.inspector_stack.isEnabled())
        self.assertTrue(self.window.bottom_tabs.isEnabled())
        self.assertTrue(self.window.toolbar.isEnabled())
        self.assertFalse(self.window.toolbar.isHidden())
        self.assertTrue(self.window.menuBar().isEnabled())
        self.assertTrue(self.window.help_action.isEnabled())
        self.assertTrue(self.window.help_menu.menuAction().isEnabled())
        self.assertTrue(self.window.file_menu.menuAction().isEnabled())
        self.assertTrue(self.window.open_action.isEnabled())
        self.assertFalse(self.window.acceptDrops())
        self.assertIsNone(
            self.window.toolbar.widgetForAction(self.window.preview_action),
        )

        with patch.object(self.window.canvas, "fit_artboard") as fit_artboard:
            self.window.bottom_tabs.setCurrentIndex(1)
            QTest.qWait(250)
        fit_artboard.assert_called_once_with()

        self.assertIsNone(self.window._inline_preview)
        self.assertEqual(self.window.bottom_tabs.currentIndex(), 1)
        self.assertIs(self.window.canvas_stack.currentWidget(), self.window.canvas)
        self.assertIs(
            self.window.bottom_workspace_stack.currentWidget(),
            self.window.bottom_tabs,
        )
        self.assertTrue(preview.stopped)
        self.assertTrue(self.window.canvas.isEnabled())
        self.assertFalse(self.window.left_workspace.isHidden())
        self.assertTrue(self.window.inspector.isEnabled())
        self.assertIs(
            self.window.inspector_stack.currentWidget(), self.window.inspector,
        )
        self.assertTrue(self.window.bottom_tabs.isEnabled())
        self.assertTrue(self.window.toolbar.isEnabled())
        self.assertTrue(self.window.menuBar().isEnabled())
        self.assertTrue(self.window.acceptDrops())
        self.assertAlmostEqual(
            self.window.main_splitter.sizes()[0], expected_sidebar_width, delta=3,
        )
        self.assertEqual(self.window._sidebar_open_width, expected_sidebar_width)

    def test_empty_preview_tab_returns_to_the_last_editing_tab(self) -> None:
        self.window.bottom_tabs.setCurrentIndex(1)
        with patch.object(QMessageBox, "warning") as warning:
            self.window.bottom_tabs.setCurrentIndex(2)

        warning.assert_called_once()
        self.assertEqual(self.window.bottom_tabs.currentIndex(), 1)
        self.assertIsNone(self.window._inline_preview)
        self.assertIs(
            self.window.inspector_stack.currentWidget(), self.window.inspector,
        )

    def test_gpu_session_prepares_opengl_before_main_window_is_shown(self) -> None:
        if (
            self.window._preview_backend_for_session == "gpu_layers"
            and GPU_TEXTURE_SURFACE_AVAILABLE
        ):
            self.assertIsNotNone(self.window._preview_gpu_composition_anchor)
            self.assertIs(
                self.window.canvas_stack.currentWidget(), self.window.canvas,
            )
        else:
            self.assertIsNone(self.window._preview_gpu_composition_anchor)

    def test_embedded_timeline_and_buttons_fit_after_resize_and_panel_drag(self) -> None:
        from PySide6.QtCore import QPoint
        from app.renderer.ffmpeg_renderer import FFmpegNotFoundError

        window = self.window
        window.resize(1366, 720)
        window.show()
        self.application.processEvents()
        original_minima = (
            window.main_splitter.minimumHeight(),
            window.bottom_workspace_stack.minimumHeight(),
        )
        track = PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)
        window.playlist_service.add_tracks([track])
        with (
            patch("app.controllers.preview_controller.FFmpegRenderer", side_effect=FFmpegNotFoundError("test")),
            patch.object(window, "check_ffmpeg_availability"),
        ):
            window.bottom_tabs.setCurrentIndex(2)
        QTest.qWait(230)
        preview = window._inline_preview
        self.assertIsNotNone(preview)

        def assert_controls_fit() -> None:
            for control in (
                preview.timeline, preview.play_button, preview.previous_button,
                preview.next_button, preview.rewind_button, preview.forward_button,
                preview.volume_slider, preview.preview_close_button,
            ):
                self.assertTrue(control.isVisible())
                child = control
                while child is not window:
                    parent = child.parentWidget()
                    self.assertIsNotNone(parent)
                    self.assertTrue(
                        parent.rect().contains(child.geometry()),
                        f"{control.objectName()}: {child.geometry()} clipped by {parent.objectName()} {parent.rect()}",
                    )
                    child = parent

        try:
            for width, height in ((1560, 920), (1366, 720), (1100, 680), (960, 520)):
                with self.subTest(size=(width, height)):
                    window.resize(width, height)
                    self.application.processEvents()
                    assert_controls_fit()
                    # A user dragging the divider cannot shrink the ruler away.
                    window.workspace_splitter.setSizes([10_000, 1])
                    self.application.processEvents()
                    assert_controls_fit()
            QTest.mouseClick(
                preview.timeline, Qt.MouseButton.LeftButton,
                pos=QPoint(preview.timeline.width() * 3 // 4, preview.timeline.height() // 2),
            )
            self.assertGreater(preview.timeline.value(), 0)
        finally:
            window.preview_controller.finish_inline_preview()
        self.assertEqual(
            (window.main_splitter.minimumHeight(), window.bottom_workspace_stack.minimumHeight()),
            original_minima,
        )

    def test_embedded_preview_keeps_complete_transport_controls(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )

        self.assertTrue(preview.embedded)
        self.assertEqual(preview.windowType(), Qt.WindowType.Widget)
        self.assertEqual(preview.minimumWidth(), 0)
        self.assertTrue(preview.play_button.isCheckable())
        self.assertIsNotNone(preview.timeline)
        self.assertIsNotNone(preview.previous_button)
        self.assertIsNotNone(preview.next_button)
        self.assertIsNotNone(preview.volume_slider)
        self.assertFalse(hasattr(preview, "quality_combo"))
        self.assertFalse(hasattr(preview, "quality_label"))
        self.assertFalse(hasattr(preview, "gpu_check"))
        self.assertIsNotNone(preview.button_box)
        self.assertTrue(preview.error_banner.isHidden())

        controls_page = preview.build_embedded_controls_page()
        self.assertEqual(preview.objectName(), "embeddedCanvasPreview")
        self.assertEqual(controls_page.objectName(), "embeddedPreviewControls")
        self.assertEqual(preview.dialog_title_label.text(), "캔버스 미리보기")
        self.assertTrue(preview.hint_label.isHidden())
        self.assertIs(preview.now_playing_card.parentWidget(), controls_page)
        self.assertIs(preview.timeline_card.parentWidget(), controls_page)
        self.assertIs(preview.transport_card.parentWidget(), controls_page)
        self.assertTrue(preview.button_box.isHidden())
        self.assertIs(
            preview.preview_close_button.parentWidget(), preview.transport_card,
        )
        self.assertEqual(preview.preview_close_button.text(), "편집으로 돌아가기")
        self.assertEqual(
            preview.preview_close_button.objectName(), "embeddedPreviewCloseButton",
        )
        preview.transport_card.resize(900, 56)
        self.application.processEvents()
        self.assertLessEqual(
            preview.rewind_button.width(), preview.forward_button.width() + 20,
        )
        self.assertEqual(preview.layout().indexOf(preview.now_playing_card), -1)
        self.assertGreaterEqual(controls_page.layout().indexOf(preview.timeline_card), 0)

        preview.gpu_preview_enabled = True
        preview._gpu_backend_failed("simulated context loss")
        self.assertFalse(preview.gpu_preview_enabled)
        self.assertEqual(preview.preview_stack.currentIndex(), 0)
        self.assertIn("simulated context loss", preview.preview_mode_label.toolTip())
        preview._stop_preview()
        controls_page.deleteLater()
        preview.deleteLater()

    def test_stopping_preview_closes_gpu_surface_before_deletion(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        surface = QWidget()
        preview.preview_stack.addWidget(surface)
        preview.gpu_surface = surface

        with patch.object(surface, "close", wraps=surface.close) as close:
            preview._stop_preview()
            close.assert_called_once_with()
        self.assertIsNone(preview.gpu_surface)
        preview.deleteLater()

    def test_preview_errors_are_visible_deduplicated_and_expandable(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )

        preview._preview_worker_failed("decoder failed at frame 42")
        self.assertFalse(preview.error_banner.isHidden())
        self.assertIn("처리하지 못했습니다", preview.error_title_label.text())
        self.assertEqual(preview.error_message_label.text(), "decoder failed at frame 42")
        self.assertEqual(len(preview._preview_error_signatures), 1)
        preview._preview_worker_failed("decoder failed at frame 42")
        self.assertEqual(len(preview._preview_error_signatures), 1)

        with patch.object(QMessageBox, "warning") as warning:
            preview._show_preview_error_details()
        warning.assert_called_once()
        self.assertIn("decoder failed at frame 42", warning.call_args.args[2])

        preview.error_dismiss_button.click()
        self.assertTrue(preview.error_banner.isHidden())
        preview._stop_preview()
        preview.deleteLater()

    def test_failed_video_probe_shows_path_in_preview_error(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )

        with patch.object(preview, "_schedule_refresh") as schedule:
            preview._store_video_duration("broken.mp4", 0.0)
        schedule.assert_called_once_with()
        self.assertFalse(preview.error_banner.isHidden())
        self.assertIn("broken.mp4", preview.error_message_label.text())
        preview._stop_preview()
        preview.deleteLater()

    def test_preview_uses_gpu_layers_by_default_when_available(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
        )

        self.assertEqual(preview.preferred_backend, "gpu_layers")
        self.assertEqual(
            preview.gpu_preview_enabled, GPU_TEXTURE_SURFACE_AVAILABLE,
        )
        self.assertFalse(hasattr(preview, "gpu_check"))
        preview._stop_preview()
        preview.deleteLater()

    def test_preview_track_navigator_highlights_and_jumps_to_track(self) -> None:
        tracks = [
            PlaylistTrack(
                "first.wav", "First", artist="Artist A",
                duration_seconds=10.0,
            ),
            PlaylistTrack(
                "second.wav", "Second", artist="Artist B",
                duration_seconds=8.0, start_time_seconds=20.0,
            ),
        ]
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, tracks, self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )

        self.assertEqual(preview.track_list.count(), 2)
        self.assertEqual(preview.track_list.currentRow(), 0)
        self.assertIn("First", preview.track_list.item(0).text())
        self.assertTrue(preview.performance_monitor.isHidden())
        preview.performance_action.trigger()
        self.assertFalse(preview.performance_monitor.isHidden())
        preview.performance_action.trigger()
        self.assertTrue(preview.performance_monitor.isHidden())
        self.assertFalse(hasattr(preview, "tracks_toggle"))
        self.assertFalse(preview.track_list.isHidden())
        self.assertEqual(preview.frame_rate_label.toolTip(), "")
        self.assertTrue(preview.performance_scale_label.text())
        # The preview renders below final resolution; tell the user the export
        # will be sharper so a soft preview is not mistaken for a soft export.
        scale_tip = preview.performance_scale_label.toolTip()
        self.assertTrue("또렷" in scale_tip or "sharper" in scale_tip)

        preview.track_list.setCurrentRow(1)
        preview.track_list.itemClicked.emit(preview.track_list.item(1))
        self.assertEqual(preview.track_list.currentRow(), 1)
        self.assertEqual(preview._highlighted_track_index, 0)
        monitor = preview.performance_monitor
        monitor.configure()
        monitor.set_field("fps_graph", False)
        self.assertTrue(monitor.fields["fps_graph"][2].isHidden())
        monitor.set_field("fps_graph", True)
        monitor.refresh()
        self.assertTrue(monitor.fields["fps_graph"][2].values)
        monitor.options.close()
        second = preview.track_list.item(1)
        preview.track_list.itemDoubleClicked.emit(second)
        self.application.processEvents()

        self.assertEqual(preview.timeline.value(), 2000)
        self.assertEqual(preview.track_list.currentRow(), 1)
        self.assertEqual(preview._highlighted_track_index, 1)
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_preview_submits_ordered_layers_without_flattening_frame(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        submissions: list[tuple[QSize, tuple[object, ...]]] = []

        class SurfaceStub:
            def set_layers(self, size: QSize, layers: object) -> None:
                submissions.append((QSize(size), tuple(layers)))

        preview.gpu_surface = SurfaceStub()
        preview.gpu_preview_enabled = True
        with patch.object(preview.preview_label, "set_image") as cpu_present:
            preview.refresh_preview()

        self.assertEqual(len(submissions), 1)
        size, layers = submissions[0]
        self.assertFalse(size.isEmpty())
        self.assertGreaterEqual(len(layers), 1)
        self.assertEqual(layers[0].key[0], "canvas-base")
        cpu_present.assert_not_called()
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_simple_video_color_effects_route_to_gpu_shader(self) -> None:
        video = Source(
            SourceType.VIDEO, "Shader video", video_paths=["missing.mp4"],
            image_fit_mode="stretch", border_radius=0.0, outline_width=0.0,
            brightness=14.0, contrast=8.0, video_saturation=1.4,
        )
        self.window.store.replace([video])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        preview.gpu_surface = MagicMock()
        preview.gpu_preview_enabled = True
        preview._refresh_source_partitions()

        first = preview._configure_gpu_video_color_filters(
            {video.id}, False, 0.0,
        )
        item = next(
            item for item in preview.scene.items()
            if isinstance(item, SourceItem) and item.source.id == video.id
        )
        self.assertTrue(item._video_gpu_color_filter)
        self.assertEqual(first, {})
        item._video_applied_gpu_color_filter = True
        filters = preview._configure_gpu_video_color_filters(
            {video.id}, False, 0.0,
        )

        self.assertEqual(filters[video.id].brightness, 14.0)
        self.assertEqual(filters[video.id].contrast, 8.0)
        self.assertEqual(filters[video.id].saturation, 1.4)
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_styled_video_falls_back_but_z_banded_video_uses_gpu_filter(self) -> None:
        video = Source(
            SourceType.VIDEO, "Styled video", video_paths=["missing.mp4"],
            image_fit_mode="stretch", border_radius=12.0, brightness=10.0,
        )
        self.window.store.replace([video])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        preview.gpu_surface = MagicMock()
        preview.gpu_preview_enabled = True
        preview._refresh_source_partitions()

        self.assertEqual(
            preview._configure_gpu_video_color_filters({video.id}, False, 0.0),
            {},
        )
        item = next(
            item for item in preview.scene.items()
            if isinstance(item, SourceItem) and item.source.id == video.id
        )
        self.assertFalse(item._video_gpu_color_filter)
        video.border_radius = 0.0
        item._video_applied_gpu_color_filter = True
        z_banded_filters = preview._configure_gpu_video_color_filters(
            {video.id}, True, 0.0,
        )
        self.assertEqual(z_banded_filters[video.id].brightness, 10.0)
        self.assertTrue(item._video_gpu_color_filter)
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_simple_video_frame_uses_a_direct_gpu_texture_layer(self) -> None:
        video = Source(
            SourceType.VIDEO, "Direct video", video_paths=["missing.mp4"],
            x=24.0, y=36.0, width=320.0, height=180.0, scale=1.25,
            rotation=7.0, opacity=0.8, z_index=4,
            image_fit_mode="stretch", border_radius=0.0, outline_width=0.0,
        )
        self.window.store.replace([video])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )
        preview.gpu_surface = MagicMock()
        preview.gpu_preview_enabled = True
        preview._refresh_source_partitions()
        item = next(
            item for item in preview.scene.items()
            if isinstance(item, SourceItem) and item.source.id == video.id
        )
        image = QImage(320, 180, QImage.Format.Format_RGBA8888)
        image.fill(QColor(20, 40, 60))
        item._video_presented_frame = image
        item._video_timeline_preview_active = True
        item._video_preview_suppressed = False

        self.assertIn(video.id, {cached.source.id for cached in preview._cached_video_items})
        self.assertTrue(item.video_preview_active)
        self.assertTrue(item.source.visible)
        self.assertEqual(item.source.image_fit_mode, "stretch")
        self.assertFalse(item.source.shadow.enabled)

        direct_ids, layers = preview._direct_gpu_video_layers(
            {video.id}, False, 0.0, {},
        )

        self.assertEqual(direct_ids, frozenset({video.id}))
        self.assertEqual(len(layers), 1)
        z_index, layer = layers[0]
        self.assertEqual(z_index, 4)
        self.assertEqual(layer.key, ("video-direct", video.id))
        self.assertEqual(layer.opacity, 0.8)
        self.assertEqual(layer.rotation, 7.0)
        self.assertEqual(layer.image.pixelColor(1, 1), QColor(20, 40, 60))
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_static_source_above_video_uses_cached_gpu_z_bands(self) -> None:
        video = Source(
            SourceType.VIDEO, "Lower video", video_paths=["missing.mp4"],
            x=20.0, y=20.0, width=160.0, height=90.0, z_index=0,
            image_fit_mode="stretch",
        )
        foreground = Source(
            SourceType.SHAPE, "Upper title plate", x=30.0, y=30.0,
            width=100.0, height=40.0, z_index=1,
        )
        self.window.store.replace([video, foreground])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )
        submissions: list[tuple[object, ...]] = []

        class SurfaceStub:
            frame_pending = False

            def set_layers(self, _size: QSize, layers: object) -> None:
                submissions.append(tuple(layers))

        preview.gpu_surface = SurfaceStub()
        preview.gpu_preview_enabled = True
        preview._refresh_source_partitions()
        item = next(
            item for item in preview.scene.items()
            if isinstance(item, SourceItem) and item.source.id == video.id
        )
        frame = QImage(160, 90, QImage.Format.Format_RGBA8888)
        frame.fill(QColor("#224466"))
        item._video_presented_frame = frame
        item._video_timeline_preview_active = True
        item._video_preview_suppressed = False
        original_capture = CanvasSnapshot.capture_track

        with (
            patch.object(preview, "_sync_video_sources"),
            patch.object(
                CanvasSnapshot, "capture_track", side_effect=original_capture,
            ) as capture,
        ):
            preview.refresh_preview()
            first_capture_count = capture.call_count
            preview.refresh_preview()
            submissions_after_duplicate_tick = len(submissions)
            item._video_presented_revision += 1
            preview.refresh_preview()

        self.assertEqual(first_capture_count, 2)
        self.assertEqual(capture.call_count, first_capture_count)
        self.assertEqual(submissions_after_duplicate_tick, 1)
        self.assertEqual(len(submissions), 2)
        keys = [layer.key for layer in submissions[-1]]
        self.assertEqual(keys[0][0], "canvas-base")
        self.assertEqual(keys[1], ("video-direct", video.id))
        self.assertEqual(keys[2][0], "video-z-foreground")
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_filter_cpu_fallback_preserves_color_adjustments(self) -> None:
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(100, 120, 140, 255))
        layer = GpuPreviewLayer(
            "filtered", image, QRectF(0, 0, 8, 8),
            color_filter=GpuColorFilter(brightness=10.0, contrast=20.0),
        )

        filtered = ExportPreviewDialog._cpu_fallback_layer_image(layer)

        color = filtered.pixelColor(4, 4)
        self.assertAlmostEqual(color.red(), 119, delta=1)
        self.assertAlmostEqual(color.green(), 143, delta=1)
        self.assertAlmostEqual(color.blue(), 167, delta=1)

    @unittest.skipIf(GpuTexturePreviewSurface is None, "OpenGL preview is unavailable")
    def test_gpu_filter_quad_uses_canvas_coordinates_and_top_left_uv(self) -> None:
        vertices = GpuTexturePreviewSurface._filtered_quad_vertices(
            QRectF(0, 0, 100, 50), QRect(0, 0, 200, 100), 0.0,
        )

        self.assertEqual(len(vertices), 16)
        self.assertEqual(vertices[:4], (-1.0, 1.0, 0.0, 1.0))
        self.assertEqual(vertices[-4:], (0.0, 0.0, 1.0, 0.0))

    def test_dynamic_canvas_source_keeps_z_order_below_static_source(self) -> None:
        dynamic = Source(
            SourceType.SHAPE, "Timed lower", x=20, y=20,
            width=120, height=90, fill_color="#EF4444",
            z_index=0, timeline_start=1.0,
        )
        static = Source(
            SourceType.SHAPE, "Static upper", x=20, y=20,
            width=120, height=90, fill_color="#2563EB", z_index=1,
        )
        self.window.store.replace([dynamic, static])
        track = PlaylistTrack("preview.wav", "Preview", duration_seconds=5.0)
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, [track], self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )

        preview.timeline.setValue(200)
        self.application.processEvents()

        self.assertTrue(
            preview._canvas_dynamic_requires_z_composition({dynamic.id}, 2.0)
        )
        sample = round(60 * preview._active_render_scale)
        self.assertEqual(
            preview._image.pixelColor(sample, sample).name().upper(), "#2563EB",
        )
        submissions: list[tuple[object, ...]] = []

        class SurfaceStub:
            frame_pending = False

            def set_layers(self, _size: QSize, layers: object) -> None:
                submissions.append(tuple(layers))

        preview.gpu_surface = SurfaceStub()
        preview.gpu_preview_enabled = True
        preview.refresh_preview()
        self.assertTrue(submissions)
        self.assertFalse(any(
            isinstance(layer.key, tuple) and layer.key[0] == "canvas-dynamic"
            for layer in submissions[-1]
        ))
        self.assertEqual(
            submissions[-1][0].image.pixelColor(sample, sample).name().upper(),
            "#2563EB",
        )
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_topmost_dynamic_canvas_source_keeps_fast_region_path(self) -> None:
        dynamic = Source(
            SourceType.SHAPE, "Timed upper", timeline_start=1.0, z_index=2,
        )
        static = Source(SourceType.SHAPE, "Static lower", z_index=1)
        self.window.store.replace([dynamic, static])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=5.0)],
            self.window.translator, parent=self.window,
            source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )
        preview._refresh_source_partitions()

        self.assertFalse(
            preview._canvas_dynamic_requires_z_composition({dynamic.id}, 2.0)
        )
        preview._stop_preview()
        preview.deleteLater()

    def test_paused_video_frame_completion_schedules_preview_refresh(self) -> None:
        video = Source(SourceType.VIDEO, "Paused clip", video_paths=["missing.mp4"])
        self.window.store.replace([video])
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        item = next(
            item for item in preview.scene.items()
            if isinstance(item, SourceItem) and item.source.id == video.id
        )

        with patch.object(preview, "_schedule_refresh") as schedule:
            item.video_frame_ready.emit()
            schedule.assert_called_once_with()
            schedule.reset_mock()
            preview._playing = True
            item.video_frame_ready.emit()
            schedule.assert_not_called()

        preview._playing = False
        preview._stop_preview()
        preview.deleteLater()

    def test_video_duration_probe_is_queued_without_blocking_preview(self) -> None:
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "clip.mp4"
            second_path = Path(directory) / "second.mp4"
            video_path.touch()
            second_path.touch()
            video = Source(
                SourceType.VIDEO, "Async probe",
                video_paths=[str(video_path), str(second_path)],
            )
            self.window.store.replace([video])
            with (
                patch.object(VideoDurationProbeWorker, "start") as start,
                patch.object(PlaylistService, "_probe_duration") as probe,
            ):
                preview = ExportPreviewDialog(
                    self.window.canvas.scene_model,
                    [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
                    self.window.translator,
                    parent=self.window,
                    source_store=self.window.store,
                    embedded=True,
                    preferred_backend="cpu",
                )

            probe.assert_not_called()
            start.assert_called_once_with()
            self.assertIsNotNone(preview._video_probe_worker)
            assert preview._video_probe_worker is not None
            self.assertEqual(preview._video_probe_worker.path, str(video_path))
            self.assertEqual(preview._video_probe_queue, [str(second_path)])
            self.assertNotIn(str(video_path), preview._video_duration_cache)

            with patch.object(preview, "_schedule_refresh") as schedule:
                preview._store_video_duration(str(video_path), 4.25)
                schedule.assert_called_once_with()
            self.assertEqual(preview._video_duration_cache[str(video_path)], 4.25)

            preview._stop_preview()
            preview.deleteLater()

    def test_video_duration_probe_worker_runs_off_the_ui_thread(self) -> None:
        ui_thread = threading.get_ident()
        probe_threads: list[int] = []

        def probe(_path: Path) -> float:
            probe_threads.append(threading.get_ident())
            return 3.5

        worker = VideoDurationProbeWorker("clip.mp4")
        with patch.object(PlaylistService, "_probe_duration", side_effect=probe):
            worker.start()
            self.assertTrue(worker.wait(3000))

        self.assertEqual(len(probe_threads), 1)
        self.assertNotEqual(probe_threads[0], ui_thread)
        worker.deleteLater()

    def test_stop_preview_detaches_a_slow_worker_without_blocking_the_ui_thread(
        self,
    ) -> None:
        """FFprobe can block a worker's run() for seconds with no way to
        interrupt it early; leaving Preview must never wait that out on the
        UI thread (see the freeze this fixed)."""
        import time

        release = threading.Event()

        def slow_probe(_path: Path) -> float:
            release.wait(timeout=3.0)
            return 3.5

        worker = VideoDurationProbeWorker("clip.mp4")
        try:
            with patch.object(PlaylistService, "_probe_duration", side_effect=slow_probe):
                worker.start()
                time.sleep(0.05)
                self.assertTrue(worker.isRunning(), "worker did not even start")
                started = time.perf_counter()
                ExportPreviewDialog._finish_or_detach_worker(worker)
                elapsed = time.perf_counter() - started
                self.assertTrue(worker.isRunning())
                self.assertLess(
                    elapsed, 0.5,
                    "detaching a still-running worker must not block the caller",
                )
        finally:
            release.set()
            worker.wait(3000)

    def test_preview_proxy_replaces_decoder_path_but_preserves_export_source(self) -> None:
        with TemporaryDirectory(prefix="preview-proxy-routing-") as raw_directory:
            directory = Path(raw_directory)
            original = directory / "original.mp4"
            proxy_path = directory / "proxy.mp4"
            original.write_bytes(b"original")
            proxy_path.write_bytes(b"proxy")
            video = Source(
                SourceType.VIDEO, "Proxy routed video",
                video_paths=[str(original)], video_repeat_mode="loop_one",
            )
            self.window.store.replace([video])
            preview = ExportPreviewDialog(
                self.window.canvas.scene_model,
                [PlaylistTrack(
                    "preview.wav", "Preview", duration_seconds=10.0,
                )],
                self.window.translator,
                parent=self.window, source_store=self.window.store,
                embedded=True, preferred_backend="cpu",
            )
            preview._refresh_source_partitions()
            preview._video_duration_cache[str(original)] = 4.0
            preview._video_proxy_paths[str(original.resolve())] = str(proxy_path)
            item = preview._cached_video_items[0]
            preview.timeline.blockSignals(True)
            preview.timeline.setValue(125)
            preview.timeline.blockSignals(False)

            with patch.object(item, "set_video_preview_position") as position:
                preview._sync_video_sources(
                    preview.tracks[0], 1.25, track_start=0.0,
                )

            self.assertEqual(position.call_args.args[:2], (str(proxy_path), 1.25))
            self.assertEqual(video.video_paths, [str(original)])
            preview._stop_preview()
            preview.deleteLater()

    def test_ready_preview_proxy_is_reused_without_starting_worker(self) -> None:
        with TemporaryDirectory(prefix="preview-proxy-cache-hit-") as raw_directory:
            directory = Path(raw_directory)
            original = directory / "original.mp4"
            ffmpeg = directory / "ffmpeg.exe"
            original.write_bytes(b"large-original")
            ffmpeg.touch()
            cache = PreviewProxyCache(directory / "cache")
            cached = cache.proxy_path(original)
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"cached-proxy")
            preview = ExportPreviewDialog(
                self.window.canvas.scene_model,
                [PlaylistTrack(
                    "preview.wav", "Preview", duration_seconds=10.0,
                )],
                self.window.translator,
                parent=self.window, source_store=self.window.store,
                embedded=True, preferred_backend="cpu",
            )
            preview._preview_proxy_ffmpeg = ffmpeg
            preview._preview_proxy_cache = cache

            preview._request_video_proxies([str(original)])

            self.assertEqual(
                preview._preview_video_path(str(original)), str(cached),
            )
            self.assertIsNone(preview._video_proxy_worker)
            self.assertEqual(preview._video_proxy_queue, [])
            preview._stop_preview()
            preview.deleteLater()

    def test_preview_prefetches_video_metadata_for_upcoming_tracks_once(self) -> None:
        source = Source(
            SourceType.VIDEO, "Per-track", video_timing_mode="track",
        )
        tracks = [
            PlaylistTrack(
                f"song-{index}.wav", f"Song {index}", duration_seconds=2.0,
                video_paths=[f"clip-{index}.mp4"],
            )
            for index in range(4)
        ]
        request = MagicMock()
        preview = SimpleNamespace(
            _last_video_prefetch_track_index=-1,
            _cached_video_items=(SimpleNamespace(source=source),),
            tracks=tracks,
            _request_video_durations=request,
        )

        ExportPreviewDialog._prefetch_upcoming_video_durations(preview, 0)
        ExportPreviewDialog._prefetch_upcoming_video_durations(preview, 0)

        request.assert_called_once_with([
            "clip-0.mp4", "clip-1.mp4", "clip-2.mp4",
        ])

    def test_inactive_video_and_running_decoder_commands_are_idempotent(self) -> None:
        source = Source(SourceType.VIDEO, "Video")
        item = SourceItem(source)
        item.release_video_decoder()

        class FakePlayer:
            def __init__(self) -> None:
                self.state = QMediaPlayer.PlaybackState.StoppedState
                self.stop_calls = 0
                self.play_calls = 0
                self.rate_calls = 0

            def playbackState(self):  # type: ignore[no-untyped-def]
                return self.state

            def stop(self) -> None:
                self.stop_calls += 1
                self.state = QMediaPlayer.PlaybackState.StoppedState

            def play(self) -> None:
                self.play_calls += 1
                self.state = QMediaPlayer.PlaybackState.PlayingState

            def pause(self) -> None:
                self.state = QMediaPlayer.PlaybackState.PausedState

            def setPlaybackRate(self, _rate: float) -> None:
                self.rate_calls += 1

        player = FakePlayer()
        item._video_player = player  # type: ignore[assignment]
        try:
            item.set_video_preview_position(None)
            item.set_video_preview_position(None)
            self.assertEqual(player.stop_calls, 1)

            item._video_timeline_preview_active = True
            item.set_video_preview_playback(True, 1.25)
            item.set_video_preview_playback(True, 1.25)
            self.assertEqual(player.play_calls, 1)
            self.assertEqual(player.rate_calls, 1)
        finally:
            item._video_player = None
            item.release_video_decoder()

    def test_visualizer_worker_uses_original_analysis_index_for_active_subset(self) -> None:
        first = VisualizerOverlay(0, 0, 32, 20, "bars", "#FFFFFF")
        second = VisualizerOverlay(0, 0, 32, 20, "wave", "#FFFFFF")
        analysis = {
            "levels": [[1.0]],
            "waveform": [[2.0]],
            "processed": ([[11.0]], [[22.0]]),
            "meters": (None, None),
        }
        worker = OverlayFrameWorker(
            "track", 30, 1, 0, 1, (second,), (1,), analysis,
        )
        emitted: list[tuple[int, ...]] = []
        worker.ready.connect(
            lambda _track, _fps, _generation, signature, _frames:
            emitted.append(signature)
        )
        image = QImage(2, 2, QImage.Format.Format_ARGB32)
        image.fill(QColor("#FFFFFF"))
        with patch(
            "app.dialogs.export_preview_dialog.PythonVisualizerRenderer.preview_image",
            return_value=image,
        ) as render:
            worker.run()

        self.assertEqual(render.call_args.args[3], [22.0])
        self.assertEqual(emitted, [(1,)])
        worker.deleteLater()

    def test_visualizer_cache_separates_equal_count_active_effect_sets(self) -> None:
        track = PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, [track], self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )
        first = QImage(2, 2, QImage.Format.Format_ARGB32)
        first.fill(QColor("#FF0000"))
        second = QImage(2, 2, QImage.Format.Format_ARGB32)
        second.fill(QColor("#0000FF"))
        preview._overlay_cache.store(track.id, (0,), [(5, (first,))])
        preview._overlay_cache.store(track.id, (1,), [(5, (second,))])

        first_layers = preview._overlay_layers_for_frame(track.id, 5, (0,))
        second_layers = preview._overlay_layers_for_frame(track.id, 5, (1,))
        missing_layers = preview._overlay_layers_for_frame(track.id, 6, (2,))

        self.assertEqual(first_layers[0].pixelColor(0, 0), QColor("#FF0000"))
        self.assertEqual(second_layers[0].pixelColor(0, 0), QColor("#0000FF"))
        self.assertEqual(missing_layers, ())
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_z_band_composition_preserves_canvas_layer_order(self) -> None:
        track = PlaylistTrack(
            "preview.wav", "Preview", duration_seconds=10.0,
        )
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, [track], self.window.translator,
            parent=self.window, source_store=self.window.store, embedded=True,
            preferred_backend="cpu",
        )
        preview._base_image = QImage(64, 36, QImage.Format.Format_RGBA8888)
        preview._base_image.fill(QColor("#101820"))
        overlay = VisualizerOverlay(
            4, 3, 12, 8, "bars", "#FFFFFF", z_index=0.5, rotation=15,
        )
        overlay_image = QImage(12, 8, QImage.Format.Format_RGBA8888)
        overlay_image.fill(QColor(255, 255, 255, 128))

        def captured(*_args: object, **kwargs: object) -> QImage:
            image = QImage(64, 36, QImage.Format.Format_RGBA8888)
            image.fill(
                QColor(0, 0, 0, 0)
                if kwargs.get("transparent") else QColor("#101820")
            )
            return image

        with (
            patch.object(CanvasSnapshot, "z_bands", return_value=[(None, 1.0), (1.0, None)]),
            patch.object(CanvasSnapshot, "capture_track", side_effect=captured),
        ):
            layers = preview._gpu_z_band_layers(
                track, 0, 0.0, 0.0, None, 1.0, 0.0,
                [overlay], (overlay_image,),
            )

        self.assertEqual(layers[0].key[0], "z-base")
        self.assertEqual(layers[1].key, ("audio-overlay", 0))
        self.assertEqual(layers[2].key[0], "z-foreground")
        self.assertEqual(layers[1].rotation, 15)
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_audio_layers_wait_for_a_complete_async_overlay_frame(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        overlays = [
            VisualizerOverlay(0, 0, 16, 8, "bars", "#FFFFFF"),
            VisualizerOverlay(20, 0, 16, 8, "wave", "#FFFFFF"),
        ]
        image = QImage(16, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#FFFFFF"))

        self.assertEqual(preview._gpu_audio_layers(overlays, ()), [])
        self.assertEqual(preview._gpu_audio_layers(overlays, (image,)), [])
        self.assertEqual(
            len(preview._gpu_audio_layers(overlays, (image, image))), 2,
        )
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_preview_defers_render_while_previous_frame_is_pending(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )

        class BusySurfaceStub:
            frame_pending = True

        preview.gpu_surface = BusySurfaceStub()
        preview.gpu_preview_enabled = True
        with patch.object(CanvasSnapshot, "capture_track") as capture:
            preview.refresh_preview()

        capture.assert_not_called()
        self.assertTrue(preview._gpu_refresh_deferred)
        with patch.object(preview, "_schedule_refresh") as schedule:
            preview._gpu_frame_presented()
        schedule.assert_called_once()
        self.assertFalse(preview._gpu_refresh_deferred)
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_preview_adapts_render_scale_without_changing_base_quality(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )

        class Stats:
            dropped_pending_frames = 0
            texture_uploads = 0
            texture_reuses = 0
            uploaded_bytes = 0
            texture_evictions = 0
            cached_textures = 0
            allocated_bytes = 0
            texture_budget_bytes = 256 * 1024 * 1024

        class SurfaceStub:
            upload_stats = Stats()

        class ClockStub:
            def elapsed(self) -> int:
                return 600

            def restart(self) -> None:
                pass

        selected_scale = preview.preview_render_scale
        preview.gpu_surface = SurfaceStub()
        preview.gpu_preview_enabled = True
        preview._playing = True
        preview._frame_stats_clock = ClockStub()
        preview._base_image = QImage(64, 36, QImage.Format.Format_RGBA8888)
        for _ in range(2):
            preview._presented_frames = 5
            preview._record_presented_frame()

        self.assertEqual(preview.preview_render_scale, selected_scale)
        self.assertLess(preview._active_render_scale, selected_scale)
        self.assertTrue(preview._base_image.isNull())
        self.assertIn("렌더 85%", preview.performance_scale_label.text())
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_gpu_watchdog_retries_once_then_falls_back_after_second_stall(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("preview.wav", "Preview", duration_seconds=10.0)],
            self.window.translator,
            parent=self.window,
            source_store=self.window.store,
            embedded=True,
            preferred_backend="cpu",
        )
        surface = MagicMock()
        preview.gpu_surface = surface
        preview.gpu_preview_enabled = True
        preview._gpu_health.frame_queued(1.0)

        with (
            patch.object(preview, "isVisible", return_value=True),
            patch.object(preview, "_gpu_backend_failed") as fallback,
        ):
            preview._gpu_watchdog_timeout()
            surface.update.assert_called_once()
            fallback.assert_not_called()
            self.assertEqual(preview._gpu_health.stats.total_stalls, 1)

            preview._gpu_watchdog_timeout()
            fallback.assert_called_once()
            self.assertIn("timed out twice", fallback.call_args.args[0])

        preview._gpu_watchdog.stop()
        preview.gpu_surface = None
        preview.gpu_preview_enabled = False
        preview._stop_preview()
        preview.deleteLater()

    def test_automix_preview_takes_over_from_background_analysis_and_hands_it_back(self) -> None:
        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=100.0)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        controller = self.window.automix_analysis_controller
        with (
            patch.object(self.window.preview_controller, "_prepare_blended_preview_audio",
                         return_value=(None, None, None)),
            patch.object(controller, "cancel") as cancel,
        ):
            self.window.preview_controller.show_export_preview(tracks)
        cancel.assert_called_once()
        self.assertFalse(self.window._automix_analysis_timer.isActive())  # no restart mid-preview
        self.window._finish_inline_preview()
        self.assertTrue(self.window._automix_analysis_timer.isActive())  # background resumes

    def _loading_popups(self) -> list:
        return [widget for widget in QApplication.topLevelWidgets()
                if widget.objectName() == "previewLoadingDialog" and widget.isVisible()]

    def _pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def test_a_loading_popup_covers_preview_setup_and_automix_saved_analysis(self) -> None:
        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=100.0), PlaylistTrack("b.wav", "B", duration_seconds=90.0)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")

        def fake_start(controller, *_args, **_options):
            controller.initial_loading = True  # saved analyses still landing

        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)))
            with patch.object(ProgressiveAutoMixController, "start", fake_start):
                self.window.preview_controller.show_export_preview(tracks)
            self.addCleanup(self.window._finish_inline_preview)
            (popup,) = self._loading_popups()
            self._pump(0.3)
            self.assertTrue(popup.isVisible())  # Preview is up, the saved analyses are not
            self.window._inline_preview._blended_audio_controller.initial_load_done.emit()
            self._pump(0.3)
            self.assertEqual(self._loading_popups(), [])

    def test_the_loading_popup_closes_by_itself_without_a_mix(self) -> None:
        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=100.0)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="none")
        self.window.preview_controller.show_export_preview(tracks)
        self.addCleanup(self.window._finish_inline_preview)
        self._pump(0.3)
        self.assertEqual(self._loading_popups(), [])

    def test_preview_hands_the_automatic_automix_settings_to_the_mix(self) -> None:
        from app.automix.settings import AUTOMIX_SETTINGS

        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=100.0), PlaylistTrack("b.wav", "B", duration_seconds=90.0)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        received = {}

        def fake_prepare(_tracks, _executable, _mode, _seconds, automix_settings=None):
            received["settings"] = automix_settings
            return None, None, None

        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)))
            with (
                patch.object(self.window.preview_controller, "_prepare_blended_preview_audio", fake_prepare),
                patch.object(PreviewAudioController, "start") as fallback_start,
            ):
                self.window.preview_controller.show_export_preview(tracks)
            self.addCleanup(self.window._finish_inline_preview)
        self.assertIs(received["settings"], AUTOMIX_SETTINGS)
        self.assertIs(fallback_start.call_args.kwargs["automix_settings"], AUTOMIX_SETTINGS)

    def test_automix_analysis_is_skipped_when_project_setting_is_off(self) -> None:
        self.window.playlist_service.replace([
            PlaylistTrack("a.mp3", "A", duration_seconds=30.0),
        ])
        with patch.object(self.window.automix_analysis_controller, "start") as start:
            self.window._maybe_start_automix_analysis()
        start.assert_not_called()

    def test_automix_analysis_is_skipped_when_transition_mode_is_crossfade(self) -> None:
        self.window.project_settings = replace(self.window.project_settings, transition_mode="crossfade")
        self.window.playlist_service.replace([
            PlaylistTrack("a.mp3", "A", duration_seconds=30.0),
        ])
        with patch.object(self.window.automix_analysis_controller, "start") as start:
            self.window._maybe_start_automix_analysis()
        start.assert_not_called()

    def test_automix_analysis_starts_when_enabled_with_tracks_and_ffmpeg(self) -> None:
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        self.window.playlist_service.replace([
            PlaylistTrack("a.mp3", "A", duration_seconds=30.0),
        ])
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "ffmpeg.exe"
            executable.touch()
            self.window.settings_service.save(
                replace(self.window.settings_service.current, ffmpeg_path=str(executable)),
            )
            with patch.object(self.window.automix_analysis_controller, "start") as start:
                self.window._maybe_start_automix_analysis()
            start.assert_called_once()
            called_tracks = start.call_args[0][0]
            self.assertEqual([track.title for track in called_tracks], ["A"])
            # "auto": the same analyzer policy Preview/Export rendering uses,
            # so the two paths can never disagree on which analyzer produced
            # a given result.
            self.assertEqual(start.call_args.kwargs.get("provider_id"), "auto")

    def test_export_custom_gpu_defaults_reveal_advanced_settings(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(video_codec="h264_nvenc"), 1, 60.0,
            self.window.translator, Path("gpu-export.mp4"),
        )
        try:
            self.assertEqual(dialog.quality_mode_combo.currentData(), "custom")
            self.assertTrue(dialog.advanced_check.isChecked())
            self.assertFalse(dialog.advanced_group.isHidden())
            self.assertIn("GPU", dialog.quality_description_label.text())
        finally:
            dialog.close()

    def test_custom_track_cover_drives_cover_and_ambient_rendering(self) -> None:
        with TemporaryDirectory(prefix="playlist-render-cover-") as raw_directory:
            cover_path = Path(raw_directory) / "render-cover.png"
            image = QImage(96, 96, QImage.Format.Format_ARGB32)
            image.fill(QColor("#16A34A"))
            self.assertTrue(image.save(str(cover_path)))

            cover = extract_track_cover("missing-audio.mp3", cover_path)
            self.assertFalse(cover.isNull())
            self.assertEqual(cover.toImage().pixelColor(20, 20), QColor("#16A34A"))
            ambient = create_cached_ambient_background(
                "missing-audio.mp3", 320, 180, 24.0, cover_path,
            )
            self.assertEqual(ambient.size(), QSize(320, 180))

    def test_ambient_album_background_flows_over_time(self) -> None:
        from PySide6.QtGui import QPainter
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.album_art import AMBIENT_FLOW_HZ
        from app.preview.canvas_snapshot import CanvasSnapshot

        with TemporaryDirectory(prefix="playlist-ambient-flow-") as directory:
            cover_path = Path(directory) / "flow-cover.png"
            art = QImage(120, 120, QImage.Format.Format_ARGB32)
            art.fill(QColor("#20308A"))
            painter = QPainter(art)
            painter.fillRect(0, 0, 60, 120, QColor("#E8532A"))
            painter.fillRect(60, 0, 60, 120, QColor("#2EC7A0"))
            painter.end()
            self.assertTrue(art.save(str(cover_path)))

            frame_a = create_cached_ambient_background(
                "missing.mp3", 240, 135, 24.0, cover_path, phase=0.0,
            )
            frame_b = create_cached_ambient_background(
                "missing.mp3", 240, 135, 24.0, cover_path, phase=6.0,
            )
            self.assertEqual(frame_a.size(), QSize(240, 135))
            self.assertNotEqual(frame_a.toImage(), frame_b.toImage())
            # Sub-step phase changes land in the same cached flow frame.
            near = create_cached_ambient_background(
                "missing.mp3", 240, 135, 24.0, cover_path,
                phase=1.0 / AMBIENT_FLOW_HZ / 4.0,
            )
            self.assertEqual(frame_a.toImage(), near.toImage())

            scene = CanvasScene()
            background = Source(
                SourceType.BACKGROUND, "BG", width=1280, height=720, z_index=-20,
                background_mode="album_art", background_ambient=True,
            )
            scene.addItem(SourceItem(background))
            track = PlaylistTrack(
                "missing.wav", "Track", duration_seconds=60.0,
                cover_path=str(cover_path),
            )
            captured_start = CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=0.0,
            )
            captured_later = CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.0,
            )
            self.assertNotEqual(captured_start, captured_later)

    def test_album_background_cross_fades_between_tracks(self) -> None:
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot

        with TemporaryDirectory(prefix="playlist-bg-fade-") as directory:
            first_cover = Path(directory) / "first.png"
            second_cover = Path(directory) / "second.png"
            green = QImage(64, 64, QImage.Format.Format_ARGB32)
            green.fill(QColor("#12B886"))
            red = QImage(64, 64, QImage.Format.Format_ARGB32)
            red.fill(QColor("#E03131"))
            self.assertTrue(green.save(str(first_cover)))
            self.assertTrue(red.save(str(second_cover)))

            scene = CanvasScene()
            background = Source(
                SourceType.BACKGROUND, "BG", width=320, height=180, z_index=-20,
                background_mode="album_art", background_ambient=False,
                background_track_transition=True,
                background_track_transition_seconds=1.0,
            )
            scene.addItem(SourceItem(background))
            tracks = [
                PlaylistTrack("a.wav", "A", duration_seconds=30.0,
                              cover_path=str(first_cover)),
                PlaylistTrack("b.wav", "B", duration_seconds=30.0,
                              cover_path=str(second_cover)),
            ]

            def capture(track_number, elapsed):
                return CanvasSnapshot.capture_track(
                    scene, tracks[track_number - 1], track_number, 2, 0.0,
                    elapsed_seconds=elapsed, playlist_tracks=tracks,
                )

            fade_start = capture(2, 0.0)     # blend ~ previous track's artwork
            fade_mid = capture(2, 0.5)       # a blend of both covers
            fade_done = capture(2, 1.5)      # past the window: only track B

            self.assertNotEqual(fade_start, fade_mid)
            self.assertNotEqual(fade_mid, fade_done)
            # The first track has no previous cover, so it never cross-fades.
            self.assertEqual(capture(1, 0.0), capture(1, 5.0))

            background.background_track_transition = False
            no_transition = capture(2, 0.0)
            self.assertEqual(no_transition, fade_done)
            self.assertNotEqual(no_transition, fade_start)

    def test_album_background_crossfade_is_dynamic_only_during_preview_transition(self) -> None:
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem

        scene = CanvasScene()
        background = Source(
            SourceType.BACKGROUND, "BG", width=320, height=180,
            background_mode="album_art",
            background_track_transition=True,
            background_track_transition_seconds=0.8,
        )
        scene.addItem(SourceItem(background))
        preview = ExportPreviewDialog(
            scene,
            [
                PlaylistTrack("a.wav", "A", duration_seconds=3.0),
                PlaylistTrack("b.wav", "B", duration_seconds=3.0),
            ],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._refresh_source_partitions()
            self.assertNotIn(
                background.id,
                preview._canvas_dynamic_source_ids(0, 0.2, None),
            )
            self.assertIn(
                background.id,
                preview._canvas_dynamic_source_ids(1, 0.4, None),
            )
            self.assertNotIn(
                background.id,
                preview._canvas_dynamic_source_ids(1, 1.0, None),
            )
        finally:
            preview.close()

    def test_preview_starts_blended_audio_render_for_non_none_transition_mode(self) -> None:
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            tracks = [
                PlaylistTrack("a.wav", "A", duration_seconds=3.0),
                PlaylistTrack("b.wav", "B", duration_seconds=3.0),
            ]
            with patch(
                "app.dialogs.export_preview_dialog.PreviewAudioController.start",
            ) as start:
                preview = ExportPreviewDialog(
                    self.window.canvas.scene_model, tracks, self.window.translator,
                    ffmpeg_executable=fake_ffmpeg, parent=self.window,
                    preferred_backend="cpu",
                    transition_mode="crossfade", crossfade_seconds=5.0,
                )
            try:
                self.assertIsNotNone(preview._blended_audio_controller)
                start.assert_called_once()
                called_tracks, _output_directory, mode, seconds = start.call_args.args
                self.assertEqual(called_tracks, tracks)
                self.assertEqual(mode, "crossfade")
                self.assertEqual(seconds, 5.0)
            finally:
                preview._stop_preview()
                preview.deleteLater()

    def test_preview_skips_blended_audio_render_for_none_transition_mode(self) -> None:
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            preview = ExportPreviewDialog(
                self.window.canvas.scene_model,
                [PlaylistTrack("a.wav", "A", duration_seconds=3.0)],
                self.window.translator,
                ffmpeg_executable=fake_ffmpeg, parent=self.window,
                preferred_backend="cpu",
                transition_mode="none",
            )
            try:
                self.assertIsNone(preview._blended_audio_controller)
            finally:
                preview._stop_preview()
                preview.deleteLater()

    def test_blended_audio_ready_swaps_source_and_defers_seek_until_seekable(self) -> None:
        """A source swap must not seek/play until the new source is actually
        ready -- QMediaPlayer.setSource() returns before loading finishes, so
        an immediate setPosition()/play() can be silently dropped or reset to
        0 once loading completes (see the near-end AutoMix transition
        regression tests below)."""
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [
                PlaylistTrack("a.wav", "A", duration_seconds=3.0),
                PlaylistTrack("b.wav", "B", duration_seconds=3.0),
            ],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            preview.timeline.setValue(round(4.0 * TIMELINE_SCALE))
            with (
                patch.object(preview.media_player, "setSource") as set_source,
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play") as play,
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), preview._compiled_plan)
                self.assertEqual(preview._blended_audio_path, Path("blended.m4a").resolve())
                self.assertEqual(preview._active_track_index, _BLENDED_AUDIO_TRACK_INDEX)
                set_source.assert_called_once_with(
                    QUrl.fromLocalFile(str(Path("blended.m4a").resolve()))
                )
                # Not seekable yet: the seek/play must be deferred, not fired blind.
                set_position.assert_not_called()
                play.assert_not_called()
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_pending_media_seek_applies_once_source_becomes_seekable(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("a.wav", "A", duration_seconds=3.0),
             PlaylistTrack("b.wav", "B", duration_seconds=3.0)],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            preview.timeline.setValue(round(2.7 * TIMELINE_SCALE))
            with (
                patch.object(preview.media_player, "setSource"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), preview._compiled_plan)
            self.assertEqual(preview._pending_media_seek_ms, 2_700)
            with (
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play") as play,
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadedMedia),
                patch.object(preview.media_player, "isSeekable", return_value=True),
            ):
                preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_called_once_with(2_700)
            play.assert_called_once()
            self.assertIsNone(preview._pending_media_seek_ms)
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_pending_media_seek_paused_does_not_auto_play(self) -> None:
        """If the user paused while the new source was still loading, the
        transport intent at the moment it becomes ready must be respected --
        not the playing state captured back when the swap started."""
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("a.wav", "A", duration_seconds=3.0),
             PlaylistTrack("b.wav", "B", duration_seconds=3.0)],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            preview.timeline.setValue(round(2.7 * TIMELINE_SCALE))
            with (
                patch.object(preview.media_player, "setSource"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), preview._compiled_plan)
            preview._playing = False  # user paused while the swap was still loading
            with (
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play") as play,
                patch.object(preview.media_player, "pause") as pause,
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadedMedia),
                patch.object(preview.media_player, "isSeekable", return_value=True),
            ):
                preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_called_once_with(2_700)
            play.assert_not_called()
            pause.assert_called_once()
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_pending_media_seek_uses_latest_playhead_after_reseek_during_load(self) -> None:
        """A seek that lands while the new source is still loading must not be
        lost: the freshest global playhead at ready-time wins, not the
        position captured when the swap first started."""
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("a.wav", "A", duration_seconds=3.0),
             PlaylistTrack("b.wav", "B", duration_seconds=3.0)],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            with (
                patch.object(preview.media_player, "setSource") as set_source,
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview.timeline.setValue(round(1.5 * TIMELINE_SCALE))
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), preview._compiled_plan)
                # The user seeks again while the blended source is still loading.
                preview.timeline.setValue(round(1.7 * TIMELINE_SCALE))
            self.assertEqual(preview._pending_media_seek_ms, 1_700)
            # One setSource for the initial per-track load, one for the blended
            # swap -- but no extra call for the reseek while it was loading,
            # since the target source (the blended mix) hadn't changed.
            self.assertEqual(set_source.call_count, 2)
            with (
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadedMedia),
                patch.object(preview.media_player, "isSeekable", return_value=True),
            ):
                preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_called_once_with(1_700)
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_stale_source_callback_after_a_newer_swap_has_no_effect(self) -> None:
        """A late mediaStatusChanged from a source that has since been
        replaced (generation mismatch) must be ignored."""
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("a.wav", "A", duration_seconds=3.0),
             PlaylistTrack("b.wav", "B", duration_seconds=3.0)],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            with (
                patch.object(preview.media_player, "setSource"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview.timeline.setValue(round(0.5 * TIMELINE_SCALE))  # Source A begins loading.
            stale_generation = preview._pending_media_generation
            with patch.object(preview.media_player, "setSource"):
                preview._skip_track(1)  # Source B replaces it before A finished.
            self.assertNotEqual(preview._pending_media_generation, stale_generation)
            with (
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadedMedia),
                patch.object(preview.media_player, "isSeekable", return_value=True),
            ):
                # Source A's late callback fires with the stale generation.
                preview._pending_media_generation = stale_generation
                preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_not_called()
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_near_end_automix_transition_does_not_jump_to_zero_when_blended_audio_becomes_ready(self) -> None:
        """The exact bug report: seek near the end of a track/AutoMix
        transition, then let the blended-audio background render finish --
        the global timeline must stay at the seeked position, never jump to 0."""
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [PlaylistTrack("a.wav", "A", duration_seconds=180.0),
             PlaylistTrack("b.wav", "B", duration_seconds=180.0)],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._playing = True
            preview.timeline.setValue(round(165.4 * TIMELINE_SCALE))
            with (
                patch.object(preview.media_player, "setSource"),
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), preview._compiled_plan)
            # Still loading: no premature seek to whatever QMediaPlayer's
            # transient starting position is, and the global playhead is untouched.
            set_position.assert_not_called()
            self.assertAlmostEqual(preview._playhead_seconds, 165.4, places=2)
            with (
                patch.object(preview.media_player, "setPosition") as set_position,
                patch.object(preview.media_player, "play"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadedMedia),
                patch.object(preview.media_player, "isSeekable", return_value=True),
            ):
                preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            set_position.assert_called_once_with(165_400)
            self.assertAlmostEqual(preview._playhead_seconds, 165.4, places=2)
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_advance_playback_skips_track_change_restart_when_blended_audio_active(self) -> None:
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model,
            [
                PlaylistTrack("a.wav", "A", duration_seconds=3.0),
                PlaylistTrack("b.wav", "B", duration_seconds=3.0),
            ],
            self.window.translator,
            parent=self.window,
            preferred_backend="cpu",
        )
        try:
            preview._blended_audio_path = Path("blended.m4a")
            preview._active_track_index = _BLENDED_AUDIO_TRACK_INDEX
            preview._playing = True
            preview._playhead_seconds = 2.9
            preview._last_media_position_ms = 2_900_000  # force the drift branch to run
            with (
                patch.object(preview.play_clock, "restart", return_value=200),
                patch.object(preview.media_player, "position", return_value=3_100),
                patch.object(preview, "_start_audio_at_playhead") as start_audio,
            ):
                preview._advance_playback()
            start_audio.assert_not_called()
            # Absolute player position (3.1s), not track-relative, drives drift correction.
            self.assertAlmostEqual(preview._playhead_seconds, 3.1, places=3)
        finally:
            preview._stop_preview()
            preview.deleteLater()

    # -- AutoMix Preview preparation dialog + timeline ownership --------

    def test_timeline_track_ownership_is_non_overlapping_despite_automix_audio_overlap(self) -> None:
        """The bug report: AutoMix audio clips legitimately overlap (0-100 /
        90-180), but the timeline's track *ownership* regions must not --
        they must come from MetadataPlan.chapters (0-90 / 90-180), not the
        raw PresentationWindow/AudioRenderClip end."""
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, [track_a, track_b], self.window.translator,
            parent=self.window, preferred_backend="cpu",
        )
        try:
            preview._compiled_plan = self._automix_plan(track_a, track_b)
            schedule = preview._build_track_schedule()
            self.assertEqual(
                [(track.id, start, end) for _index, track, start, end in schedule],
                [(track_a.id, 0.0, 90.0), (track_b.id, 90.0, 180.0)],
            )
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_transition_display_regions_computes_geometry_from_audio_render_plan(self) -> None:
        transitions = (
            AudioRenderTransition(
                clip_a="a", clip_b="b", timeline_start=90.0, duration=10.0, type=TransitionType.AUTOMIX,
            ),
            AudioRenderTransition(
                clip_a="b", clip_b="c", timeline_start=40.0, duration=3.0, type=TransitionType.CROSSFADE,
            ),
            AudioRenderTransition(clip_a="c", clip_b="d", timeline_start=10.0, duration=1.0),  # CUT
        )
        regions = _transition_display_regions(transitions)
        # Sorted by start; the plain CUT transition (no real blend) is excluded.
        self.assertEqual(
            regions,
            ((40.0, 43.0, TransitionType.CROSSFADE), (90.0, 100.0, TransitionType.AUTOMIX)),
        )

    def test_on_blended_audio_ready_updates_the_timeline_widgets_own_schedule(self) -> None:
        """Regression: the slider widget caches its own copy of the schedule
        for paintEvent; _on_blended_audio_ready must push the final,
        non-overlapping schedule and transition overlay into it too, not
        just onto the dialog's own _track_schedule attribute."""
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        preview = ExportPreviewDialog(
            self.window.canvas.scene_model, [track_a, track_b], self.window.translator,
            parent=self.window, preferred_backend="cpu",
        )
        try:
            plan = self._automix_plan(track_a, track_b)
            with (
                patch.object(preview.media_player, "setSource"),
                patch.object(preview.media_player, "mediaStatus",
                              return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                patch.object(preview.media_player, "isSeekable", return_value=False),
            ):
                preview._on_blended_audio_ready(str(Path("blended.m4a").resolve()), plan)
            self.assertEqual(
                [(track.id, start, end) for _index, track, start, end in preview.timeline.schedule],
                [(track_a.id, 0.0, 90.0), (track_b.id, 90.0, 180.0)],
            )
            self.assertEqual(
                preview.timeline._transition_regions,
                ((90.0, 100.0, TransitionType.AUTOMIX),),
            )
        finally:
            preview._stop_preview()
            preview.deleteLater()

    def test_preparation_dialog_is_not_shown_for_sequential_transition_mode(self) -> None:
        tracks = [PlaylistTrack("a.wav", "A", duration_seconds=3.0)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="none")
        with patch.object(
            self.window.preview_controller, "_prepare_blended_preview_audio",
        ) as prepare:
            self.window.preview_controller.show_export_preview(tracks)
        prepare.assert_not_called()
        try:
            self.assertIsNone(self.window._inline_preview._blended_audio_controller)
        finally:
            self.window._finish_inline_preview()

    def test_a_mix_ready_during_start_opens_preview_already_on_the_final_plan(self) -> None:
        """A render that finishes inside start() (e.g. everything cached):
        Preview opens with the final AutoMix plan/timeline from the first
        frame -- no sequential-then-AutoMix jump, and no waiting dialog."""
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        tracks = [track_a, track_b]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(
                replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)),
            )
            plan = self._automix_plan(track_a, track_b)
            blended_path = Path(directory) / "blended.m4a"
            blended_path.touch()

            def fake_start(self, _tracks, _directory, _mode, _seconds, **_options):
                self.audio_ready.emit(str(blended_path), plan)

            with patch.object(ProgressiveAutoMixController, "start", fake_start):
                self.window.preview_controller.show_export_preview(tracks)
            try:
                preview = self.window._inline_preview
                self.assertIsNotNone(preview)
                self.assertEqual(preview._blended_audio_path, blended_path)
                self.assertIsNone(preview._blended_audio_controller)
                self.assertEqual(
                    [(track.id, start, end) for _index, track, start, end in preview._track_schedule],
                    [(track_a.id, 0.0, 90.0), (track_b.id, 90.0, 180.0)],
                )
                self.assertEqual(preview.timeline.maximum(), round(180.0 * TIMELINE_SCALE))
            finally:
                self.window._finish_inline_preview()

    def test_preview_opens_at_once_adopts_the_running_controller_and_preserves_playhead(self) -> None:
        """No waiting dialog: Preview opens immediately on the sequential plan,
        using the SAME still-running controller (no duplicate render). The
        hot-swap machinery then takes over once that controller's render
        actually finishes, and the user's playhead survives the swap."""
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        tracks = [track_a, track_b]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(
                replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)),
            )

            with patch.object(ProgressiveAutoMixController, "start") as start:
                self.window.preview_controller.show_export_preview(tracks)
                start.assert_called_once()
            try:
                preview = self.window._inline_preview
                self.assertIsNotNone(preview)
                self.assertIsNone(preview._blended_audio_path)
                self.assertIsNotNone(preview._blended_audio_controller)
                # The adopted controller was not restarted a second time.
                start.assert_called_once()

                # The background render shows up in the bottom status-bar progress.
                activity = self.window.activity_progress
                self.assertIn("preview_mix", activity.active_keys)
                preview._blended_audio_controller.progress.emit("Combining mix", 0.4, "Combining mix 12.0s / 30.0s")
                self.assertEqual(activity.progress_bar.value(), 400)
                self.assertIn("Combining mix 12.0s / 30.0s", activity.details_text())

                # Seek near the end and start playing, then simulate the
                # adopted controller's render finishing -- same regression
                # as the near-end AutoMix transition hot-swap fix.
                preview._playing = True
                preview.timeline.setValue(round(85.0 * TIMELINE_SCALE))
                plan = self._automix_plan(track_a, track_b)
                blended_path = Path(directory) / "blended.m4a"
                blended_path.touch()
                with (
                    patch.object(preview.media_player, "setSource"),
                    patch.object(preview.media_player, "setPosition") as set_position,
                    patch.object(preview.media_player, "play"),
                    patch.object(preview.media_player, "mediaStatus",
                                  return_value=QMediaPlayer.MediaStatus.LoadingMedia),
                    patch.object(preview.media_player, "isSeekable", return_value=False),
                ):
                    preview._blended_audio_controller.audio_ready.emit(str(blended_path), plan)
                set_position.assert_not_called()  # deferred until seekable, per the hot-swap fix
                self.assertAlmostEqual(preview._playhead_seconds, 85.0, places=2)
                self.assertNotIn("preview_mix", activity.active_keys)
                # A coalesced progress update that lands after the mix is ready
                # must not bring the finished task back (it came back titled
                # with its raw key, "preview_mix", stuck at 100 %).
                preview._blended_audio_controller.progress.emit("AutoMix", 1.0, "AutoMix ready")
                self.assertNotIn("preview_mix", activity.active_keys)
            finally:
                self.window._finish_inline_preview()

    def test_partial_mixes_and_progress_reach_the_open_preview_and_status_bar(self) -> None:
        """Preview is already open when the first AutoMix partial lands: it swaps
        onto that partial, and the render's progress shows in the status bar."""
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        tracks = [track_a, track_b]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        plan = self._automix_plan(track_a, track_b)
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(
                replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)),
            )
            partial = Path(directory) / "partial.flac"
            partial.touch()

            with patch.object(ProgressiveAutoMixController, "start", autospec=True) as start:
                self.window.preview_controller.show_export_preview(tracks)
            try:
                preview = self.window._inline_preview
                self.assertIsNone(preview._blended_audio_path)  # opened at once, per-track audio
                controller = start.call_args.args[0]
                controller.progress.emit("AutoMix", 0.4, "Analyzing 2 / 5")
                controller.latest_partial = (str(partial), plan, 150.0, 0.5)  # as _on_partial_ready does
                controller.progressive_ready.emit(*controller.latest_partial)
                self.assertIs(preview._compiled_plan, plan)
                self.assertEqual(preview._blended_audio_path, partial)
                self.assertEqual(preview._blended_audio_until, 150.0)
                self.assertEqual(preview._per_track_gain, 0.5)
                self.assertIs(preview._blended_audio_controller, start.call_args.args[0])
                start.assert_called_once()  # adopted, not restarted
                activity = self.window.activity_progress
                self.assertEqual(activity.active_keys, ("preview_mix",))  # Preview itself is no task
                self.assertEqual(activity.progress_bar.value(), 400)
                self.assertIn("Analyzing 2 / 5", activity.details_text())
            finally:
                self.window._finish_inline_preview()

    def test_progressive_partial_mix_swaps_in_and_per_track_audio_plays_after_it(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            self.assertTrue(preview._progressive)
            controller = preview._blended_audio_controller
            first = plans[2].audio.transitions[0]
            covered = plans[2].audio.clips[1].timeline_end
            patches = self._media_patches(preview)
            with patches[0] as set_source, patches[1], patches[2], patches[3], patches[4], patches[5]:
                self._play_at(preview, 5.0)
                controller.progressive_ready.emit(str(files["p2"]), plans[2], covered, 0.5)
                self.assertIs(preview._compiled_plan, plans[2])
                self.assertEqual(preview._blended_audio_until, covered)
                self.assertEqual(preview._per_track_gain, 0.5)
                self.assertEqual(Path(set_source.call_args.args[0].toLocalFile()), files["p2"])
                # The seek waits for the new source; it targets exactly the kept playhead.
                self.assertEqual(preview._pending_media_seek_ms, 5000)
                self.assertAlmostEqual(preview._playhead_seconds, 5.0)
                self.assertEqual(preview.timeline.value(), round(5.0 * TIMELINE_SCALE))
                self.assertEqual(preview.progressive_swap_count, 1)

                # Past the partial mix, the sequential tail is per-track audio at the mix's level.
                with patch.object(preview._live_mix, "sync") as live_sync:
                    self._play_at(preview, covered + 1.0)
                    preview._start_audio_at_playhead()
                self.assertEqual(preview._active_track_index, _LIVE_MIX_TRACK_INDEX)
                self.assertIs(live_sync.call_args.args[0], plans[2])
                self.assertAlmostEqual(live_sync.call_args.args[1], covered + 1.0)
                self.assertAlmostEqual(live_sync.call_args.args[3],
                                       preview.volume_slider.value() / 100.0 * 0.5, places=3)
            self.assertLess(first.timeline_start, covered)

    def test_progressive_swap_waits_while_a_transition_is_sounding(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            first = plans[2].audio.transitions[0]
            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p2"], plans[2], plans[2].audio.clips[1].timeline_end)
                preview._media_source_ready = True
                self._play_at(preview, first.timeline_start + first.duration / 2)
                controller.progressive_ready.emit(str(files["p3"]), plans[3], plans[3].audio.clips[2].timeline_end, 0.5)
                self.assertIs(preview._compiled_plan, plans[2])  # the sounding A->B keeps its audio
                self.assertIsNotNone(preview._pending_swap)
                self._play_at(preview, first.timeline_start + first.duration + 1.0)
                preview._try_apply_pending_swap()
                self.assertIs(preview._compiled_plan, plans[3])  # B->C arrives at a solo stretch

    def test_a_mix_arriving_mid_swap_waits_for_the_previous_source_to_load(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            patches = self._media_patches(preview)
            with patches[0] as set_source, patches[1], patches[2], patches[3], patches[4] as status, patches[5] as seekable:
                self._play_at(preview, 5.0)
                controller.progressive_ready.emit(str(files["p2"]), plans[2], plans[2].audio.clips[1].timeline_end, 0.5)
                self.assertFalse(preview._media_source_ready)
                controller.progressive_ready.emit(str(files["p3"]), plans[3], plans[3].audio.clips[2].timeline_end, 0.5)
                self.assertIs(preview._compiled_plan, plans[2])  # previous swap still loading
                status.return_value = QMediaPlayer.MediaStatus.LoadedMedia
                seekable.return_value = True
                preview._resume_pending_media_seek()
                self.assertIs(preview._compiled_plan, plans[3])
                self.assertEqual(Path(set_source.call_args.args[0].toLocalFile()), files["p3"])

    def test_user_seek_applies_a_mix_that_was_unsafe_at_the_old_position(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            from app.automix.progressive import divergence_seconds

            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p2"], plans[2], plans[2].audio.clips[1].timeline_end)
                preview._media_source_ready = True
                self._play_at(preview, divergence_seconds(plans[2], plans[3]) + 3.0)  # listener outran analysis
                controller.progressive_ready.emit(str(files["p3"]), plans[3], plans[3].audio.clips[2].timeline_end, 0.5)
                self.assertIs(preview._compiled_plan, plans[2])
                preview.timeline.setValue(round(10.0 * TIMELINE_SCALE))  # user seek
                self.assertIs(preview._compiled_plan, plans[3])
                self.assertAlmostEqual(preview._playhead_seconds, 10.0)

    def test_final_mix_takes_over_a_listener_who_outran_analysis_at_the_same_music(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p2"], plans[2], plans[2].audio.clips[1].timeline_end)
                preview._media_source_ready = True
                self._play_at(preview, plans[2].audio.clips[2].timeline_start + 30.0)  # 30 s into sequential C
                controller.audio_ready.emit(str(files["final"]), plans[3])
                self.assertEqual(preview._blended_audio_path, files["final"])
                c = plans[3].audio.clips[2]
                self.assertAlmostEqual(c.source_in + (preview._playhead_seconds - c.timeline_start) * c.playback_rate,
                                       30.0)  # still 30 s into C, on the final timeline

    def test_paused_preview_takes_a_mix_even_inside_a_transition(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            first = plans[2].audio.transitions[0]
            patches = self._media_patches(preview)
            with patches[0] as set_source, patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p2"], plans[2], plans[2].audio.clips[1].timeline_end)
                self._play_at(preview, first.timeline_start + 1.0, playing=False)
                controller.progressive_ready.emit(str(files["p3"]), plans[3], plans[3].audio.clips[2].timeline_end, 0.5)
                self.assertIs(preview._compiled_plan, plans[3])
                set_source.assert_not_called()  # the new source loads on the next Play
                self.assertAlmostEqual(preview._playhead_seconds, first.timeline_start + 1.0)

    def test_final_mix_waits_for_a_safe_moment_and_is_not_displaced_by_a_late_partial(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            controller = preview._blended_audio_controller
            first = plans[3].audio.transitions[0]
            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p3"], plans[3], plans[3].audio.clips[2].timeline_end)
                preview._media_source_ready = True
                self._play_at(preview, first.timeline_start + 1.0)
                controller.audio_ready.emit(str(files["final"]), plans[3])
                controller.progressive_ready.emit(str(files["p2"]), plans[2], 1.0, 0.5)
                self.assertEqual(preview._pending_swap[0], files["final"])
                self._play_at(preview, first.timeline_start + first.duration + 1.0)
                preview._try_apply_pending_swap()
                self.assertEqual(preview._blended_audio_path, files["final"])
                self.assertEqual(preview._blended_audio_until, math.inf)

    def test_automix_details_follow_the_playing_plan_from_provisional_to_final(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, tracks, plans, files = self._open_progressive_preview(directory)
            panel = preview.automix_details
            self.assertIsNotNone(panel)
            self.assertIn(panel, preview.track_list_panel.findChildren(type(panel)))
            self.assertEqual(panel._state[0], "waiting")
            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["p2"], plans[2], plans[2].audio.clips[1].timeline_end)
                self.assertEqual(panel._state, ("provisional", 2))
                self.assertEqual(len(panel.rows), len(tracks) - 1)
                first = plans[2].audio.transitions[0]
                preview.timeline.setValue(round((first.timeline_start + 0.5) * TIMELINE_SCALE))
                self.assertEqual(panel.current_index, 0)
                preview._apply_blended_audio(files["final"], plans[3])
                self.assertEqual(panel._state, ("final", None))
                # With C analysed, the final plan beat-matches B -> C (its length is the scorer's call).
                self.assertIn(("strategy", "beat_match"), plans[3].audio.transitions[1].details)
                # "Open window": the drawn view follows the same plan and seeks Preview.
                panel.open_window_button.click()
                window = preview._transition_window
                self.assertTrue(window.isVisible())
                self.assertEqual(len(window.junctions), len(tracks) - 1)
                window._user_select(1)
                window.jump_button.click()
                self.assertAlmostEqual(preview.timeline.value() / TIMELINE_SCALE,
                                       window.junctions[1].start, places=2)
                panel.open_window_button.click()
                self.assertIs(preview._transition_window, window)  # reused, not duplicated

    def test_transition_loop_restarts_audio_after_the_preview_frame_returns(self) -> None:
        with TemporaryDirectory(prefix="playlist-progressive-") as directory:
            preview, _tracks, plans, files = self._open_progressive_preview(directory)
            patches = self._media_patches(preview)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                preview._apply_blended_audio(files["final"], plans[3])
                preview._open_transition_window()
                window = preview._transition_window
                window._user_select(0)
                window.loop_check.setChecked(True)
                window.set_playing(True)
                junction = window.junction
                with patch.object(preview, "_start_audio_at_playhead") as restart:
                    for cycle in range(50):
                        self._play_at(preview, junction.end + 2.1)
                        # Multiple frame updates must still queue only one seek.
                        window.set_playhead(junction.end + 2.2)
                        self.assertEqual(restart.call_count, cycle)
                        self.application.processEvents()
                        self.assertEqual(restart.call_count, cycle + 1)
                        self.assertAlmostEqual(preview._playhead_seconds, junction.start - 4, places=2)
                        self.assertFalse(window._loop_seek_timer.isActive())

    def test_background_mix_progress_clears_on_failure_or_when_preview_closes(self) -> None:
        track_a = PlaylistTrack("a.wav", "A", duration_seconds=100.0)
        track_b = PlaylistTrack("b.wav", "B", duration_seconds=90.0)
        self.window.playlist_service.replace([track_a, track_b])
        self.window.project_settings = replace(self.window.project_settings, transition_mode="automix")
        with TemporaryDirectory(prefix="playlist-fake-ffmpeg-") as directory:
            fake_ffmpeg = Path(directory) / "ffmpeg.exe"
            fake_ffmpeg.touch()
            self.window.settings_service.save(
                replace(self.window.settings_service.current, ffmpeg_path=str(fake_ffmpeg)),
            )

            activity = self.window.activity_progress
            for outcome in ("failed", "closed"):
                with self.subTest(outcome):
                    with patch.object(ProgressiveAutoMixController, "start"):
                        self.window.preview_controller.show_export_preview([track_a, track_b])
                    self.assertIn("preview_mix", activity.active_keys)
                    if outcome == "failed":
                        self.window._inline_preview._blended_audio_controller.audio_failed.emit("boom")
                        self.assertNotIn("preview_mix", activity.active_keys)
                    self.window._finish_inline_preview()
                    self.assertNotIn("preview_mix", activity.active_keys)

    def test_track_lyrics_dialog_previews_audio_with_synchronized_lyrics(self) -> None:
        saved_volumes: list[int] = []
        volume_reader = patch(
            "app.dialogs.track_details_dialog.preview_volume", return_value=37,
        )
        volume_writer = patch(
            "app.dialogs.track_details_dialog.save_preview_volume",
            side_effect=lambda value: saved_volumes.append(value) or value,
        )
        volume_reader.start()
        volume_writer.start()
        self.addCleanup(volume_writer.stop)
        self.addCleanup(volume_reader.stop)
        with TemporaryDirectory(prefix="playlist-track-preview-") as raw_directory:
            audio_path = Path(raw_directory) / "preview.wav"
            audio_path.touch()
            track = PlaylistTrack(
                str(audio_path), "Preview", duration_seconds=12.0,
                lyrics=[
                    {"start": 2.0, "end": 4.0, "text": "First lyric"},
                    {"start": 6.0, "end": 8.0, "text": "Second lyric"},
                ],
            )
            dialog = TrackDetailsDialog(track, self.window.translator, self.window)
            try:
                self.assertLessEqual(dialog.width(), 900)
                self.assertLessEqual(dialog.height(), 640)
                self.assertEqual((dialog.minimumWidth(), dialog.minimumHeight()), (780, 540))
                self.assertTrue(dialog.play_button.isEnabled())
                self.assertTrue(dialog.playback_slider.isEnabled())
                self.assertGreaterEqual(dialog.playback_slider.minimumWidth(), 220)
                self.assertGreaterEqual(dialog.playback_slider.minimumHeight(), 30)
                self.assertEqual(dialog.lyrics_preview_layout.spacing(), 3)
                self.assertEqual(
                    dialog.lyrics_preview_layout.indexOf(dialog.current_lyric), 1,
                )
                self.assertEqual(dialog.volume_slider.value(), 37)
                self.assertAlmostEqual(dialog.audio_output.volume(), 0.37, places=2)
                self.assertFalse(dialog.lyrics_group.isAncestorOf(dialog.playback_group))
                self.assertEqual(
                    Path(dialog.media_player.source().toLocalFile()), audio_path.resolve(),
                )

                dialog.volume_slider.setValue(64)
                self.assertEqual(saved_volumes, [64])
                self.assertAlmostEqual(dialog.audio_output.volume(), 0.64, places=2)
                self.assertEqual(dialog.volume_value.text(), "64%")

                korean = self.window.translator.language is Language.KOREAN
                dialog._playback_state_changed(
                    dialog.media_player.PlaybackState.PlayingState
                )
                self.assertEqual(dialog.play_button.text(), "일시정지" if korean else "Pause")
                dialog._playback_state_changed(
                    dialog.media_player.PlaybackState.PausedState
                )
                self.assertEqual(dialog.play_button.text(), "재생" if korean else "Play")

                dialog._playback_position_changed(0)
                self.assertIn("00:02.000", dialog.current_lyric.text())
                self.assertEqual(dialog.next_lyric.text(), "First lyric")

                dialog._playback_position_changed(2_000)
                self.assertEqual(dialog.current_lyric.text(), "First lyric")
                self.assertEqual(dialog.next_lyric.text(), "Second lyric")

                dialog._playback_position_changed(6_500)
                self.assertEqual(dialog.previous_lyric.text(), "First lyric")
                self.assertEqual(dialog.current_lyric.text(), "Second lyric")
                self.assertEqual(dialog.playback_slider.value(), 6_500)
                self.assertIn("00:06", dialog.playback_time.text())
            finally:
                dialog.reject()
                self.application.processEvents()
            self.assertEqual(
                dialog.media_player.playbackState(),
                dialog.media_player.PlaybackState.StoppedState,
            )

    def test_dynamic_export_streams_base_and_transparent_z_bands(self) -> None:
        track = PlaylistTrack(
            file_path="streamed-dynamic.mp3",
            title="Streamed dynamic",
            duration_seconds=0.5,
        )
        self.window.playlist_service.replace([track])
        particle = Source(SourceType.PARTICLE_OVERLAY, "Particles", z_index=1.0)
        self.window.store.add(particle)
        self.window.store.add(Source(SourceType.TEXT, "Foreground", z_index=2.0))
        captured_worker_arguments: list[tuple[object, ...]] = []
        active_encoder_count = 0
        maximum_active_encoder_count = 0

        class SignalStub:
            def connect(self, _callback: object) -> None:
                pass

        class WorkerStub:
            def __init__(self, *arguments: object, **keywords: object) -> None:
                captured_worker_arguments.append(arguments)
                self.progress = SignalStub()
                self.succeeded = SignalStub()
                self.failed = SignalStub()
                self.cancelled = SignalStub()
                self.finished = SignalStub()

            def start(self) -> None:
                pass

            def cancel(self) -> None:
                pass

            def isRunning(self) -> bool:
                return False

            def deleteLater(self) -> None:
                pass

        class EncoderStub:
            def __init__(
                self, _executable: object, output_path: Path, fps: int,
                *, preserve_alpha: bool = False, **_kwargs: object,
            ) -> None:
                nonlocal active_encoder_count, maximum_active_encoder_count
                self.output_path = output_path
                self.fps = fps
                self.preserve_alpha = preserve_alpha
                self.width = 4
                self.height = 4
                self.durations: list[float] = []
                self.active = True
                active_encoder_count += 1
                maximum_active_encoder_count = max(
                    maximum_active_encoder_count, active_encoder_count,
                )

            def submit(self, image: QImage, duration: float) -> None:
                self.width = image.width()
                self.height = image.height()
                self.durations.append(duration)

            def finish(self) -> StaticVideoStreamResult:
                nonlocal active_encoder_count
                self.output_path.touch()
                if self.active:
                    self.active = False
                    active_encoder_count -= 1
                return StaticVideoStreamResult(
                    self.output_path,
                    sum(self.durations),
                    self.width,
                    self.height,
                    self.fps,
                    self.preserve_alpha,
                    max(1, len(self.durations)),
                    1,
                )

            def cancel(self) -> None:
                nonlocal active_encoder_count
                if self.active:
                    self.active = False
                    active_encoder_count -= 1

        overlay = VisualizerOverlay(
            0, 0, 4, 4, "noise", "#FFFFFF",
            kind="particles", z_index=1.0,
        )

        def capture_layer(*_arguments: object, **state: object) -> QImage:
            transparent = bool(state.get("transparent"))
            image = QImage(
                4, 4,
                QImage.Format.Format_ARGB32_Premultiplied
                if transparent else QImage.Format.Format_RGB32,
            )
            image.fill(QColor(255, 0, 0, 128) if transparent else QColor("#123456"))
            return image

        try:
            with (
                patch("app.controllers.export_controller.FFmpegRenderer") as renderer_type,
                patch("app.controllers.export_controller.RenderWorker", WorkerStub),
                patch("app.preview.export_session.StaticVideoStreamEncoder", EncoderStub),
                # This test covers the intermediate-file path.
                patch("app.preview.export_plan.piped_export_supported",
                      return_value=False),
                patch("app.controllers.export_controller.ExportSettingsDialog.exec",
                      return_value=QDialog.DialogCode.Accepted),
                patch.object(CanvasSnapshot, "z_bands",
                             return_value=[
                                 (None, 1.0), (1.0, 2.0), (2.0, None),
                             ]),
                patch.object(
                    CanvasSnapshot, "split_mixed_capture_bands",
                    return_value=[
                        (None, 1.0), (1.0, 2.0), (2.0, None),
                    ],
                ),
                patch.object(CanvasSnapshot, "capture_track", side_effect=capture_layer),
                patch.object(
                    self.window, "_stage_export_frame",
                    wraps=self.window._stage_export_frame,
                ) as png_stage,
                patch.object(self.window, "_export_visualizers", return_value=[overlay]),
                patch.object(QMessageBox, "critical") as critical_message,
            ):
                renderer_type.return_value.ensure_encoder_available.return_value = None
                self.window._export_video()

            critical_message.assert_not_called()
            # The unchanged base is represented by one lossless PNG instead of
            # expanding it into a full-length CPU-only CFR intermediate video.
            self.assertEqual(png_stage.call_count, 1)
            self.assertEqual(len(captured_worker_arguments), 1)
            arguments = captured_worker_arguments[0]
            self.assertIsInstance(arguments[1], list)
            self.assertEqual(arguments[5], [overlay])
            self.assertEqual(len(arguments[6]), 2)
            self.assertIsInstance(arguments[6][0], PreparedStaticOverlayLayer)
            self.assertEqual(arguments[6][0].z_index, 1.0)
            self.assertEqual(arguments[6][1].z_index, 2.0)
            self.assertEqual(self.window.export_orchestrator.frames.index, 1)
            assert self.window._export_dialog is not None
            self.assertEqual(self.window._export_dialog.progress_bar.value(), 25)
            self.assertIn("3/3", self.window._export_dialog.detail_label.text())
            self.assertIn("100%", self.window._export_dialog.detail_label.text())
            self.assertEqual(maximum_active_encoder_count, 2)
            self.assertEqual(active_encoder_count, 0)
        finally:
            if self.window._export_dialog is not None:
                self.window._export_dialog.complete(False)
                self.window._export_dialog = None
            self.window._export_finished()
            self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
