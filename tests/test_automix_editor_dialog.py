"""The AutoMix editor: its own plan, undo steps, typed values, modes, and what it never touches."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.automix.overrides import TransitionOverride, pair_key  # noqa: E402
from app.dialogs.automix_editor_dialog import AutoMixEditorDialog  # noqa: E402
from app.models.project import ProjectSettings  # noqa: E402
from app.utils.i18n import Translator  # noqa: E402
from tests.test_automix_manual_transitions import _analyses, _tracks  # noqa: E402


class AutoMixEditorTests(unittest.TestCase):
    def test_bluetooth_notice_fits_small_window_with_playback_controls(self):
        host = self._host()
        with patch("app.widgets.audio_sync_warning.is_bluetooth_output", return_value=True):
            for language in ("ko", "en"):
                with self.subTest(language=language):
                    host.translator.set_language(language)
                    _host, editor = self._editor(host)
                    editor.show()
                    editor.resize(editor.minimumSize())
                    self.app.processEvents()
                    warning = editor.audio_sync_warning
                    self.assertTrue(warning.isVisible())
                    self.assertGreaterEqual(warning.height(), warning.heightForWidth(warning.width()))
                    self.assertTrue(editor.rect().contains(warning.mapTo(editor, warning.rect().bottomRight())))
                    self.assertTrue(editor.play_button.isVisible())
                    editor._ensure_player()
                    self.assertIs(warning._audio_output, editor._audio_output)
                    editor.close()

    def test_dj_effect_controls_commit_custom_values_and_undo(self):
        host, editor = self._editor()
        panel = editor.properties
        panel.style_buttons["beat_roll"].click()
        self.assertFalse(panel.roll_box.isHidden())
        self.assertTrue(panel.cutoff_box.isHidden())
        panel.roll_combo.setCurrentIndex(0)
        self.assertEqual(editor._overrides["a>b"].roll_beats, 0.25)
        self.assertEqual(editor._plan.audio.transitions[0].roll_beats, 0.25)
        editor.undo_stack.undo()
        self.assertEqual(editor._overrides["a>b"].roll_beats, 0.5)
        panel.style_buttons["lowpass_out"].click()
        self.assertFalse(panel.cutoff_box.isHidden())
        self.assertTrue(panel.roll_box.isHidden())
        panel.cutoff_spin.setValue(350)
        panel.flush()
        self.assertEqual(editor._plan.audio.transitions[0].filter_cutoff_hz, 350)
        self.assertEqual(host._set_automix_override.call_args.args[1].filter_cutoff_hz, 350)
        editor._copy()
        editor._user_select(1)
        editor._paste()
        self.assertEqual(editor._overrides["b>c"].filter_cutoff_hz, 350)
        self.assertEqual(editor._overrides["b>c"].style, "lowpass_out")
        self.addCleanup(QSettings().remove, "automix_editor/presets")
        editor._save_presets({"DJ lowpass": editor._overrides["b>c"]})
        self.assertEqual(editor._load_presets()["DJ lowpass"].filter_cutoff_hz, 350)
        editor._reset_item("effect")
        self.assertEqual(editor._overrides["b>c"].filter_cutoff_hz, 800)
        editor._apply_preset("DJ lowpass")
        self.assertEqual(editor._overrides["b>c"].filter_cutoff_hz, 350)

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # The bilingual layout check changes persistent settings. Keep the same
        # Korean baseline as the main-window suites and restore it after each test.
        settings = QSettings()
        previous_language = settings.value("language", None)
        settings.setValue("language", "ko")
        if previous_language is None:
            self.addCleanup(settings.remove, "language")
        else:
            self.addCleanup(settings.setValue, "language", previous_language)
        QSettings().setValue("automix_editor/advanced", False)
        for key in ("geometry", "splitter", "properties_visible", "lanes_visible", "snap"):
            QSettings().remove(f"automix_editor/{key}")
            self.addCleanup(QSettings().remove, f"automix_editor/{key}")

    def _host(self, overrides=None):
        host = QWidget()
        self.addCleanup(host.deleteLater)
        tracks = _tracks()
        host.playlist_service = SimpleNamespace(tracks=tracks)
        host.translator = Translator()
        host.project_settings = ProjectSettings(automix_overrides=overrides or {})
        host.automix_analyses = _analyses(tracks)
        host.automix_structures = {}
        host._set_automix_override = Mock()
        return host

    def _editor(self, host=None, pair=("a", "b")):
        host = host or self._host()
        editor = AutoMixEditorDialog(host, pair)
        self.addCleanup(lambda: editor._finished or editor.close())
        return host, editor

    def test_opens_on_the_chip_junction_without_preview_or_any_render(self):
        from app.controllers.preview_controller import PreviewController

        host = self._host()
        controller = PreviewController(host)
        controller.open_playlist_preview = Mock()
        with patch("app.renderer.ffmpeg_renderer.FFmpegRenderer.prepare_playlist_audio") as full_render:
            controller.edit_transition("b", "c")
            editor = host.findChild(AutoMixEditorDialog)
            self.addCleanup(editor.close)
            self.assertEqual(editor._index, 1)
            controller.open_playlist_preview.assert_not_called()
            full_render.assert_not_called()
        self.assertEqual(editor.audition.render_count, 0)  # no FFmpeg configured: nothing renders
        self.assertEqual(editor.audition.state, "unavailable")
        self.assertFalse(editor.play_button.isEnabled())
        controller.edit_transition()  # the playlist's "AutoMix editor" button: first transition
        self.assertEqual([e._index for e in host.findChildren(AutoMixEditorDialog)][-1], 0)

    def test_f1_opens_its_help_tab_and_shift_f1_the_shortcut_summary(self):
        _host, editor = self._editor()
        keys = {shortcut.key().toString() for shortcut in editor.findChildren(QShortcut)}
        self.assertTrue({"F1", "Shift+F1"}.issubset(keys))
        with patch("app.dialogs.help_dialog.HelpDialog") as help_dialog:
            editor.help_button.click()
        _translator, parent = help_dialog.call_args.args
        self.assertIs(parent, editor)
        self.assertEqual(help_dialog.call_args.kwargs, {"tab": "automix_editor", "topic": "automix_editor"})
        help_dialog.return_value.exec.assert_called_once()
        with patch.object(QApplication, "focusWidget", return_value=editor.properties):
            self.assertEqual(editor._help_context(), ("automix_editor", "automix_properties"))

    def test_it_can_be_maximized_and_reopens_the_way_it_was_left(self):
        if not QApplication.organizationName():  # QSettings stores nothing without one
            QApplication.setOrganizationName("Music Motion Studio Tests")
            self.addCleanup(QApplication.setOrganizationName, "")
        _host, editor = self._editor()
        self.assertTrue(editor.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint)
        editor.show()
        editor.resize(780, 600)  # fits the 800 px offscreen test screen
        editor.close()
        _host, again = self._editor()
        again.show()
        self.assertEqual((again.width(), again.height()), (780, 600))

    def test_analysis_arriving_after_the_editor_is_deleted_is_ignored(self):
        from PySide6.QtCore import QObject, Signal
        from shiboken6 import delete

        class Controller(QObject):
            analyses_updated = Signal(object)

        host = self._host()
        host.automix_analysis_controller = Controller()
        _host, editor = self._editor(host)
        editor._finished = True  # skip the cleanup close() on a deleted object
        delete(editor)
        with patch("sys.excepthook") as hook:
            host.automix_analysis_controller.analyses_updated.emit({})
            QApplication.processEvents()
        hook.assert_not_called()

    def test_an_edit_is_stored_replanned_and_undone_in_one_step(self):
        host, editor = self._editor()
        following = editor._junctions[1].start
        editor.properties.outgoing_spin.setValue(editor.properties.outgoing_spin.value() - 8.0)
        editor.properties.flush()
        key, override = host._set_automix_override.call_args.args
        self.assertEqual(key, pair_key("a", "b"))
        # The whole plan follows at once (CPU only): the next transition moved 8 s earlier.
        self.assertAlmostEqual(editor._junctions[1].start, following - 8.0, places=3)
        self.assertTrue(editor.transition_combo.currentText().startswith("✎"))
        self.assertEqual(editor.undo_stack.count(), 1)
        editor.undo_button.click()
        self.assertEqual(host._set_automix_override.call_args.args, (key, None))
        self.assertAlmostEqual(editor._junctions[1].start, following, places=6)
        editor.redo_button.click()
        self.assertEqual(host._set_automix_override.call_args.args, (key, override))

    def test_a_length_preset_keeps_the_cues(self):
        host, editor = self._editor()
        before = editor._base()
        editor.properties.length_presets[4.0].click()
        after = host._set_automix_override.call_args.args[1]
        self.assertEqual(after.duration, 4.0)
        self.assertAlmostEqual(after.outgoing_cue, before.outgoing_cue, places=6)
        self.assertAlmostEqual(after.incoming_cue, before.incoming_cue, places=6)

    def test_modes_show_the_same_edit_and_switching_changes_nothing(self):
        host, editor = self._editor()
        self.assertTrue(editor.properties.cue_box.isHidden())
        self.assertTrue(editor.properties.facts_box.isHidden())
        editor._set_advanced(True)
        self.assertFalse(editor.properties.cue_box.isHidden())
        self.assertFalse(editor.properties.facts_box.isHidden())
        self.assertTrue(editor.timeline.advanced)
        editor.properties.style_buttons["vocal_safe_eq"].click()
        editor.properties.handoff_slider.setValue(65)
        editor.properties.flush()
        edited = editor._overrides[pair_key("a", "b")]
        self.assertEqual(edited.vocal_handoff, 0.65)
        calls = host._set_automix_override.call_count
        editor._set_advanced(False)
        editor._set_advanced(True)
        self.assertEqual(host._set_automix_override.call_count, calls)
        self.assertEqual(editor._overrides[pair_key("a", "b")], edited)

    def test_a_value_being_typed_lands_on_its_own_transition(self):
        host, editor = self._editor()
        editor.properties.length_spin.setValue(5.5)
        self.assertTrue(editor.properties.pending)
        editor.next_button.click()  # moves before the debounce fired
        self.assertEqual(editor._index, 1)
        self.assertEqual(host._set_automix_override.call_args.args[0], pair_key("a", "b"))
        self.assertEqual(host._set_automix_override.call_args.args[1].duration, 5.5)
        self.assertNotIn(pair_key("b", "c"), editor._overrides)

    def test_closing_saves_a_pending_typed_value(self):
        host, editor = self._editor()
        editor.properties.length_spin.setValue(7.0)
        editor.close()
        self.assertEqual(host._set_automix_override.call_args.args[1].duration, 7.0)

    def test_a_drag_is_one_undo_step_and_clicks_onto_the_downbeat(self):
        host, editor = self._editor()
        editor.timeline.resize(1000, 400)
        before = editor._base()
        editor._drag_started("move")
        editor._drag_moved("move", -2.0, False)
        editor._drag_moved("move", -4.2, False)
        self.assertEqual(host._set_automix_override.call_count, 0)  # nothing stored mid-drag
        self.assertTrue(editor.timeline.draft)
        editor._drag_finished()
        self.assertEqual(editor.undo_stack.count(), 1)
        after = host._set_automix_override.call_args.args[1]
        self.assertLess(after.outgoing_cue, before.outgoing_cue)
        self.assertIn(after.outgoing_cue, _analyses(_tracks())["a"].downbeats)
        self.assertFalse(editor.timeline.draft)

    def test_a_drag_stops_at_the_song_end_and_says_why(self):
        _host, editor = self._editor()
        editor.timeline.resize(1000, 400)
        junction = editor.junction
        editor._drag_started("move")
        editor._drag_moved("move", 60.0, True)
        self.assertIn("나가는 곡 끝", editor.timeline.drag_hint)
        drawn = editor.timeline.junction
        self.assertAlmostEqual(drawn.end - drawn.start, junction.end - junction.start, places=3)
        editor._drag_finished()

    def test_nudge_moves_the_selected_handle_by_a_beat(self):
        host, editor = self._editor()
        before = editor._base()
        editor.timeline.selected_part = "start"
        editor.timeline.setFocus()
        QTest.keyClick(editor.timeline, Qt.Key.Key_Left)
        after = host._set_automix_override.call_args.args[1]
        self.assertAlmostEqual(after.outgoing_cue, before.outgoing_cue - 0.5, places=3)  # 120 BPM

    def test_typing_space_in_a_number_field_does_not_start_playback(self):
        _host, editor = self._editor()
        editor._set_advanced(True)
        editor.show()
        spin = editor.properties.position_spin
        spin.setFocus()
        self.app.processEvents()
        with patch.object(editor, "_toggle_play") as toggle:
            QTest.keyClick(spin, Qt.Key.Key_Space)
            QTest.keyClick(spin, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        toggle.assert_not_called()
        self.assertEqual(editor.undo_stack.count(), 0)

    def test_copy_paste_carries_how_it_mixes_not_where(self):
        host, editor = self._editor()
        editor.properties.style_buttons["echo_out"].click()
        editor.properties._emit({"echo_feedback": 0.7, "echo_beats": 0.5})
        editor._copy()
        editor._user_select(1)
        cues = editor._base()
        editor._paste()
        pasted = host._set_automix_override.call_args.args
        self.assertEqual(pasted[0], pair_key("b", "c"))
        self.assertEqual((pasted[1].style, pasted[1].echo_feedback, pasted[1].echo_beats), ("echo_out", 0.7, 0.5))
        self.assertEqual((pasted[1].outgoing_cue, pasted[1].incoming_cue), (cues.outgoing_cue, cues.incoming_cue))

    def test_reset_goes_back_to_automatic_and_band_drags_become_custom_eq(self):
        host, editor = self._editor()
        editor.properties.style_buttons["bass_swap"].click()
        bands = (((0.1, 0.3), (0.2, 0.4)),) * 3
        editor._bands_changed(bands)
        self.assertEqual(host._set_automix_override.call_args.args[1].style, "eq")
        self.assertEqual(host._set_automix_override.call_args.args[1].eq_bands, bands)
        editor.properties.reset_button.click()
        self.assertEqual(host._set_automix_override.call_args.args, (pair_key("a", "b"), None))
        self.assertFalse(editor.properties.reset_button.isEnabled())

    def test_a_b_plays_the_automatic_version_until_the_next_edit(self):
        from pathlib import Path

        _host, editor = self._editor()
        self.assertFalse(editor.compare_button.isEnabled())  # automatic: nothing to compare
        editor.audition._executable = Path("ffmpeg.exe")
        editor.audition.request = Mock()
        editor.properties.length_presets[4.0].click()
        self.assertTrue(editor.compare_button.isEnabled())

        def heard_mode():
            plan, index, _tracks_ = editor.audition.request.call_args.args
            return dict(plan.audio.transitions[index].details)["mode"]

        self.assertEqual(heard_mode(), "manual")
        editor.compare_button.click()
        self.assertEqual(heard_mode(), "auto")
        self.assertIn("듣는 중: 자동 결과", editor.state_label.text())  # says which of A/B is heard
        self.assertFalse(editor.mine_button.isChecked())
        editor.compare_button.click()
        self.assertEqual(heard_mode(), "manual")
        editor.compare_button.click()
        editor.properties.length_presets[8.0].click()  # an edit is heard as edited
        self.assertFalse(editor.compare_button.isChecked())
        self.assertEqual(heard_mode(), "manual")

    def test_presets_save_how_it_mixes_and_apply_elsewhere(self):
        store = {}
        fake = Mock()
        fake.return_value.value.side_effect = lambda key, default=None: store.get(key, default)
        fake.return_value.setValue.side_effect = store.__setitem__
        host, editor = self._editor()
        editor.properties.style_buttons["filter_sweep"].click()
        editor.properties.length_presets[16.0].click()
        with patch("app.dialogs.automix_editor_dialog.QSettings", fake), \
                patch("PySide6.QtWidgets.QInputDialog.getText", return_value=("Club sweep", True)):
            editor._save_preset()
            self.assertEqual(list(editor._load_presets()), ["Club sweep"])
            editor._user_select(1)
            cues = editor._base()
            editor._fill_presets_menu()
            titles = [action.text() for action in editor.presets_menu.actions()]
            self.assertTrue(any(title.startswith("Club sweep") for title in titles), titles)
            editor._apply_preset("Club sweep")
            key, applied = host._set_automix_override.call_args.args
            self.assertEqual(key, pair_key("b", "c"))
            self.assertEqual((applied.style, applied.duration), ("filter_sweep", 16.0))
            self.assertEqual((applied.outgoing_cue, applied.incoming_cue), (cues.outgoing_cue, cues.incoming_cue))
            store["automix_editor/presets"] = '{"preset>Broken": {"outgoing_cue": 0, "style": "reverb"}}'
            self.assertEqual(editor._load_presets(), {})  # a broken preset is dropped, not fatal
            editor._delete_preset("Club sweep")

    def test_new_analysis_keeps_the_selection_and_the_view(self):
        host, editor = self._editor(pair=("b", "c"))
        view = editor.timeline.view()
        host.automix_analyses = {}
        editor._analysis_arrived()
        self.assertEqual(editor._index, 1)
        self.assertEqual(editor.timeline.view(), view)

    def test_closing_re_mixes_an_open_preview_once_and_only_after_an_edit(self):
        host = self._host()
        host._inline_preview = SimpleNamespace(_transition_mode="automix", remix_automix=Mock(),
                                               _automix_settings=None)
        _host, untouched = self._editor(host)
        untouched.close()
        host._inline_preview.remix_automix.assert_not_called()
        _host, editor = self._editor(host)
        editor.properties.length_presets[4.0].click()
        editor.properties.length_presets[16.0].click()
        host.project_settings = ProjectSettings(
            automix_overrides={"a>b": TransitionOverride(150.0).to_dict()})
        editor.close()
        host._inline_preview.remix_automix.assert_called_once()
        self.assertEqual(host._inline_preview._automix_settings.override_for("a", "b"), TransitionOverride(150.0))



class EditorWorkflowTests(AutoMixEditorTests):
    """Selection, drags, kept values, recovery and the workspace (runs the base tests' fixtures too)."""

    def _storing_host(self):
        """A host that saves every edit into its project settings, as MainWindow does."""
        from app.automix.overrides import serialize_overrides

        host = self._host()
        saved = {}

        def store(key, override):
            if override is None:
                saved.pop(key, None)
            else:
                saved[key] = override
            host.project_settings = ProjectSettings(transition_mode="automix",
                                                    automix_overrides=serialize_overrides(saved))
        host._set_automix_override = Mock(side_effect=store)
        return host

    def test_selecting_on_the_timeline_shows_that_targets_settings_and_modes_keep_it(self):
        _host, editor = self._editor()
        editor._set_advanced(True)
        panel = editor.properties
        editor.timeline._select("incoming")
        self.assertIs(panel.pages.currentWidget(), panel.page_widgets["incoming"])
        self.assertEqual(panel.selection_title.text(), "B 시작 · 들어오는 곡")
        height = panel.pages.sizeHint().height()
        editor.timeline._select("band:low")
        self.assertEqual(panel.pages.sizeHint().height(), height)  # the panel does not jump between targets
        editor._set_advanced(False)
        self.assertIs(panel.pages.currentWidget(), panel.page_widgets["transition"])
        editor._set_advanced(True)
        self.assertIs(panel.pages.currentWidget(), panel.page_widgets["band"])
        editor._analysis_arrived()  # nor does new analysis move the selection
        self.assertEqual(editor.timeline.selection, "band:low")

    def test_tempo_settings_remain_reachable_after_disabling_and_selecting_another_target(self):
        host = self._storing_host()
        host.automix_analyses = _analyses(host.playlist_service.tracks, (120.0, 126.0, 120.0))
        _host, editor = self._editor(host)
        editor._set_advanced(True)
        editor.timeline._select("tempo")
        panel = editor.properties
        panel.tempo_check.setChecked(True)
        self.assertIsNotNone(editor.timeline._ramp_span())
        panel.tempo_check.click()
        self.assertIsNone(editor.timeline._ramp_span())
        self.assertEqual(editor.timeline.selection, "transition")
        self.assertIsNone(editor.timeline.selected_part)
        self.assertIs(panel.pages.currentWidget(), panel.page_widgets["transition"])
        editor.timeline._select("incoming")
        editor.properties_button.setChecked(False)
        before = editor.undo_stack.count()

        self.assertFalse(editor.tempo_button.isHidden())
        editor.tempo_button.click()
        self.assertTrue(editor.properties_button.isChecked())
        self.assertEqual(editor.timeline.selection, "tempo")
        self.assertIs(panel.pages.currentWidget(), panel.page_widgets["tempo"])
        self.assertTrue(panel.tempo_check.isChecked())
        self.assertTrue(panel.tempo_check.isEnabled())
        self.assertEqual(editor.undo_stack.count(), before + 1)
        self.assertTrue(host.project_settings.automix_overrides[pair_key("a", "b")]["tempo_match"])
        self.assertIsNotNone(editor.timeline._ramp_span())
        editor.timeline._select("incoming")
        editor.tempo_button.click()
        self.assertEqual(editor.timeline.selection, "tempo")
        self.assertEqual(editor.undo_stack.count(), before + 1)
        editor.undo_button.click()
        self.assertFalse(panel.tempo_check.isChecked())
        editor.redo_button.click()
        self.assertTrue(panel.tempo_check.isChecked())

    def test_esc_cancels_a_drag_and_one_drag_is_one_undo_step(self):
        host, editor = self._editor()
        editor.timeline.resize(1000, 400)
        editor.timeline.setFocus()
        junction = editor.junction
        x, y = editor.timeline.x_of(junction.end), editor.timeline._lane_top(0) + 30
        QTest.mousePress(editor.timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(x), round(y)))
        QTest.mouseMove(editor.timeline, QPoint(round(x) + 60, round(y)))
        QTest.mouseMove(editor.timeline, QPoint(round(x) + 80, round(y)))
        self.assertIn("Δ", editor.timeline.drag_hint)  # the value and how far it moved
        QTest.keyClick(editor.timeline, Qt.Key.Key_Escape)
        QTest.mouseRelease(editor.timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(x) + 80, round(y)))
        self.assertEqual(editor.undo_stack.count(), 0)
        host._set_automix_override.assert_not_called()
        self.assertFalse(editor.timeline.draft)
        self.assertAlmostEqual(editor.timeline.junction.end, junction.end)
        editor._drag_started("end")
        editor._drag_moved("end", 3.0, False)
        editor._drag_moved("end", 4.0, False)
        editor._drag_finished()
        self.assertEqual(editor.undo_stack.count(), 1)
        longer = editor._overrides[pair_key("a", "b")].duration
        editor.undo_button.click()
        self.assertNotIn(pair_key("a", "b"), editor._overrides)
        editor.redo_button.click()
        self.assertEqual(editor._overrides[pair_key("a", "b")].duration, longer)

    def test_text_typed_without_enter_is_kept_when_moving_on_or_closing(self):
        host, editor = self._editor()
        editor.show()
        spin = editor.properties.length_spin
        spin.setFocus()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), "6.25")
        editor.next_button.click()  # a tool button takes no focus: the field never saw Enter
        self.assertEqual(host._set_automix_override.call_args.args[0], pair_key("a", "b"))
        self.assertAlmostEqual(host._set_automix_override.call_args.args[1].duration, 6.25)
        spin.setFocus()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), "7.5")
        editor.close()
        self.assertEqual(host._set_automix_override.call_args.args[0], pair_key("b", "c"))
        self.assertAlmostEqual(host._set_automix_override.call_args.args[1].duration, 7.5)

    def test_a_kept_length_survives_a_new_recommendation_paste_and_drags(self):
        host, editor = self._editor()
        editor._set_advanced(True)
        length = editor.junction.end - editor.junction.start
        editor.properties.length_header.lock.click()  # still automatic, now keeping its length
        kept = editor._overrides[pair_key("a", "b")]
        self.assertEqual((kept.locked, kept.recommend), (("duration",), True))
        self.assertAlmostEqual(editor.junction.end - editor.junction.start, length, places=6)
        self.assertIn("고정", editor.properties.mode_chip.text())
        editor.properties.style_buttons["echo_out"].click()  # a hand edit keeps the lock
        self.assertEqual(editor._overrides[pair_key("a", "b")].locked, ("duration",))
        self.assertAlmostEqual(editor._overrides[pair_key("a", "b")].duration, length, places=6)
        editor._drag_started("end")
        editor._drag_moved("end", 4.0, True)
        self.assertIn("고정", editor.timeline.drag_hint)
        editor._drag_finished()
        self.assertAlmostEqual(editor._overrides[pair_key("a", "b")].duration, length, places=6)
        editor._user_select(1)
        editor.properties.length_presets[16.0].click()
        editor._copy()
        editor._user_select(0)
        editor._paste()
        pasted = editor._overrides[pair_key("a", "b")]
        self.assertAlmostEqual(pasted.duration, length, places=6)  # the paste kept the kept length
        self.assertIn("유지", editor.message_label.text())
        self.assertEqual(editor.properties.reset_button.text(), "자동 추천 다시 받기")
        editor.properties.reset_button.click()  # a new recommendation, keeping the length
        again = editor._overrides[pair_key("a", "b")]
        self.assertTrue(again.recommend)
        self.assertAlmostEqual(editor.junction.end - editor.junction.start, length, places=6)
        self.assertEqual(dict(editor.junction.transition.details)["recommendation"], "locked")
        editor._reset_all()
        self.assertNotIn(pair_key("a", "b"), editor._overrides)

    def test_a_kept_cue_nothing_can_hold_is_not_moved_and_the_reason_is_shown(self):
        _host, editor = self._editor()
        editor._set_advanced(True)
        editor.properties.position_spin.setValue(60.0)  # far from anything analysis would pick
        editor.properties.flush()
        editor.properties.position_header.lock.click()
        editor._recommend()
        stored = editor._overrides[pair_key("a", "b")]
        self.assertTrue(stored.recommend)
        self.assertAlmostEqual(stored.outgoing_cue, 60.0)
        self.assertAlmostEqual(editor.junction.outgoing.source_at(editor.junction.start), 60.0, places=3)
        self.assertIn("추천이 없어", editor.properties.note_label.text())
        self.assertFalse(editor.properties.note_label.isHidden())

    def test_changed_items_are_marked_and_reset_one_at_a_time(self):
        host, editor = self._editor()
        editor._set_advanced(True)
        editor.properties.length_presets[4.0].click()
        editor.properties.style_buttons["short_fade"].click()
        headers = editor.properties.headers
        self.assertFalse(headers["duration"].mark.isHidden())
        self.assertFalse(headers["style"].mark.isHidden())
        self.assertTrue(headers["incoming_cue"].mark.isHidden())
        headers["duration"].reset.click()
        automatic = editor._automatic_junction()
        self.assertAlmostEqual(editor.junction.end - editor.junction.start, automatic.end - automatic.start, places=6)
        self.assertEqual(editor._overrides[pair_key("a", "b")].style, "short_fade")  # the style stays
        headers["style"].reset.click()
        self.assertNotIn(pair_key("a", "b"), editor._overrides)  # nothing differs any more: automatic

    def test_simple_mode_says_which_advanced_settings_are_in_force(self):
        _host, editor = self._editor()
        editor._set_advanced(True)
        editor.properties.incoming_spin.setValue(12.0)
        editor.properties.flush()
        editor._set_advanced(False)
        summary = editor.properties.summary_label
        self.assertFalse(summary.isHidden())
        self.assertIn("B 시작", summary.text())
        self.assertAlmostEqual(editor._overrides[pair_key("a", "b")].incoming_cue, 12.0)

    def test_paste_including_cues_is_a_separate_choice(self):
        _host, editor = self._editor()
        editor._set_advanced(True)
        editor.properties.incoming_spin.setValue(9.0)
        editor.properties.flush()
        editor._copy()
        editor._user_select(1)
        editor._paste()
        self.assertNotEqual(editor._overrides[pair_key("b", "c")].incoming_cue, 9.0)
        editor._paste(cues=True)
        self.assertEqual(editor._overrides[pair_key("b", "c")].incoming_cue, 9.0)

    def test_what_is_heard_says_when_it_is_an_older_edit(self):
        from pathlib import Path

        _host, editor = self._editor()
        editor.audition._executable = Path("ffmpeg.exe")
        editor.audition._start = Mock()  # never renders here
        editor._on_audition_state("ready", "")
        editor._on_audio_ready("window.flac", 100.0, 100.0, 130.0)
        self.assertIn("최신 편집 반영됨", editor.state_label.text())
        editor.properties.length_presets[4.0].click()
        self.assertEqual(editor.audition.state, "waiting")
        self.assertIn("갱신 필요", editor.state_label.text())
        self.assertTrue(editor.timeline.audition_stale)
        editor._on_audition_state("failed", "boom")
        self.assertIn("렌더 실패", editor.state_label.text())
        self.assertIn("이전 편집", editor.state_label.text())

    def test_the_editor_plans_exactly_what_preview_and_export_will(self):
        from app.automix.planner import compile_automix
        from app.automix.settings import automix_settings_for

        host = self._storing_host()
        _host, editor = self._editor(host)
        editor._set_advanced(True)
        editor.properties.style_buttons["bass_swap"].click()
        editor._bands_changed((((0.1, 0.3), (0.2, 0.4)),) * 3)
        editor._user_select(1)
        editor.properties.length_header.lock.click()
        exported = compile_automix(host.playlist_service.tracks, host.automix_analyses,
                                   automix_settings_for(host.project_settings), {}, log_diagnostics=False)
        self.assertEqual(exported, editor._plan)
        self.assertEqual(editor.audition.render_count, 0)  # and nothing rendered the playlist

    def test_the_workspace_is_remembered_and_can_go_back_to_the_default(self):
        if not QApplication.organizationName():
            QApplication.setOrganizationName("Music Motion Studio Tests")
            self.addCleanup(QApplication.setOrganizationName, "")
        for key in ("splitter", "properties_visible", "lanes_visible", "snap"):
            self.addCleanup(QSettings().remove, f"automix_editor/{key}")
        _host, editor = self._editor()
        editor.properties_button.setChecked(False)
        editor.lanes_button.setChecked(False)
        editor.snap_combo.setCurrentIndex(editor.snap_combo.findData("bar"))
        editor.close()
        _host, again = self._editor()
        self.assertFalse(again.properties_button.isChecked())
        self.assertFalse(again.lanes_button.isChecked())
        self.assertEqual(again.timeline.snap_unit, "bar")
        again._reset_workspace()
        self.assertTrue(again.properties_button.isChecked())
        self.assertTrue(again.lanes_button.isChecked())
        self.assertEqual(again.timeline.snap_unit, "beat")

    def test_snap_choices_follow_what_the_analysis_can_be_trusted_for(self):
        from tests.test_automix_planner import _analysis

        host = self._host()
        host.automix_analyses = {track.id: _analysis(track.id, 120.0, track.duration_seconds, meter_confidence=0.1)
                                 for track in host.playlist_service.tracks}
        _host, editor = self._editor(host)
        model = editor.snap_combo.model()
        enabled = {editor.snap_combo.itemData(row): model.item(row).isEnabled() for row in range(3)}
        self.assertEqual(enabled, {"off": True, "beat": True, "bar": False})  # beats, but no trusted bars
        self.assertEqual(editor.properties.bars_spin.isHidden(), True)

    def _studio_style(self):
        """The app's own font and stylesheet (what the sizes below are about), put back afterwards."""
        from app.ui.design_system import apply_studio_style

        app = self.app
        before = (app.style().name(), app.font(), app.palette(), app.styleSheet(),
                  app.property("playlistCanvasStudioStyle"))

        def restore():
            name, font, palette, sheet, flag = before
            app.setStyleSheet(sheet)
            app.setFont(font)
            app.setPalette(palette)
            app.setStyle(name)
            app.setProperty("playlistCanvasStudioStyle", flag)
        self.addCleanup(restore)
        app.setProperty("playlistCanvasStudioStyle", False)
        apply_studio_style(app)

    def test_every_core_control_fits_the_smallest_window_in_both_languages(self):
        from dataclasses import replace

        self._studio_style()
        host = self._host()
        host.playlist_service.tracks[0] = replace(
            host.playlist_service.tracks[0], title="아주 긴 곡 제목 " * 8 + "(Extended Club Remix)")
        for language in ("ko", "en"):
            host.translator.set_language(language)
            _host, editor = self._editor(host)
            editor.show()
            editor.resize(editor.minimumSize())
            self.app.processEvents()
            self.assertLessEqual(editor.minimumSizeHint().width(), 760)
            window = editor.rect()
            for control in (editor.previous_button, editor.next_button, editor.transition_combo,
                            editor.simple_button, editor.advanced_button, editor.more_button, editor.undo_button,
                            editor.redo_button, editor.snap_combo, editor.fit_button, editor.play_button,
                            editor.loop_button, editor.compare_button, editor.properties_button):
                self.assertTrue(control.isVisible(), control)
                corner = control.mapTo(editor, control.rect().bottomRight())
                self.assertTrue(window.contains(corner), (language, control.text() if hasattr(control, "text") else control))
            for selection in ("transition", "outgoing", "tempo", "band:low"):  # no side panel page overflows
                editor._set_advanced(True)
                editor.timeline._select(selection)
                self.assertLessEqual(editor.properties.minimumSizeHint().width(),
                                     editor.properties_scroll.viewport().width() + 1, (language, selection))
            editor.close()
        self.assertEqual(editor.snap_label.text(), "Snap")

    def test_keyboard_reaches_the_timeline_and_moves_the_selection(self):
        host, editor = self._editor()
        editor._set_advanced(True)
        editor.timeline.setFocus()
        QTest.keyClick(editor.timeline, Qt.Key.Key_Down)  # move
        QTest.keyClick(editor.timeline, Qt.Key.Key_Down)  # start
        QTest.keyClick(editor.timeline, Qt.Key.Key_Down)  # end
        QTest.keyClick(editor.timeline, Qt.Key.Key_Down)  # A cue
        self.assertEqual(editor.timeline.selection, "outgoing")
        self.assertIs(editor.properties.pages.currentWidget(), editor.properties.page_widgets["outgoing"])
        before = editor._base().outgoing_cue
        QTest.keyClick(editor.timeline, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
        self.assertAlmostEqual(host._set_automix_override.call_args.args[1].outgoing_cue, before - 0.01, places=4)
        self.assertEqual(editor.undo_stack.count(), 1)


if __name__ == "__main__":
    unittest.main()
