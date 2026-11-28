"""Project new/open/save/load orchestration and background-save ownership.

ProjectSession owns editing state; the window supplies workspace/UI adapters.
AutosaveController owns recovery writes and startup recovery.
"""

from __future__ import annotations

import logging
import traceback as traceback_module
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from PySide6.QtCore import QEventLoop, QSize, QSizeF, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog, QPushButton

from app.dialogs.missing_media_dialog import MissingMediaDialog
from app.dialogs.new_project_dialog import NewProjectDialog
from app.dialogs.startup_dialog import StartupDialog
from app.dialogs.project_crash_report_dialog import ProjectCrashReportDialog
from app.models.project import CanvasSettings, ProjectDocument, clamp_canvas_zoom
from app.preview.canvas_snapshot import CanvasSnapshot
from app.services.project_media_service import ProjectMediaService
from app.services.project_persistence_service import PROJECT_FILE_FILTER, PROJECT_SUFFIX, default_project_path, is_project_path
from app.services.export_storage_service import format_bytes
from app.services.project_load_worker import ProjectLoadWorker
from app.services.project_save_worker import ProjectSaveWorker
from app.services.project_salvage_service import ProjectSalvageResult
from app.services.project_salvage_worker import ProjectSalvageWorker
from app.services.project_service import (
    ProjectError, ProjectLoadCancelled, ProjectRevision, ProjectSaveConflict, ProjectService,
)
from app.utils.logging_setup import (
    log_directory, record_crash_occurrence, handle_crash_exit_choice,
)
from app.utils.crash_diagnostics import build_crash_report, save_crash_report
from app import __version__

from app.services.project_session import ProjectSession
from app.utils.qt_worker_lifecycle import wait_for_qthread

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)


class ProjectController:
    """Own new/open/save/load orchestration on behalf of a MainWindow."""

    def __init__(self, window: "MainWindow", session: ProjectSession) -> None:
        self.window = window
        self.session = session
        # The background save in flight (at most one), the (change serial,
        # previous path) it was started with, and how the last save ended.
        self.save_worker: ProjectSaveWorker | None = None
        self.save_context: tuple[int, Path | None] | None = None
        self.save_succeeded: bool | None = None
        self.loaded_revision: ProjectRevision | None = None
        self.media_relinked = False
        self.loading = False
        self._pending_crash_exit_choice: str | None = None

    @property
    def saving(self) -> bool:
        return self.save_worker is not None

    # -- document assembly -------------------------------------------------

    def document(self) -> ProjectDocument:
        """Collect current UI and domain state into a portable document."""
        window = self.window
        artboard = window.canvas.scene_model.artboard_rect
        canvas = CanvasSettings(
            width=artboard.width(), height=artboard.height(),
            show_grid=window.canvas.scene_model.show_grid,
            snap_enabled=window.canvas.scene_model.snap_enabled,
            zoom=clamp_canvas_zoom(window.canvas.transform().m11()),
        )
        return ProjectDocument(
            sources=window.store.sources(), playlist=window.playlist_service.tracks,
            groups=window.store.groups(),
            canvas=canvas, theme=window._project_theme_metadata,
            language=window._project_language_metadata,
            settings=window.project_settings,
            content_library=window.project_content_service.items,
        )

    def apply(self, document: ProjectDocument, *, restore_canvas_zoom: bool = True) -> None:
        """Restore project content while preserving app-wide UI preferences."""
        window = self.window
        window.project_settings = document.settings
        window.project_content_service.replace(document.content_library)
        window._project_theme_metadata = document.theme
        window._project_language_metadata = document.language
        window.canvas.scene_model.set_artboard_size(
            document.canvas.width, document.canvas.height
        )
        window.grid_action.setChecked(document.canvas.show_grid)
        window.canvas.scene_model.snap_enabled = document.canvas.snap_enabled
        if restore_canvas_zoom:
            window.canvas.set_zoom(document.canvas.zoom)
        else:
            window.canvas.refit_artboard()
        window.store.replace(document.sources, document.groups)
        window.playlist_service.replace(document.playlist)
        window._synchronize_content_library()

    def thumbnail_image(self) -> QImage:
        """Capture the live project's canvas for every saved thumbnail.png.

        Custom thumbnail paths never replace the visual project identity.
        Capture from the artboard model to exclude editor chrome and selection
        handles, and never reuse a previous exported frame or cover art.
        """
        scene = self.window.canvas.scene_model
        size = scene.artboard_rect.size().scaled(
            QSizeF(480, 270), Qt.AspectRatioMode.KeepAspectRatio,
        )
        return CanvasSnapshot.capture(
            scene, output_size=QSize(max(1, round(size.width())), max(1, round(size.height()))),
        )

    # -- status/error UI -----------------------------------------------

    def update_status(self) -> None:
        """Keep a compact, non-modal project/save-state indicator in the toolbar."""
        window = self.window
        if not hasattr(window, "project_status_label"):
            return
        korean = window.translator.is_korean
        if not getattr(window, '_project_open', True):
            window.project_status_label.setText('열린 프로젝트 없음' if korean else 'No project open')
            window.project_status_label.setToolTip(
                '파일 메뉴에서 프로젝트를 만들거나 열어 주세요.' if korean
                else 'Create or open a project from the File menu.')
            window.setWindowTitle('Music Motion Studio')
            window.setWindowModified(False)
            return
        title = window.project_settings.title
        # "Untitled Project" is the model's stored default, not a name the user chose.
        name = title if title and title != "Untitled Project" else (
            "새 프로젝트" if korean else "New project"
        )
        saving = (
            self.save_worker is not None
            and self.save_worker.isRunning()
        )
        # A project that was never written to disk is not "saved", even with no edits yet.
        never_saved = window.current_project_path is None
        state = (
            "저장 중" if korean and saving else
            "Saving" if saving else
            "저장 필요" if korean and self.session.dirty else
            "Unsaved" if self.session.dirty else
            "저장 전" if korean and never_saved else
            "Not saved yet" if never_saved else
            "저장됨" if korean else "Saved"
        )
        window.project_status_label.setText(f"{name}  ·  {state}")
        # Taskbar/Alt+Tab: which project is open and whether it has unsaved edits.
        window.setWindowTitle(f"{name}[*] — Music Motion Studio")
        window.setWindowModified(bool(self.session.dirty))
        window.project_status_label.setToolTip(
            "프로젝트 스냅샷을 백그라운드에서 저장하고 있습니다."
            if korean and saving else
            "The project snapshot is being saved in the background."
            if saving else
            "프로젝트를 저장하려면 Ctrl+S를 누르세요." if korean and (self.session.dirty or never_saved) else
            "프로젝트가 저장되어 있습니다." if korean else
            "Press Ctrl+S to save this project." if self.session.dirty or never_saved else
            "This project is saved."
        )

    def show_error(self, error: ProjectError) -> None:
        """Display a concise persistence failure without crashing the application."""
        window = self.window
        LOGGER.error("Project operation failed: %s", error)
        QMessageBox.critical(
            window,
            "프로젝트 오류" if window.translator.is_korean else "Project error",
            str(error),
        )

    # -- new project ---------------------------------------------------

    def new_project(self, *, confirm_unsaved: bool = True) -> bool:
        window = self.window
        if confirm_unsaved and not window._confirm_unsaved_changes():
            return False
        dialog = NewProjectDialog(
            window.translator, window, preview_sources=window._preset_sources_for_canvas,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        canvas_width, canvas_height = dialog.canvas_size
        selected_preset = dialog.selected_design_preset
        project_settings = dialog.project_settings
        music_paths = list(dialog.music_paths)
        previous_project_path = window.current_project_path
        with self.session.suspend_changes():
            window.current_project_path = None
            self.loaded_revision = None
            window.project_settings = project_settings
            window._project_theme_metadata = window.theme_service.preference.value
            window._project_language_metadata = window.translator.language.value
            window.project_content_service.replace([])
            window.store.replace([])
            window.playlist_service.replace([])
            window.canvas.scene_model.set_artboard_size(canvas_width, canvas_height)
            window.grid_action.setChecked(True)
            window.canvas.scene_model.snap_enabled = True
            if selected_preset is None:
                window._add_welcome_sources()
            else:
                window.store.replace(window._preset_sources_for_canvas(
                    selected_preset, canvas_width, canvas_height,
                ))
            window.canvas.fit_artboard()
        try:
            window.autosave.clear(previous_project_path)
            window.autosave.clear(None)
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
        if self.session.ready:
            window.history.reset(window._project_document().to_dict())
            self.session.dirty = True
            window._autosave_debounce_timer.start()
        else:
            self.session.dirty = False
        window.set_project_open(True)
        window._update_project_status()
        if music_paths:
            # The normal import path: missing-tag review, M3U8 review, lyrics, project content.
            window._add_music_paths(music_paths)
        return True

    def show_project_start_dialog(self) -> bool:
        """Reuse the launch project chooser for File > New and the toolbar action."""
        window = self.window
        if self.loading or not window._confirm_unsaved_changes():
            return False
        while True:
            dialog = StartupDialog(window.translator, window.recent_projects, window)
            if dialog.exec() != dialog.DialogCode.Accepted:
                return False
            if dialog.action == StartupDialog.NEW_PROJECT:
                if window._new_project(confirm_unsaved=False):
                    return True
                continue
            if dialog.project_path is not None and window._load_project_path(dialog.project_path):
                return True

    def show_startup_dialog(self) -> bool:
        """Choose a project, or keep the app open with project tools disabled."""
        window = self.window
        # Restore a pending workspace quietly instead of showing a modal
        # confirmation every launch. Keep the snapshot and project save state
        # intact; this follows the existing no-confirmation update recovery path.
        # Recovery must still run before the project chooser to avoid losing edits.
        if window._offer_recovery(confirm=False):
            return True
        while window.isVisible():
            dialog = StartupDialog(window.translator, window.recent_projects, window)
            if dialog.exec() != dialog.DialogCode.Accepted:
                window.set_project_open(False)
                return False
            if dialog.action == StartupDialog.NEW_PROJECT:
                if window._new_project(confirm_unsaved=False):
                    return True
                continue
            if dialog.project_path is not None and window._load_project_path(dialog.project_path):
                return True
        return False

    # -- save ------------------------------------------------------------

    def save(
        self, force_choose: bool = False, *, wait_for_completion: bool = False,
    ) -> bool:
        """Start a background save and optionally wait in a responsive event loop."""
        window = self.window
        if self.loading:
            return False
        active_worker = self.save_worker
        if active_worker is not None:
            window.statusBar().showMessage(
                "이미 프로젝트를 저장하고 있습니다."
                if window.translator.is_korean
                else "The project is already being saved.",
                3000,
            )
            if wait_for_completion:
                window._wait_for_project_save(active_worker)
                return self.save_succeeded is True
            return False
        target = None if force_choose else window.current_project_path
        if target is None:
            default = str(default_project_path(window.project_settings.title))
            selected, _ = QFileDialog.getSaveFileName(
                window,
                "프로젝트 저장" if window.translator.is_korean else "Save project",
                default,
                PROJECT_FILE_FILTER,
            )
            if not selected:
                return False
            target = Path(selected)
        try:
            target = Path(target).expanduser()
            if not target.suffix:
                target = target.with_suffix(PROJECT_SUFFIX)
            target = target.resolve()
            current = window.current_project_path
            same_project = current is not None and target == current.expanduser().resolve()
            observed = ProjectService.revision(target)
            expected = self.loaded_revision if same_project else observed
            if same_project and observed != expected:
                if self.offer_conflict_copy() and not force_choose:
                    return self.save(True, wait_for_completion=wait_for_completion)
                return False
            document_data = window._project_document().to_dict()
            thumbnail = QImage(window._project_thumbnail_image())
        except (ProjectError, OSError, TypeError, ValueError) as error:
            window._show_project_error(
                ProjectError(f"Could not prepare project save: {error}")
            )
            return False

        previous_project_path = window.current_project_path
        worker = ProjectSaveWorker(target, document_data, thumbnail, expected_revision=expected)
        self.save_worker = worker
        self.save_context = (
            self.session.change_serial, previous_project_path,
        )
        self.save_succeeded = None
        worker.succeeded.connect(window._project_save_finished_successfully)
        worker.failed.connect(window._project_save_failed)
        worker.finished.connect(lambda: self.save_thread_finished(worker))
        window.save_action.setEnabled(False)
        window.save_as_action.setEnabled(False)
        window._autosave_debounce_timer.stop()
        window.statusBar().showMessage(
            "프로젝트 저장 중..." if window.translator.is_korean
            else "Saving project..."
        )
        korean = window.translator.is_korean
        window.activity_progress.begin(
            "project_save", "프로젝트 저장" if korean else "Saving project",
            detail=Path(target).name,
        )
        worker.start()
        window._update_project_status()
        if wait_for_completion:
            window._wait_for_project_save(worker)
            return self.save_succeeded is True
        return True

    def offer_conflict_copy(self) -> bool:
        """Offer a safe copy; closing or cancelling never authorizes overwrite."""
        window = self.window
        korean = window.translator.is_korean
        dialog = QMessageBox(
            QMessageBox.Icon.Warning,
            "프로젝트 저장 충돌" if korean else "Project save conflict",
            ("다른 창이나 프로그램이 프로젝트를 변경했거나 이전 파일 상태를 확인할 수 없습니다. "
             "기존 파일과 현재 편집 내용은 유지됩니다. 다른 이름으로 저장하거나 취소 후 "
             "프로젝트를 다시 불러오세요."
             if korean else
             "The project changed elsewhere, or its original revision is unknown. "
             "Your edits and the existing file are preserved. Save under another name, "
             "or cancel and reopen the project."),
            QMessageBox.StandardButton.Cancel, window,
        )
        copy_button = dialog.addButton(
            "다른 이름으로 저장" if korean else "Save As",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_button = dialog.button(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setEscapeButton(cancel_button)
        try:
            dialog.exec()
            return dialog.clickedButton() is copy_button
        finally:
            dialog.deleteLater()

    def wait_for_save(self, worker: ProjectSaveWorker) -> None:
        """Wait for a required save while continuing to process Qt events."""
        wait_for_qthread(worker)
        QApplication.processEvents()

    def save_finished_successfully(self, saved_path: object) -> None:
        """Commit saved state without hiding edits made during the save."""
        window = self.window
        context = self.save_context
        if context is None:
            return
        saved_serial, previous_project_path = context
        try:
            window.current_project_path = Path(saved_path)
            self.loaded_revision = self.save_worker.saved_revision if self.save_worker is not None else None
            window.recent_projects.add(window.current_project_path)
            unchanged_since_snapshot = self.session.mark_saved(saved_serial)
            if unchanged_since_snapshot:
                window.autosave.clear(previous_project_path)
                window.autosave.clear(window.current_project_path)
            else:
                window._autosave_debounce_timer.start()
            window._update_project_status()
            korean = window.translator.is_korean
            message = (
                "프로젝트를 저장했습니다. 저장 중 변경된 내용은 아직 저장되지 않았습니다."
                if korean and not unchanged_since_snapshot else
                "Project saved. Changes made during saving remain unsaved."
                if not unchanged_since_snapshot else
                "프로젝트를 저장했습니다." if korean else "Project saved."
            )
            window.statusBar().showMessage(message, 4000)
        except ProjectError as error:
            window._show_project_error(error)
            self.save_succeeded = False
            return
        self.save_succeeded = True

    def save_failed(self, message: str) -> None:
        window = self.window
        self.save_succeeded = False
        if self.session.dirty:
            window._autosave_debounce_timer.start()
        if self.save_worker is not None and self.save_worker.conflict:
            if window.translator.is_korean:
                message = ("다른 곳에서 프로젝트가 변경되었거나 저장 중입니다. "
                           "현재 편집 내용은 유지됩니다. 다른 이름으로 저장하거나 다시 불러오세요.\n\n"
                           + message)
        window._show_project_error(ProjectError(message))

    def save_thread_finished(self, worker: ProjectSaveWorker) -> None:
        window = self.window
        window.activity_progress.finish("project_save")
        if self.save_worker is worker:
            self.save_worker = None
            self.save_context = None
        window.save_action.setEnabled(True)
        window.save_as_action.setEnabled(True)
        window._update_project_status()
        worker.deleteLater()

    # -- open/load ---------------------------------------------------------

    def open_project(self) -> None:
        """Choose a project package."""
        window = self.window
        if self.loading:
            return
        selected, _ = QFileDialog.getOpenFileName(
            window,
            "프로젝트 열기" if window.translator.is_korean else "Open project",
            "",
            PROJECT_FILE_FILTER,
        )
        if not selected:
            return
        window._open_project_with_confirmation(Path(selected))

    def open_project_with_confirmation(self, path: Path) -> bool:
        """Replace the workspace after safely resolving unsaved changes."""
        window = self.window
        if self.loading:
            return False
        if not is_project_path(path):
            window._show_project_error(ProjectError(
                ".pvsproj 프로젝트 파일을 선택하세요." if window.translator.is_korean
                else "Choose a .pvsproj project file."
            ))
            return False
        previous_project_path = window.current_project_path
        had_unsaved_changes = self.session.dirty
        if not window._confirm_unsaved_changes():
            return False
        loaded = window._load_project_path(Path(path))
        if loaded and had_unsaved_changes:
            try:
                window.autosave_controller.wait_for_pending()
                window.autosave.clear(previous_project_path)
            except ProjectError as error:
                window.statusBar().showMessage(str(error), 5000)
        return loaded

    def open_project_path(self, path: Path) -> bool:
        """Open a project requested by Explorer or another external launcher."""
        return self.window._load_project_path(Path(path))

    def confirm_unsaved_changes(self) -> bool:
        """Save, explicitly discard, or keep the active unsaved workspace."""
        window = self.window
        # Opening/replacing the workspace while an older snapshot is still
        # saving would let its completion overwrite the new active path.
        if self.save_worker is not None:
            window._wait_for_project_save(self.save_worker)
        if not self.session.dirty:
            return True
        korean = window.translator.is_korean
        response = QMessageBox.warning(
            window,
            "저장되지 않은 변경 사항" if korean else "Unsaved changes",
            "현재 프로젝트에 저장되지 않은 변경 사항이 있습니다. 계속하기 전에 저장할까요?"
            if korean else
            "The current project has unsaved changes. Save before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if response == QMessageBox.StandardButton.Save:
            window._save_project(wait_for_completion=True)
            return not self.session.dirty
        return response == QMessageBox.StandardButton.Discard

    def load_path(self, path: Path) -> bool:
        """Restore a selected project path through the normal safe load workflow."""
        if self.loading or self.saving:
            return False
        self.loading = True
        try:
            return self._load_path(path)
        finally:
            self.loading = False
            self.window.activity_progress.finish("project_load")
            # A failed load is still unwinding here. Run any selected exit
            # only after the load guard has cleared and the UI is idle.
            choice = self._pending_crash_exit_choice
            self._pending_crash_exit_choice = None
            if choice is not None:
                QTimer.singleShot(
                    0, self.window,
                    lambda selected=choice: handle_crash_exit_choice(self.window, selected),
                )

    def _load_path(self, path: Path) -> bool:
        window = self.window
        korean = window.translator.is_korean
        stage = "프로젝트 파일 읽기" if korean else "Reading project file"
        window.activity_progress.begin(
            "project_load", "프로젝트 불러오기" if korean else "Loading project",
            detail=f"{stage} · {path.name}",
        )
        previous_document: ProjectDocument | None = None
        previous_open = getattr(window, "_project_open", True)
        previous_path = window.current_project_path
        previous_revision = self.loaded_revision
        previous_dirty = self.session.dirty
        previous_selection = window.store.selected_ids
        previous_active = window.store.selected.id if window.store.selected is not None else None
        try:
            path = path.expanduser().resolve()
            revision = ProjectService.revision(path)
            document = self._load_document_off_gui_thread(path)
            if ProjectService.revision(path) != revision:
                raise ProjectSaveConflict(f"Project changed during loading: {path}. Reopen it.")
            if document.app_version and document.app_version != __version__:
                korean = window.translator.is_korean
                QMessageBox.warning(
                    window,
                    "프로젝트 버전 차이" if korean else "Project version differs",
                    (
                        "이 프로젝트는 다른 버전의 Music Motion Studio에서 저장되었습니다.\n\n"
                        f"프로젝트 저장 버전: {document.app_version}\n"
                        f"현재 프로그램 버전: {__version__}\n\n"
                        "일부 기능이나 표시 결과가 달라질 수 있습니다. 프로젝트를 계속 엽니다."
                        if korean else
                        "This project was saved with a different version of Music Motion Studio.\n\n"
                        f"Project version: {document.app_version}\n"
                        f"Current app version: {__version__}\n\n"
                        "Some features or visual results may differ. The project will continue opening."
                    ),
                    QMessageBox.StandardButton.Ok,
                    QMessageBox.StandardButton.Ok,
                )
            stage = "누락된 미디어 확인" if korean else "Validating project media"
            window.activity_progress.update("project_load", detail=stage)
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            self.media_relinked = False
            if not window._resolve_project_media(document, path):
                raise ProjectLoadCancelled("Project loading was cancelled while relinking media.")
            stage = "프로젝트 작업공간 적용" if korean else "Applying project workspace"
            window.activity_progress.update("project_load", 0.8, stage)
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            previous_document = ProjectDocument.from_dict(window._project_document().to_dict())
            with self.session.suspend_changes():
                window._apply_project(document, restore_canvas_zoom=False)
                loaded_snapshot = window._project_document().to_dict()
                loaded_snapshot["canvas"]["zoom"] = document.canvas.zoom
            window.current_project_path = path
            self.loaded_revision = revision
            window.recent_projects.add(window.current_project_path)
            self.session.dirty = self.media_relinked
            window.set_project_open(True)
            window.history.reset(loaded_snapshot)
            window.canvas.set_zoom(document.canvas.zoom)
            if self.media_relinked:
                window._autosave_debounce_timer.start()
            window._update_project_status()
            message = "프로젝트를 불러왔습니다." if window.translator.is_korean else "Project loaded."
            window.statusBar().showMessage(message, 4000)
            return True
        except ProjectLoadCancelled:
            # Cancelling only happens before apply, so the workspace is untouched.
            window.statusBar().showMessage(
                "프로젝트 불러오기를 취소했습니다." if korean else "Project loading cancelled.",
                4000,
            )
            return False
        except Exception as error:
            rollback_error: Exception | None = None
            if previous_document is not None:
                try:
                    with self.session.suspend_changes():
                        window._apply_project(previous_document, restore_canvas_zoom=False)
                        valid_selection = [
                            source_id for source_id in previous_selection
                            if window.store.get(source_id) is not None
                        ]
                        active = previous_active if previous_active in valid_selection else None
                        window.store.select_many(valid_selection, active)
                except Exception as restore_error:  # pragma: no cover - last-resort diagnostics
                    rollback_error = restore_error
                    LOGGER.exception("Failed to restore the workspace after project load failure")
            window.current_project_path = previous_path
            self.loaded_revision = previous_revision
            self.session.dirty = previous_dirty
            if getattr(window, "_project_open", True) != previous_open:
                window.set_project_open(previous_open)
            window._update_project_status()
            self.show_load_crash(path, stage, error, rollback_error)
            return False

    _LOAD_DIALOG_DELAY_MS = 400

    def _load_document_off_gui_thread(self, path: Path) -> ProjectDocument:
        """Run ProjectService.load in a worker while the GUI keeps repainting.

        Short loads finish with user input excluded (nothing can start a second
        load or edit mid-load). Past a short delay a window-modal progress
        dialog with Cancel appears; its modality keeps blocking the editor.
        """
        window = self.window
        korean = window.translator.is_korean
        worker = ProjectLoadWorker(path)
        loop = QEventLoop()
        dialog: QProgressDialog | None = None
        last_progress: tuple[int, int] | None = None
        preparing_covers = False
        receiving_updates = True

        def on_progress(done: int, total: int) -> None:
            nonlocal last_progress
            if not receiving_updates or worker.cancel_event.is_set():
                return
            last_progress = (done, total)
            fraction = done / total if total > 0 else 0.0
            sizes = f"{format_bytes(done)} / {format_bytes(total)}"
            window.activity_progress.update(
                "project_load", 0.7 * fraction,
                f"{'포함 미디어 풀기' if korean else 'Extracting media'} · {sizes}",
            )
            if dialog is not None:
                dialog.setRange(0, 1000 if total else 0)
                dialog.setLabelText(
                    f"{path.name}\n\n"
                    f"{'포함 미디어 풀기' if korean else 'Extracting embedded media'}  {sizes}"
                )
                dialog.setValue(round(1000 * fraction))

        def on_preparing_covers() -> None:
            nonlocal preparing_covers
            if not receiving_updates or worker.cancel_event.is_set():
                return
            preparing_covers = True
            label = "앨범 커버 준비 중" if korean else "Preparing album artwork"
            window.activity_progress.update("project_load", 0.7, label)
            if dialog is not None:
                dialog.setRange(0, 0)
                dialog.setLabelText(f"{path.name}\n\n{label}")

        # Both queued across threads: a fast finish can't quit before exec(),
        # and progress callbacks run on the GUI thread.
        worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
        worker.progress.connect(on_progress, Qt.ConnectionType.QueuedConnection)
        worker.preparing_covers.connect(on_preparing_covers, Qt.ConnectionType.QueuedConnection)
        worker.start()
        delay = QTimer()
        delay.setSingleShot(True)
        delay.timeout.connect(loop.quit)
        delay.start(self._LOAD_DIALOG_DELAY_MS)
        loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        delay.stop()
        if worker.isRunning():
            dialog = QProgressDialog(
                f"{path.name}\n\n{'프로젝트 정보 읽는 중' if korean else 'Reading project'}",
                "", 0, 0, window,
            )
            dialog.setWindowTitle("프로젝트 불러오기" if korean else "Loading project")
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.setMinimumDuration(0)
            cancel_button = QPushButton("취소" if korean else "Cancel")
            dialog.setCancelButton(cancel_button)

            def request_cancel() -> None:
                worker.cancel_event.set()
                label = "불러오기를 취소하는 중…" if korean else "Cancelling project load…"
                window.activity_progress.update("project_load", detail=label)
                cancel_button.setEnabled(False)
                dialog.setRange(0, 0)
                dialog.setLabelText(f"{path.name}\n\n{label}")
                dialog.show()

            dialog.canceled.connect(request_cancel)
            if preparing_covers:
                on_preparing_covers()
            elif last_progress is not None:
                on_progress(*last_progress)
            dialog.show()
            if worker.isRunning():
                loop.exec()
            dialog.canceled.disconnect()
            dialog.hide()
            dialog.deleteLater()
        receiving_updates = False
        worker.wait()
        document, error = worker.document, worker.error
        cancelled = worker.cancel_event.is_set()
        worker.deleteLater()
        if cancelled:
            raise ProjectLoadCancelled("Project loading was cancelled.")
        if error is not None:
            raise error
        assert document is not None
        return document

    def show_load_crash(
        self, path: Path, stage: str, error: Exception,
        rollback_error: Exception | None = None,
    ) -> None:
        """Show detailed, copyable diagnostics for a recoverable load failure."""
        window = self.window
        LOGGER.exception("Project load failed during %s: %s", stage, path)
        cause: BaseException = error
        while cause.__cause__ is not None:
            cause = cause.__cause__
        korean = window.translator.is_korean
        guidance = window._project_load_guidance(cause, korean)
        traceback_text = "".join(
            traceback_module.format_exception(type(error), error, error.__traceback__)
        )
        rollback_text = ""
        if rollback_error is not None:
            rollback_text = (
                "\n\nWORKSPACE RESTORE ERROR\n" + "".join(
                    traceback_module.format_exception(
                        type(rollback_error), rollback_error, rollback_error.__traceback__
                    )
                )
            )
        resolved_path = path.absolute()
        report_context = (
            f"Project: {resolved_path}\n"
            f"Stage: {stage}\n"
            f"Exception: {type(error).__name__}: {error}\n"
            f"Root cause: {type(cause).__name__}: {cause}\n"
            f"Workspace restored: {'no' if rollback_error else 'yes'}\n"
            f"Suggested action: {guidance}\n\n"
            f"{traceback_text}{rollback_text}"
        )
        repeat_count = record_crash_occurrence()
        app_log_path = getattr(logging.getLogger(), "_playlist_canvas_log_path", None)
        report = build_crash_report(
            type(error).__name__, str(error), report_context, app_log_path,
            window=window, repeat_count=repeat_count,
        )
        report_path: str | None = None
        try:
            report_path = str(save_crash_report(
                report, log_directory() / "crash-reports",
            ))
        except OSError:
            LOGGER.warning("Could not persist project load crash report", exc_info=True)
        dialog = ProjectCrashReportDialog(
            project_path=str(resolved_path),
            stage=stage,
            exception_type=type(error).__name__,
            exception_message=str(error),
            cause_type=type(cause).__name__,
            cause_message=str(cause),
            guidance=guidance,
            report_text=report,
            log_path=str(log_directory()),
            report_path=report_path,
            repeat_count=repeat_count,
            workspace_restored=rollback_error is None,
            allow_salvage=(
                stage in {"프로젝트 파일 읽기", "Reading project file"}
                and isinstance(error, ProjectError)
                and not isinstance(error, ProjectSaveConflict)
                and rollback_error is None
                and is_project_path(resolved_path) and resolved_path.is_file()
            ),
            korean=korean,
            parent=window,
        )
        try:
            dialog.exec()
            if dialog.exit_choice:
                self._pending_crash_exit_choice = dialog.exit_choice
            elif dialog.salvage_requested:
                self.attempt_salvage(resolved_path)
        finally:
            dialog.deleteLater()

    def attempt_salvage(self, source: Path) -> None:
        """Extract a separate copy; the failed load never replaces this workspace."""
        window = self.window
        korean = window.translator.is_korean
        parent = QFileDialog.getExistingDirectory(
            window,
            "복구 폴더를 만들 위치 선택" if korean else "Choose where to create a recovery folder",
            str(source.parent),
        )
        if not parent:
            return
        # Use a bounded unique child directory. The service rejects any
        # existing directory rather than merging with user files.
        directory = Path(parent) / f"MMS-recovery-{uuid4().hex[:12]}"
        worker = ProjectSalvageWorker(source, directory)
        loop = QEventLoop()
        title = "프로젝트 부분 복구" if korean else "Partial project recovery"
        progress = QProgressDialog(
            ("정상 데이터를 별도 폴더에 복구하는 중입니다. 원본은 변경하지 않습니다."
             if korean else "Recovering readable data into a separate folder. The original is unchanged."),
            "", 0, 0, window,
        )
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        cancel = QPushButton("취소" if korean else "Cancel")
        progress.setCancelButton(cancel)

        def request_cancel() -> None:
            worker.cancel_event.set()
            cancel.setEnabled(False)
            progress.setLabelText("복구 취소 중..." if korean else "Cancelling recovery...")
            progress.show()

        progress.canceled.connect(request_cancel)
        worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
        poll = QTimer()
        poll.timeout.connect(lambda: loop.quit() if worker.isFinished() else None)
        window.activity_progress.update("project_load", detail=title)
        try:
            progress.show()
            worker.start()
            poll.start(50)
            if not worker.isFinished():
                loop.exec()
            worker.wait()
            result, failure = worker.result, worker.error
        finally:
            poll.stop()
            if worker.isRunning():
                worker.cancel_event.set()
                wait_for_qthread(worker)
            progress.canceled.disconnect(request_cancel)
            progress.hide()
            progress.deleteLater()
            worker.deleteLater()

        if isinstance(failure, ProjectLoadCancelled):
            window.statusBar().showMessage("복구를 취소했습니다." if korean else "Recovery cancelled.", 5000)
        elif failure is not None:
            window._show_project_error(ProjectError(str(failure)))
        elif result is not None:
            # Do not auto-open or discard the current workspace. The result
            # remains available for the user to open through the usual workflow.
            self.show_salvage_result(result)

    def show_salvage_result(self, result: ProjectSalvageResult) -> None:
        window = self.window
        korean = window.translator.is_korean
        dialog = QMessageBox(window)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setWindowTitle("부분 복구 결과" if korean else "Partial recovery results")
        summary = (
            f"미디어 {len(result.recovered_assets)}개 복구 · 문제 {len(result.problems)}건"
            if korean else
            f"{len(result.recovered_assets)} media file(s) recovered; {len(result.problems)} issue(s)"
        )
        if result.project_path is not None:
            description = (
                "복구된 프로젝트는 외부 미디어를 참조합니다. 복구 폴더를 함께 보관하고, "
                "현재 작업을 저장한 후 복구된 프로젝트를 여세요."
                if korean else
                "The recovered project references external media. Keep the entire recovery folder "
                "and save your current work before opening the recovered project."
            )
            description += f"\n{result.project_path}"
        else:
            description = (
                "프로젝트 설정을 복원하지 못했습니다. 추출된 미디어와 보고서를 확인하세요."
                if korean else "The project manifest could not be restored. Inspect the extracted media and report."
            )
        dialog.setText(summary)
        dialog.setInformativeText(f"{description}\n\n{result.directory}")
        details = "\n".join(f"{item.entry}: {item.reason[:500]}" for item in result.problems[:20])
        dialog.setDetailedText(f"{result.directory / 'recovery-report.json'}\n{details}")
        dialog.setStandardButtons(QMessageBox.StandardButton.Close)
        folder = dialog.addButton(
            "복구 폴더 열기" if korean else "Open recovery folder", QMessageBox.ButtonRole.ActionRole,
        )
        try:
            dialog.exec()
            if dialog.clickedButton() is folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.directory)))
        finally:
            dialog.deleteLater()

    @staticmethod
    def load_guidance(error: BaseException, korean: bool) -> str:
        """Return a practical explanation based on the deepest load exception."""
        name = type(error).__name__
        message = str(error).casefold()
        if name == "JSONDecodeError":
            return ("프로젝트 내부 정보가 손상되었습니다. 백업 또는 자동 복구 파일을 사용해 보세요."
                    if korean else "The project manifest is malformed. Try a backup or autosave recovery file.")
        if name == "BadZipFile" or "zip" in message:
            return ("프로젝트 패키지가 손상되었거나 올바른 .pvsproj 파일이 아닙니다. 다시 복사하거나 백업을 사용해 보세요."
                    if korean else "The project package is damaged or is not a valid .pvsproj file. Copy it again or use a backup.")
        if name in {"PermissionError", "FileNotFoundError"}:
            return ("파일 위치와 읽기 권한을 확인한 뒤 다시 시도하세요."
                    if korean else "Check the file location and read permission, then try again.")
        if name in {"UnicodeDecodeError", "UnicodeError"}:
            return ("프로젝트 문자가 UTF-8 형식이 아닙니다. 원본 프로그램에서 다시 저장해 보세요."
                    if korean else "The project text is not valid UTF-8. Save it again from the original application.")
        if name in {"ValueError", "TypeError", "KeyError"}:
            return ("프로젝트 데이터가 지원 형식과 맞지 않습니다. 상세 보고서에서 잘못된 항목을 확인하세요."
                    if korean else "The project data does not match the supported format. Check the detailed report for the invalid field.")
        return ("상세 보고서를 복사해 문제 파일과 함께 개발자에게 전달하세요."
                if korean else "Copy the detailed report and provide it with the problematic file to the developer.")

    def resolve_media(self, document: ProjectDocument, project_path: Path) -> bool:
        """Relink missing project assets before the document changes the live workspace."""
        window = self.window
        self.media_relinked = False
        missing = ProjectMediaService.validate(document, project_path)
        if not missing:
            return True
        dialog = MissingMediaDialog(missing, window.translator, window)
        try:
            accepted = dialog.exec() == dialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()
        if not accepted:
            return False
        previous = document.to_dict()
        ProjectMediaService.apply_replacements(document, missing)
        self.media_relinked = document.to_dict() != previous
        return True
