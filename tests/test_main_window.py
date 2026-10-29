"""Main window workspace: canvas, inspector, sources, layers, menus and layout."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QPointF,
    QSettings,
    QSize,
    Qt,
    QUrl,
)
from PySide6.QtGui import QColor, QContextMenuEvent, QDropEvent, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFormLayout,
    QListView,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QDoubleSpinBox,
    QStyle,
    QStyleOptionSpinBox,
    QWidget,
)
from app.models.project import CanvasSettings, ProjectContent, ProjectDocument
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.help_dialog import HelpDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.dialogs.text_editor_dialog import TextEditorDialog
from app.dialogs.video_source_dialog import VideoSourceDialog
from app.widgets.source_template_button import SourceTemplateButton, read_source_template_mime
from app.services.app_settings_service import AppSettings
from app.services.theme_service import Theme
from app.ui.main_window import MainWindow
from app.utils.i18n import Language
from app.widgets.token_text_editor import TokenLineEdit, TokenPlainTextEdit
from app.renderer.ffmpeg_renderer import EncoderUnavailableError, FFmpegRenderer, RenderError
from app.services.video_encoder_service import (
    AUTO_VIDEO_ENCODER,
    CPU_H264_ENCODER,
    NVIDIA_H264_ENCODER,
    VideoEncoderAdvisor,
)
from app.presets.preset_service import PresetService
from tests.main_window_base import MainWindowTestCase


class MainWindowWorkspaceTests(MainWindowTestCase):
    def test_status_bar_activity_progress_tracks_multiple_operations(self) -> None:
        progress = self.window.activity_progress
        self.assertTrue(progress.isHidden())

        progress.begin("save", "프로젝트 저장", detail="example.pvsproj")
        self.assertFalse(progress.isHidden())
        self.assertEqual(progress.label.text(), "프로젝트 저장")  # one operation: its own title
        self.assertEqual(progress.progress_bar.minimum(), 0)
        self.assertEqual(progress.progress_bar.maximum(), 0)
        self.assertIn("example.pvsproj", progress.details_text())

        progress.begin("update", "업데이트 다운로드", 0.42, "Setup 다운로드 중")
        self.assertEqual(progress.label.text(), "작업 중...")  # several: the combined bar
        self.assertEqual(progress.progress_bar.value(), 420)  # the only measurable one
        self.assertIn("업데이트 다운로드 — 42%", progress.details_text())
        self.assertIn("프로젝트 저장", progress.details_text())
        self.assertEqual(progress.toolTip(), "")  # the live popup replaces the static tooltip

        progress.finish("update")
        self.assertEqual(progress.label.text(), "프로젝트 저장")
        progress.finish("save")
        self.assertTrue(progress.isHidden())

    def test_background_automix_analysis_shows_its_stages_after_a_short_delay(self) -> None:
        from pathlib import Path

        from app.controllers.automix_analysis_controller import _AutoMixAnalysisWorker
        from app.models.playlist import PlaylistTrack

        controller = self.window.automix_analysis_controller
        progress = self.window.activity_progress
        worker = _AutoMixAnalysisWorker([PlaylistTrack("a.mp3", "A", duration_seconds=30.0)], Path("ffmpeg"))
        controller._worker = worker
        self.addCleanup(lambda: setattr(controller, "_worker", None))
        controller.running_changed.emit(True)
        controller._report(worker, "rhythm", 0, 4)
        controller._report(worker, "structure", 0, 4)
        self.assertNotIn("automix_analysis", progress.active_keys)  # a cached pass ends before this
        self.assertTrue(self.window._automix_activity_timer.isActive())

        self.window._automix_activity_timer.timeout.emit()
        controller._report(worker, "rhythm", 2, 4)
        self.assertIn("automix_analysis", progress.active_keys)
        self.assertEqual(progress.progress_bar.value(), 250)  # 2 of 8 track-stages
        text = progress.details_text()
        self.assertIn("박자·보컬 분석 — 50% (2 / 4곡)", text)
        self.assertIn("곡 구조 분석 — 0%", text)

        controller.running_changed.emit(False)
        self.assertNotIn("automix_analysis", progress.active_keys)

    def test_menu_bar_is_grouped_and_fully_localized(self) -> None:
        original_language = self.window.translator.language
        try:
            self.window.translator.set_language(Language.KOREAN)
            self.application.processEvents()
            top_titles = [
                action.text() for action in self.window.menuBar().actions()
            ]
            self.assertEqual(
                top_titles,
                ["파일", "프로젝트", "편집", "추가", "보기", "도구", "도움말"],
            )
            self.assertIn(self.window.export_action, self.window.file_menu.actions())
            self.assertIn(
                self.window.recent_projects_menu.menuAction(),
                self.window.file_menu.actions(),
            )
            self.assertEqual(self.window.recent_projects_menu.title(), "최근 프로젝트")
            self.assertNotIn(self.window.preview_action, self.window.file_menu.actions())
            self.assertIn(self.window.presets_action, self.window.project_menu.actions())
            self.assertIn(self.window.preview_action, self.window.view_menu.actions())
            self.assertIn(self.window.settings_action, self.window.tools_menu.actions())
            self.assertIn(self.window.lrc_generator_action, self.window.tools_menu.actions())
            self.assertEqual(self.window.lrc_generator_action.text(), "가사/자막 편집기")
            self.assertIn(self.window.automix_editor_action, self.window.tools_menu.actions())
            self.assertEqual(self.window.automix_editor_action.text(), "AutoMix 편집기")
            from dataclasses import replace
            original_settings = self.window.project_settings
            try:
                for mode in ("none", "automix"):
                    self.window.project_settings = replace(original_settings, transition_mode=mode)
                    self.assertEqual(self.window.automix_editor_action.isEnabled(), mode == "automix")
            finally:
                self.window.project_settings = original_settings
            # Every menu row shows an icon, except check-mark rows and the recent-files list.
            for menu in self.window.menuBar().findChildren(QMenu):
                if menu is self.window.recent_projects_menu or not menu.title():
                    continue  # untitled: Qt's own overflow menu
                for action in menu.actions():
                    if not (action.isSeparator() or action.isCheckable()):
                        self.assertFalse(action.icon().isNull(), f"{menu.title()} > {action.text()}")
            self.assertEqual(
                set(self.window.source_insert_actions), set(SourceType)
            )
            self.assertEqual(len(self.window.insert_category_menus), 4)
            for action in self.window.source_insert_actions.values():
                self.assertTrue(action.text())
                self.assertTrue(action.statusTip())

            before = len(self.window.store.sources())
            self.window.source_insert_actions[SourceType.IMAGE].trigger()
            self.assertEqual(len(self.window.store.sources()), before + 1)

            self.window.translator.set_language(Language.ENGLISH)
            self.application.processEvents()
            self.assertEqual(
                [action.text() for action in self.window.menuBar().actions()],
                ["File", "Project", "Edit", "Add", "View", "Tools", "Help"],
            )
            self.assertEqual(self.window.exit_action.text(), "Exit")
            self.assertEqual(self.window.recent_projects_menu.title(), "Recent projects")
            self.assertEqual(
                self.window.lrc_generator_action.text(), "Lyrics/Subtitle Editor"
            )
            self.assertEqual(
                self.window.clear_selection_action.text(), "Clear selection"
            )
            self.assertEqual(
                self.window.insert_category_menus["audio_effects"].title(),
                "Audio visuals",
            )
        finally:
            self.window.translator.set_language(original_language)
            self.application.processEvents()

    def test_view_menu_toggles_each_workspace_panel_with_shortcuts(self) -> None:
        settings = QSettings()
        keys = (
            "workspace/left_panel_visible",
            "workspace/right_panel_visible",
            "workspace/bottom_panel_visible",
        )
        original_values = {key: settings.value(key, None) for key in keys}
        actions = (
            self.window.panels_action,
            self.window.inspector_panel_action,
            self.window.bottom_panel_action,
        )
        try:
            for action in actions:
                action.setChecked(True)
            QTest.qWait(230)
            self.assertTrue(all(
                action in self.window.view_menu.actions() for action in actions
            ))
            self.assertEqual(
                [action.shortcut().toString() for action in actions],
                ["Ctrl+Alt+L", "Ctrl+Alt+R", "Ctrl+Alt+B"],
            )

            self.window.panels_action.trigger()
            self.application.processEvents()
            self.assertFalse(self.window.left_workspace.isHidden())
            QTest.qWait(230)
            self.assertTrue(self.window.left_workspace.isHidden())
            self.assertFalse(self.window.panels_action.isChecked())

            self.window.inspector_panel_action.trigger()
            self.application.processEvents()
            self.assertFalse(self.window.inspector_stack.isHidden())
            QTest.qWait(230)
            self.assertTrue(self.window.inspector_stack.isHidden())
            self.assertFalse(self.window.inspector_panel_action.isChecked())

            self.window.bottom_panel_action.trigger()
            self.application.processEvents()
            self.assertFalse(self.window.bottom_workspace_stack.isHidden())
            QTest.qWait(230)
            self.assertTrue(self.window.bottom_workspace_stack.isHidden())
            self.assertFalse(self.window.bottom_panel_action.isChecked())

            self.window._show_bottom_panel(1)
            QTest.qWait(230)
            self.assertFalse(self.window.bottom_workspace_stack.isHidden())
            self.assertTrue(self.window.bottom_panel_action.isChecked())
            self.assertEqual(self.window.bottom_tabs.currentIndex(), 1)

            # Preview can be closed while its sidebar collapse is still moving.
            # Reversing that transition must leave the editable sidebar open.
            self.window._set_sidebar_visible(
                False, persist=False, sync_action=False,
            )
            QTest.qWait(40)
            self.window._set_sidebar_visible(
                True, persist=False, sync_action=False,
            )
            QTest.qWait(230)
            self.assertFalse(self.window.left_workspace.isHidden())
            self.assertGreaterEqual(self.window.left_workspace.minimumWidth(), 180)
        finally:
            for action in actions:
                action.setChecked(True)
            QTest.qWait(230)
            for key, value in original_values.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)

    def test_ask_mode_prompts_before_attaching_an_exact_match(self) -> None:
        with patch.object(
            QMessageBox, "question",
            return_value=QMessageBox.StandardButton.No,
        ) as question:
            imported, notes = self._import_with_sidecar(
                {"Nightfall.lrc": self._LRC}, "ask", "Nightfall.mp3",
            )
        question.assert_called_once()
        self.assertFalse(imported.lyrics)
        self.assertEqual(notes, [])

    def test_legacy_theme_choices_keep_studio_and_open_dialogs_dark(self) -> None:
        dialog = QDialog(self.window)
        dialog.show()
        try:
            for preference in Theme:
                self.window.theme_service.set_preference(preference)
                self.application.processEvents()
                self.assertIs(self.window.theme_service.preference, Theme.DARK)
                self.assertLess(self.application.palette().window().color().lightness(), 128)
                self.assertLess(dialog.palette().window().color().lightness(), 128)
            self.assertIn("QMenu::item:selected", self.application.styleSheet())
            self.assertFalse(hasattr(self.window, "theme_menu"))
        finally:
            dialog.close()

    def test_source_buttons_show_localized_settings_on_hover(self) -> None:
        original_language = self.window.translator.language
        try:
            self.window.translator.set_language(Language.KOREAN)
            self.application.processEvents()
            self.assertEqual(set(self.window._source_buttons), set(SourceType))
            for button in self.window._source_buttons.values():
                self.assertIn("추가 후 설정", button.toolTip())
                self.assertIn("캔버스로 드래그하면 추가됩니다.", button.toolTip())
                self.assertGreaterEqual(button.toolTipDuration(), 10_000)
                self.assertTrue(button.accessibleDescription())

            self.window.translator.set_language(Language.ENGLISH)
            self.application.processEvents()
            for button in self.window._source_buttons.values():
                self.assertIn("Settings after adding", button.toolTip())
                self.assertIn("drag it onto the Canvas", button.toolTip())
        finally:
            self.window.translator.set_language(original_language)
            self.application.processEvents()

    def test_spin_boxes_have_separate_vertical_step_button_hit_areas(self) -> None:
        # Inspector inputs are hidden and disabled until a source is selected.
        # Unpolished, hidden controls have unreliable QStyle geometry under
        # the offscreen Qt platform. Check the global style on real, visible
        # controls at the inspector's minimum editor width instead.
        for spin in (QSpinBox(), QDoubleSpinBox()):
            with self.subTest(spin=type(spin).__name__):
                spin.setRange(-100, 100)
                spin.setValue(0)
                spin.setFixedSize(96, 36)
                spin.show()
                try:
                    self.application.processEvents()
                    option = QStyleOptionSpinBox()
                    spin.initStyleOption(option)
                    style = spin.style()
                    up_rect = style.subControlRect(
                        QStyle.ComplexControl.CC_SpinBox, option,
                        QStyle.SubControl.SC_SpinBoxUp, spin,
                    )
                    down_rect = style.subControlRect(
                        QStyle.ComplexControl.CC_SpinBox, option,
                        QStyle.SubControl.SC_SpinBoxDown, spin,
                    )
                    edit_rect = style.subControlRect(
                        QStyle.ComplexControl.CC_SpinBox, option,
                        QStyle.SubControl.SC_SpinBoxEditField, spin,
                    )
                    self.assertGreater(up_rect.height(), 0)
                    self.assertGreater(down_rect.height(), 0)
                    self.assertLessEqual(up_rect.bottom(), down_rect.top())
                    # QRect includes both boundary pixels. A styled step
                    # button can share its 1px border with the edit field;
                    # this is not an overlapping interactive hit area.
                    for step_rect in (up_rect, down_rect):
                        self.assertLessEqual(
                            step_rect.intersected(edit_rect).width(), 1,
                            f"step={step_rect}, edit={edit_rect}",
                        )
                        self.assertGreater(
                            step_rect.center().x(), edit_rect.right(),
                            f"step={step_rect}, edit={edit_rect}",
                        )

                    before = spin.value()
                    QTest.mouseClick(
                        spin, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier, up_rect.center(),
                    )
                    self.assertGreater(spin.value(), before)
                    raised = spin.value()
                    QTest.mouseClick(
                        spin, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier, down_rect.center(),
                    )
                    self.assertLess(spin.value(), raised)
                finally:
                    spin.close()

    def test_inspector_continuous_values_pair_sliders_with_precise_spins(self) -> None:
        inspector = self.window.inspector
        linked = inspector._linked_sliders
        for spin in (
            inspector.width_spin, inspector.height_spin, inspector.rotation_spin,
            inspector.scale_spin, inspector.opacity_spin, inspector.radius_spin,
            inspector.font_size_spin, inspector.blur_spin,
            inspector.brightness_spin, inspector.contrast_spin,
            inspector.shadow_opacity_spin, inspector.shadow_blur_spin,
        ):
            with self.subTest(control=spin):
                self.assertIn(spin, linked)
                self.assertIs(inspector._slider_hosts[spin], spin.parentWidget())

        self.assertNotIn(inspector.x_spin, linked)
        opacity_slider = linked[inspector.opacity_spin]
        opacity_slider.setValue(12)
        self.assertAlmostEqual(inspector.opacity_spin.value(), 0.6)
        inspector.opacity_spin.setValue(0.35)
        self.assertEqual(opacity_slider.value(), 7)

        # Exact input still accepts values beyond the ergonomic drag range.
        inspector.width_spin.setValue(3200)
        self.assertEqual(inspector.width_spin.value(), 3200)
        self.assertEqual(linked[inspector.width_spin].value(),
                         linked[inspector.width_spin].maximum())

        first = Source(SourceType.TEXT, "First", opacity=0.25)
        second = Source(SourceType.TEXT, "Second", opacity=0.75)
        self.window.store.replace([first, second])
        inspector.set_sources((first.id, second.id), second)
        self.assertFalse(opacity_slider.isEnabled())
        self.assertEqual(inspector.opacity_spin.lineEdit().text(), "")
        inspector.set_source(first)
        self.assertTrue(opacity_slider.isEnabled())
        self.assertAlmostEqual(inspector.opacity_spin.value(), 0.25)

    def test_source_sidebar_has_no_footer_tip(self) -> None:
        self.assertFalse(hasattr(self.window, "sidebar_hint"))
        self.assertIs(self.window.source_cards_scroll.parent(), self.window.source_sidebar)

    def test_source_sidebar_groups_rich_cards_and_filters_whole_sections(self) -> None:
        self.assertEqual(
            set(self.window._source_category_sections),
            {"basic", "playback", "audio", "scene"},
        )
        image_button = self.window._source_buttons[SourceType.IMAGE]
        self.assertTrue(image_button.title_label.text())
        self.assertTrue(image_button.description_label.text())
        self.assertFalse(image_button.icon_label.pixmap().isNull())
        self.assertGreaterEqual(image_button.minimumHeight(), 50)

        self.window.source_search.setText("audio_visualizer")
        self.application.processEvents()
        self.assertFalse(
            self.window._source_category_sections["audio"].isHidden()
        )
        for category in ("basic", "playback", "scene"):
            self.assertTrue(
                self.window._source_category_sections[category].isHidden(),
                category,
            )
        self.assertIn("1", self.window.source_result_label.text())

        self.window.source_search.clear()
        self.application.processEvents()
        self.assertTrue(all(
            not section.isHidden()
            for section in self.window._source_category_sections.values()
        ))
        self.assertIn("17", self.window.source_result_label.text())

    def test_source_palette_category_tabs_and_search_interplay(self) -> None:
        window = self.window
        tab_bar = window.source_tab_bar
        self.assertEqual(
            [tab_bar.tabText(i) for i in range(tab_bar.count())],
            ["전체", "기본", "재생", "오디오", "장면"],
        )

        # A category tab shows only its own section.
        tab_bar.tabBarClicked.emit(3)  # "audio"
        self.application.processEvents()
        self.assertEqual(window._active_source_category, "audio")
        self.assertFalse(window._source_category_sections["audio"].isHidden())
        for other in ("basic", "playback", "scene"):
            self.assertTrue(window._source_category_sections[other].isHidden(), other)

        # Typing a search pins the bar back to "All" and spans every section.
        window.source_search.setText("progress")
        self.application.processEvents()
        self.assertEqual(tab_bar.currentIndex(), 0)
        self.assertEqual(window._active_source_category, "all")
        self.assertFalse(window._source_category_sections["playback"].isHidden())

        # Clicking a category while searching clears the query and restores it.
        tab_bar.tabBarClicked.emit(1)  # "basic"
        self.application.processEvents()
        self.assertEqual(window.source_search.text(), "")
        self.assertEqual(window._active_source_category, "basic")
        self.assertFalse(window._source_category_sections["basic"].isHidden())
        self.assertTrue(window._source_category_sections["audio"].isHidden())

        tab_bar.tabBarClicked.emit(0)  # back to "All"
        self.application.processEvents()
        self.assertTrue(all(
            not section.isHidden()
            for section in window._source_category_sections.values()
        ))

    def test_background_defaults_unlocked_and_album_inspector_size_stays_square(self) -> None:
        welcome_background = next(
            source for source in self.window.store.sources()
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertFalse(welcome_background.locked)

        self.window._add_source(SourceType.BACKGROUND)
        self.assertFalse(self.window.store.selected.locked)
        preset_background = next(
            source for source in PresetService.all()[0].builder()
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertFalse(preset_background.locked)

        self.window._add_source(SourceType.ALBUM_COVER)
        cover = self.window.store.selected
        self.window.store.update(cover.id, width=245.0)
        self.assertEqual((cover.width, cover.height), (245.0, 245.0))
        self.window.store.update(cover.id, height=132.0)
        self.assertEqual((cover.width, cover.height), (132.0, 132.0))

    def test_image_variants_expand_under_parent_and_create_parent_sources(self) -> None:
        toggle = self.window._source_variant_toggles[SourceType.IMAGE]
        container = self.window._source_variant_containers[SourceType.IMAGE]
        self.assertFalse(toggle.isChecked())
        self.assertTrue(container.isHidden())

        toggle.click()
        self.application.processEvents()
        self.assertTrue(toggle.isChecked())
        self.assertFalse(container.isHidden())
        self.assertIs(
            self.window._source_variant_parents[SourceType.LOGO], SourceType.IMAGE
        )
        self.assertIs(
            self.window._source_variant_parents[SourceType.TIME], SourceType.TEXT
        )

        before = len(self.window.store.sources())
        self.window._source_buttons[SourceType.WATERMARK].click()
        self.assertEqual(len(self.window.store.sources()), before + 1)
        source = self.window.store.selected
        self.assertIsNotNone(source)
        self.assertIs(source.source_type, SourceType.IMAGE)
        self.assertEqual(source.name, "Watermark")
        self.assertEqual(source.image_fit_mode, "contain")
        self.assertAlmostEqual(source.opacity, 0.45)

        self.window._source_buttons[SourceType.TIME].click()
        source = self.window.store.selected
        self.assertIsNotNone(source)
        self.assertIs(source.source_type, SourceType.TEXT)
        self.assertEqual(source.text, "%current_time% / %total_time%")

    def test_source_template_drag_payload_adds_parent_at_drop_position(self) -> None:
        button = self.window._source_buttons[SourceType.LOGO]
        self.assertIsInstance(button, SourceTemplateButton)
        self.assertEqual(
            read_source_template_mime(button.create_mime_data()),
            (SourceType.LOGO.value, SourceType.IMAGE.value),
        )

        before = len(self.window.store.sources())
        self.window.canvas.source_template_dropped.emit(
            SourceType.LOGO.value, SourceType.IMAGE.value, QPointF(640.0, 360.0)
        )
        self.assertEqual(len(self.window.store.sources()), before + 1)
        source = self.window.store.selected
        self.assertIsNotNone(source)
        self.assertIs(source.source_type, SourceType.IMAGE)
        self.assertEqual((source.x, source.y), (550.0, 270.0))

        self.window.canvas.source_template_dropped.emit(
            SourceType.LOGO.value, SourceType.TEXT.value, QPointF(20.0, 20.0)
        )
        self.assertEqual(len(self.window.store.sources()), before + 1)

    def test_canvas_accepts_source_template_drop_event(self) -> None:
        button = self.window._source_buttons[SourceType.WATERMARK]
        before = len(self.window.store.sources())
        mime_data = button.create_mime_data()
        event = QDropEvent(
            QPointF(160.0, 140.0),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.window.canvas.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(len(self.window.store.sources()), before + 1)
        source = self.window.store.selected
        self.assertIsNotNone(source)
        self.assertIs(source.source_type, SourceType.IMAGE)
        artboard = self.window.canvas.scene_model.artboard_rect
        self.assertGreaterEqual(source.x, artboard.left())
        self.assertGreaterEqual(source.y, artboard.top())
        self.assertLessEqual(source.x + source.width, artboard.right())
        self.assertLessEqual(source.y + source.height, artboard.bottom())

    def test_project_content_image_and_video_urls_create_matching_canvas_elements(self) -> None:
        with TemporaryDirectory(prefix="playlist-content-canvas-drop-") as directory:
            image_path = Path(directory) / "cover.png"
            image = QImage(96, 54, QImage.Format.Format_ARGB32)
            image.fill(QColor("#336699"))
            self.assertTrue(image.save(str(image_path)))
            video_path = Path(directory) / "clip.mp4"
            video_path.write_bytes(b"project content video placeholder")
            self.window.project_content_service.add_paths([image_path, video_path])
            before = len(self.window.store.sources())

            for path in (image_path, video_path):
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(str(path))])
                event = QDropEvent(
                    QPointF(240.0, 180.0), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                )
                self.window.canvas.dropEvent(event)
                self.assertTrue(event.isAccepted())
            added = self.window.store.sources()[before:]
            self.assertEqual(
                [source.source_type for source in added],
                [SourceType.IMAGE, SourceType.VIDEO],
            )
            self.assertEqual(Path(added[0].content_path).name, "cover.png")
            self.assertEqual(Path(added[1].content_path).name, "clip.mp4")
            for source in added:
                self.window.store.remove(source.id)
            self.application.processEvents()
            QTest.qWait(50)

    def test_project_content_items_publish_native_local_file_drag_payloads(self) -> None:
        with TemporaryDirectory(prefix="playlist-content-mime-") as directory:
            image_path = Path(directory) / "drag-cover.png"
            image = QImage(20, 20, QImage.Format.Format_ARGB32)
            image.fill(QColor("#123456"))
            self.assertTrue(image.save(str(image_path)))
            self.window.project_content_service.add_paths([image_path])
            self.application.processEvents()

            content_list = self.window.content_library_panel.list
            item = next(
                content_list.item(index) for index in range(content_list.count())
                if Path(str(content_list.item(index).data(
                    Qt.ItemDataRole.UserRole + 1
                ))).name == image_path.name
            )
            content_list.setCurrentItem(item)
            mime = content_list.mimeData([item])

            self.assertTrue(item.flags() & Qt.ItemFlag.ItemIsDragEnabled)
            self.assertEqual(
                content_list.supportedDropActions(), Qt.DropAction.CopyAction,
            )
            self.assertTrue(mime.hasUrls())
            self.assertEqual(
                Path(mime.urls()[0].toLocalFile()), image_path.resolve(),
            )

    def test_inspector_properties_show_localized_detailed_hover_help(self) -> None:
        original_language = self.window.translator.language
        inspector = self.window.inspector
        try:
            self.window.translator.set_language(Language.KOREAN)
            self.application.processEvents()
            self.assertGreaterEqual(len(inspector._form_labels), 80)
            for key, label in inspector._form_labels.items():
                widget = inspector._field_widgets[key]
                self.assertTrue(label.toolTip(), key)
                self.assertEqual(label.toolTip(), widget.toolTip(), key)
                self.assertIn(label.text(), label.toolTip(), key)
                self.assertGreaterEqual(widget.toolTipDuration(), 10_000, key)
                self.assertTrue(widget.accessibleDescription(), key)
            self.assertIn("범위", inspector.width_spin.toolTip())
            self.assertIn("조절 단위", inspector.visualizer.widgets["visualizer_sensitivity"].toolTip())
            self.assertIn("완전히 투명", inspector.particle.widgets["particle_opacity"].toolTip())
            self.assertTrue(inspector.visible_check.toolTip())
            self.assertTrue(inspector.locked_check.toolTip())

            self.window.translator.set_language(Language.ENGLISH)
            self.application.processEvents()
            self.assertIn("Range", inspector.width_spin.toolTip())
            self.assertIn("Stacking order", inspector.z_spin.toolTip())
            self.assertIn("current-track data", inspector.text_edit.toolTip())
        finally:
            self.window.translator.set_language(original_language)
            self.application.processEvents()

    def test_field_specific_guidance_survives_the_shared_hover_help(self) -> None:
        # These notes used to be set with setToolTip and then silently replaced
        # by the shared hover help, so users never saw them.
        inspector = self.window.inspector
        expected = {
            "subtitle_animation": "라이즈",
            "subtitle_context_lines": "줄 간격에 맞춰",
            "subtitle_next_lines": "줄 간격에 맞춰",
            "text": "%video_total_time%",
            "track_list_count": "표시 곡 수가 바뀝니다",
            "visualizer_min_level": "0으로 설정",
            "visualizer_curve": "1보다 작으면",
        }
        for key, fragment in expected.items():
            with self.subTest(key=key):
                field = inspector._field_widgets[key]
                self.assertTrue(field.toolTip().startswith("<div"))  # still the shared rich help
                self.assertIn(fragment, field.toolTip())
                self.assertIn(fragment, field.accessibleDescription())
        self.assertIn("%video_total_time%", inspector.text_edit.toolTip())

    def test_fill_color_alpha_can_make_only_the_background_transparent(self) -> None:
        self.window.translator.set_language(Language.ENGLISH)
        source = Source(
            SourceType.TEXT, "Transparent background", text="Visible text",
            fill_color="#334455", outline_color="#FFFFFF", opacity=0.7,
        )
        self.window.store.add(source)
        self.application.processEvents()

        transparent = QColor("#334455")
        transparent.setAlpha(0)
        fake_dialog = MagicMock()
        fake_dialog.exec.return_value = QDialog.DialogCode.Accepted
        fake_dialog.selected_color = transparent
        fake_dialog.personal_settings.return_value = {
            "personal_color_enabled": False,
            "personal_color_brightness": 0.0,
            "personal_color_saturation": 0.0,
            "personal_color_hue_shift": 0.0,
            "personal_color_strength": 1.0,
        }
        with patch(
            "app.inspector.source_inspector.ColorEditorDialog",
            return_value=fake_dialog,
        ):
            self.window.inspector._choose_color(
                "fill_color", self.window.inspector.fill_color_button,
            )

        self.assertEqual(source.fill_color, "#00334455")
        self.assertEqual(source.opacity, 0.7)
        self.assertEqual(
            self.window.inspector.fill_color_button.text(), "Transparent"
        )

    def test_color_editor_dialog_commits_color_and_personal_policy(self) -> None:
        first = Source(SourceType.SHAPE, "First personal color")
        second = Source(SourceType.SHAPE, "Second personal color")
        self.window.store.replace([first, second])
        self.window.store.select_many([first.id, second.id], second.id)
        inspector = self.window.inspector

        fake_dialog = MagicMock()
        fake_dialog.exec.return_value = QDialog.DialogCode.Accepted
        fake_dialog.selected_color = QColor("#123456")
        fake_dialog.personal_settings.return_value = {
            "personal_color_enabled": True,
            "personal_color_brightness": 18.0,
            "personal_color_saturation": -12.0,
            "personal_color_hue_shift": 35.0,
            "personal_color_strength": 0.7,
        }
        with patch(
            "app.inspector.source_inspector.ColorEditorDialog",
            return_value=fake_dialog,
        ):
            inspector._choose_color("fill_color", inspector.fill_color_button)

        for source in (first, second):
            self.assertEqual(source.fill_color, "#123456")
            self.assertTrue(source.personal_color_enabled)
            self.assertEqual(source.personal_color_brightness, 18.0)
            self.assertEqual(source.personal_color_saturation, -12.0)
            self.assertEqual(source.personal_color_hue_shift, 35.0)
            self.assertEqual(source.personal_color_strength, 0.7)

    def test_empty_inspector_centers_selection_hint(self) -> None:
        self.window.store.select(None)
        self.application.processEvents()
        inspector = self.window.inspector
        self.assertFalse(inspector.empty_state.isHidden())
        self.assertTrue(inspector._content.isHidden())
        self.assertEqual(
            inspector.empty_state.alignment(), Qt.AlignmentFlag.AlignCenter
        )
        self.assertEqual(
            inspector.empty_state.text(), "요소를 선택해 속성을 편집하세요."
        )
        source = self.window.store.sources()[0]
        self.window.store.select(source.id)
        self.application.processEvents()
        self.assertTrue(inspector.empty_state.isHidden())
        self.assertFalse(inspector._content.isHidden())

    def test_inspector_uses_responsive_forms_without_horizontal_scrolling(self) -> None:
        inspector = self.window.inspector
        source = Source(SourceType.LYRICS, "Responsive inspector")
        self.window.store.add(source)
        self.window.store.select(source.id)
        inspector.resize(inspector.minimumWidth(), 520)
        self.application.processEvents()

        self.assertGreaterEqual(inspector.minimumWidth(), 290)
        self.assertEqual(
            inspector.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertEqual(inspector.horizontalScrollBar().maximum(), 0)
        forms = inspector._content.findChildren(QFormLayout)
        self.assertTrue(forms)
        for form in forms:
            self.assertEqual(
                form.rowWrapPolicy(), QFormLayout.RowWrapPolicy.WrapLongRows,
            )
            self.assertEqual(
                form.fieldGrowthPolicy(),
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow,
            )

    def test_window_resize_changes_only_the_central_canvas_extent(self) -> None:
        self.window.resize(1560, 920)
        self.window.show()
        self.application.processEvents()
        horizontal_before = self.window.main_splitter.sizes()
        vertical_before = self.window.workspace_splitter.sizes()

        self.window.resize(1840, 1080)
        self.application.processEvents()
        horizontal_after = self.window.main_splitter.sizes()
        vertical_after = self.window.workspace_splitter.sizes()

        self.assertEqual(horizontal_after[0], horizontal_before[0])
        self.assertEqual(horizontal_after[2], horizontal_before[2])
        self.assertGreater(horizontal_after[1], horizontal_before[1])
        self.assertEqual(vertical_after[1], vertical_before[1])
        self.assertGreater(vertical_after[0], vertical_before[0])
        self.assertEqual(
            self.window.left_workspace.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Preferred,
        )
        self.assertEqual(
            self.window.inspector.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Preferred,
        )
        self.assertEqual(
            self.window.bottom_tabs.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Preferred,
        )

        self.window.resize(1560, 920)
        self.application.processEvents()
        self.assertEqual(
            self.window.main_splitter.sizes(), horizontal_before,
        )
        self.assertEqual(
            self.window.workspace_splitter.sizes(), vertical_before,
        )

        # A user-adjusted splitter position becomes the new fixed edge size.
        total_width = sum(self.window.main_splitter.sizes())
        self.window.main_splitter.setSizes([520, max(300, total_width - 860), 340])
        total_height = sum(self.window.workspace_splitter.sizes())
        self.window.workspace_splitter.setSizes([max(300, total_height - 240), 240])
        self.application.processEvents()
        user_horizontal = self.window.main_splitter.sizes()
        user_vertical = self.window.workspace_splitter.sizes()
        self.window.resize(1760, 1020)
        self.application.processEvents()
        resized_horizontal = self.window.main_splitter.sizes()
        resized_vertical = self.window.workspace_splitter.sizes()
        self.assertEqual(resized_horizontal[0], user_horizontal[0])
        self.assertEqual(resized_horizontal[2], user_horizontal[2])
        self.assertEqual(resized_vertical[1], user_vertical[1])

    def test_workspace_panel_sizes_persist_across_program_restarts(self) -> None:
        with TemporaryDirectory(prefix="pvs-panel-settings-") as directory:
            settings_path = Path(directory) / "workspace.ini"

            def isolated_settings() -> QSettings:
                return QSettings(
                    str(settings_path), QSettings.Format.IniFormat,
                )

            first_window = None
            second_window = None
            with patch("app.ui.main_window.QSettings", side_effect=isolated_settings):
                try:
                    first_window = MainWindow()
                    first_window.resize(1600, 980)
                    first_window.show()
                    self.application.processEvents()

                    horizontal_total = sum(first_window.main_splitter.sizes())
                    first_window.main_splitter.setSizes([
                        360, max(300, horizontal_total - 780), 420,
                    ])
                    vertical_total = sum(first_window.workspace_splitter.sizes())
                    first_window.workspace_splitter.setSizes([
                        max(300, vertical_total - 310), 310,
                    ])
                    self.application.processEvents()
                    saved = QSignalSpy(first_window._workspace_settings_timer.timeout)
                    first_window.main_splitter.splitterMoved.emit(360, 1)
                    first_window.workspace_splitter.splitterMoved.emit(310, 1)
                    self.assertTrue(saved.wait(3000), "Workspace settings save timed out")

                    stored = isolated_settings()
                    stored.sync()
                    self.assertEqual(
                        int(stored.value("workspace/left_panel_width")), 360,
                    )
                    self.assertEqual(
                        int(stored.value("workspace/right_panel_width")), 420,
                    )
                    self.assertEqual(
                        int(stored.value("workspace/bottom_panel_height")), 310,
                    )

                    # Hiding a panel must not replace its useful open size with 0.
                    for panel in (
                        first_window.left_workspace,
                        first_window.inspector_stack,
                        first_window.bottom_workspace_stack,
                    ):
                        panel.setVisible(False)
                    first_window._save_workspace_layout()
                    stored.sync()
                    self.assertEqual(
                        int(stored.value("workspace/left_panel_width")), 360,
                    )
                    self.assertEqual(
                        int(stored.value("workspace/right_panel_width")), 420,
                    )
                    self.assertEqual(
                        int(stored.value("workspace/bottom_panel_height")), 310,
                    )
                    for panel in (
                        first_window.left_workspace,
                        first_window.inspector_stack,
                        first_window.bottom_workspace_stack,
                    ):
                        panel.setVisible(True)

                    second_window = MainWindow()
                    second_window.resize(1600, 980)
                    second_window.show()
                    self.application.processEvents()

                    self.assertAlmostEqual(
                        second_window.main_splitter.sizes()[0], 360, delta=2,
                    )
                    self.assertAlmostEqual(
                        second_window.main_splitter.sizes()[2], 420, delta=2,
                    )
                    self.assertAlmostEqual(
                        second_window.workspace_splitter.sizes()[1], 310, delta=2,
                    )
                    self.assertEqual(second_window._sidebar_open_width, 360)
                    self.assertEqual(second_window._inspector_open_width, 420)
                    self.assertEqual(second_window._bottom_open_height, 310)
                finally:
                    for window in (second_window, first_window):
                        if window is not None:
                            window.project_session.dirty = False
                            window.close()

    def test_canvas_hover_uses_directional_cursor_on_selected_resize_handles(self) -> None:
        source = next(
            source for source in self.window.store.sources() if not source.locked
        )
        self.window.store.select(source.id)
        self.window.show()
        self.window.canvas.fit_artboard()
        self.application.processEvents()
        item = self.window.canvas._items[source.id]
        expected = {
            "e": Qt.CursorShape.SizeHorCursor,
            "n": Qt.CursorShape.SizeVerCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
        }
        for handle, cursor_shape in expected.items():
            with self.subTest(handle=handle):
                scene_position = item.mapToScene(
                    item.resize_handle_rects()[handle].center()
                )
                viewport_position = self.window.canvas.mapFromScene(scene_position)
                # QTest.mouseMove relies on the process-global pointer and the
                # offscreen backend may coalesce it across test modules. Send
                # an explicit widget-local move to exercise the same event path
                # deterministically.
                global_position = self.window.canvas.viewport().mapToGlobal(
                    viewport_position
                )
                move_event = QMouseEvent(
                    QEvent.Type.MouseMove,
                    QPointF(viewport_position),
                    QPointF(global_position),
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                QApplication.sendEvent(self.window.canvas.viewport(), move_event)
                self.application.processEvents()
                self.assertEqual(
                    self.window.canvas.viewport().cursor().shape(), cursor_shape,
                )

    def test_canvas_press_uses_the_same_tolerant_handle_as_its_cursor(self) -> None:
        source = next(
            source for source in self.window.store.sources() if not source.locked
        )
        self.window.store.select(source.id)
        self.window.show()
        self.application.processEvents()
        item = self.window.canvas._items[source.id]
        scene_center = item.mapToScene(item.content_rect().center())
        viewport_center = self.window.canvas.mapFromScene(scene_center)

        with patch.object(
            self.window.canvas, "_edit_handle_at_view_position",
            side_effect=lambda candidate, _position: (
                "e" if candidate is item else None
            ),
        ):
            QTest.mousePress(
                self.window.canvas.viewport(), Qt.MouseButton.LeftButton,
                pos=viewport_center,
            )
            self.assertTrue(item._resizing)
            self.assertEqual(item._resize_handle, "e")
            QTest.mouseRelease(
                self.window.canvas.viewport(), Qt.MouseButton.LeftButton,
                pos=viewport_center,
            )

        self.assertFalse(item._resizing)
        self.assertIsNone(item._resize_handle)

    def test_undo_and_redo_keep_moved_source_selected(self) -> None:
        source = self.window.store.sources()[0]
        original_x = source.x
        moved_x = original_x + 140
        self.window.store.select(source.id)
        self.window.store.update(source.id, x=moved_x)

        self.window._undo()
        restored = self.window.store.get(source.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.x, original_x)
        self.assertEqual(self.window.store.selected.id, source.id)
        self.assertEqual(self.window.inspector._source_id, source.id)
        self.assertTrue(self.window.canvas._items[source.id].isSelected())
        self.assertEqual(self.window.canvas._items[source.id].pos().x(), original_x)

        self.window._redo()
        redone = self.window.store.get(source.id)
        self.assertIsNotNone(redone)
        self.assertEqual(redone.x, moved_x)
        self.assertEqual(self.window.store.selected.id, source.id)
        self.assertTrue(self.window.canvas._items[source.id].isSelected())

    def test_layer_multi_selection_is_mirrored_on_canvas(self) -> None:
        first = Source(SourceType.TEXT, "MULTI_LAYER_FIRST", x=80, y=80)
        second = Source(SourceType.TEXT, "MULTI_LAYER_SECOND", x=260, y=80)
        self.window.store.add(first)
        self.window.store.add(second)
        self.application.processEvents()
        panel = self.window.layer_panel
        tree_items = {}
        for row in range(panel.tree.topLevelItemCount()):
            root = panel.tree.topLevelItem(row)
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                tree_items[child.data(0, Qt.ItemDataRole.UserRole)] = child

        panel._refreshing = True
        try:
            panel.tree.clearSelection()
            tree_items[first.id].setSelected(True)
            tree_items[second.id].setSelected(True)
            panel.tree.setCurrentItem(
                tree_items[second.id], 0,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        finally:
            panel._refreshing = False
        panel._publish_selection()

        self.assertEqual(set(self.window.store.selected_ids), {first.id, second.id})
        self.assertEqual(self.window.store.selected.id, second.id)
        self.assertTrue(self.window.canvas._items[first.id].isSelected())
        self.assertTrue(self.window.canvas._items[second.id].isSelected())
        self.assertEqual(self.window.inspector._source_id, second.id)

    def test_canvas_multi_selection_is_mirrored_in_layer_panel(self) -> None:
        first = Source(SourceType.TEXT, "MULTI_CANVAS_FIRST", x=80, y=180)
        second = Source(SourceType.TEXT, "MULTI_CANVAS_SECOND", x=260, y=180)
        self.window.store.add(first)
        self.window.store.add(second)
        self.application.processEvents()
        self.window.store.select(None)

        self.window.canvas._items[first.id].setSelected(True)
        self.window.canvas._items[second.id].setSelected(True)

        self.assertEqual(set(self.window.store.selected_ids), {first.id, second.id})
        self.assertEqual(self.window.store.selected.id, second.id)
        self.assertEqual(
            set(self.window.layer_panel.selected_source_ids()),
            {first.id, second.id},
        )

    def test_inspector_multi_selection_shows_and_applies_common_properties(self) -> None:
        first = Source(
            SourceType.TEXT, "First", text="Alpha", x=40, opacity=0.7,
            fill_color="#FF0000", font_size=28, text_alignment="left",
            visible=True,
        )
        second = Source(
            SourceType.TEXT, "Second", text="Beta", x=180, opacity=0.7,
            fill_color="#0000FF", font_size=28, text_alignment="right",
            visible=False,
        )
        second.gradient.enabled = True
        self.window.store.replace([first, second])
        self.window.store.select_many([first.id, second.id], second.id)
        inspector = self.window.inspector

        self.assertEqual(set(inspector._source_ids), {first.id, second.id})
        self.assertEqual(inspector.x_spin.lineEdit().text(), "")
        self.assertNotEqual(inspector.opacity_spin.lineEdit().text(), "")
        self.assertEqual(inspector.fill_color_button.text(), "")
        self.assertEqual(inspector.text_edit.text(), "")
        self.assertEqual(inspector.text_alignment_combo.currentIndex(), -1)
        self.assertEqual(
            inspector.visible_check.checkState(), Qt.CheckState.PartiallyChecked,
        )
        self.assertEqual(
            inspector.gradient_check.checkState(), Qt.CheckState.PartiallyChecked,
        )
        self.assertTrue(inspector._field_visibility["font_family"])

        # Leaving a mixed blank line edit untouched must never overwrite values.
        inspector.text_edit.editingFinished.emit()
        self.assertEqual((first.text, second.text), ("Alpha", "Beta"))

        inspector.x_spin.setValue(320)
        self.assertEqual((first.x, second.x), (320, 320))
        inspector.text_alignment_combo.setCurrentIndex(
            inspector.text_alignment_combo.findData("center")
        )
        self.assertEqual(
            (first.text_alignment, second.text_alignment), ("center", "center"),
        )
        inspector.visible_check.click()
        self.assertEqual(first.visible, second.visible)
        inspector.gradient_check.click()
        self.assertEqual(first.gradient.enabled, second.gradient.enabled)

        inspector.text_edit.setFocus()
        QTest.keyClicks(inspector.text_edit, "Unified")
        inspector.text_edit.editingFinished.emit()
        self.assertEqual((first.text, second.text), ("Unified", "Unified"))

        shape = Source(SourceType.SHAPE, "Shape")
        self.window.store.add(shape)
        self.window.store.select_many([first.id, shape.id], shape.id)
        self.assertFalse(inspector._field_visibility["font_family"])
        self.assertFalse(inspector._field_visibility["font_size"])
        self.assertFalse(inspector._field_visibility["text"])

    def test_token_editor_pairs_percent_and_inserts_completion_from_keyboard(self) -> None:
        editor = TokenLineEdit(self.window.translator)
        editor.show()
        editor.setFocus()

        QTest.keyClicks(editor, "%")
        self.application.processEvents()
        self.assertEqual(editor.text(), "%%")
        self.assertEqual(editor.cursorPosition(), 1)
        self.assertTrue(editor.token_popup.isVisible())
        self.assertEqual(editor.token_popup.list.count(), 12)
        self.assertEqual(editor.token_popup.current_token(), "title")
        self.assertIn("%title%", editor.token_popup.description.text())

        QTest.keyClick(editor, Qt.Key.Key_Down)
        self.assertEqual(editor.token_popup.current_token(), "artist")
        self.assertIn("아티스트", editor.token_popup.description.text())
        QTest.keyClick(editor, Qt.Key.Key_Right)
        self.assertEqual(editor.text(), "%artist%")
        self.assertEqual(editor.cursorPosition(), len("%artist%"))
        self.assertFalse(editor.token_popup.isVisible())

        editor.clear()
        QTest.keyClicks(editor, "%")
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        self.assertEqual(editor.text(), "")
        editor.close()

    def test_token_editor_filters_prefix_and_multiline_editor_wraps_selection(self) -> None:
        editor = TokenPlainTextEdit(self.window.translator)
        editor.show()
        editor.setFocus()
        editor.setPlainText("Track: ")
        editor.moveCursor(editor.textCursor().MoveOperation.End)

        QTest.keyClicks(editor, "%ti")
        self.application.processEvents()
        self.assertEqual(editor.toPlainText(), "Track: %ti%")
        self.assertEqual(editor.token_popup.list.count(), 1)
        self.assertEqual(editor.token_popup.current_token(), "title")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(editor.toPlainText(), "Track: %title%")

        editor.selectAll()
        QTest.keyClicks(editor, "%")
        self.assertEqual(editor.toPlainText(), "%Track: %title%%")
        editor.close()

    def test_expanded_text_dialog_and_inspector_apply_long_text(self) -> None:
        source = Source(SourceType.TEXT, "Long text", text="Short")
        self.window.store.add(source)
        self.application.processEvents()
        inspector = self.window.inspector
        self.assertFalse(inspector.expand_text_button.isHidden())

        with patch("app.inspector.source_inspector.TextEditorDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.text.return_value = "First line\n%artist% — second line"
            inspector._open_expanded_text_editor()

        self.assertEqual(source.text, "First line\n%artist% — second line")
        self.assertEqual(inspector.text_edit.text(), source.text)

        expanded = TextEditorDialog(source.text, self.window.translator)
        self.assertEqual(expanded.editor.toPlainText(), source.text)
        self.assertGreaterEqual(expanded.minimumWidth(), 480)
        expanded.close()

    def test_layer_panel_drag_reorders_canvas_and_supports_group_drop(self) -> None:
        first = Source(SourceType.TEXT, "First", z_index=0)
        second = Source(SourceType.SHAPE, "Second", z_index=1)
        third = Source(SourceType.IMAGE, "Third", z_index=2)
        self.window.store.replace([first, second, third])
        panel = self.window.layer_panel
        panel.refresh()
        self.assertEqual(
            panel.tree.dragDropMode(),
            QAbstractItemView.DragDropMode.InternalMove,
        )

        ungrouped = next(
            panel.tree.topLevelItem(row)
            for row in range(panel.tree.topLevelItemCount())
            if panel.tree.topLevelItem(row).data(0, panel._kind_role) == "root"
        )
        # The tree is front-to-back. Move the back row to the visible top and
        # commit exactly as a completed internal drag would.
        moved = ungrouped.takeChild(ungrouped.childCount() - 1)
        ungrouped.insertChild(0, moved)
        panel._commit_tree_order()
        self.assertEqual(first.z_index, 2)
        self.assertEqual(
            [source.id for source in self.window.store.sources()],
            [second.id, third.id, first.id],
        )

        group = self.window.store.add_group("Artwork", [third.id])
        panel.refresh()
        group_item = next(
            panel.tree.topLevelItem(row)
            for row in range(panel.tree.topLevelItemCount())
            if panel.tree.topLevelItem(row).data(0, panel._group_role) == group.id
        )
        ungrouped = next(
            panel.tree.topLevelItem(row)
            for row in range(panel.tree.topLevelItemCount())
            if panel.tree.topLevelItem(row).data(0, panel._kind_role) == "root"
        )
        first_item = next(
            ungrouped.child(index) for index in range(ungrouped.childCount())
            if ungrouped.child(index).data(0, Qt.ItemDataRole.UserRole) == first.id
        )
        ungrouped.takeChild(ungrouped.indexOfChild(first_item))
        group_item.insertChild(0, first_item)
        panel._commit_tree_order()
        self.assertEqual(first.group_id, group.id)

        group_item.setText(0, "Renamed artwork")
        self.assertEqual(group.name, "Renamed artwork")

    def test_layer_panel_moves_multi_selection_as_a_stable_block(self) -> None:
        first = Source(SourceType.TEXT, "First", z_index=0)
        second = Source(SourceType.SHAPE, "Second", z_index=1)
        third = Source(SourceType.IMAGE, "Third", z_index=2)
        self.window.store.replace([first, second, third])
        self.window.store.select_many([first.id, second.id], second.id)
        panel = self.window.layer_panel
        panel.refresh()
        panel._move_selected(1)
        self.assertEqual(
            [source.id for source in self.window.store.sources()],
            [third.id, first.id, second.id],
        )
        self.assertIn("3", panel.summary_label.text())
        self.assertIn("2", panel.summary_label.text())

    def test_canvas_sources_support_copy_cut_paste_and_standard_shortcuts(self) -> None:
        first = Source(SourceType.TEXT, "First", x=10, y=20, z_index=0)
        second = Source(SourceType.SHAPE, "Second", x=30, y=40, z_index=1)
        self.window.store.replace([first, second])
        group = self.window.store.add_group("Original group", [first.id, second.id])
        self.assertEqual(first.group_id, group.id)
        self.window.store.select_many([first.id, second.id], second.id)

        class FakeClipboard:
            def __init__(self) -> None:
                self.data = QMimeData()

            def setMimeData(self, data: QMimeData) -> None:
                self.data = data

            def mimeData(self) -> QMimeData:
                return self.data

        clipboard = FakeClipboard()
        with patch(
            "app.ui.main_window.QApplication.clipboard", return_value=clipboard,
        ):
            self.assertTrue(self.window._copy_selected_sources())
            self.assertTrue(clipboard.data.hasFormat(
                "application/x-playlist-video-studio-sources+json"
            ))

            self.window._paste_sources()
            pasted_ids = self.window.store.selected_ids
            self.assertEqual(len(pasted_ids), 2)
            pasted = [self.window.store.get(source_id) for source_id in pasted_ids]
            self.assertTrue(all(source is not None for source in pasted))
            self.assertEqual(
                {(source.x, source.y) for source in pasted if source is not None},
                {(34.0, 44.0), (54.0, 64.0)},
            )
            self.assertTrue(all(
                source.group_id is None for source in pasted if source is not None
            ))
            self.assertTrue(all(
                source.id not in {first.id, second.id}
                for source in pasted if source is not None
            ))

            self.window._cut_selected_sources()
            self.assertEqual(
                {source.id for source in self.window.store.sources()},
                {first.id, second.id},
            )
            self.window._paste_sources()
            self.assertEqual(len(self.window.store.sources()), 4)

        self.assertEqual(self.window.cut_action.shortcut().toString(), "Ctrl+X")
        self.assertEqual(self.window.copy_action.shortcut().toString(), "Ctrl+C")
        self.assertEqual(self.window.paste_action.shortcut().toString(), "Ctrl+V")
        initial_zoom = self.window.canvas.transform().m11()
        self.window._adjust_canvas_zoom(1.15)
        self.assertGreater(self.window.canvas.transform().m11(), initial_zoom)

    def test_toolbar_centers_selected_sources_and_tracks_editable_selection(self) -> None:
        first = Source(
            SourceType.TEXT, "Center me", x=35, y=45,
            width=240, height=120, scale=1.25,
        )
        second = Source(
            SourceType.SHAPE, "Also center me", x=410, y=280,
            width=160, height=90, scale=0.75,
        )
        self.window.store.replace([first, second])
        self.window.store.select_many([first.id, second.id], second.id)
        self.application.processEvents()

        toolbar_actions = self.window.toolbar.actions()
        self.assertIn(self.window.center_horizontal_action, toolbar_actions)
        self.assertIn(self.window.center_vertical_action, toolbar_actions)
        self.assertTrue(self.window.center_horizontal_action.isEnabled())
        self.assertTrue(self.window.center_vertical_action.isEnabled())
        self.assertIn("Ctrl+Shift+H", self.window.center_horizontal_action.toolTip())
        self.assertIn("Ctrl+Shift+V", self.window.center_vertical_action.toolTip())

        original_delta = (second.x - first.x, second.y - first.y)
        artboard = self.window.canvas.scene_model.artboard_rect
        self.window.center_horizontal_action.trigger()
        self.window.center_vertical_action.trigger()
        items = [
            self.window.canvas._items[first.id],
            self.window.canvas._items[second.id],
        ]
        bounds = items[0].mapRectToScene(items[0].content_rect()).united(
            items[1].mapRectToScene(items[1].content_rect())
        )
        self.assertAlmostEqual(bounds.center().x(), artboard.center().x())
        self.assertAlmostEqual(bounds.center().y(), artboard.center().y())
        self.assertAlmostEqual(second.x - first.x, original_delta[0])
        self.assertAlmostEqual(second.y - first.y, original_delta[1])

        self.window.store.update(first.id, locked=True)
        self.window.store.update(second.id, locked=True)
        self.assertFalse(self.window.center_horizontal_action.isEnabled())
        self.assertFalse(self.window.center_vertical_action.isEnabled())
        self.window.store.select(None)
        self.assertFalse(self.window.center_horizontal_action.isEnabled())

    def test_multi_selection_guide_resizes_sources_as_one_group(self) -> None:
        first = Source(
            SourceType.SHAPE, "First", x=100, y=100, width=100, height=80,
        )
        second = Source(
            SourceType.TEXT, "Second", x=300, y=200, width=100, height=80,
        )
        self.window.store.replace([first, second])
        self.window.store.select_many([first.id, second.id], second.id)
        self.window.show()
        self.application.processEvents()

        canvas = self.window.canvas
        scene = canvas.scene_model
        original = scene.multi_selection_bounds()
        self.assertIsNotNone(original)
        self.assertTrue(scene.has_multi_selection())
        assert original is not None
        self.assertFalse(canvas.viewport().grab().isNull())
        self.assertEqual(
            canvas._group_handle_at_view_position(
                canvas.mapFromScene(original.bottomRight())
            ),
            "se",
        )

        canvas._begin_group_resize("se")
        target = original.topLeft() + (
            original.bottomRight() - original.topLeft()
        ) * 0.5
        canvas._resize_group_to(target)
        canvas._finish_group_resize()

        resized = scene.multi_selection_bounds()
        self.assertIsNotNone(resized)
        assert resized is not None
        self.assertAlmostEqual(resized.left(), original.left())
        self.assertAlmostEqual(resized.top(), original.top())
        self.assertAlmostEqual(resized.width(), original.width() * 0.5)
        self.assertAlmostEqual(resized.height(), original.height() * 0.5)
        self.assertAlmostEqual(first.scale, 0.5)
        self.assertAlmostEqual(second.scale, 0.5)

    def test_canvas_context_menu_exposes_multi_source_editing_commands(self) -> None:
        first = Source(SourceType.TEXT, "First", x=40, y=80, z_index=0)
        second = Source(SourceType.SHAPE, "Second", x=260, y=180, z_index=1)
        third = Source(SourceType.IMAGE, "Third", x=500, y=240, z_index=2)
        self.window.store.replace([first, second, third])
        self.window.store.select_many([first.id, second.id], second.id)

        def actions_by_command(menu: object) -> dict[str, object]:
            result: dict[str, object] = {}
            for action in menu.actions():
                submenu = action.menu()
                if submenu is not None:
                    result.update(actions_by_command(submenu))
                elif action.data() is not None:
                    result[str(action.data())] = action
            return result

        menu = self.window.canvas._create_context_menu(
            self.window.canvas._items[first.id]
        )
        actions = actions_by_command(menu)
        self.assertTrue({
            "cut", "copy", "paste", "duplicate", "delete",
            "move_forward", "move_backward", "bring_front", "send_back",
            "center_horizontal", "center_vertical", "align_left",
            "align_hcenter", "align_right", "align_top", "align_vcenter",
            "align_bottom", "distribute_horizontal", "distribute_vertical",
            "group", "ungroup", "toggle_visible",
            "toggle_lock", "select_all",
        }.issubset(actions))
        self.assertTrue(actions["align_left"].isEnabled())
        # Distribute needs three sources; only two are selected here.
        self.assertFalse(actions["distribute_horizontal"].isEnabled())
        self.assertFalse(actions["ungroup"].isEnabled())

        actions["align_left"].trigger()
        self.assertEqual(first.x, second.x)
        actions["move_forward"].trigger()
        ordered_ids = [
            source.id for source in sorted(
                self.window.store.sources(), key=lambda source: source.z_index
            )
        ]
        self.assertEqual(ordered_ids, [third.id, first.id, second.id])
        actions["group"].trigger()
        self.assertIsNotNone(first.group_id)
        self.assertEqual(first.group_id, second.group_id)
        actions["toggle_lock"].trigger()
        self.assertTrue(first.locked)
        self.assertTrue(second.locked)

        empty_actions = actions_by_command(
            self.window.canvas._create_context_menu(None)
        )
        self.assertEqual(
            set(empty_actions),
            {"paste", "select_all", "unlock_all_layers", "fit_canvas"},
        )
        # Two sources are locked above, so the unlock-all command is offered.
        self.assertTrue(empty_actions["unlock_all_layers"].isEnabled())

    def test_canvas_right_click_never_changes_source_selection(self) -> None:
        first = Source(SourceType.TEXT, "Selected", x=80, y=90, width=220, height=100)
        second = Source(SourceType.SHAPE, "Not selected", x=420, y=260, width=180, height=120)
        self.window.store.replace([first, second])
        self.window.store.select(first.id)
        self.window.show()
        self.application.processEvents()

        canvas = self.window.canvas
        second_item = canvas._items[second.id]
        position = canvas.mapFromScene(second_item.sceneBoundingRect().center())
        fake_menu = MagicMock()
        with patch.object(canvas, "_create_context_menu", return_value=fake_menu) as create_menu:
            QTest.mousePress(canvas.viewport(), Qt.MouseButton.RightButton, pos=position)
            QTest.mouseRelease(canvas.viewport(), Qt.MouseButton.RightButton, pos=position)
            self.application.processEvents()

            create_menu.reset_mock()
            canvas.contextMenuEvent(QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                position,
                canvas.viewport().mapToGlobal(position),
            ))

        self.assertEqual(self.window.store.selected_ids, (first.id,))
        create_menu.assert_called_once_with(None)

    def test_canvas_animation_preview_is_non_blocking_and_restores_source(self) -> None:
        source = self.window.store.sources()[0]
        self.window.store.update(
            source.id,
            animation_in="slide_left",
            animation_out="zoom",
            animation_in_duration=0.1,
            animation_out_duration=0.1,
        )
        self.window.store.select(source.id)
        source = self.window.store.get(source.id)
        item = self.window.canvas._items[source.id]
        original_model = source.to_dict()
        original_position = QPointF(item.pos())
        original_scale = item.scale()
        original_opacity = item.opacity()

        self.window._preview_source_animation(source.id)
        self.assertTrue(self.window._animation_preview_active)
        self.assertTrue(self.window.animation_preview_controller.active)
        # The preview is non-blocking: the window stays interactive.
        self.assertTrue(self.window.isEnabled())
        self.assertNotEqual(item.pos(), original_position)
        self.assertEqual(source.to_dict(), original_model)

        # Once armed, any further user action stops the preview immediately.
        QTest.qWait(1)
        self.window.store.source_changed.emit(source)
        self.assertFalse(self.window._animation_preview_active)
        self.assertFalse(self.window.animation_preview_controller.active)
        self.assertEqual(item.pos(), original_position)
        self.assertEqual(item.scale(), original_scale)
        self.assertEqual(item.opacity(), original_opacity)

    def test_canvas_animation_preview_completes_when_left_alone(self) -> None:
        source = self.window.store.sources()[0]
        self.window.store.update(
            source.id, animation_in="slide_left", animation_out="zoom",
            animation_in_duration=0.1, animation_out_duration=0.1,
        )
        self.window.store.select(source.id)
        source = self.window.store.get(source.id)
        item = self.window.canvas._items[source.id]
        original_position = QPointF(item.pos())

        self.window._preview_source_animation(source.id)
        QTest.qWait(900)
        self.assertFalse(self.window._animation_preview_active)
        self.assertEqual(item.pos(), original_position)
        self.assertTrue(item.isSelected())
        self.assertEqual(self.window.store.selected.id, source.id)

    def test_text_tokens_render_as_labelled_placeholders_on_canvas(self) -> None:
        self.window._add_source(SourceType.TEXT)
        source = self.window.store.selected
        item = self.window.canvas._items[source.id]

        # Main-window tests run in Korean; surrounding literal text is preserved.
        self.window.store.update(source.id, text="%title%이것은 제목입니다")
        self.assertEqual(item._render_text(), "(제목)이것은 제목입니다")

        self.window.store.update(source.id, text="%title% - %artist%")
        self.assertEqual(item._render_text(), "(제목) - (아티스트)")

        # Unknown tokens are left untouched.
        self.window.store.update(source.id, text="%title% %mystery%")
        self.assertEqual(item._render_text(), "(제목) %mystery%")

        # There is no longer a sample-data toggle.
        self.assertFalse(hasattr(self.window, "sample_data_action"))

    def test_alignment_uses_scaled_rotated_content_bounds(self) -> None:
        for mode in ("left", "hcenter", "right", "top", "vcenter", "bottom"):
            with self.subTest(mode=mode):
                sources = [
                    Source(SourceType.SHAPE, "First", x=100, y=150, width=120,
                           height=80, scale=2.0, rotation=30),
                    Source(SourceType.SHAPE, "Second", x=500, y=400, width=90,
                           height=140, scale=0.7, rotation=75),
                ]
                self.window.store.replace(sources)
                self.window.store.select_many([source.id for source in sources], sources[0].id)
                self.window._align_selected_sources(mode)
                bounds = [
                    self.window.canvas._items[source.id].mapRectToScene(
                        self.window.canvas._items[source.id].content_rect()
                    ) for source in sources
                ]
                def edge(rect: object) -> float:
                    return {
                        "left": rect.left(), "hcenter": rect.center().x(),
                        "right": rect.right(), "top": rect.top(),
                        "vcenter": rect.center().y(), "bottom": rect.bottom(),
                    }[mode]
                self.assertAlmostEqual(edge(bounds[0]), edge(bounds[1]))

    def test_distribution_uses_scaled_rotated_edges_and_keeps_outermost(self) -> None:
        for axis in ("horizontal", "vertical"):
            with self.subTest(axis=axis):
                sources = [
                    Source(SourceType.SHAPE, str(index), x=x, y=x, width=80,
                           height=100, scale=scale, rotation=angle)
                    for index, (x, scale, angle) in enumerate(
                        ((100, 1.5, 30), (290, 0.7, 70), (700, 1.2, 15))
                    )
                ]
                self.window.store.replace(sources)
                self.window.store.select_many([source.id for source in sources], sources[0].id)
                outer_positions = [(source.x, source.y) for source in (sources[0], sources[-1])]
                self.window._distribute_selected_sources(axis)
                bounds = [
                    self.window.canvas._items[source.id].mapRectToScene(
                        self.window.canvas._items[source.id].content_rect()
                    ) for source in sources
                ]
                gaps = [
                    bounds[index + 1].left() - bounds[index].right()
                    if axis == "horizontal" else
                    bounds[index + 1].top() - bounds[index].bottom()
                    for index in range(2)
                ]
                self.assertAlmostEqual(gaps[0], gaps[1])
                for source, (x, y) in zip((sources[0], sources[-1]), outer_positions):
                    self.assertAlmostEqual(source.x, x)
                    self.assertAlmostEqual(source.y, y)

    def test_distribute_spacing_evens_gaps_and_keeps_outermost(self) -> None:
        ids: list[str] = []
        for x, width in ((0, 100), (140, 60), (400, 120), (700, 40)):
            self.window._add_source(SourceType.SHAPE)
            source = self.window.store.selected
            self.window.store.update(
                source.id, x=float(x), y=0.0,
                width=float(width), height=50.0, scale=1.0,
            )
            ids.append(source.id)
        self.window.store.select_many(ids, ids[-1])

        self.window._handle_canvas_context_command("distribute_horizontal")
        by_id = {source.id: source for source in self.window.store.sources()}
        ordered = sorted((by_id[i] for i in ids), key=lambda s: s.x)
        gaps = [
            round(ordered[k + 1].x - (ordered[k].x + ordered[k].width), 3)
            for k in range(len(ordered) - 1)
        ]
        self.assertEqual(len(set(gaps)), 1)
        self.assertEqual(ordered[0].x, 0.0)
        self.assertAlmostEqual(ordered[-1].x, 700.0)

        # Fewer than three selected sources leave positions untouched.
        self.window.store.select_many(ids[:2], ids[1])
        before = by_id[ids[0]].x
        self.window._handle_canvas_context_command("distribute_horizontal")
        self.assertEqual(by_id[ids[0]].x, before)

    def test_unlock_all_sources_from_canvas_menu_and_layer_panel(self) -> None:
        self.window._add_source(SourceType.TEXT)
        first = self.window.store.selected
        self.window._add_source(SourceType.SHAPE)
        second = self.window.store.selected
        self.window.store.update(first.id, locked=True)
        self.window.store.update(second.id, locked=True)
        self.application.processEvents()

        panel = self.window.layer_panel
        self.assertFalse(panel.unlock_all_button.isHidden())
        self.assertTrue(panel.unlock_all_button.isEnabled())

        menu = self.window.canvas._create_context_menu(None)
        unlock_action = next(
            action for action in menu.actions()
            if action.data() == "unlock_all_layers"
        )
        self.assertTrue(unlock_action.isEnabled())

        self.window._handle_canvas_context_command("unlock_all_layers")
        self.assertEqual(
            sum(source.locked for source in self.window.store.sources()), 0
        )
        self.application.processEvents()
        self.assertTrue(panel.unlock_all_button.isHidden())

        # The empty-canvas action is disabled again once nothing is locked.
        menu = self.window.canvas._create_context_menu(None)
        unlock_action = next(
            action for action in menu.actions()
            if action.data() == "unlock_all_layers"
        )
        self.assertFalse(unlock_action.isEnabled())

    def test_major_window_state_changes_schedule_canvas_fit(self) -> None:
        normal = Qt.WindowState.WindowNoState
        maximized = Qt.WindowState.WindowMaximized
        minimized = Qt.WindowState.WindowMinimized

        with patch.object(self.window, "_schedule_canvas_fit") as schedule_fit:
            self.window._handle_canvas_window_state_change(normal, maximized)
            schedule_fit.assert_called_once_with(100)

        self.window._canvas_fit_pending = False
        with patch.object(self.window, "_schedule_canvas_fit") as schedule_fit:
            self.window._handle_canvas_window_state_change(normal, minimized)
            schedule_fit.assert_not_called()
            self.assertTrue(self.window._canvas_fit_pending)
            self.window._handle_canvas_window_state_change(minimized, normal)
            schedule_fit.assert_called_once_with(100)

    def test_ordinary_window_state_event_does_not_reset_canvas_zoom(self) -> None:
        normal = Qt.WindowState.WindowNoState
        with patch.object(self.window, "_schedule_canvas_fit") as schedule_fit:
            self.window._handle_canvas_window_state_change(normal, normal)
        schedule_fit.assert_not_called()

    def test_preview_renderer_setting_is_deferred_until_next_program_start(self) -> None:
        original = self.window.settings_service.current
        session_backend = self.window._preview_backend_for_session
        selected_backend = "cpu" if session_backend == "gpu_layers" else "gpu_layers"
        try:
            self.window.settings_service.save(replace(
                original, preview_backend=selected_backend,
            ))

            self.assertEqual(
                self.window.settings_service.current.preview_backend,
                selected_backend,
            )
            self.assertEqual(
                self.window._preview_backend_for_session, session_backend,
            )
        finally:
            self.window.settings_service.save(original)

    def test_animation_preview_button_requires_configured_animation(self) -> None:
        source = self.window.store.sources()[0]
        self.window.store.update(
            source.id, animation_in="none", animation_out="none"
        )
        self.window.store.select(source.id)
        self.assertFalse(self.window.inspector.animation_preview_button.isEnabled())
        self.window.store.update(source.id, animation_in="fade")
        self.assertTrue(self.window.inspector.animation_preview_button.isEnabled())

    def test_animation_inspector_offers_pop_and_rotate(self) -> None:
        values = [
            self.window.inspector.animation_in_combo.itemData(index)
            for index in range(self.window.inspector.animation_in_combo.count())
        ]
        self.assertIn("pop", values)
        self.assertIn("rotate", values)

    def test_video_settings_explain_scope_without_discarding_other_mode_media(self) -> None:
        source = Source(
            SourceType.VIDEO, "Video",
            video_timing_mode="track",
            video_repeat_mode="sequence",
            video_paths=["C:/Videos/whole-a.mp4", "C:/Videos/whole-b.mp4"],
        )
        dialog = VideoSourceDialog(source, True, self.window)
        try:
            self.assertEqual(dialog.timing.currentData(), "track")
            self.assertIn("곡마다 다른", dialog.timing.currentText())
            self.assertFalse(dialog.track_scope_panel.isHidden())
            self.assertTrue(dialog.timeline_media_group.isHidden())
            self.assertEqual(dialog.values["video_paths"], source.video_paths)

            dialog.timing.setCurrentIndex(dialog.timing.findData("timeline"))
            self.assertTrue(dialog.track_scope_panel.isHidden())
            self.assertFalse(dialog.timeline_media_group.isHidden())
            self.assertIn("현재 2개", dialog.scope_summary.text())

            dialog.repeat.setCurrentIndex(dialog.repeat.findData("once"))
            self.assertTrue(dialog.cycle_host.isHidden())
            dialog.repeat.setCurrentIndex(dialog.repeat.findData("random"))
            self.assertFalse(dialog.cycle_host.isHidden())
            self.assertEqual(dialog.values["video_repeat_mode"], "random")
        finally:
            dialog.close()

    def test_video_inspector_summarizes_current_media_scope(self) -> None:
        source = Source(
            SourceType.VIDEO, "Video", video_timing_mode="track",
        )
        self.window.store.add(source)
        self.window.store.select(source.id)
        self.application.processEvents()
        self.assertEqual(
            self.window.inspector._form_labels["video_settings"].text(),
            "영상 사용 범위",
        )
        self.assertIn("곡마다 다른 영상", self.window.inspector.video_settings_button.text())

        self.window.store.update(
            source.id, video_timing_mode="timeline",
            video_paths=["one.mp4", "two.mp4"],
        )
        self.application.processEvents()
        self.assertIn("전체에서 같은 영상", self.window.inspector.video_settings_button.text())
        self.assertIn("2개", self.window.inspector.video_settings_button.text())

    def test_every_source_type_has_its_own_palette_glyph(self) -> None:
        from app.ui.studio_icons import source_icon

        for source_type in SourceType:
            with self.subTest(source_type):
                icon = source_icon(source_type.value)
                self.assertIsNotNone(icon)
                self.assertFalse(icon.isNull())

    def test_reset_panel_sizes_restores_the_default_workspace(self) -> None:
        self.window.show()
        QApplication.processEvents()
        self.window._reset_workspace_layout()
        left, _center, right = self.window.main_splitter.sizes()
        self.assertEqual((left, right), (MainWindow._DEFAULT_LEFT_PANEL_WIDTH, MainWindow._DEFAULT_RIGHT_PANEL_WIDTH))
        self.assertIn(self.window.reset_layout_action, self.window.view_menu.actions())

    def test_timeline_move_buttons_follow_the_selection_and_empty_state_explains(self) -> None:
        panel = self.window.timeline_panel
        self.window.playlist_service.replace([])
        panel.refresh()
        self.assertFalse(panel.track_empty_label.isHidden())
        self.assertFalse(panel.up_button.isEnabled())
        tracks = [PlaylistTrack(f"{name}.wav", name, duration_seconds=10.0) for name in "abc"]
        self.window.playlist_service.replace(tracks)
        panel.refresh()
        self.assertTrue(panel.track_empty_label.isHidden())
        self.assertFalse(panel.up_button.isEnabled())
        self.assertFalse(panel.down_button.isEnabled())
        panel.track_table.selectRow(0)
        self.assertFalse(panel.up_button.isEnabled())
        self.assertTrue(panel.down_button.isEnabled())
        panel.track_table.selectRow(2)
        self.assertTrue(panel.up_button.isEnabled())
        self.assertFalse(panel.down_button.isEnabled())

    def test_visual_timeline_edits_undo_and_restore_shared_selection(self) -> None:
        source = Source(SourceType.TEXT, "Timed title", timeline_start=5, timeline_duration=10)
        self.window.store.add(source)
        self.window.project_session.ready = True
        self.window._history_timer.stop()
        self.window.history.reset(self.window._project_document().to_dict())
        panel = self.window.timeline_panel
        panel._commit_visual_timing("source", source.id, 8.25, 6.5)
        self.assertEqual(source.timeline_start, 8.25)
        self.window._undo()
        restored = self.window.store.get(source.id)
        self.assertEqual((restored.timeline_start, restored.timeline_duration), (5, 10))
        self.assertEqual(self.window.store.selected.id, source.id)
        self.window._redo()
        restored = self.window.store.get(source.id)
        self.assertEqual((restored.timeline_start, restored.timeline_duration), (8.25, 6.5))
        self.application.processEvents()
        self.assertEqual(panel.visual.selected, ("source", source.id))
        self.window.show()
        self.window._show_bottom_panel(1)
        self.application.processEvents()
        self.assertTrue(panel.visual.hasFocus())

    def test_snap_setting_round_trip(self) -> None:
        self.window.canvas.scene_model.snap_enabled = False
        self.assertFalse(self.window._project_document().canvas.snap_enabled)
        self.window._apply_project(ProjectDocument(
            canvas=CanvasSettings(snap_enabled=False)
        ))
        self.assertFalse(self.window.canvas.scene_model.snap_enabled)
        self.assertFalse(self.window.snap_action.isChecked())

    def test_smooth_scroll_settings_are_exposed_by_dialog(self) -> None:
        dialog = SettingsDialog(
            self.window.settings_service.current,
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            dialog.smooth_scroll_check.setChecked(False)
            dialog.smooth_scroll_duration_slider.setValue(320)
            settings = dialog.app_settings
            self.assertFalse(settings.smooth_scrolling)
            self.assertEqual(settings.smooth_scroll_duration_ms, 320)
            self.assertFalse(dialog.smooth_scroll_duration_slider.isEnabled())
            self.assertEqual(AppSettings().preview_backend, "gpu_layers")
            initial_backend = dialog.app_settings.preview_backend
            target_backend = "cpu" if initial_backend == "gpu_layers" else "gpu_layers"
            dialog.preview_backend_combo.setCurrentIndex(
                dialog.preview_backend_combo.findData(target_backend)
            )
            self.assertEqual(dialog.app_settings.preview_backend, target_backend)
            self.assertFalse(dialog.preview_backend_restart_hint.isHidden())
            self.assertIn("다시 실행", dialog.preview_backend_restart_hint.text())
            self.assertIn("미리보기 화면에서 변경할 수 없습니다", dialog.preview_backend_hint.text())
        finally:
            dialog.close()

    def test_pending_preview_renderer_setting_shows_restart_notice(self) -> None:
        active_backend = self.window._preview_backend_for_session
        pending_backend = "cpu" if active_backend == "gpu_layers" else "gpu_layers"
        dialog = SettingsDialog(
            replace(
                self.window.settings_service.current,
                preview_backend=pending_backend,
            ),
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
            active_preview_backend=active_backend,
        )
        try:
            self.assertFalse(dialog.preview_backend_restart_hint.isHidden())
            self.assertIn("재시작", dialog.preview_backend_restart_hint.text())
        finally:
            dialog.close()

    def test_smooth_scroll_animates_wheel_but_preserves_ctrl_gestures(self) -> None:
        area = QScrollArea(self.window)
        content = QWidget()
        content.setFixedSize(200, 1200)
        area.setWidget(content)
        area.resize(220, 240)
        area.show()
        self.application.processEvents()
        service = self.window.smooth_scroll
        service.configure(True, 180)
        wheel = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        self.assertTrue(service.eventFilter(area.viewport(), wheel))
        self.assertGreater(service._targets[area.verticalScrollBar()], 0)

        ctrl_wheel = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        self.assertFalse(service.eventFilter(area.viewport(), ctrl_wheel))
        service._stop_animations()
        area.close()

    def test_project_content_and_other_item_views_scroll_per_pixel(self) -> None:
        service = self.window.smooth_scroll
        service.configure(True, 180)
        self.window.show()
        self.window.left_tabs.setCurrentWidget(self.window.content_library_panel)
        content_list = self.window.content_library_panel.list
        content_list.setFixedHeight(160)
        self.window.content_library_panel.filter_combo.setCurrentIndex(
            self.window.content_library_panel.filter_combo.findData("all")
        )
        self.window.project_content_service.replace([
            ProjectContent(f"content-{index}.png", "image", f"Content {index}")
            for index in range(40)
        ])
        # A pending marker refresh must preserve these model-backed fixture rows.
        self.window.content_library_panel.refresh()
        self.application.processEvents()
        content_list.doItemsLayout()
        self.assertEqual(content_list.count(), 40)
        self.assertGreater(content_list.verticalScrollBar().maximum(), 0)
        wheel = QWheelEvent(
            QPointF(20, 20), QPointF(20, 20), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        self.assertTrue(service.eventFilter(content_list.viewport(), wheel))
        self.assertEqual(
            content_list.verticalScrollMode(),
            QAbstractItemView.ScrollMode.ScrollPerPixel,
        )
        self.assertGreater(service._targets[content_list.verticalScrollBar()], 0)

        other_item_views = (
            self.window.layer_panel.tree,
            self.window.playlist_editor.list_widget,
            self.window.timeline_panel.track_table,
            self.window.timeline_panel.source_table,
        )
        for view in other_item_views:
            service._prepare_scroll_area(view)
            self.assertEqual(
                view.verticalScrollMode(),
                QAbstractItemView.ScrollMode.ScrollPerPixel,
            )
            self.assertEqual(
                view.horizontalScrollMode(),
                QAbstractItemView.ScrollMode.ScrollPerPixel,
            )
        service._stop_animations()

    def test_project_content_drag_returning_to_source_panel_is_rejected(self) -> None:
        panel = self.window.content_library_panel
        source_list = panel.list
        self.window.show()
        self.window.left_tabs.setCurrentWidget(panel)
        panel.show()
        self.application.processEvents()

        panel_center = panel.rect().center()
        window_point = panel.mapTo(self.window, panel_center)
        event = MagicMock()
        event.source.return_value = source_list
        event.position.return_value = QPointF(window_point)

        self.assertTrue(self.window._drag_returned_to_project_content(event))
        self.window.dragEnterEvent(event)

        event.setDropAction.assert_called_once_with(Qt.DropAction.IgnoreAction)
        event.accept.assert_called_once_with()
        event.acceptProposedAction.assert_not_called()
        event.mimeData.assert_not_called()

        outside = self.window.canvas.mapTo(
            self.window, self.window.canvas.rect().center(),
        )
        event.position.return_value = QPointF(outside)
        self.assertFalse(self.window._drag_returned_to_project_content(event))

    def test_arrow_keys_nudge_selection_and_shift_scales_the_step(self) -> None:
        source = Source(SourceType.TEXT, "Nudge me", x=100.0, y=100.0)
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.canvas.setFocus()
        self.application.processEvents()

        QTest.keyClick(self.window.canvas, Qt.Key.Key_Right)
        QTest.keyClick(self.window.canvas, Qt.Key.Key_Down)
        self.assertEqual((self.window.store.get(source.id).x,
                          self.window.store.get(source.id).y), (101.0, 101.0))

        QTest.keyClick(
            self.window.canvas, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier,
        )
        self.assertEqual(self.window.store.get(source.id).x, 91.0)

    def test_double_clicking_a_text_source_opens_the_expanded_editor(self) -> None:
        source = Source(SourceType.TEXT, "Editable", text="before")
        self.window.store.replace([source])
        self.application.processEvents()

        fake = MagicMock()
        fake.exec.return_value = QDialog.DialogCode.Accepted
        fake.text.return_value = "after"
        with patch("app.ui.main_window.TextEditorDialog", return_value=fake):
            self.window.canvas.edit_requested.emit(source.id)

        self.assertEqual(self.window.store.get(source.id).text, "after")

    def test_status_bar_zoom_readout_follows_the_canvas(self) -> None:
        self.window.canvas.set_zoom(1.0)
        self.application.processEvents()
        self.assertEqual(self.window.zoom_reset_button.text(), "100%")
        self.window._adjust_canvas_zoom(1.15)
        self.application.processEvents()
        self.assertEqual(self.window.zoom_reset_button.text(), "115%")

    def test_canvas_zoom_is_view_state_not_document_state(self) -> None:
        self.window.project_session.dirty = False
        with patch.object(self.window, "_schedule_history") as schedule:
            self.window._adjust_canvas_zoom(1.15)
            self.window.canvas.set_zoom(1.0)
        schedule.assert_not_called()
        self.assertFalse(self.window.project_session.dirty)

    def test_ctrl_plus_and_ctrl_equals_both_zoom_the_canvas_in(self) -> None:
        sequences = {
            action.shortcut().toString()
            for action in self.window._canvas_shortcut_actions
        }
        self.assertIn("Ctrl++", sequences)
        self.assertIn("Ctrl+=", sequences)

    def test_f2_moves_focus_to_the_inspector_name_field(self) -> None:
        source = Source(SourceType.TEXT, "Rename via F2")
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.show()
        self.window.canvas.setFocus()
        self.application.processEvents()
        QTest.keyClick(self.window.canvas, Qt.Key.Key_F2)
        self.application.processEvents()
        self.assertTrue(self.window.inspector.name_edit.hasFocus())

    def test_inspector_properties_are_grouped_into_tabs(self) -> None:
        source = Source(SourceType.IMAGE, "Tabbed properties")
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.show()
        self.application.processEvents()

        inspector = self.window.inspector
        expected_categories = {
            "name": "layout", "opacity": "shape", "fill_color": "fill",
            "blur": "filter", "animation_in": "animation", "layer": "other",
        }
        for field, category in expected_categories.items():
            self.assertEqual(inspector._field_categories[field], category)
        filter_index = inspector._tab_indices["filter"]
        inspector.property_tabs.setCurrentIndex(inspector._tab_indices["layout"])
        inspector.property_tabs.setCurrentIndex(filter_index)
        self.application.processEvents()
        self.assertEqual(inspector.property_tabs.currentIndex(), filter_index)

    def test_inspector_special_tab_sub_sections_follow_the_source_type(self) -> None:
        inspector = self.window.inspector

        # Every sectioned field is registered with its group and category.
        self.assertEqual(inspector._field_sections["visualizer_attack"],
                         ("special", "vz_response"))
        self.assertEqual(inspector._field_categories["visualizer_attack"], "special")

        groups = inspector._sections
        visualizer = Source(SourceType.AUDIO_VISUALIZER, "VZ")
        self.window.store.replace([visualizer])
        self.window.store.select(visualizer.id)
        self.application.processEvents()
        self.assertFalse(groups[("special", "vz_response")].isHidden())
        self.assertTrue(groups[("special", "tl_layout")].isHidden())

        # A group with no fields for the current source folds away entirely.
        shape = Source(SourceType.SHAPE, "Shape")
        self.window.store.replace([shape])
        self.window.store.select(shape.id)
        self.application.processEvents()
        self.assertTrue(groups[("special", "vz_response")].isHidden())

        # Collapsing a section hides its body but keeps the header.
        self.window.store.replace([visualizer])
        self.window.store.select(visualizer.id)
        self.application.processEvents()
        response = groups[("special", "vz_response")]
        response.header.setChecked(False)
        self.assertTrue(response._body.isHidden())
        self.assertFalse(response.header.isHidden())

    def test_dependent_inspector_fields_hide_until_their_toggle_is_active(self) -> None:
        source = Source(SourceType.IMAGE, "Conditional", width=300.0, height=200.0)
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.show()
        self.application.processEvents()
        inspector = self.window.inspector
        widgets = inspector._field_widgets

        # Gradient stops hide until "use gradient" is on.
        self.assertFalse(inspector._field_visibility["gradient_start"])
        self.assertFalse(inspector._field_visibility["gradient_end"])
        inspector.gradient_check.setChecked(True)
        self.application.processEvents()
        self.assertTrue(inspector._field_visibility["gradient_start"])
        inspector.gradient_check.setChecked(False)
        self.application.processEvents()
        self.assertFalse(inspector._field_visibility["gradient_start"])

        # Outline colour follows the outline width.
        self.assertFalse(inspector._field_visibility["outline_color"])
        inspector.outline_spin.setValue(4.0)
        self.application.processEvents()
        self.assertTrue(inspector._field_visibility["outline_color"])

        # Shadow sub-fields follow the shadow toggle.
        self.assertFalse(inspector._field_visibility["shadow_color"])
        inspector.shadow_check.setChecked(True)
        self.application.processEvents()
        self.assertTrue(inspector._field_visibility["shadow_color"])

        # Exit-animation duration hides while the style is "none".
        self.assertFalse(inspector._field_visibility["animation_out_duration"])
        index = inspector.animation_out_combo.findData("fade")
        inspector.animation_out_combo.setCurrentIndex(index)
        self.application.processEvents()
        self.assertTrue(inspector._field_visibility["animation_out_duration"])

    def test_delete_action_also_accepts_backspace(self) -> None:
        sequences = {
            sequence.toString() for sequence in self.window.delete_action.shortcuts()
        }
        self.assertIn("Del", sequences)
        self.assertIn("Backspace", sequences)

    def test_canvas_shows_and_clears_the_resize_readout(self) -> None:
        source = Source(SourceType.TEXT, "Readout", width=200.0, height=100.0)
        self.window.store.replace([source])
        self.application.processEvents()
        item = self.window.canvas._items[source.id]

        item.interaction_hint.emit("240 × 160")
        self.assertFalse(self.window.canvas._interaction_hint.isHidden())
        self.assertEqual(
            self.window.canvas._interaction_hint.text(), "240 × 160",
        )
        item.interaction_hint.emit("")
        self.assertTrue(self.window.canvas._interaction_hint.isHidden())

    def test_left_workspace_combines_sources_content_and_layers_as_tabs(self) -> None:
        tabs = self.window.left_tabs

        self.assertEqual(tabs.count(), 3)
        self.assertIs(tabs.widget(0), self.window.source_sidebar)
        self.assertIs(tabs.widget(1), self.window.content_library_panel)
        self.assertIs(tabs.widget(2), self.window.layer_panel)
        self.assertIs(tabs.parentWidget(), self.window.left_workspace)
        self.assertEqual(
            [tabs.tabText(index) for index in range(tabs.count())],
            ["요소", "프로젝트 콘텐츠", "레이어"],
        )

    def test_project_content_context_menu_adds_removes_and_shows_information(self) -> None:
        panel = self.window.content_library_panel
        with TemporaryDirectory(prefix="pvs-content-menu-") as raw_directory:
            image_path = Path(raw_directory) / "cover.png"
            image_path.write_bytes(b"test image placeholder")
            self.window.project_content_service.add_paths([image_path])
            item = panel.list.item(0)
            panel.list.setCurrentItem(item)
            menu = panel._create_context_menu(item)
            actions = {
                str(action.data()): action
                for action in menu.actions() if action.data() is not None
            }
            self.assertEqual(
                list(actions), ["preview", "remove", "information", "import"],
            )
            self.assertTrue(actions["preview"].isEnabled())

            with patch.object(QMessageBox, "information") as information:
                actions["information"].trigger()
            self.assertIn(str(image_path.resolve()), information.call_args.args[2])

            actions["remove"].trigger()
            self.assertEqual(panel.list.count(), 0)

        empty_menu = panel._create_context_menu(None)
        self.assertEqual(
            [action.data() for action in empty_menu.actions()], ["import"],
        )

    def test_project_content_switches_between_list_grid_and_compact_views(self) -> None:
        panel = self.window.content_library_panel
        with TemporaryDirectory(prefix="pvs-content-views-") as raw_directory:
            image_path = Path(raw_directory) / "thumbnail.png"
            image = QImage(80, 60, QImage.Format.Format_ARGB32)
            image.fill(QColor("#36A2EB"))
            self.assertTrue(image.save(str(image_path)))
            self.window.project_content_service.add_paths([image_path])
            content_id = panel.list.item(0).data(Qt.ItemDataRole.UserRole)
            panel.list.setCurrentRow(0)

            panel._set_view_mode("grid", persist=False)
            self.assertEqual(panel.view_mode, "grid")
            self.assertEqual(panel.list.viewMode(), QListView.ViewMode.IconMode)
            self.assertEqual(panel.list.iconSize(), QSize(72, 72))
            self.assertTrue(panel.view_buttons["grid"].isChecked())
            self.assertNotIn("\n", panel.list.item(0).text())
            self.assertEqual(
                panel.list.currentItem().data(Qt.ItemDataRole.UserRole), content_id,
            )

            panel._set_view_mode("compact", persist=False)
            self.assertEqual(panel.list.viewMode(), QListView.ViewMode.ListMode)
            self.assertEqual(panel.list.iconSize(), QSize(22, 22))
            self.assertEqual(panel.list.item(0).sizeHint().height(), 32)

            panel._set_view_mode("list", persist=False)
            self.assertEqual(panel.view_mode, "list")
            self.assertEqual(panel.list.iconSize(), QSize(38, 38))
            self.assertIn("\n", panel.list.item(0).text())

    def test_project_content_filters_all_supported_categories(self) -> None:
        panel = self.window.content_library_panel
        panel.filter_combo.setCurrentIndex(panel.filter_combo.findData("all"))
        with TemporaryDirectory(prefix="pvs-content-filter-") as raw_directory:
            directory = Path(raw_directory)
            paths = [
                directory / "cover.png",
                directory / "clip.mp4",
                directory / "song.mp3",
                directory / "captions.lrc",
                directory / "typeface.ttf",
            ]
            for path in paths:
                path.write_bytes(b"fixture")
            self.window.project_content_service.add_paths(paths)
            self.assertEqual(panel.filter_combo.count(), 6)
            self.assertEqual(panel.content_filter, "all")
            self.assertEqual(panel.list.count(), 5)
            self.assertEqual(panel.filter_count_label.text(), "5 / 5")

            for media_type in ("image", "video", "audio", "lyrics", "font"):
                panel.filter_combo.setCurrentIndex(
                    panel.filter_combo.findData(media_type)
                )
                self.assertEqual(panel.content_filter, media_type)
                self.assertEqual(panel.list.count(), 1)
                self.assertEqual(
                    panel.list.item(0).data(Qt.ItemDataRole.UserRole + 2),
                    media_type,
                )
                self.assertEqual(panel.filter_count_label.text(), "1 / 5")

            panel.filter_combo.setCurrentIndex(panel.filter_combo.findData("all"))
            self.assertEqual(panel.list.count(), 5)

    def test_about_action_opens_program_information(self) -> None:
        with patch("app.ui.main_window.AboutDialog") as about_dialog:
            self.window._show_about()
        about_dialog.assert_called_once()
        about_dialog.return_value.exec.assert_called_once()

    def test_help_action_opens_the_guide_for_what_is_in_use(self) -> None:
        with patch("app.ui.main_window.HelpDialog") as help_dialog:
            self.window._show_help()
        help_dialog.assert_called_once_with(
            self.window.translator, self.window, tab="canvas", topic="workspace",
        )
        help_dialog.return_value.exec.assert_called_once()
        self.assertEqual(self.window.help_action.shortcut().toString(), "F1")
        focus = self.window.playlist_editor.findChildren(QWidget)[0]
        with patch("app.ui.main_window.QApplication.focusWidget", return_value=focus):
            self.assertEqual(self.window._help_context(), ("canvas", "playlist"))
        with patch("app.ui.main_window.QApplication.focusWidget", return_value=self.window.layer_panel):
            self.assertEqual(self.window._help_context(), ("canvas", "layers"))
        self.window._inline_preview = MagicMock()
        try:
            self.assertEqual(self.window._help_context(), ("preview", "full_preview"))
        finally:
            self.window._inline_preview = None

    def test_help_dialog_has_a_tab_per_window_with_tagged_topics(self) -> None:
        dialog = HelpDialog(self.window.translator, self.window, tab="automix_editor")
        try:
            self.assertEqual(dialog.tab_bar.count(), 6)
            self.assertEqual(
                [dialog.tab_bar.tabText(index) for index in range(6)],
                ["캔버스", "미리보기", "가사/자막 편집기", "AutoMix 편집기", "곡 정보/설정", "기타"],
            )
            self.assertEqual(dialog.current_tab_id, "automix_editor")
            self.assertIn("automix_timeline", dialog.visible_topic_ids())
            self.assertIn("#AutoMix", dialog.browser.toPlainText())
            dialog.select_topic("lrc_timing")
            self.assertEqual(dialog.current_tab_id, "lyrics_editor")
            self.assertEqual(dialog.current_topic_id, "lrc_timing")
            dialog._open_link(QUrl("topic:track_audio"))
            self.assertEqual((dialog.current_tab_id, dialog.current_topic_id), ("track", "track_audio"))
            self.assertTrue(dialog.browser.images, "the topic's screenshot is rendered")
            dialog._open_link(QUrl("tag:단축키"))
            self.assertEqual(dialog.tag_combo.currentData(), "단축키")
            for index, tab in enumerate(("canvas", "preview", "lyrics_editor", "automix_editor")):
                dialog.select_tab(tab)
                self.assertTrue(dialog.tab_bar.tabText(index).endswith("1"), dialog.tab_bar.tabText(index))
                self.assertTrue(all(
                    "단축키" in dialog._topic(identifier).tags for identifier in dialog.visible_topic_ids()
                ))
            dialog.set_tag_filter(None)
            self.assertEqual(dialog.tab_bar.tabText(0), "캔버스")
        finally:
            dialog.close()

    def test_help_search_spans_every_tab_and_shows_a_no_result_state(self) -> None:
        dialog = HelpDialog(self.window.translator, self.window, tab="other")
        try:
            dialog.search_edit.setText("볼륨")
            found = set()
            for tab in ("canvas", "preview", "lyrics_editor", "automix_editor", "track", "other"):
                dialog.select_tab(tab)
                found.update(dialog.visible_topic_ids())
            self.assertTrue({"preview_controls", "track_audio", "lrc_timing"}.issubset(found))
            dialog.search_edit.setText("FFmpeg")
            self.assertEqual(dialog.current_tab_id, "other")
            self.assertEqual(dialog.visible_topic_ids()[0], "ffmpeg")  # title matches rank first
            dialog.search_edit.setText("#AutoMix")
            self.assertEqual(dialog.current_tab_id, "automix_editor", "jumps to a tab that has matches")
            dialog.search_edit.setText("__NO_HELP_RESULT__")
            self.assertEqual(dialog.topic_list.count(), 0)
            self.assertIn("검색 결과 없음", dialog.browser.toPlainText())
        finally:
            dialog.close()

    def test_automatic_nvidia_failure_offers_cpu_preflight_retry(self) -> None:
        track = PlaylistTrack(
            file_path="automatic-encoder-test.mp3",
            title="Automatic encoder",
            duration_seconds=1.0,
        )
        self.window.playlist_service.replace([track])
        original = self.window.settings_service.current
        self.window.settings_service.save(replace(
            original, video_codec=AUTO_VIDEO_ENCODER,
        ))
        try:
            with (
                patch("app.controllers.export_controller.FFmpegRenderer") as renderer_type,
                patch(
                    "app.controllers.export_controller.ExportSettingsDialog.exec",
                    return_value=QDialog.DialogCode.Accepted,
                ),
                patch.object(
                    VideoEncoderAdvisor, "automatic_encoder",
                    return_value=NVIDIA_H264_ENCODER,
                ),
                patch.object(
                    QMessageBox, "warning",
                    return_value=QMessageBox.StandardButton.Yes,
                ) as warning,
                patch.object(QMessageBox, "critical") as critical,
            ):
                renderer_type.return_value.preflight_export.side_effect = [
                    EncoderUnavailableError("NVENC startup failed"),
                    RenderError("stop after CPU retry"),
                ]
                self.window._export_video()

            self.assertEqual(
                renderer_type.return_value.preflight_export.call_count, 2,
            )
            first_settings = (
                renderer_type.return_value.preflight_export.call_args_list[0].args[2]
            )
            second_settings = (
                renderer_type.return_value.preflight_export.call_args_list[1].args[2]
            )
            self.assertEqual(first_settings.video_codec, NVIDIA_H264_ENCODER)
            self.assertEqual(second_settings.video_codec, CPU_H264_ENCODER)
            self.assertIn("NVIDIA 인코더 사용 실패", warning.call_args.args[1])
            self.assertIn("CPU H.264", warning.call_args.args[2])
            critical.assert_called_once()
        finally:
            self.window.settings_service.save(original)


if __name__ == "__main__":
    unittest.main()
