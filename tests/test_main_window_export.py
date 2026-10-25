"""Main window export: settings, staging, progress, notifications and FFmpeg setup."""

from __future__ import annotations

from dataclasses import replace
import os
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QCloseEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QScrollArea
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.preset_dialog import DesignPresetDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.dialogs.export_progress_dialog import ExportEtaEstimator, ExportProgressDialog
from app.dialogs.export_complete_dialog import ExportCompleteDialog
from app.services.export_storage_service import ExportStorageSnapshot, estimate_export_storage
from app.dialogs.ffmpeg_install_progress_dialog import FFmpegInstallProgressDialog
from app.ffmpeg.managed_installer import FFmpegReleaseOption, ManagedFFmpegInstallation
from app.services.app_settings_service import AppSettings
from app.services.playlist_service import PlaylistService
from app.ui.main_window import MainWindow
from app.preview.canvas_snapshot import CanvasSnapshot
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer,
    FFmpegNotFoundError,
    PipedVideoInput,
    PreparedVideoInput,
    RenderResult,
    RenderError,
    RenderFrame,
    RenderSettings,
    WORK_MODE_AUTO,
    WORK_MODE_MAX_SPEED,
    WORK_MODE_STABLE,
)
from app.services.video_encoder_service import (
    AUTO_VIDEO_ENCODER,
    CPU_H264_ENCODER,
    NVIDIA_H264_ENCODER,
    VideoEncoderAdvisor,
)
from app.renderer.static_video_stream import DirectVideoEncodingProfile, StaticVideoStreamResult
from tests.main_window_base import MainWindowTestCase


class MainWindowExportTests(MainWindowTestCase):
    def test_close_during_export_preparation_cancels_then_resumes_close(self) -> None:
        preparation_cancel = threading.Event()
        dialog = MagicMock()

        def request_cancel() -> bool:
            preparation_cancel.set()
            return True

        dialog.request_cancel.side_effect = request_cancel
        self.window._export_preparation_cancel = preparation_cancel
        self.window._export_dialog = dialog
        event = QCloseEvent()
        try:
            self.window.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertTrue(preparation_cancel.is_set())
            self.assertTrue(self.window._close_after_export_cancel)
            with patch.object(QTimer, "singleShot") as single_shot:
                self.window._resume_close_after_export_cancel()
            self.assertFalse(self.window._close_after_export_cancel)
            single_shot.assert_called_once()
            self.assertEqual(single_shot.call_args.args[0], 0)
        finally:
            self.window._export_preparation_cancel = None
            self.window._export_dialog = None
            self.window._close_after_export_cancel = False

    def test_close_during_render_requests_cancel_and_defers_shutdown(self) -> None:
        worker = MagicMock()
        worker.isRunning.return_value = True
        self.window._render_worker = worker
        self.window._export_dialog = None
        event = QCloseEvent()
        try:
            self.window.closeEvent(event)

            self.assertFalse(event.isAccepted())
            worker.cancel.assert_called_once()
            self.assertTrue(self.window._close_after_export_cancel)
        finally:
            self.window._render_worker = None
            self.window._close_after_export_cancel = False

    def test_close_while_export_is_already_cancelling_still_defers_shutdown(self) -> None:
        worker = MagicMock()
        worker.isRunning.return_value = True
        dialog = MagicMock()
        dialog.is_cancelling = True
        self.window._render_worker = worker
        self.window._export_dialog = dialog
        event = QCloseEvent()
        try:
            self.window.closeEvent(event)

            self.assertFalse(event.isAccepted())
            dialog.request_cancel.assert_not_called()
            self.assertTrue(self.window._close_after_export_cancel)
        finally:
            self.window._render_worker = None
            self.window._export_dialog = None
            self.window._close_after_export_cancel = False

    def test_export_visualizer_receives_each_tracks_personal_color(self) -> None:
        with TemporaryDirectory(prefix="playlist-visualizer-color-") as directory:
            red_path = Path(directory) / "red.png"
            green_path = Path(directory) / "green.png"
            red = QImage(24, 24, QImage.Format.Format_ARGB32)
            green = QImage(24, 24, QImage.Format.Format_ARGB32)
            red.fill(QColor("#E03030"))
            green.fill(QColor("#30D050"))
            self.assertTrue(red.save(str(red_path)))
            self.assertTrue(green.save(str(green_path)))

            source = Source(
                SourceType.AUDIO_VISUALIZER, "Personal visualizer",
                fill_color="#FFFFFF", personal_color_enabled=True,
            )
            self.window.store.replace([source])
            tracks = [
                PlaylistTrack(
                    "missing-a.wav", "A", duration_seconds=2.0,
                    cover_path=str(red_path),
                ),
                PlaylistTrack(
                    "missing-b.wav", "B", duration_seconds=2.0,
                    cover_path=str(green_path),
                ),
            ]

            overlays = self.window._export_visualizers(tracks)

        self.assertEqual(len(overlays), 1)
        self.assertEqual(len(overlays[0].personal_colors), 2)
        first = QColor(overlays[0].personal_colors[0])
        second = QColor(overlays[0].personal_colors[1])
        self.assertGreater(first.red(), first.green())
        self.assertGreater(second.green(), second.red())

    def test_preset_dialog_lists_and_exports_user_presets(self) -> None:
        from app.presets.user_preset_service import UserPresetService
        from app.dialogs.preset_dialog import DesignPresetDialog

        self.window._add_source(SourceType.TEXT)
        preset = UserPresetService.save(
            "Portable One", self.window.store.sources(), 1280.0, 720.0,
        )
        dialog = DesignPresetDialog(self.window.translator, self.window)
        try:
            labels = [
                dialog.list_widget.item(row).text()
                for row in range(dialog.list_widget.count())
            ]
            self.assertIn("Portable One", labels)
            export_path = Path(self._preset_dir) / "exported.pcpreset.json"
            UserPresetService.export_to(preset.identifier, export_path)
            self.assertTrue(export_path.is_file())
            reimported = UserPresetService.import_from(export_path)
            self.assertEqual(reimported.name("en"), "Portable One")
        finally:
            dialog.close()
            for definition in UserPresetService.all():
                UserPresetService.delete(definition.identifier)

    def test_export_resolutions_follow_project_canvas_ratio(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(
                preview_backend="cpu", work_mode=WORK_MODE_MAX_SPEED,
            ), 1, 60.0, self.window.translator,
            Path("portrait-export.mp4"), canvas_size=(800, 1900),
        )
        try:
            width, height, _base_name = dialog.resolution_combo.currentData()
            self.assertLess(abs(width / height - 800 / 1900), 0.001)
            self.assertLess(width, height)
            render_settings = dialog.app_settings.render_settings()
            self.assertEqual(
                (render_settings.output_width, render_settings.output_height),
                (width, height),
            )
            self.assertIn("프로젝트 비율", dialog.resolution_combo.currentText())
            self.assertEqual(dialog.app_settings.preview_backend, "cpu")
            self.assertEqual(dialog.app_settings.work_mode, WORK_MODE_MAX_SPEED)
        finally:
            dialog.close()

    def test_export_dialog_starts_with_beginner_recommended_mode(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(), 3, 180.0, self.window.translator,
            Path("beginner-export.mp4"), canvas_size=(1920, 1080),
        )
        try:
            self.assertEqual(dialog.quality_mode_combo.currentData(), "balanced")
            self.assertFalse(dialog.advanced_group.isHidden())
            self.assertIn("권장", dialog.quality_mode_combo.currentText())
            self.assertIn("예상 작업량", dialog.workload_label.text())
            self.assertIn("권장", dialog.quality_description_label.text())
            settings = dialog.app_settings
            self.assertEqual(settings.video_codec, AUTO_VIDEO_ENCODER)
            self.assertEqual(
                (settings.crf, settings.preset, settings.audio_bitrate),
                ExportSettingsDialog.QUALITY_PROFILES["balanced"],
            )
        finally:
            dialog.close()

    def test_export_dialog_uses_compact_two_column_layout(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(), 12, 754.0, self.window.translator,
            Path("layout-export.mp4"), canvas_size=(1920, 1080),
        )
        try:
            dialog.show()
            self.application.processEvents()
            render_geometry = dialog.render_group.geometry()
            quality_geometry = dialog.quality_group.geometry()
            output_geometry = dialog.output_group.geometry()

            self.assertLessEqual(
                abs(render_geometry.top() - quality_geometry.top()), 2,
            )
            self.assertLess(render_geometry.right(), quality_geometry.left())
            self.assertLess(output_geometry.bottom(), render_geometry.top())
            self.assertGreaterEqual(dialog.width(), 760)
            self.assertLess(dialog.height(), dialog.width())

            dialog.advanced_check.setChecked(False)
            self.application.processEvents()
            compact_height = dialog.height()
            dialog.advanced_check.setChecked(True)
            self.application.processEvents()
            self.assertFalse(dialog.advanced_group.isHidden())
            self.assertGreaterEqual(dialog.height(), compact_height)
        finally:
            dialog.close()

    def test_export_quality_modes_apply_plain_language_tradeoffs(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(), 1, 60.0, self.window.translator,
            Path("quality-mode.mp4"), canvas_size=(1920, 1080),
        )
        try:
            dialog.quality_mode_combo.setCurrentIndex(
                dialog.quality_mode_combo.findData("fast")
            )
            self.assertEqual(
                (dialog.crf_spin.value(), dialog.preset_combo.currentText(),
                 dialog.audio_bitrate_combo.currentText()),
                ExportSettingsDialog.QUALITY_PROFILES["fast"],
            )
            self.assertIn("빠르게", dialog.quality_description_label.text())

            dialog.quality_mode_combo.setCurrentIndex(
                dialog.quality_mode_combo.findData("high")
            )
            self.assertEqual(
                (dialog.crf_spin.value(), dialog.preset_combo.currentText(),
                 dialog.audio_bitrate_combo.currentText()),
                ExportSettingsDialog.QUALITY_PROFILES["high"],
            )
            self.assertIn("파일이 커", dialog.quality_description_label.text())

            dialog.advanced_check.setChecked(True)
            dialog.crf_spin.setValue(17)
            self.assertEqual(dialog.quality_mode_combo.currentData(), "custom")
            self.assertFalse(dialog.advanced_group.isHidden())
        finally:
            dialog.close()

    def test_4k_export_selection_reaches_ffmpeg_dimensions(self) -> None:
        dialog = ExportSettingsDialog(
            AppSettings(), 1, 60.0, self.window.translator,
            Path("4k-export.mp4"), canvas_size=(1280, 720),
        )
        try:
            four_k_index = next(
                index for index in range(dialog.resolution_combo.count())
                if "4K" in str(dialog.resolution_combo.itemData(index)[2])
            )
            dialog.resolution_combo.setCurrentIndex(four_k_index)
            render_settings = dialog.app_settings.render_settings()
            self.assertEqual(
                (render_settings.output_width, render_settings.output_height),
                (3840, 2160),
            )
            scaling_filter = FFmpegRenderer._output_scaling_filter(
                render_settings.fps,
                render_settings.output_width,
                render_settings.output_height,
            )
            self.assertIn("scale=3840:2160", scaling_filter)
            self.assertIn("pad=3840:2160", scaling_filter)
        finally:
            dialog.close()

    def test_export_existing_file_accepts_native_yes_button_value(self) -> None:
        with TemporaryDirectory(prefix="pvs-export-overwrite-") as raw_directory:
            output = Path(raw_directory) / "existing.mp4"
            output.write_bytes(b"existing video")
            dialog = ExportSettingsDialog(
                AppSettings(), 1, 60.0, self.window.translator, output,
            )
            try:
                with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.Yes.value,
                ) as confirmation:
                    dialog._accept_if_valid()

                confirmation.assert_called_once()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.output_path, output.resolve())
            finally:
                dialog.close()

    def test_export_existing_file_no_keeps_settings_dialog_open(self) -> None:
        with TemporaryDirectory(prefix="pvs-export-no-overwrite-") as raw_directory:
            output = Path(raw_directory) / "existing.mp4"
            output.write_bytes(b"existing video")
            dialog = ExportSettingsDialog(
                AppSettings(), 1, 60.0, self.window.translator, output,
            )
            try:
                with patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No,
                ):
                    dialog._accept_if_valid()

                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                self.assertTrue(output.is_file())
            finally:
                dialog.close()

    def test_track_lyrics_dialog_exports_registered_lyrics_as_lrc(self) -> None:
        track = PlaylistTrack(
            "track.wav", "Export Track", artist="Artist", duration_seconds=20.0,
            lyrics_path="captions.vtt",
            lyrics=[{
                "start": 5.0, "end": 8.0,
                "text": "First visual line\nSecond visual line",
            }],
            lyrics_timing_offset_seconds=1.0,
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        try:
            self.assertTrue(dialog.export_lrc_button.isEnabled())
            dialog.timing_offset_spin.setValue(2.0)
            with TemporaryDirectory(prefix="track-lyrics-lrc-export-") as raw_directory:
                output = Path(raw_directory) / "converted.lrc"
                with (
                    patch.object(
                        QFileDialog, "getSaveFileName",
                        return_value=(str(output), "LRC lyrics (*.lrc)"),
                    ),
                    patch.object(QMessageBox, "information") as information,
                ):
                    dialog._export_current_lyrics_as_lrc()
                rendered = output.read_text(encoding="utf-8")
            self.assertIn("[ti:Export Track]", rendered)
            self.assertIn("[ar:Artist]", rendered)
            self.assertIn(r"[00:03.00]First visual line\nSecond visual line", rendered)
            self.assertNotIn("First visual line\nSecond visual line", rendered)
            information.assert_called_once()
            self.assertEqual(track.lyrics_timing_offset_seconds, 1.0)
        finally:
            dialog.close()

    def test_playlist_multi_select_and_context_menu_toggle_export_inclusion(
        self,
    ) -> None:
        tracks = [
            PlaylistTrack(f"{i}.wav", f"Track {i}", duration_seconds=10.0)
            for i in range(3)
        ]
        self.window.playlist_service.replace(tracks)
        self.application.processEvents()
        editor = self.window.playlist_editor
        for index in (0, 2):
            editor.list_widget.item(index).setSelected(True)
        self.assertEqual(len(editor._selected_ids()), 2)

        editor._toggle_selected()
        self.application.processEvents()
        by_id = {t.id: t for t in self.window.playlist_service.tracks}
        self.assertFalse(by_id[tracks[0].id].enabled)
        self.assertFalse(by_id[tracks[2].id].enabled)
        self.assertTrue(by_id[tracks[1].id].enabled)

        editor._toggle_selected()
        self.application.processEvents()
        self.assertTrue(
            all(t.enabled for t in self.window.playlist_service.tracks)
        )

    def test_grid_covers_workspace_but_not_export_snapshot(self) -> None:
        scene = self.window.canvas.scene_model
        x_positions, y_positions = scene.grid_positions(scene.sceneRect())
        self.assertLess(min(x_positions), scene.artboard_rect.left())
        self.assertGreater(max(x_positions), scene.artboard_rect.right())
        self.assertLess(min(y_positions), scene.artboard_rect.top())
        self.assertGreater(max(y_positions), scene.artboard_rect.bottom())
        self.assertTrue(all(position % 40 == 0 for position in x_positions))
        self.assertTrue(all(position % 40 == 0 for position in y_positions))

        scene.show_grid = True
        snapshot_with_editor_grid = CanvasSnapshot.capture(scene, output_scale=0.25)
        self.assertTrue(scene.show_grid)
        scene.show_grid = False
        snapshot_without_editor_grid = CanvasSnapshot.capture(scene, output_scale=0.25)
        self.assertEqual(snapshot_with_editor_grid, snapshot_without_editor_grid)

    def test_ffmpeg_install_progress_belongs_to_modal_settings_dialog(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        self.window._settings_dialog = dialog
        dialog.download_requested.connect(
            lambda: self.window._start_ffmpeg_install(dialog)
        )
        dialog.set_ffmpeg_catalog([FFmpegReleaseOption(
            "9.0", "n9.0-test", "latest", "Latest", "2026-08-25",
            "ffmpeg-test-win64-gpl-9.0.zip", "https://example.test/ffmpeg.zip",
            "https://example.test/checksums.sha256", "Test release notes", True,
        )])
        with (
            patch.object(
                QMessageBox, "question",
                # Real PySide calls may return a value-equivalent wrapper.
                # This catches incorrect identity (`is`) comparisons.
                return_value=QMessageBox.StandardButton.Yes.value,
            ),
            patch("app.ui.main_window.FFmpegInstallWorker.start") as start,
        ):
            QTest.mouseClick(
                dialog.ffmpeg_download_button, Qt.MouseButton.LeftButton
            )
        self.assertTrue(dialog._ffmpeg_installing)
        self.assertFalse(dialog.ffmpeg_download_button.isEnabled())
        self.assertIsNotNone(self.window._ffmpeg_install_dialog)
        self.assertIsInstance(
            self.window._ffmpeg_install_dialog, FFmpegInstallProgressDialog
        )
        self.assertIs(self.window._ffmpeg_install_dialog.parent(), dialog)
        self.assertTrue(self.window._ffmpeg_install_dialog.isVisible())
        self.assertTrue(self.window._ffmpeg_install_dialog.isModal())
        self.assertEqual(
            self.window._ffmpeg_install_dialog.windowModality(),
            Qt.WindowModality.ApplicationModal,
        )
        self.assertIn("GitHub", self.window._ffmpeg_install_dialog.detail_label.text())
        start.assert_called_once()
        self.window._ffmpeg_install_dialog.complete(False)
        self.window._ffmpeg_install_dialog = None
        self.window._ffmpeg_install_worker = None
        self.window._settings_dialog = None
        dialog.deleteLater()

    def test_export_work_mode_setting_and_layer_limits(self) -> None:
        dialog = SettingsDialog(
            AppSettings(work_mode=WORK_MODE_MAX_SPEED),
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            self.assertEqual(
                dialog.work_mode_combo.currentData(), WORK_MODE_MAX_SPEED,
            )
            self.assertEqual(dialog.app_settings.work_mode, WORK_MODE_MAX_SPEED)
            self.assertIn("CPU", dialog.work_mode_hint.text())
        finally:
            dialog.close()

        self.assertEqual(MainWindow._export_layer_worker_count(
            RenderSettings(work_mode=WORK_MODE_STABLE), 5,
        ), 1)
        self.assertEqual(MainWindow._export_layer_worker_count(
            RenderSettings(work_mode=WORK_MODE_AUTO), 5,
        ), 2)
        self.assertEqual(MainWindow._export_layer_worker_count(
            RenderSettings(work_mode=WORK_MODE_MAX_SPEED), 5,
        ), 3)
        self.assertEqual(MainWindow._export_layer_worker_count(
            RenderSettings(
                work_mode=WORK_MODE_MAX_SPEED,
                output_width=3840, output_height=2160,
            ), 5,
        ), 2)
        self.assertEqual(MainWindow._export_png_queue_capacity(
            RenderSettings(work_mode=WORK_MODE_STABLE),
        ), 1)
        self.assertEqual(MainWindow._export_png_queue_capacity(
            RenderSettings(work_mode=WORK_MODE_AUTO),
        ), 3)
        self.assertEqual(MainWindow._export_png_queue_capacity(
            RenderSettings(work_mode=WORK_MODE_MAX_SPEED),
        ), 5)
        self.assertEqual(MainWindow._export_png_queue_capacity(RenderSettings(
            work_mode=WORK_MODE_MAX_SPEED,
            output_width=3840, output_height=2160,
        )), 2)

    def test_export_without_ffmpeg_opens_the_ffmpeg_setup_page(self) -> None:
        self.window.playlist_service.add_tracks([
            PlaylistTrack("C:/music/song.mp3", "Song", duration_seconds=120.0),
        ])
        with (
            patch.object(ExportSettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted),
            patch(
                "app.controllers.export_controller.FFmpegRenderer",
                side_effect=FFmpegNotFoundError("missing"),
            ),
            patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok,
            ) as warning,
            patch.object(self.window, "_show_settings") as show_settings,
        ):
            self.window._export_video()

        warning.assert_called_once()
        self.assertIn("FFmpeg", warning.call_args.args[2])
        self.assertIn("설치 화면", warning.call_args.args[2])
        show_settings.assert_called_once_with(focus_ffmpeg=True)

    def test_settings_tabs_scroll_so_the_dialog_fits_a_laptop_screen(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            # The General tab alone needed ~900 px before its content scrolled.
            self.assertLessEqual(dialog.minimumSizeHint().height(), 600)
            self.assertLessEqual(dialog.height(), 600)
            for page in (dialog.general_page, dialog.export_page, dialog.ffmpeg_page):
                self.assertIsInstance(page, QScrollArea)
            self.assertTrue(dialog.general_page.widget().isAncestorOf(dialog.app_group))
            self.assertTrue(dialog.notifications_page.widget().isAncestorOf(dialog.notification_group))
            self.assertTrue(dialog.ffmpeg_page.widget().isAncestorOf(dialog.ffmpeg_group))
            self.assertFalse(dialog.general_page.isAncestorOf(dialog.button_box))
        finally:
            dialog.close()

    def test_settings_can_open_directly_on_ffmpeg_installation(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            dialog.tabs.setCurrentWidget(dialog.general_page)
            dialog.show()
            dialog.open_ffmpeg_page()
            dialog.set_ffmpeg_catalog([FFmpegReleaseOption(
                "9.0", "n9.0-test", "latest", "Latest", "2026-08-25",
                "ffmpeg-test-win64-gpl-9.0.zip", "https://example.test/ffmpeg.zip",
                "https://example.test/checksums.sha256", "Test notes", True,
            )])
            self.application.processEvents()
            self.assertIs(dialog.tabs.currentWidget(), dialog.ffmpeg_page)
            self.assertTrue(dialog.ffmpeg_download_button.hasFocus())
        finally:
            dialog.close()

    def test_ffmpeg_version_manager_labels_recommended_and_updates_button_states(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        recommended = FFmpegReleaseOption(
            "9.0", "n9-current", "latest", "Latest", "2026-08-25",
            "ffmpeg-9.zip", "https://test/9", "https://test/sums",
            "Recommended patch notes", True,
        )
        older = FFmpegReleaseOption(
            "8.1", "n8-old", "latest", "Latest", "2026-08-25",
            "ffmpeg-8.zip", "https://test/8", "https://test/sums",
            "Older patch notes", False,
        )
        try:
            dialog.set_ffmpeg_catalog([recommended, older])
            self.assertEqual(dialog.ffmpeg_about_title.text(), "FFmpeg이란?")
            self.assertIn("동영상 내보내기에는 필요", dialog.ffmpeg_about_description.text())
            self.assertIn("권장 버전", dialog.ffmpeg_about_description.text())
            self.assertEqual(dialog.ffmpeg_version_combo.itemText(0), "9.0(권장)")
            self.assertEqual(dialog.ffmpeg_version_combo.itemText(1), "8.1")
            self.assertIn("빌드 n9-current", dialog.ffmpeg_release_info.text())
            self.assertIn("Recommended patch notes", dialog.ffmpeg_release_info.toolTip())
            current = ManagedFFmpegInstallation(
                Path("managed/ffmpeg.exe"), "n9-current", "9.0", "latest"
            )
            dialog.set_managed_installation(current)
            self.assertFalse(dialog.ffmpeg_update_button.isEnabled())
            self.assertTrue(dialog.ffmpeg_reinstall_button.isEnabled())
            self.assertTrue(dialog.ffmpeg_delete_button.isEnabled())

            dialog.set_managed_installation(ManagedFFmpegInstallation(
                Path("managed/old.exe"), "n8-old", "8.1", "latest"
            ))
            self.assertTrue(dialog.ffmpeg_update_button.isEnabled())
            dialog.set_ffmpeg_installing(True)
            self.assertFalse(dialog.ffmpeg_download_button.isEnabled())
            self.assertFalse(dialog.ffmpeg_update_button.isEnabled())
            self.assertFalse(dialog.ffmpeg_reinstall_button.isEnabled())
            self.assertFalse(dialog.ffmpeg_delete_button.isEnabled())
        finally:
            dialog.close()

    def test_recommended_ffmpeg_skips_warning_but_other_versions_require_it(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        recommended = FFmpegReleaseOption(
            "9.0", "n9-current", "latest", "Latest", "2026-08-25",
            "ffmpeg-9.zip", "https://test/9", "https://test/sums",
            "Recommended release notes", True,
        )
        unsupported = FFmpegReleaseOption(
            "8.1", "n8-old", "latest", "Latest", "2026-08-25",
            "ffmpeg-8.zip", "https://test/8", "https://test/sums",
            "Compatibility warning and release notes", False,
        )
        self.window._settings_dialog = dialog
        try:
            dialog.set_ffmpeg_catalog([recommended, unsupported])
            with (
                patch.object(self.window, "_confirm_ffmpeg_release") as confirm,
                patch("app.ui.main_window.FFmpegInstallWorker.start") as start,
            ):
                self.window._start_ffmpeg_install(dialog, recommended)
                confirm.assert_not_called()
                start.assert_called_once()
            if self.window._ffmpeg_install_dialog:
                self.window._ffmpeg_install_dialog.complete(False)
            self.window._ffmpeg_install_dialog = None
            self.window._ffmpeg_install_worker = None
            dialog.set_ffmpeg_installing(False)

            with patch.object(
                self.window, "_confirm_ffmpeg_release", return_value=False,
            ) as confirm:
                self.window._start_ffmpeg_install(dialog, unsupported)
            confirm.assert_called_once_with(dialog, unsupported, "설치")
            self.assertIsNone(self.window._ffmpeg_install_worker)
        finally:
            self.window._settings_dialog = None
            self.window._ffmpeg_install_dialog = None
            self.window._ffmpeg_install_worker = None
            dialog.close()

    def test_ffmpeg_install_success_is_applied_and_persisted_automatically(self) -> None:
        original = self.window.settings_service.current
        dialog = SettingsDialog(
            original,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        self.window._settings_dialog = dialog
        try:
            with TemporaryDirectory() as directory:
                executable = Path(directory) / "ffmpeg.exe"
                executable.touch()
                installation = ManagedFFmpegInstallation(executable, "latest-test")
                with patch.object(QMessageBox, "information"):
                    self.window._ffmpeg_install_succeeded(installation)
                self.assertEqual(
                    self.window.settings_service.current.ffmpeg_path,
                    str(executable),
                )
                self.assertEqual(dialog.ffmpeg_edit.text(), str(executable))
                self.assertFalse(dialog._ffmpeg_installing)
        finally:
            self.window.settings_service.save(original)
            self.window._settings_dialog = None
            dialog.deleteLater()

    def test_export_notification_settings_are_individually_configurable(self) -> None:
        configured = replace(
            self.window.settings_service.current,
            export_notifications_enabled=True,
            export_notification_mode="always",
            export_notify_visuals=False,
            export_notify_audio=True,
            export_notify_effects=False,
            export_notify_encode=True,
            export_notify_complete=True,
            export_notify_failures=False,
        )
        dialog = SettingsDialog(
            configured,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            self.assertTrue(dialog.export_notifications_check.isChecked())
            self.assertEqual(
                dialog.export_notification_mode_combo.currentData(), "always",
            )
            result = dialog.app_settings
            self.assertFalse(result.export_notify_visuals)
            self.assertTrue(result.export_notify_audio)
            self.assertFalse(result.export_notify_effects)
            self.assertTrue(result.export_notify_encode)
            self.assertTrue(result.export_notify_complete)
            self.assertFalse(result.export_notify_failures)

            dialog.export_notifications_check.setChecked(False)
            self.assertFalse(dialog.export_notification_mode_combo.isEnabled())
            self.assertTrue(all(
                not checkbox.isEnabled()
                for checkbox in dialog._export_notification_stage_checks()
            ))
            # Turning off the master must retain the user's per-stage choices.
            self.assertFalse(dialog.app_settings.export_notify_visuals)
            self.assertTrue(dialog.app_settings.export_notify_audio)
        finally:
            dialog.close()

    def test_export_notification_policy_respects_focus_and_each_stage(self) -> None:
        configured = AppSettings(
            export_notifications_enabled=True,
            export_notification_mode="unfocused",
            export_notify_visuals=False,
            export_notify_audio=True,
            export_notify_complete=True,
        )
        allowed = self.window._export_notification_allowed

        self.assertFalse(allowed(configured, "visuals", False))
        self.assertFalse(allowed(configured, "audio", True))
        self.assertTrue(allowed(configured, "audio", False))
        self.assertTrue(allowed(
            replace(configured, export_notification_mode="always"),
            "complete",
            True,
        ))

    def test_export_stage_notifications_are_emitted_only_once_per_phase(self) -> None:
        original = self.window.settings_service.current
        configured = replace(
            original,
            export_notifications_enabled=True,
            export_notification_mode="always",
        )
        self.window.settings_service._current = configured
        self.window._export_notified_steps.clear()
        self.window._active_export_output_path = Path("C:/Videos/result.mp4")
        try:
            with patch.object(
                self.window, "_show_system_notification", return_value=True,
            ) as notify:
                self.window._notify_export_stage("Preparing audio")
                self.window._notify_export_stage("Combining audio")
                self.window._notify_export_stage("Encoding video")
                self.window._notify_export_stage("Finalizing export")
                self.window._notify_export_stage("Complete")

            self.assertEqual(notify.call_count, 3)
            self.assertEqual(
                self.window._export_notified_steps,
                {"audio", "encode", "complete"},
            )
        finally:
            self.window.settings_service._current = original
            self.window._active_export_output_path = None

    def test_export_complete_dialog_provides_post_export_tools(self) -> None:
        with TemporaryDirectory(prefix="pvs-export-complete-") as raw_directory:
            output = Path(raw_directory) / "playlist.mp4"
            output.write_bytes(b"completed video")
            dialog = ExportCompleteDialog(
                output, self.window.translator, self.window,
            )
            try:
                with patch(
                    "app.dialogs.export_complete_dialog.QDesktopServices.openUrl",
                    return_value=True,
                ) as open_url:
                    dialog._play_video()
                    dialog._open_folder()
                self.assertEqual(open_url.call_count, 2)

                dialog._copy_path()
                self.assertEqual(QApplication.clipboard().text(), str(output.resolve()))
                self.assertIn("복사", dialog.copy_path_button.text())

                dialog._request_export_again()
                self.assertTrue(dialog.export_again_requested)
            finally:
                dialog.close()

    def test_format_bytes_scales_units(self) -> None:
        self.assertEqual(self.window._format_bytes(0), "0.0 B")
        self.assertEqual(self.window._format_bytes(2048), "2.0 KB")
        self.assertEqual(self.window._format_bytes(5 * 1024**3), "5.0 GB")

    def test_export_staging_relocates_to_output_drive_when_temp_is_short(self) -> None:
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        with TemporaryDirectory(prefix="export-output-") as output_directory:
            self.window._active_export_output_path = (
                Path(output_directory) / "video.mp4"
            )
            settings = RenderSettings(
                fps=30, output_width=1920, output_height=1080,
            )

            output_root = str(Path(output_directory).resolve())

            def fake_usage(path: object) -> object:
                if str(Path(str(path)).resolve()).startswith(output_root):
                    return Usage(0, 0, 900 * 1024**3)       # output drive: 900 GB
                return Usage(0, 0, 200 * 1024**2)           # temp drive: 200 MB

            with (
                patch("app.ui.main_window.shutil.disk_usage", side_effect=fake_usage),
                patch.object(QMessageBox, "warning") as warning,
            ):
                proceed = self.window._prepare_export_staging_space(
                    settings, 120.0, 2, True, korean=False,
                )

            self.assertTrue(proceed)
            warning.assert_not_called()  # output drive has room, so no warning
            self.assertIsNotNone(self.window.export_orchestrator.frames.staging)
            staging = Path(self.window.export_orchestrator.frames.staging.name).resolve()
            self.assertTrue(
                str(staging).startswith(str(Path(output_directory).resolve()))
            )
        self.window._clear_export_frame_staging()

    def test_export_low_space_on_both_drives_asks_before_continuing(self) -> None:
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        self.window._active_export_output_path = Path(
            tempfile.gettempdir()
        ) / "video.mp4"
        settings = RenderSettings(fps=30, output_width=1920, output_height=1080)
        with (
            patch(
                "app.ui.main_window.shutil.disk_usage",
                return_value=Usage(0, 0, 50 * 1024**2),
            ),
            patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.No,
            ) as warning,
        ):
            proceed = self.window._prepare_export_staging_space(
                settings, 120.0, 2, True, korean=False,
            )
        self.assertFalse(proceed)
        warning.assert_called_once()
        self.window._clear_export_frame_staging()

    def test_export_warns_when_a_raster_source_is_upscaled_past_its_pixels(self) -> None:
        with TemporaryDirectory(prefix="playlist-upscale-warn-") as directory:
            small = Path(directory) / "small.png"
            big = Path(directory) / "big.png"
            self.assertTrue(
                QImage(160, 90, QImage.Format.Format_ARGB32).save(str(small))
            )
            self.assertTrue(
                QImage(1920, 1080, QImage.Format.Format_ARGB32).save(str(big))
            )
            soft = Source(
                SourceType.IMAGE, "Tiny logo", width=1280.0, height=720.0,
                content_path=str(small),
            )
            crisp = Source(
                SourceType.IMAGE, "Sharp art", width=1280.0, height=720.0,
                content_path=str(big),
            )
            self.window.store.replace([soft, crisp])
            track = PlaylistTrack("song.wav", "Song", duration_seconds=2.0)

            fhd = RenderSettings(output_width=1920, output_height=1080)
            warnings = self.window._export_upscale_warnings(
                [track], fhd, 1.5, korean=False,
            )
            self.assertEqual(len(warnings), 1)
            self.assertIn("Tiny logo", warnings[0])
            self.assertNotIn("Sharp art", warnings[0])

            # Shrink the tiny image on the canvas until it fits its own pixels.
            soft.width = soft.height = 100.0
            self.window.store.replace([soft, crisp])
            self.assertEqual(
                self.window._export_upscale_warnings(
                    [track], fhd, 1.5, korean=False,
                ),
                [],
            )

    def test_export_staging_reuses_identical_consecutive_frames(self) -> None:
        self.window._clear_export_frame_staging()
        self.window.export_orchestrator.frames.staging = TemporaryDirectory(
            prefix="playlist-video-test-frames-"
        )
        image = QImage(48, 32, QImage.Format.Format_ARGB32)
        image.fill(QColor("#336699"))
        try:
            first = self.window._stage_export_frame(image, 0.5, "base")
            repeated = self.window._stage_export_frame(image.copy(), 1.0, "base")
            changed = image.copy()
            changed.setPixelColor(0, 0, QColor("#ffffff"))
            third = self.window._stage_export_frame(changed, 0.5, "base")

            self.assertEqual(first.image, repeated.image)
            self.assertNotEqual(first.image, third.image)
            self.assertEqual(first.duration_seconds, 0.5)
            self.assertEqual(repeated.duration_seconds, 1.0)
            self.assertEqual(third.duration_seconds, 0.5)
            self.assertEqual(self.window.export_orchestrator.frames.capture_count, 3)
            self.assertEqual(self.window.export_orchestrator.frames.index, 2)
            self.assertTrue(first.image.is_file())
            self.assertTrue(third.image.is_file())
            metrics = self.window.export_orchestrator.frames.metrics
            self.assertIsNotNone(metrics)
            assert metrics is not None
            expected_bytes = first.image.stat().st_size + third.image.stat().st_size
            self.assertEqual(metrics.capture_count, 3)
            self.assertEqual(metrics.unique_file_count, 2)
            self.assertEqual(metrics.reused_frame_count, 1)
            self.assertEqual(metrics.total_bytes, expected_bytes)
            self.assertEqual(metrics.stream_file_counts, {"base": 2})
            self.assertEqual(metrics.stream_bytes, {"base": expected_bytes})
            self.assertEqual((metrics.largest_width, metrics.largest_height), (48, 32))
        finally:
            self.window._clear_export_frame_staging()
        completed_metrics = self.window.export_orchestrator.frames.last_metrics
        self.assertIsNotNone(completed_metrics)
        assert completed_metrics is not None
        self.assertEqual(completed_metrics.capture_count, 3)
        self.assertEqual(completed_metrics.unique_file_count, 2)
        self.assertEqual(completed_metrics.reused_frame_count, 1)
        self.assertGreaterEqual(completed_metrics.elapsed_seconds, 0.0)

    def test_export_animation_sampling_matches_output_frame_rate(self) -> None:
        self.assertEqual(self.window._export_animation_sample_rate(60), 60)
        self.assertEqual(self.window._export_animation_sample_rate(50), 50)
        self.assertEqual(self.window._export_animation_sample_rate(24), 24)
        self.assertEqual(self.window._export_animation_sample_rate(10), 10)

    def test_export_preflight_failure_stops_before_canvas_capture(self) -> None:
        track = PlaylistTrack(
            file_path="missing-before-capture.mp3",
            title="Missing before capture",
            duration_seconds=1.0,
        )
        self.window.playlist_service.replace([track])
        with (
            patch("app.controllers.export_controller.FFmpegRenderer") as renderer_type,
            patch("app.controllers.export_controller.RenderWorker") as worker_type,
            patch(
                "app.controllers.export_controller.ExportSettingsDialog.exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
            patch.object(CanvasSnapshot, "capture_track") as capture,
            patch.object(QMessageBox, "critical") as critical_message,
        ):
            renderer_type.return_value.preflight_export.side_effect = RenderError(
                "Audio file is missing: missing-before-capture.mp3"
            )
            self.window._export_video()

        capture.assert_not_called()
        worker_type.assert_not_called()
        critical_message.assert_called_once()
        self.assertIsNone(self.window._export_dialog)
        self.assertIsNone(self.window.export_orchestrator.frames.staging)

    def test_automatic_encoder_prefers_nvidia_then_cpu(self) -> None:
        with patch.object(VideoEncoderAdvisor, "has_nvidia_gpu", return_value=True):
            self.assertEqual(
                VideoEncoderAdvisor.automatic_encoder(), NVIDIA_H264_ENCODER,
            )
            self.assertEqual(
                AppSettings().render_settings().video_codec,
                NVIDIA_H264_ENCODER,
            )
        with patch.object(VideoEncoderAdvisor, "has_nvidia_gpu", return_value=False):
            self.assertEqual(
                VideoEncoderAdvisor.automatic_encoder(), CPU_H264_ENCODER,
            )
            self.assertEqual(
                AppSettings().render_settings().video_codec,
                CPU_H264_ENCODER,
            )

    def test_export_static_layers_keep_intro_stable_outro_timeline_order(self) -> None:
        """Transparent Z bands must be captured in the same order as base frames."""
        track = PlaylistTrack(
            file_path="animation-order.mp3", title="Animation order",
            duration_seconds=2.0, start_time_seconds=0.5,
        )
        self.window.playlist_service.replace([track])
        self.window.store.add(Source(
            SourceType.TEXT, "Animated", animation_in="fade",
            animation_out="fade", animation_duration=0.2, z_index=2.0,
        ))
        self.window.store.add(Source(
            SourceType.TIME, "Export clock", text="%current_time%", z_index=3.0,
        ))
        captured_worker_arguments: list[tuple[object, ...]] = []

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

        phase_colors = {
            "in": QColor("#ff0000"),
            "stable": QColor("#00ff00"),
            "out": QColor("#0000ff"),
        }
        captured_states: list[dict[str, object]] = []

        def capture_phase(*_arguments: object, **state: object) -> QImage:
            captured_states.append(dict(state))
            image = QImage(4, 4, QImage.Format.Format_ARGB32)
            image.fill(phase_colors[str(state.get("animation_phase") or "stable")])
            return image

        def stage_image(image: QImage, duration: float, _key: str) -> RenderFrame:
            return RenderFrame(image.copy(), duration)

        try:
            with (
                patch("app.controllers.export_controller.FFmpegRenderer") as renderer_type,
                patch("app.controllers.export_controller.RenderWorker", WorkerStub),
                # This test covers the intermediate-file path.
                patch("app.preview.export_plan.piped_export_supported",
                      return_value=False),
                patch("app.controllers.export_controller.ExportSettingsDialog.exec",
                      return_value=QDialog.DialogCode.Accepted),
                patch.object(CanvasSnapshot, "z_bands",
                             return_value=[(None, 1.0), (1.0, None)]),
                patch.object(
                    CanvasSnapshot, "split_mixed_capture_bands",
                    return_value=[(None, 1.0), (1.0, None)],
                ),
                patch.object(CanvasSnapshot, "capture_track", side_effect=capture_phase),
                patch.object(self.window, "_stage_export_frame", side_effect=stage_image),
                patch.object(self.window, "_export_visualizers", return_value=[]),
                patch.object(QMessageBox, "critical") as critical_message,
            ):
                renderer_type.return_value.ensure_encoder_available.side_effect = (
                    lambda encoder: (
                        (_ for _ in ()).throw(RenderError("ffv1 unavailable"))
                        if encoder == "ffv1" else None
                    )
                )
                self.window._export_video()

            critical_message.assert_not_called()
            self.assertEqual(len(captured_worker_arguments), 1)
            static_layers = captured_worker_arguments[0][6]
            self.assertEqual(len(static_layers), 1)
            colors = [
                frame.image.pixelColor(0, 0).name()
                for frame in static_layers[0].frames
            ]
            self.assertIn("#ff0000", colors)
            self.assertIn("#00ff00", colors)
            self.assertIn("#0000ff", colors)
            self.assertLess(max(i for i, color in enumerate(colors) if color == "#ff0000"),
                            min(i for i, color in enumerate(colors) if color == "#00ff00"))
            self.assertLess(max(i for i, color in enumerate(colors) if color == "#00ff00"),
                            min(i for i, color in enumerate(colors) if color == "#0000ff"))
            entrance_states = [
                state for state in captured_states
                if state.get("animation_phase") == "in"
            ]
            exit_states = [
                state for state in captured_states
                if state.get("animation_phase") == "out"
            ]
            self.assertEqual(entrance_states[0]["animation_progress"], 0.0)
            self.assertEqual(entrance_states[0]["elapsed_seconds"], 0.0)
            self.assertEqual(exit_states[0]["animation_progress"], 0.0)
            self.assertAlmostEqual(float(exit_states[0]["elapsed_seconds"]), 1.8)
            stable_elapsed = [
                float(state["elapsed_seconds"])
                for state in captured_states
                if state.get("animation_phase") is None
                and "elapsed_seconds" in state
            ]
            self.assertTrue(any(0.99 < elapsed < 1.01 for elapsed in stable_elapsed))
        finally:
            if self.window._export_dialog is not None:
                self.window._export_dialog.complete(False)
                self.window._export_dialog = None
            self.window._export_finished()
            self.application.processEvents()

    def test_static_export_streams_without_staging_png_frames(self) -> None:
        track = PlaylistTrack(
            file_path="streamed-static.mp3",
            title="Streamed static",
            duration_seconds=1.0,
        )
        self.window.playlist_service.replace([track])
        self.window.store.add(Source(SourceType.TEXT, "Static title"))
        captured_worker_arguments: list[tuple[object, ...]] = []
        submitted_durations: list[float] = []
        direct_profiles: list[object] = []

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
                **kwargs: object,
            ) -> None:
                self.output_path = output_path
                self.fps = fps
                self.width = 4
                self.height = 4
                direct_profiles.append(kwargs.get("direct_profile"))

            def submit(self, image: QImage, duration: float) -> None:
                self.width = image.width()
                self.height = image.height()
                submitted_durations.append(duration)

            def finish(self) -> StaticVideoStreamResult:
                self.output_path.touch()
                return StaticVideoStreamResult(
                    self.output_path,
                    sum(submitted_durations),
                    self.width,
                    self.height,
                    self.fps,
                    False,
                    30,
                    1,
                )

            def cancel(self) -> None:
                pass

        def capture_static(*_arguments: object, **_state: object) -> QImage:
            image = QImage(4, 4, QImage.Format.Format_RGB32)
            image.fill(QColor("#336699"))
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
                patch.object(CanvasSnapshot, "z_bands", return_value=[(None, None)]),
                patch.object(CanvasSnapshot, "capture_track", side_effect=capture_static),
                patch.object(self.window, "_stage_export_frame") as png_stage,
                patch.object(self.window, "_export_visualizers", return_value=[]),
                patch.object(QMessageBox, "critical") as critical_message,
            ):
                renderer_type.return_value.ensure_encoder_available.return_value = None
                renderer_type.return_value.direct_encoding_profile.side_effect = (
                    lambda settings: DirectVideoEncodingProfile(
                        settings.output_width, settings.output_height,
                        settings.video_codec, (),
                    )
                )
                self.window._export_video()

            critical_message.assert_not_called()
            png_stage.assert_not_called()
            self.assertTrue(submitted_durations)
            self.assertAlmostEqual(sum(submitted_durations), track.duration_seconds)
            self.assertEqual(len(captured_worker_arguments), 1)
            prepared = captured_worker_arguments[0][1]
            self.assertIsInstance(prepared, PreparedVideoInput)
            self.assertTrue(prepared.ready_for_mux)
            self.assertEqual(len(direct_profiles), 1)
            self.assertIsNotNone(direct_profiles[0])
            self.assertEqual(prepared.encoded_codec, direct_profiles[0].codec)
            self.assertEqual(captured_worker_arguments[0][6], [])
            self.assertEqual(self.window.export_orchestrator.frames.index, 0)
        finally:
            if self.window._export_dialog is not None:
                self.window._export_dialog.complete(False)
                self.window._export_dialog = None
            self.window._export_finished()
            self.application.processEvents()

    def _run_live_export(self, render_side_effect, *, image_size=(4, 4)):
        """Drive ``_export_video`` down the live (piped) path with a mocked FFmpeg."""
        track = PlaylistTrack(
            file_path="live-capture.mp3", title="Live capture", duration_seconds=1.0,
        )
        self.window.playlist_service.replace([track])
        self.window.store.add(Source(SourceType.TEXT, "Moving title"))
        render_inputs: list[object] = []
        encoder_submissions: list[float] = []

        class EncoderStub:
            def __init__(self, _executable, output_path, fps, **_kwargs) -> None:
                self.output_path = Path(output_path)
                self.fps = fps
                self.frame_count = 0

            def submit(self, _image, duration) -> None:
                encoder_submissions.append(duration)

            def finish(self) -> StaticVideoStreamResult:
                self.output_path.touch()
                return StaticVideoStreamResult(
                    self.output_path, sum(encoder_submissions), 4, 4, self.fps,
                    False, 30, 1,
                )

            def cancel(self) -> None:
                pass

        def capture(*_arguments: object, **_state: object) -> QImage:
            image = QImage(*image_size, QImage.Format.Format_RGB32)
            image.fill(QColor("#224466"))
            return image

        def render(image, *arguments, **keywords):
            render_inputs.append(image)
            return render_side_effect(len(render_inputs), image, keywords)

        with (
            patch("app.controllers.export_controller.FFmpegRenderer") as renderer_type,
            patch("app.preview.export_session.StaticVideoStreamEncoder", EncoderStub),
            patch("app.controllers.export_controller.ExportSettingsDialog.exec",
                  return_value=QDialog.DialogCode.Accepted),
            patch.object(CanvasSnapshot, "z_bands", return_value=[(None, None)]),
            patch.object(CanvasSnapshot, "invariant_stream_keys", return_value=set()),
            patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=[(None, None)]),
            patch(
                "app.preview.export_canvas_capture.ExportCanvasCapturer.iter_coalesced_samples",
                lambda _self, samples, _key, _wait=None: iter(samples),
            ),
            patch.object(CanvasSnapshot, "capture_track", side_effect=capture),
            patch.object(self.window, "_export_visualizers", return_value=[]),
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(
                type(self.window.export_orchestrator), "show_complete_dialog",
            ) as complete_dialog,
            patch.object(QMessageBox, "critical") as critical_message,
        ):
            renderer_type.return_value.ensure_encoder_available.return_value = None
            renderer_type.return_value.direct_encoding_profile.return_value = None
            renderer_type.return_value.render.side_effect = render
            self.window._export_video()
            deadline = monotonic() + 20.0
            while (
                self.window._render_worker is not None
                or self.window._export_dialog is not None
            ) and monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(10)
            self.application.processEvents()
            QTest.qWait(20)
        return render_inputs, encoder_submissions, critical_message, complete_dialog

    @unittest.skipUnless(hasattr(os, "mkfifo") or os.name == "nt", "named pipes")
    def test_live_export_hands_capture_to_the_running_final_encoder(self) -> None:
        received = bytearray()

        def render(_call, image, _keywords):
            self.assertIsInstance(image, PipedVideoInput)
            with open(image.input_arguments[-1], "rb") as reader:
                while chunk := reader.read(65536):
                    received.extend(chunk)
            return RenderResult(Path("live-capture.mp4"), 1)

        render_inputs, submissions, critical, complete_dialog = self._run_live_export(render)

        critical.assert_not_called()
        self.assertEqual(len(render_inputs), 1)
        self.assertEqual(submissions, [])  # no intermediate video at all
        self.assertEqual(len(received), 30 * 4 * 4 * 4)
        complete_dialog.assert_called_once()
        self.assertIsNone(self.window._render_worker)

    @unittest.skipUnless(hasattr(os, "mkfifo") or os.name == "nt", "named pipes")
    def test_live_export_falls_back_to_intermediate_files_when_ffmpeg_dies(self) -> None:
        def render(call, image, _keywords):
            if call == 1:
                with open(image.input_arguments[-1], "rb") as reader:
                    reader.read(1)
                raise RenderError("Error while decoding stream #0:0: broken pipe")
            self.assertIsInstance(image, PreparedVideoInput)
            return RenderResult(Path("live-capture.mp4"), 1)

        render_inputs, submissions, critical, complete_dialog = self._run_live_export(
            render, image_size=(640, 360),
        )

        critical.assert_not_called()
        self.assertEqual(len(render_inputs), 2)
        self.assertAlmostEqual(sum(submissions), 1.0)
        complete_dialog.assert_called_once()

    @unittest.skipUnless(hasattr(os, "mkfifo") or os.name == "nt", "named pipes")
    def test_live_export_does_not_retry_when_ffmpeg_fails_before_reading(self) -> None:
        def render(_call, _image, _keywords):
            raise RenderError("The prepared playlist audio is missing.")

        render_inputs, _submissions, critical, complete_dialog = self._run_live_export(
            render, image_size=(640, 360),
        )

        self.assertEqual(len(render_inputs), 1)
        critical.assert_called_once()
        self.assertIn("audio is missing", critical.call_args.args[2])
        complete_dialog.assert_not_called()
        self.assertIsNone(self.window._export_dialog)

    def test_export_probes_independent_video_files_concurrently(self) -> None:
        with TemporaryDirectory(prefix="parallel-video-probe-") as raw_directory:
            paths = [Path(raw_directory) / f"clip-{index}.mp4" for index in range(3)]
            for path in paths:
                path.touch()
            track = PlaylistTrack(
                "song.wav", "Song", duration_seconds=3.0,
                video_paths=[str(path) for path in paths],
            )
            source = Source(
                SourceType.VIDEO, "Per-track videos",
                video_timing_mode="track", video_repeat_mode="sequence",
            )
            self.window.store.add(source)
            running = 0
            maximum_running = 0
            lock = threading.Lock()

            def probe(_path: Path) -> float:
                nonlocal running, maximum_running
                with lock:
                    running += 1
                    maximum_running = max(maximum_running, running)
                threading.Event().wait(0.04)
                with lock:
                    running -= 1
                return 1.0

            with patch.object(PlaylistService, "_probe_duration", side_effect=probe) as duration:
                clips = self.window._export_video_clips([track], 3.0)

        self.assertEqual(duration.call_count, 3)
        self.assertGreaterEqual(maximum_running, 2)
        self.assertEqual(len(clips), 3)

    def test_video_clip_geometry_and_blur_follow_the_export_render_scale(self) -> None:
        with TemporaryDirectory(prefix="clip-render-scale-") as raw_directory:
            clip_path = Path(raw_directory) / "clip.mp4"
            clip_path.touch()
            source = Source(
                SourceType.VIDEO, "Overlay clip",
                x=100.0, y=50.0, width=400.0, height=300.0,
                blur=8.0, border_radius=20.0,
                video_timing_mode="timeline", video_paths=[str(clip_path)],
            )
            self.window.store.add(source)
            track = PlaylistTrack("song.wav", "Song", duration_seconds=3.0)

            with patch.object(
                PlaylistService, "_probe_duration", return_value=3.0,
            ):
                base = self.window._export_video_clips([track], 3.0, render_scale=1.0)
                scaled = self.window._export_video_clips(
                    [track], 3.0, WORK_MODE_AUTO, 1.5,
                )

        self.assertEqual((base[0].width, base[0].height), (400, 300))
        self.assertEqual((scaled[0].width, scaled[0].height), (600, 450))
        self.assertEqual((scaled[0].x, scaled[0].y), (150, 75))
        self.assertAlmostEqual(scaled[0].blur, 12.0)
        self.assertAlmostEqual(scaled[0].border_radius, 30.0)

    def test_export_locks_and_restores_every_main_form_interaction(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.window._export_dialog = dialog
        try:
            self.window._lock_main_form_for_export()

            self.assertFalse(self.window.centralWidget().isEnabled())
            self.assertFalse(self.window.menuBar().isEnabled())
            self.assertFalse(self.window.toolbar.isEnabled())
            self.assertFalse(self.window.export_action.isEnabled())
            self.assertFalse(self.window.acceptDrops())
            self.assertTrue(dialog.isEnabled())
            self.assertEqual(
                dialog.windowModality(), Qt.WindowModality.WindowModal,
            )

            self.window._unlock_main_form_after_export()

            self.assertTrue(self.window.centralWidget().isEnabled())
            self.assertTrue(self.window.menuBar().isEnabled())
            self.assertTrue(self.window.toolbar.isEnabled())
            self.assertTrue(self.window.export_action.isEnabled())
            self.assertTrue(self.window.acceptDrops())
        finally:
            self.window._unlock_main_form_after_export()
            self.window._export_dialog = None

    def test_export_progress_can_minimize_and_restore_with_main_window(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        self.window._export_dialog = dialog
        self.window._lock_main_form_for_export()
        dialog.minimize_requested.connect(self.window._minimize_during_export)
        try:
            self.window.show()
            dialog.show()
            self.application.processEvents()
            self.assertEqual(dialog.minimize_button.text(), "최소화")

            QTest.mouseClick(dialog.minimize_button, Qt.MouseButton.LeftButton)
            self.application.processEvents()
            self.assertTrue(self.window.isMinimized())
            self.assertFalse(dialog.isVisible())
            self.assertTrue(self.window._export_restore_pending)

            self.window.showNormal()
            self.application.processEvents()
            self.application.processEvents()
            self.assertFalse(self.window.isMinimized())
            self.assertTrue(dialog.isVisible())
            self.assertFalse(self.window._export_restore_pending)
        finally:
            dialog.complete(False)
            self.window._export_dialog = None
            self.window._unlock_main_form_after_export()
            dialog.complete(False)

    def test_export_cancel_requires_confirmation(self) -> None:
        dialog = ExportProgressDialog(self.window)
        requested: list[bool] = []
        dialog.cancel_requested.connect(lambda: requested.append(True))
        try:
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No,
            ):
                self.assertFalse(dialog.request_cancel())
            self.assertEqual(requested, [])
            self.assertTrue(dialog.cancel_button.isEnabled())

            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes,
            ):
                self.assertTrue(dialog.request_cancel())
            self.assertEqual(requested, [True])
            self.assertTrue(dialog.is_cancelling)
            self.assertFalse(dialog.cancel_button.isEnabled())
        finally:
            dialog.complete(False)

    def test_export_progress_translates_renderer_and_installer_messages(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            translations = {
                "Analyzing audio and rendering Python visualizer frames":
                    "오디오를 분석하고 비주얼라이저 프레임을 생성하는 중",
                "Normalizing track.mp3": "오디오 정규화 중 · track.mp3",
                "Normalizing 6 independent track(s) with 4 parallel worker(s)":
                    "독립 오디오 6곡 정규화 준비 · 병렬 작업 4개",
                "Normalized 3/6 tracks": "오디오 정규화 완료 3/6곡",
                "Inserted 2.5s of silence": "무음 구간 2.5s 추가",
                "Encoding 12.0s / 60.0s": "영상 인코딩 중 · 12.0s / 60.0s",
                "Combining audio 12.0s / 60.0s · 20%":
                    "오디오 결합 중 · 12.0s / 60.0s · 20%",
                "Preparing visual layer 1/2 · frame 8/20 · 40%":
                    "시각 레이어 준비 중 · 1/2 · 프레임 8/20 · 40%",
                "Downloaded 24.0 MB": "다운로드됨 · 24.0 MB",
                "Checksum verified; extracting archive safely":
                    "체크섬 검증 완료 · 안전하게 압축 해제 중",
            }
            for source, expected in translations.items():
                self.assertEqual(dialog._detail_text(source), expected)
            self.assertEqual(
                dialog._stage_text("Preparing visualizers"),
                "음악 반응 효과 준비",
            )
            self.assertEqual(dialog._stage_text("Downloading FFmpeg"), "FFmpeg 다운로드")
            # The status-bar progress details show the same localized line and phases.
            self.assertEqual(dialog.status_line("Encoding video", "Encoding 12.0s / 60.0s"),
                             "최종 영상 만들기 · 영상 인코딩 중 · 12.0s / 60.0s")
            self.assertEqual(dialog.step_progress("Preparing audio"), [
                ("화면 준비", 1.0), ("오디오 준비", None), ("효과 준비", 0.0), ("영상 만들기", 0.0),
            ])
            combined = dialog._detail_text(
                "Visualizer 1/2 · frame 12/30 · 40.0%\n"
                "Visualizer 2/2 · frame 6/30 · 20.0%\n"
                "All visualizers · frame 18/60 · 30.0%"
            )
            self.assertIn("비주얼라이저 1/2 · 프레임 12/30", combined)
            self.assertIn("비주얼라이저 2/2 · 프레임 6/30", combined)
            self.assertIn("전체 비주얼라이저 · 프레임 18/60", combined)
            dialog.set_busy(
                "Preparing visualizers",
                "Analyzing audio and rendering Python visualizer frames",
            )
            self.assertEqual(dialog.stage_label.text(), "음악 반응 효과 준비")
            self.assertEqual(
                dialog.detail_label.text(),
                "오디오를 분석하고 비주얼라이저 프레임을 생성하는 중",
            )
        finally:
            dialog.complete(False)

    def test_export_progress_shows_remaining_time_only_in_dedicated_label(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            with patch.object(dialog._eta_estimator, "update", return_value=40.0):
                dialog.update_progress(
                    "Preparing visualizers", 0.7,
                    "Visualizer 1/2 · frame 120/300 · 20.0% · about 00:40 remaining",
                )
            self.assertNotIn("남은", dialog.stage_label.text())
            self.assertNotIn("남은", dialog.detail_label.text())
            self.assertNotIn("남은", dialog.log_output.toPlainText())
            self.assertIn("남은 시간 약", dialog.time_label.text())
            self.assertIn("비주얼라이저 1/2", dialog.detail_label.text())

            with patch.object(dialog._eta_estimator, "remaining", return_value=35.0):
                dialog.set_busy("Preparing export", "Preparing temporary files")
            # A short indeterminate hand-off must keep counting down the last
            # stable estimate instead of blanking it at every stage boundary.
            self.assertIn("남은 시간 약", dialog.time_label.text())
        finally:
            dialog.complete(False)

    def test_export_progress_steps_and_technical_details_are_user_friendly(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            self.assertEqual(
                [label.property("stepState") for label in dialog.step_labels],
                ["active", "pending", "pending", "pending", "pending"],
            )
            dialog.update_progress(
                "Combining audio", 0.60,
                "Combining audio 12.0s / 60.0s · 20%",
            )
            self.assertEqual(
                [label.property("stepState") for label in dialog.step_labels],
                ["completed", "active", "pending", "pending", "pending"],
            )
            dialog.update_progress("Encoding video", 0.80, "Encoding 8.0s / 60.0s")
            self.assertEqual(
                [label.property("stepState") for label in dialog.step_labels],
                ["completed", "completed", "completed", "active", "pending"],
            )
            friendly = dialog._detail_text(
                "Z 레이어 2 인코더 버퍼 처리 중 · "
                "17,827/17,828 프레임 · 100% · 창을 닫지 않아도 계속 진행됩니다."
            )
            self.assertEqual(
                friendly,
                "화면 구성 요소를 영상으로 변환하는 중 · "
                "17,827/17,828 프레임 · 100%",
            )
            self.assertNotIn("Z 레이어", friendly)
            self.assertNotIn("버퍼", friendly)
            self.assertFalse(dialog.log_output.isVisible())
            dialog.details_button.setChecked(True)
            self.assertFalse(dialog.log_output.isHidden())
            self.assertEqual(dialog.details_button.text(), "상세 현황 숨기기")
        finally:
            dialog.complete(False)

    def test_export_progress_resize_keeps_summary_controls_anchored(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_export_details(
            3, 145.5,
            "Resolution 1920 × 1080 · 30 FPS\nVideo encoder NVIDIA NVENC",
            Path("C:/Videos/playlist.mp4"),
        )
        try:
            dialog.resize(680, 470)
            dialog.show()
            self.application.processEvents()
            original_positions = {
                "steps": dialog.steps_widget.geometry().top(),
                "settings": dialog.export_settings_label.geometry().top(),
                "stage": dialog.stage_label.geometry().top(),
                "progress": dialog.progress_bar.geometry().top(),
                "detail": dialog.detail_label.geometry().top(),
                "time": dialog.time_label.geometry().top(),
            }

            dialog.resize(680, 780)
            self.application.processEvents()
            resized_positions = {
                "steps": dialog.steps_widget.geometry().top(),
                "settings": dialog.export_settings_label.geometry().top(),
                "stage": dialog.stage_label.geometry().top(),
                "progress": dialog.progress_bar.geometry().top(),
                "detail": dialog.detail_label.geometry().top(),
                "time": dialog.time_label.geometry().top(),
            }

            self.assertEqual(resized_positions, original_positions)
            self.assertGreater(
                dialog.buttons.geometry().top(), dialog.time_label.geometry().bottom(),
            )

            dialog.details_button.setChecked(True)
            self.application.processEvents()
            initial_log_height = dialog.log_output.height()
            dialog.resize(680, 920)
            self.application.processEvents()
            self.assertGreater(dialog.log_output.height(), initial_log_height)
        finally:
            dialog.complete(False)

    def test_export_eta_blends_recent_stage_speed_and_counts_down_while_busy(self) -> None:
        estimator = ExportEtaEstimator(0.0)

        self.assertIsNone(estimator.update("Preparing visual frames", 0.01, 1.0))
        initial = estimator.update("Preparing visual frames", 0.10, 10.0)
        slowed = estimator.update("Preparing visual frames", 0.12, 20.0)

        self.assertIsNotNone(initial)
        self.assertIsNotNone(slowed)
        assert initial is not None and slowed is not None
        self.assertGreater(slowed, initial)
        self.assertAlmostEqual(
            estimator.remaining(25.0), max(0.0, slowed - 5.0), delta=0.001,
        )

    def test_export_eta_smooths_weighted_progress_jumps_between_stages(self) -> None:
        estimator = ExportEtaEstimator(0.0)
        before = estimator.update("Preparing visual frames", 0.25, 25.0)
        after = estimator.update("Preparing export", 0.50, 26.0)

        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        # A raw whole-export extrapolation falls from 75s to 26s here. Preserve
        # evidence from the completed preparation stage instead of presenting
        # that artificial progress-weight jump as a real 49-second speed-up.
        self.assertGreater(after, 40.0)
        self.assertLess(after, before)

        self.assertEqual(estimator.update("Complete", 1.0, 60.0), 0.0)

    def test_export_progress_keeps_selected_settings_visible(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            dialog.set_export_details(
                12, 185.0,
                "해상도 3840 × 2160 · 60 FPS\n"
                "비디오 인코더 NVIDIA GPU · H.264 (NVENC)\n"
                "화질 CRF 18 · 인코딩 속도 medium · 오디오 AAC 320k",
                Path("C:/Videos/playlist.mp4"),
            )
            settings_text = dialog.export_settings_label.text()
            dialog.set_busy("Preparing visual frames", "화면 프레임 준비 중")
            dialog.update_progress("Combining audio", 0.6, "Combining audio 30.0s / 185.0s · 16%")

            self.assertEqual(dialog.export_settings_label.text(), settings_text)
            self.assertIn("3840 × 2160", settings_text)
            self.assertIn("60 FPS", settings_text)
            self.assertIn("NVENC", settings_text)
            self.assertIn("CRF 18", settings_text)
            self.assertIn("medium", settings_text)
            self.assertIn("AAC 320k", settings_text)
            self.assertIn("playlist.mp4", settings_text)
            self.assertIn("오디오 결합 중", dialog.detail_label.text())
        finally:
            dialog.complete(False)

    def test_export_progress_shows_live_storage_breakdown_and_disk_space(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            estimate = estimate_export_storage(
                1920, 1080, 30, 60.0, 18, "192k", 2,
            )
            dialog.set_storage_estimate(estimate)
            dialog.update_storage_snapshot(ExportStorageSnapshot(
                categories={
                    "visuals": 3 * 1024**3,
                    "audio": 256 * 1024**2,
                    "effects": 128 * 1024**2,
                    "processing": 16 * 1024**2,
                },
                temporary_total=4 * 1024**3,
                output_in_progress=640 * 1024**2,
                disk_total=1024 * 1024**3,
                disk_free=901 * 1024**3,
            ))

            self.assertEqual(dialog.storage_rows["visuals"][1].text(), "3.0 GB")
            self.assertEqual(dialog.storage_rows["output"][1].text(), "640.0 MB")
            self.assertEqual(dialog.storage_total_value.text(), "4.0 GB")
            self.assertIn("901.0 GB", dialog.storage_disk_label.text())
            self.assertIn("1.0 TB", dialog.storage_disk_label.text())
            self.assertIn("예상 결과 영상", dialog.storage_widget.toolTip())
        finally:
            dialog.complete(False)

    def test_export_progress_storage_panel_can_be_toggled_off_and_on(self) -> None:
        dialog = ExportProgressDialog(self.window)
        dialog.set_korean(True)
        try:
            self.assertTrue(dialog.storage_button.isChecked())
            self.assertFalse(dialog.storage_widget.isHidden())

            dialog.storage_button.setChecked(False)
            self.assertTrue(dialog.storage_widget.isHidden())
            self.assertTrue(dialog.storage_heading.isHidden())
            self.assertIn("보기", dialog.storage_button.text())

            dialog.storage_button.setChecked(True)
            self.assertFalse(dialog.storage_widget.isHidden())
            self.assertIn("숨기기", dialog.storage_button.text())
        finally:
            dialog.complete(False)

    def test_export_progress_dialog_grows_for_wrapped_content_instead_of_compressing(
        self,
    ) -> None:
        from PySide6.QtWidgets import QLayout

        dialog = ExportProgressDialog(self.window)
        dialog.resize(700, 620)
        try:
            self.assertEqual(
                dialog.layout().sizeConstraint(),
                QLayout.SizeConstraint.SetMinimumSize,
            )
            dialog.set_export_details(2, 120.0, "single line summary", "C:/out.mp4")
            dialog.layout().activate()
            short_hint = dialog.sizeHint().height()

            tall_summary = "\n".join(
                f"detail row {index} with a meaningful amount of text"
                for index in range(6)
            )
            dialog.set_export_details(2, 120.0, tall_summary, "C:/out.mp4")
            dialog.layout().activate()
            tall_hint = dialog.sizeHint().height()

            # More content must make the dialog want to be taller, not squeeze
            # the cards above the label.
            self.assertGreater(tall_hint, short_hint)
        finally:
            dialog.complete(False)


if __name__ == "__main__":
    unittest.main()
