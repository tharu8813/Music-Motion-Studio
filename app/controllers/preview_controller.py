"""Inline embedded playback preview and Canvas animation preview orchestration.

Extracted from MainWindow: the "Preview" bottom-tab embedded playback flow
(swap the Canvas for an embedded ExportPreviewDialog, lock editing, restore
on close) and the non-blocking single-source animation preview. MainWindow
keeps identically-named thin wrapper methods that delegate here, so every
existing call site and signal connection keeps working unchanged. Tests
patch this module's own names ("app.controllers.preview_controller.<Name>").

This does not touch the underlying playback backends: each dialog
(ExportPreviewDialog, content preview, track order, LRC generator, ...)
still owns its own QMediaPlayer. Unifying those into a single Preview
backend is a larger, separate redesign called out in the architecture
audit -- out of scope for this mechanical MainWindow decomposition.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox, QWidget

from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.dialogs.preview_loading_dialog import PreviewLoadingDialog
from app.renderer.ffmpeg_renderer import FFmpegNotFoundError, FFmpegRenderer

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

PREVIEW_TAB_INDEX = 2
PREVIEW_MIX_ACTIVITY = "preview_mix"
INITIAL_LOAD_LIMIT_MS = 4000
"""The loading popup never waits longer than this for AutoMix's saved analyses."""


class PreviewController:
    """Own inline playback preview and Canvas animation preview for a MainWindow."""

    def __init__(self, window: "MainWindow") -> None:
        self.window = window

    # -- bottom-tab embedded playback preview ---------------------------

    def preview_at(self, seconds: float) -> None:
        """Open the existing player from a visual timeline position."""
        window = self.window
        window._show_bottom_panel(PREVIEW_TAB_INDEX)
        if window._inline_preview is not None:
            window._inline_preview._seek_to_seconds(seconds)

    def open_playlist_preview(self) -> None:
        """Select the bottom Preview tab and start its embedded playback mode."""
        window = self.window
        if window.bottom_tabs.currentIndex() != PREVIEW_TAB_INDEX:
            window._show_bottom_panel(PREVIEW_TAB_INDEX)
            return
        if window._inline_preview is not None:
            window.canvas_stack.setCurrentWidget(window._inline_preview)
            window._inline_preview.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        tracks = [track for track in window.playlist_service.tracks if track.enabled]
        if not tracks:
            self.select_edit_bottom_tab(window._last_edit_bottom_tab)
            if window._offer_music_for_empty_playlist(
                "미리보기" if window.translator.is_korean else "Preview"
            ):
                # Songs were just added: continue straight into the Preview they asked for.
                window._show_bottom_panel(PREVIEW_TAB_INDEX)
            return
        self.show_export_preview(tracks)

    def bottom_workspace_tab_changed(self, index: int) -> None:
        """Enter Preview from its tab and restore editing from either edit tab."""
        window = self.window
        if window._bottom_tab_change_guard:
            return
        if index == PREVIEW_TAB_INDEX:
            self.open_playlist_preview()
            return
        if index not in {0, 1}:
            return
        window._last_edit_bottom_tab = index
        QSettings().setValue("workspace/bottom_tab", index)
        if window._inline_preview is not None:
            window._finish_inline_preview()

    def edit_transition(self, outgoing_track_id: str = "", incoming_track_id: str = "") -> None:
        """Open the AutoMix editor on one junction (the first without one) -- never Preview or a render."""
        from app.dialogs.automix_editor_dialog import AutoMixEditorDialog

        editor = AutoMixEditorDialog(self.window, (outgoing_track_id, incoming_track_id))
        editor.show()
        QTimer.singleShot(0, editor, lambda: self._guide_automix_editor(editor))

    @staticmethod
    def _guide_automix_editor(editor) -> None:
        """The first time the editor opens, walk through it once on top of it."""
        from app.dialogs.welcome_dialog import AUTOMIX_GUIDE, AUTOMIX_GUIDE_KEY, AUTOMIX_GUIDE_TITLE, GuideDialog, first_time

        if first_time(AUTOMIX_GUIDE_KEY):
            guide = GuideDialog(AUTOMIX_GUIDE, AUTOMIX_GUIDE_TITLE, editor.korean, editor)
            guide.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            guide.open()

    def select_edit_bottom_tab(self, index: int | None = None) -> None:
        """Select one persisted editing tab without recursively changing modes."""
        window = self.window
        selected = max(0, min(1, window._last_edit_bottom_tab if index is None else index))
        window._last_edit_bottom_tab = selected
        window._bottom_tab_change_guard = True
        try:
            window.bottom_tabs.setCurrentIndex(selected)
        finally:
            window._bottom_tab_change_guard = False
        QSettings().setValue("workspace/bottom_tab", selected)

    def _show_export_preview(self, tracks: list) -> None:
        window = self.window
        executable = None
        try:
            executable = FFmpegRenderer(
                window.settings_service.current.ffmpeg_path or None
            ).executable
        except FFmpegNotFoundError:
            window.check_ffmpeg_availability()
        transition_mode = window.project_settings.transition_mode
        crossfade_seconds = window.project_settings.crossfade_seconds
        # Per-track volume/EQ only exists in a rendered mix, even without transitions.
        needs_mix = (
            (transition_mode != "none" or any(track.audio_filter for track in tracks))
            and executable is not None and bool(tracks)
        )
        korean = window.translator.is_korean
        steps = [
            ("오디오 믹스 준비 시작", "Starting the audio mix"),
            ("미리보기 화면 만들기", "Building the preview screen"),
            ("재생 화면 배치", "Laying out the player"),
        ]
        if needs_mix and transition_mode == "automix":
            steps.append(("저장된 곡 분석 불러오기", "Loading saved track analysis"))
        if not needs_mix:
            steps.pop(0)
        loading = PreviewLoadingDialog(
            [korean_text if korean else english for korean_text, english in steps], korean, window,
        )
        self._loading = loading
        loading.start()
        from app.automix.settings import automix_settings_for

        automix_settings = automix_settings_for(window.project_settings)
        if transition_mode == "automix":
            # Preview analyzes the same files into the same caches at full
            # speed; a background pass still running would only duplicate
            # uncached work and compete for the CPU. It resumes on close.
            window._automix_analysis_timer.stop()
            window.automix_analysis_controller.cancel()
        preloaded_blended_audio = None
        blended_audio_controller = None
        blended_audio_temp_dir = None
        if needs_mix:
            preloaded_blended_audio, blended_audio_controller, blended_audio_temp_dir = (
                self._prepare_blended_preview_audio(
                    tracks, executable, transition_mode, crossfade_seconds, automix_settings,
                )
            )
            loading.advance(loading.current + 1)
        preview = ExportPreviewDialog(
            window.canvas.scene_model, tracks, window.translator,
            window._export_visualizers(tracks), executable, window, source_store=window.store,
            embedded=True,
            preferred_backend=window._preview_backend_for_session,
            transition_mode=transition_mode,
            crossfade_seconds=crossfade_seconds,
            preloaded_blended_audio=preloaded_blended_audio,
            blended_audio_controller=blended_audio_controller,
            blended_audio_temp_dir=blended_audio_temp_dir,
            automix_settings=automix_settings,
        )
        controls_page = preview.build_embedded_controls_page()
        loading.advance(loading.current + 1)
        window._inline_preview = preview
        window._inline_preview_controls = controls_page
        preview.finished.connect(window._finish_inline_preview)
        window.canvas_stack.addWidget(preview)
        window.preview_tab_layout.addWidget(controls_page)
        window.canvas_stack.setCurrentWidget(preview)
        self.lock_editor_for_inline_preview()
        track_panel = getattr(preview, "track_list_panel", None)
        if isinstance(track_panel, QWidget):
            window._inline_preview_track_panel = track_panel
            track_panel.setMinimumWidth(0)
            track_panel.setMaximumWidth(16_777_215)
            track_panel.setStyleSheet(controls_page.styleSheet())
            window.preview_track_inspector_layout.addWidget(track_panel)
        window.inspector_stack.setCurrentWidget(window.preview_track_inspector)
        tracks_toggle = getattr(preview, "tracks_toggle", None)
        if tracks_toggle is not None:
            tracks_toggle.toggled.connect(window.inspector_stack.setVisible)
            window.inspector_stack.setVisible(tracks_toggle.isChecked())
        window.statusBar().showMessage(
            "캔버스에서 전체 미리보기를 재생합니다 · 편집 기능이 잠겼습니다."
            if window.translator.is_korean else
            "Playing the full preview on the Canvas · Editing is locked."
        )
        self._track_background_mix_progress(preview)
        preview.show()
        preview.setFocus(Qt.FocusReason.OtherFocusReason)
        preview.finished.connect(loading.finish)
        if getattr(blended_audio_controller, "initial_loading", False):
            # Saved analyses land (and are planned) in a quick burst right after
            # start: keep the popup until then. Real analysis runs for minutes;
            # the status-bar activity shows that, so the popup closes when it starts.
            loading.advance(loading.current + 1)
            blended_audio_controller.initial_load_done.connect(loading.finish_soon)
            QTimer.singleShot(INITIAL_LOAD_LIMIT_MS, loading, loading.finish)
        else:
            loading.finish_soon()

    def show_export_preview(self, tracks: list) -> None:
        """Show a track-aware playback preview in the main Canvas workspace.

        A modal popup lists what is loading meanwhile; it closes once Preview
        is up (AutoMix: once its saved analyses are loaded, too).
        """
        window = self.window
        if window._inline_preview is not None:
            window.canvas_stack.setCurrentWidget(window._inline_preview)
            window._inline_preview.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self._loading = None
        try:
            self._show_export_preview(tracks)
        except BaseException:
            if self._loading is not None:
                self._loading.finish()
            raise

    def _prepare_blended_preview_audio(
        self, tracks: list, executable, transition_mode: str, crossfade_seconds: float,
        automix_settings=None,
    ) -> tuple[tuple[Path, object] | None, object | None, TemporaryDirectory | None]:
        """Start rendering blended preview audio and hand it to Preview without waiting.

        Preview opens at once on per-track audio; the render's progress shows in
        the status-bar activity (_track_background_mix_progress) and Preview
        hot-swaps to the mix -- AutoMix: to each partial mix -- as it lands.
        Returns ``(preloaded, controller, temp_dir)``:

        - ``preloaded`` is ``(path, plan)`` when the render finished during
          ``start()`` itself, so Preview opens already on the final plan.
        - ``controller`` is otherwise the still-running controller, for
          ExportPreviewDialog to adopt (same worker, no duplicate render).
        - ``temp_dir`` backs whichever of the above is not None, and must
          stay alive (owned by ExportPreviewDialog from here on) for as long
          as the rendered file might still be read.

        When the render fails during ``start()``, returns ``(None, None,
        None)``: ExportPreviewDialog falls back to its normal best-effort
        background render, exactly as when a render fails mid-preview.
        """
        from app.controllers.preview_audio_controller import PreviewAudioController
        from app.renderer.ffmpeg_renderer import FFmpegRenderer

        window = self.window
        temp_dir = TemporaryDirectory(prefix="playlist-preview-audio-", ignore_cleanup_errors=True)
        if transition_mode == "automix":
            # Analysis lands per track; once Preview is open it plays AutoMix
            # as far as it is analyzed, then the unchanged export mix.
            from app.controllers.progressive_automix_controller import ProgressiveAutoMixController

            controller = ProgressiveAutoMixController(
                FFmpegRenderer(executable), window,
                korean=window.translator.is_korean,
            )
        else:
            controller = PreviewAudioController(FFmpegRenderer(executable), window)
        result: dict[str, object] = {}

        def on_ready(path_str: str, plan: object) -> None:
            result["preloaded"] = (Path(path_str), plan)

        def on_failed(_message: str) -> None:
            result["failed"] = True

        controller.audio_ready.connect(on_ready)
        controller.audio_failed.connect(on_failed)
        controller.start(tracks, Path(temp_dir.name), transition_mode, crossfade_seconds,
                         automix_settings=automix_settings)
        if getattr(controller, "progressive_ready", None) is not None:
            # Attach as a paused listener at 0 so partial mixes render right away.
            controller.report_playhead(0.0, False)
        controller.audio_ready.disconnect(on_ready)
        controller.audio_failed.disconnect(on_failed)

        if "preloaded" in result:
            # The render already finished and forgot its own worker; nothing
            # further to adopt, so release the empty controller shell.
            controller.deleteLater()
            return result["preloaded"], None, temp_dir
        if "failed" in result:
            # Nothing to hand off: ExportPreviewDialog tries its own normal
            # background render from scratch.
            controller.shutdown()
            temp_dir.cleanup()
            return None, None, None
        # Hand the running controller to Preview: analysis and rendering continue.
        return None, controller, temp_dir

    def _track_background_mix_progress(self, preview) -> None:
        """Mirror a still-running blended-audio render in the status bar progress.

        Preview plays the per-track audio meanwhile and hot-swaps to the mix
        when it lands (ExportPreviewDialog._on_blended_audio_ready); this only
        makes that background work visible until it finishes or Preview closes.
        """
        controller = getattr(preview, "_blended_audio_controller", None)
        if controller is None:
            return
        window = self.window
        korean = window.translator.is_korean
        window.activity_progress.begin(
            PREVIEW_MIX_ACTIVITY,
            "미리보기 믹스 준비" if korean else "Preparing preview mix",
            0.0,
            detail=(
                "준비되는 동안 개별 곡 오디오로 재생하고, 완료되면 믹스로 전환합니다."
                if korean else
                "Playing per-track audio meanwhile; switches to the mix when ready."
            ),
        )

        settled = False  # ready or failed: a late (coalesced) update must not revive the task

        def on_progress(_stage: str, fraction: float, message: str) -> None:
            if settled:
                return
            # The live state ("Analyzing 3 / 12 ...", "AutoMix ready through track 4")
            # is the detail line of the status-bar popup; the title stays the task's name.
            # AutoMix also lists its stages (analysis, preview mix, final mix) with their own progress.
            window.activity_progress.update(
                PREVIEW_MIX_ACTIVITY, fraction, detail=message or None,
                steps=getattr(controller, "progress_steps", None) or None,
            )

        def on_ready(*_args: object) -> None:
            nonlocal settled
            settled = True
            window.activity_progress.finish(PREVIEW_MIX_ACTIVITY)
            window.statusBar().showMessage(
                getattr(controller, "fallback_message", None) or (
                    "미리보기 믹스가 준비되어 재생 중인 위치에서 전환했습니다." if korean else
                    "Preview mix ready; switched over at the current position."),
                4000,
            )

        def on_failed(*_args: object) -> None:
            nonlocal settled
            settled = True
            window.activity_progress.finish(PREVIEW_MIX_ACTIVITY)
            window.statusBar().showMessage(
                "미리보기 믹스를 준비하지 못해 개별 곡 오디오로 계속 재생합니다." if korean else
                "Could not prepare the preview mix; continuing with per-track audio.",
                6000,
            )

        controller.progress.connect(on_progress)
        controller.audio_ready.connect(on_ready)
        controller.audio_failed.connect(on_failed)
        last = getattr(controller, "last_progress", None)
        if last is not None:
            on_progress(*last)  # continue from where the preparation popup was

    def lock_editor_for_inline_preview(self) -> None:
        """Lock project mutation while keeping bottom mode tabs interactive."""
        window = self.window
        if window._preview_ui_lock_state is not None:
            return
        widgets = (window.canvas,)
        # Help only opens read-only dialogs, so it stays usable (F1 included);
        # every other menu keeps its action disabled like before.
        read_only_actions = {
            window.help_menu.menuAction(), window.help_action,
            window.shortcuts_action, window.about_action,
        }
        actions = tuple(
            (action, action.isEnabled()) for action in window.findChildren(QAction)
            if action not in read_only_actions
        )
        window._preview_ui_lock_state = {
            "widgets": tuple((widget, widget.isEnabled()) for widget in widgets),
            "menu": window.menuBar().isEnabled(),
            "toolbar": window.toolbar.isEnabled(),
            "toolbar_visible": not window.toolbar.isHidden(),
            "drops": window.acceptDrops(),
            "actions": actions,
            "left_visible": not window.left_workspace.isHidden(),
            "inspector_visible": not window.inspector_stack.isHidden(),
            "inspector_page": window.inspector_stack.currentWidget(),
            "workspace_sizes": tuple(window.workspace_splitter.sizes()),
            "top_panel_minimum_height": window.main_splitter.minimumHeight(),
            "bottom_panel_minimum_height": window.bottom_workspace_stack.minimumHeight(),
            "main_splitter_sizes": tuple(window.main_splitter.sizes()),
            "sidebar_open_width": max(
                180, window.left_workspace.width(), window._sidebar_open_width,
            ),
        }
        for widget in widgets:
            widget.setEnabled(False)
        for action, _enabled in actions:
            action.setEnabled(False)
        window.toolbar.setEnabled(False)
        window._set_sidebar_visible(False, persist=False, sync_action=False)
        controls_page = window._inline_preview_controls
        if controls_page is not None:
            controls_page.ensurePolished()
            if controls_page.layout() is not None:
                controls_page.layout().activate()
            controls_minimum = max(controls_page.minimumHeight(), controls_page.minimumSizeHint().height())
            margins = window.preview_tab_layout.contentsMargins()
            controls_minimum += (
                window.bottom_tabs.tabBar().sizeHint().height()
                + margins.top() + margins.bottom()
            )
            window.bottom_workspace_stack.setMinimumHeight(max(120, controls_minimum))
        # Playback controls must remain usable in a short window; the video
        # stage can shrink further than the editor's property panels.
        window.main_splitter.setMinimumHeight(120)
        sizes = window.workspace_splitter.sizes()
        if len(sizes) == 2:
            total = sum(sizes)
            controls_height = max(
                window.bottom_workspace_stack.minimumHeight(),
                min(250, max(210, round(total * 0.27))),
            )
            window.workspace_splitter.setSizes([
                max(120, total - controls_height), controls_height,
            ])
        window.setAcceptDrops(False)

    def finish_inline_preview(self, _result: int = 0) -> None:
        """Return from the embedded playback page to the editable Canvas."""
        window = self.window
        preview = window._inline_preview
        if preview is None:
            return
        controls_page = window._inline_preview_controls
        track_panel = window._inline_preview_track_panel
        window._inline_preview = None
        window._inline_preview_controls = None
        window._inline_preview_track_panel = None
        preview._stop_preview()
        window.canvas_stack.setCurrentWidget(window.canvas)
        window.canvas_stack.removeWidget(preview)
        if track_panel is not None:
            window.preview_track_inspector_layout.removeWidget(track_panel)
            track_panel.setParent(preview)
        if controls_page is not None:
            window.preview_tab_layout.removeWidget(controls_page)
            controls_page.deleteLater()
        preview.deleteLater()
        self.unlock_editor_after_inline_preview()
        if window.bottom_tabs.currentIndex() == PREVIEW_TAB_INDEX:
            self.select_edit_bottom_tab()
        window.activity_progress.finish(PREVIEW_MIX_ACTIVITY)  # render cancelled with Preview
        window.statusBar().showMessage(
            "미리보기를 종료하고 캔버스 편집으로 돌아왔습니다."
            if window.translator.is_korean else
            "Preview closed; returned to Canvas editing.",
            2500,
        )
        window.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        # Background analysis stood down for Preview; pick it up again (the
        # tracks Preview analyzed are cache hits now, so this mostly refreshes badges).
        window._automix_analysis_timer.start()
        # The left workspace expands for 190 ms when Preview releases its UI
        # lock. Fit against the final layout, not the transient narrow Canvas.
        window._schedule_canvas_fit(230)

    def unlock_editor_after_inline_preview(self) -> None:
        """Restore exactly the interaction state that preceded inline preview."""
        window = self.window
        state = window._preview_ui_lock_state
        if state is None:
            return
        window._preview_ui_lock_state = None
        for widget, enabled in state["widgets"]:
            widget.setEnabled(enabled)
        for action, enabled in state["actions"]:
            action.setEnabled(enabled)
        window.menuBar().setEnabled(bool(state["menu"]))
        window.toolbar.setEnabled(bool(state["toolbar"]))
        window.toolbar.setVisible(bool(state["toolbar_visible"]))
        window._sidebar_open_width = max(
            180, int(state.get("sidebar_open_width", window._sidebar_open_width)),
        )
        window._set_sidebar_visible(
            bool(state["left_visible"]), persist=False, sync_action=False,
            restore_sizes=list(state.get("main_splitter_sizes", ())),
        )
        inspector_page = state.get("inspector_page", window.inspector)
        if isinstance(inspector_page, QWidget):
            window.inspector_stack.setCurrentWidget(inspector_page)
        else:
            window.inspector_stack.setCurrentWidget(window.inspector)
        window.inspector_stack.setVisible(bool(state["inspector_visible"]))
        window.main_splitter.setMinimumHeight(
            state.get("top_panel_minimum_height", window.main_splitter.minimumHeight())
        )
        window.bottom_workspace_stack.setMinimumHeight(
            state.get("bottom_panel_minimum_height", window.bottom_workspace_stack.minimumHeight())
        )
        workspace_sizes = list(state["workspace_sizes"])
        if len(workspace_sizes) == 2:
            window.workspace_splitter.setSizes(workspace_sizes)
        window.setAcceptDrops(bool(state["drops"]))
        window._sync_canvas_shortcut_actions(None, window.canvas)
        window._update_alignment_toolbar_actions()

    # -- canvas animation preview -----------------------------------------

    def preview_source_animation(self, source_id: str) -> None:
        """Play one source's configured animation directly on the Canvas.

        The preview is non-blocking: the window stays interactive and the very
        next user action (a click, key press, selection change or edit) stops it
        and snaps the source back to its real position.
        """
        window = self.window
        source = window.store.get(source_id)
        item = window.canvas._items.get(source_id)
        if source is None or item is None:
            return
        if window._animation_preview_active:
            window.animation_preview_controller.cancel()
        window._animation_preview_active = True
        window._animation_preview_cancel_armed = False
        korean = window.translator.is_korean
        window.statusBar().showMessage(
            "애니메이션 미리보기 재생 중 · 다른 동작을 하면 중단됩니다."
            if korean else
            "Playing animation preview · Any further action stops it.",
            2500,
        )
        if not window.animation_preview_controller.preview(item, source):
            window._finish_canvas_animation_preview()
            return
        window.canvas.viewport().installEventFilter(window)
        QTimer.singleShot(0, window._arm_animation_preview_cancel)

    def arm_animation_preview_cancel(self) -> None:
        """Start honouring cancel triggers once the launching edit has settled."""
        window = self.window
        if window._animation_preview_active:
            window._animation_preview_cancel_armed = True

    def cancel_animation_preview(self, *_args: object) -> None:
        """Stop an armed preview in response to any further user action."""
        window = self.window
        if window._animation_preview_active and window._animation_preview_cancel_armed:
            window.animation_preview_controller.cancel()

    def handle_viewport_event(self, event: QEvent) -> None:
        """Cancel an armed animation preview on the next real user interaction."""
        window = self.window
        if window._animation_preview_cancel_armed and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.KeyPress,
            QEvent.Type.Wheel,
        ):
            window.animation_preview_controller.cancel()

    def finish_canvas_animation_preview(self) -> None:
        """Restore interaction after the Canvas preview returns to its source state."""
        window = self.window
        if not window._animation_preview_active:
            return
        window._animation_preview_active = False
        window._animation_preview_cancel_armed = False
        window.canvas.viewport().removeEventFilter(window)
        window.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
