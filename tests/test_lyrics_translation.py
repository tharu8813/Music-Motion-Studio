"""Translation contracts, review isolation, persistence and shared rendering."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPlainTextEdit, QPushButton

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.dialogs.lyrics_translation_style_dialog import LyricsTranslationStyleDialog
from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.services.lyrics_translation_service import (
    LyricsTranslationService, TranslationCancelled, TranslationConfig, TranslationError,
    apply_translations, approved_translation, display_cue, request_json, validate_endpoint,
)
from app.services.translation_credentials import TranslationCredentials, _SESSION, _crypt
from app.utils.i18n import Translator


class TranslationServiceTests(unittest.TestCase):
    def setUp(self):
        self.cues = [{"start": 0, "end": 2, "text": "Hello\nworld", "words": [
            {"start": 0, "end": 1, "text": "Hello", "offset": 0}]},
            {"start": 2, "end": 4, "text": "Again"}]
        self.config = TranslationConfig("openai", key="secret", model="test-model")

    def test_reviewed_translation_roundtrip_is_separate_from_original_and_word_offsets(self):
        before = deepcopy(self.cues)
        translated = apply_translations(self.cues, ["안녕\n세상", None], self.config)
        self.assertEqual(self.cues, before)
        self.assertEqual(translated[0]["text"], before[0]["text"])
        self.assertEqual(translated[0]["words"], before[0]["words"])
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=4, lyrics=translated)
        project = ProjectDocument(playlist=[track])
        restored = ProjectDocument.from_dict(json.loads(json.dumps(project.to_dict())))
        self.assertEqual(approved_translation(restored.playlist[0].lyrics[0]), "안녕\n세상")
        self.assertEqual(display_cue(translated[0])["display_line_positions"], (0, 1, -1, -1))
        self.assertNotIn("secret", json.dumps(project.to_dict()))

    def test_stale_or_unapproved_translation_is_hidden(self):
        cue = apply_translations(self.cues, ["안녕", None], self.config)[0]
        cue["text"] = "Changed"
        self.assertEqual(approved_translation(cue), "")
        cue["translation"]["source_text"] = "Changed"
        cue["translation"]["approved"] = False
        self.assertEqual(approved_translation(cue), "")

    def test_unchecked_preserves_previous_translation_checked_empty_removes(self):
        cues = apply_translations(self.cues, ["안녕", "다시"], self.config)
        changed = apply_translations(cues, [None, ""], self.config)
        self.assertEqual(changed[0], cues[0])
        self.assertNotIn("translation", changed[1])



    def test_same_explicit_language_skips_requests_and_auth_requirements(self):
        service = LyricsTranslationService(lambda *args, **kwargs: self.fail("Must not request"))
        for provider in ("openai", "local_nmt", "chatgpt", "ollama"):
            with self.subTest(provider=provider):
                progress = []
                self.assertEqual(service.translate(self.cues, TranslationConfig(provider, source="EN", target="en"),
                    progress=lambda value, total: progress.append((value, total))), ["", ""])
                self.assertEqual(progress, [(2, 2)])

    def test_regional_variants_are_not_mistaken_for_identical_languages(self):
        for source, target in (("en-GB", "en-US"), ("zh-Hans", "zh-Hant")):
            calls = []
            def transport(*args, **kwargs):
                calls.append(args)
                return json.dumps({"translations": [{"id": 0, "text": "changed variant"}]})
            result = LyricsTranslationService(transport).translate([{"text": "original variant"}],
                replace(self.config, source=source, target=target))
            self.assertEqual(result, ["changed variant"])
            self.assertEqual(len(calls), 1)

    def test_duplicate_review_text_is_not_stored_and_existing_duplicate_is_hidden(self):
        cues = apply_translations(self.cues, ["안녕", None], self.config)
        changed = apply_translations(cues, ["  Hello\r\nworld  ", None], self.config)
        self.assertNotIn("translation", changed[0])
        self.assertEqual(cues[0]["translation"]["text"], "안녕")
        legacy = deepcopy(cues[0])
        legacy["translation"]["text"] = "Hello\\nworld"
        self.assertEqual(approved_translation(legacy), "")
        self.assertEqual(display_cue(legacy)["text"], legacy["text"])
        unicode_cues = [{"text": "Café"}]
        self.assertNotIn("translation", apply_translations(unicode_cues, ["Cafe\u0301"], self.config)[0])


    def test_invalid_project_translation_rejected(self):
        track = PlaylistTrack("", "Song", lyrics=deepcopy(self.cues))
        track.lyrics[0]["translation"] = {"text": "Oops"}
        with self.assertRaises(ValueError):
            PlaylistTrack.from_dict(track.to_dict())

    def test_literal_lrc_line_breaks_keep_translation_after_project_reload(self):
        cues = [{"start": 0, "end": 4, "text": "Hello\\nworld"}]
        translated = apply_translations(cues, ["안녕"], self.config)
        restored = PlaylistTrack.from_dict(PlaylistTrack("", "Song", lyrics=translated).to_dict())
        self.assertEqual(approved_translation(restored.lyrics[0]), "안녕")

    def test_papago_display_reserves_attribution_marker(self):
        cues = apply_translations(self.cues, ["안녕", None], replace(self.config, provider="papago"))
        self.assertEqual(display_cue(cues[0])["display_line_positions"], (0, 1, -2))



    def test_llm_response_ids_restore_order_for_every_adapter(self):
        raw = json.dumps({"translations": [{"id": 1, "text": "다시"}, {"id": 2, "text": ""},
            {"id": 0, "text": "안녕\n세상"}]})
        for provider in ("chatgpt", "openai", "claude", "gemini", "grok", "ollama", "lmstudio"):
            with self.subTest(provider=provider):
                def transport(url, body=None, headers=None, **kwargs):
                    if provider in {"chatgpt", "openai"}:
                        self.assertIs(body["stream"], True)
                        self.assertIs(body["store"], False)
                        return raw
                    if provider == "claude":
                        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": raw}]}
                    if provider == "gemini":
                        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": raw}]}}]}
                    return {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}
                config = replace(self.config, provider=provider, model="chosen-model", endpoint="http://localhost:1234/v1")
                self.assertEqual(LyricsTranslationService(transport).translate(
                    self.cues + [{"text": "이미 한국어"}], config), ["안녕\n세상", "다시", ""])

    def test_missing_duplicate_or_boolean_llm_ids_fail(self):
        for rows in ([{"id": 0, "text": "a"}], [{"id": 0, "text": "a"}, {"id": 0, "text": "b"}],
                     [{"id": False, "text": "a"}, {"id": 1, "text": "b"}]):
            service = LyricsTranslationService(lambda *a, **kw: json.dumps({"translations": rows}))
            with self.assertRaises(TranslationError):
                service.translate(self.cues, replace(self.config, provider="chatgpt", model="chosen"))

    def test_cancel_and_provider_count_mismatch_never_apply_partial_results(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(TranslationCancelled):
            LyricsTranslationService().translate(self.cues, self.config, cancel)
        with self.assertRaises(TranslationError):
            LyricsTranslationService(lambda *a, **kw: {"data": {"translations": []}}).translate(self.cues, self.config)

    def test_batches_keep_global_ids_and_skip_blank_cues(self):
        cues = [{"text": str(index)} for index in range(43)] + [{"text": ""}]
        batches = []
        def transport(url, body=None, headers=None, **kwargs):
            rows = json.loads(body["input"][0]["content"])["lyrics"]
            batches.append(rows)
            return json.dumps({"translations": [{"id": r["id"], "text": "T" + r["text"]} for r in rows]})
        result = LyricsTranslationService(transport).translate(cues, replace(self.config, provider="chatgpt", model="chosen"))
        self.assertEqual([len(rows) for rows in batches], [20, 20, 3])
        self.assertEqual(result[42:], ["T42", ""])

    def test_custom_endpoint_rejects_unprotected_remote_credentials_and_userinfo(self):
        for url in ("http://remote.example/v1", "https://user:password@host/v1", "file:///etc/passwd", "https://host/v1?key=secret"):
            with self.assertRaises(TranslationError):
                validate_endpoint(url)

    def test_invalid_later_cue_is_rejected_before_any_request(self):
        calls = []
        service = LyricsTranslationService(lambda *args, **kwargs: calls.append(args))
        with self.assertRaisesRegex(TranslationError, "row 22"):
            service.translate([{"text": "hello"}] * 21 + [{"text": "x" * 4001}], self.config)
        self.assertEqual(calls, [])

    def test_empty_input_and_invalid_language_codes_do_not_request(self):
        service = LyricsTranslationService(lambda *args, **kwargs: self.fail("Must not request"))
        for cues in ([], [{"text": " \n "}]):
            with self.subTest(cues=cues), self.assertRaises(TranslationError):
                service.translate(cues, self.config)
        for source, target in (("", "ko"), ("auto", "auto"), ("auto", "한국어"), ("auto", "en?x")):
            with self.subTest(source=source, target=target), self.assertRaises(TranslationError):
                service.translate(self.cues, replace(self.config, source=source, target=target))
        for target in ("ko", "zh-Hant", "pt-BR", "en-GB"):
            replace(self.config, target=target).validate()

    def test_malformed_endpoint_is_a_safe_validation_error(self):
        for endpoint in ("https://[bad", "http://localhost:bad/v1", "http://localhost:99999/v1",
                "https://host/a b", "https://host/\npath"):
            with self.subTest(endpoint=endpoint), self.assertRaises(TranslationError):
                validate_endpoint(endpoint)


    def test_progress_counts_translatable_rows_and_finishes_with_trailing_blanks(self):
        progress = []
        service = LyricsTranslationService(lambda *args, **kwargs:
            json.dumps({"translations": [{"id": 1, "text": "안녕"}]}))
        result = service.translate([{"text": ""}, {"text": "Hello"}, {"text": "  "}], self.config,
            progress=lambda value, total: progress.append((value, total)))
        self.assertEqual(result, ["", "안녕", ""])
        self.assertEqual(progress, [(0, 1), (1, 1)])

    def test_oversized_review_edit_returns_actionable_error(self):
        with self.assertRaises(TranslationError):
            apply_translations(self.cues, ["x" * 64001, None], self.config)

    def test_session_credentials_are_isolated_by_settings_store(self):
        with TemporaryDirectory() as directory:
            first = TranslationCredentials(QSettings(str(Path(directory) / "first.ini"), QSettings.Format.IniFormat))
            second = TranslationCredentials(QSettings(str(Path(directory) / "second.ini"), QSettings.Format.IniFormat))
            first.save("google", {"key": "first-key"})
            self.assertEqual(second.load("google"), {})
            second.save("google", {"key": "second-key"})
            first.clear("google")
            self.assertEqual(second.load("google"), {"key": "second-key"})

    def test_sse_must_complete_and_http_error_does_not_echo_key(self):
        class Response:
            def __init__(self, events): self.events = events
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self): return iter(self.events)
        def event(value): return b"data: " + json.dumps(value).encode() + b"\n"
        opener = unittest.mock.Mock()
        opener.open.return_value = Response([event({"type": "response.output_text.delta", "delta": "ok"}),
                                             event({"type": "response.completed"})])
        with patch("app.services.lyrics_translation_service.build_opener", return_value=opener):
            self.assertEqual(request_json("https://api.openai.com/v1/responses", {}, stream=True), "ok")
            opener.open.return_value = Response([event({"type": "response.output_text.delta", "delta": "partial"})])
            with self.assertRaises(TranslationError):
                request_json("https://api.openai.com/v1/responses", {}, stream=True)

            opener.open.return_value = Response([event({"type": "response.failed", "response": {
                "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "sensitive"}}})])
            with self.assertRaisesRegex(TranslationError, "sharing settings") as error:
                request_json("https://api.openai.com/v1/responses", {}, stream=True)
            self.assertNotIn("sensitive", str(error.exception))

    def test_sse_completion_is_terminal_and_malformed_events_are_safe(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __iter__(self):
                yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n'
                yield b'data: {"type":"response.completed"}\n'
                raise AssertionError("Do not wait for EOF after terminal completion")
        opener = unittest.mock.Mock()
        opener.open.return_value = Response()
        with patch("app.services.lyrics_translation_service.build_opener", return_value=opener):
            self.assertEqual(request_json("https://example.com", stream=True), "ok")


class TranslationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "test.ini"), QSettings.Format.IniFormat)
        self.cues = [{"start": 0, "end": 4, "text": "Hello"}]
        self.translator = Translator()
        _SESSION.clear()

    def dialog(self):
        dialog = LyricsTranslationDialog(self.cues, self.translator, settings=self.settings,
            service=LyricsTranslationService(lambda *a, **kw: json.dumps({"translations": [{"id": 0, "text": "안녕"}]})))
        self.addCleanup(dialog.deleteLater)
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("openai"))
        dialog.key_edit.setText("test-key")
        dialog.model_combo.setEditText("test-model")
        return dialog

    def wait(self, dialog):
        deadline = time.monotonic() + 3
        while dialog.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(dialog.worker)

    def test_draft_requires_explicit_apply_and_cancel_preserves_original(self):
        dialog = self.dialog()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog._translate()
        self.wait(dialog)
        self.assertTrue(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.selected_cues, self.cues)
        self.assertEqual(dialog.table.item(0, 3).text(), "안녕")
        dialog.reject()
        self.assertTrue(self.settings.contains(dialog.draft.key))
        self.assertEqual(dialog.selected_cues, self.cues)
        self.assertEqual(self.cues[0]["text"], "Hello")

    def test_review_edits_and_selection_are_applied(self):
        dialog = self.dialog()
        dialog._translate()
        self.wait(dialog)
        dialog.table.item(0, 3).setText("안녕하세요")
        dialog._apply()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(approved_translation(dialog.selected_cues[0]), "안녕하세요")
        self.assertNotIn("translation", self.cues[0])
        self.assertNotIn("test-key", Path(self.settings.fileName()).read_text(encoding="utf-8"))

    def test_unnecessary_result_stays_blank_and_removes_old_translation_only_after_apply(self):
        self.cues = apply_translations(self.cues, ["이전 번역"], TranslationConfig("google"))
        dialog = self.dialog()
        dialog.workflow.service = LyricsTranslationService(lambda *args, **kwargs:
            json.dumps({"translations": [{"id": 0, "text": "Hello"}]}))
        with patch("app.services.translation_workflow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            dialog._translate()
        self.wait(dialog)
        self.assertEqual(dialog.table.item(0, 3).text(), "")
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertTrue(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.selected_cues, self.cues)
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            dialog._apply()
        self.assertNotIn("translation", dialog.selected_cues[0])
        self.assertEqual(self.cues[0]["translation"]["text"], "이전 번역")

    def test_identical_chatgpt_language_pair_does_not_request_access_token(self):
        dialog = self.dialog()
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("chatgpt"))
        dialog.source_combo.setCurrentIndex(dialog.source_combo.findData("ko"))
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData("ko"))
        with patch.object(dialog.auth, "access_token", side_effect=AssertionError("Must not authenticate")):
            dialog._translate()
            self.wait(dialog)
        self.assertEqual(dialog.table.item(0, 3).text(), "")
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertTrue(dialog.apply_button.isEnabled())

    def test_empty_lyrics_disable_translation_and_cannot_be_selected(self):
        self.cues = [{"start": 0, "end": 4, "text": "   "}]
        dialog = self.dialog()
        self.assertFalse(dialog.translate_button.isEnabled())
        dialog._check_all(True)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)

    def test_dialog_opens_with_all_nonblank_lyrics_selected(self):
        self.cues += [{"start": 4, "end": 8, "text": "Next"},
                      {"start": 8, "end": 9, "text": "   "}]
        dialog = self.dialog()
        self.assertEqual([dialog.table.item(row, 0).checkState() for row in range(3)],
            [Qt.CheckState.Checked, Qt.CheckState.Checked, Qt.CheckState.Unchecked])
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIn("2", dialog.selection_label.text())
        self.assertIn("0", dialog.review_summary.text().split("·")[-1])

    def test_select_all_button_checks_untranslated_rows_and_is_idempotent(self):
        self.cues += [{"start": 4, "end": 8, "text": "Next"}]
        dialog = self.dialog()
        select_all = next(button for button in dialog.findChildren(QPushButton)
            if button.text() == dialog.tr_text("전체 선택", "Select all"))
        clear_selection = next(button for button in dialog.findChildren(QPushButton)
            if button.text() == dialog.tr_text("전체 해제", "Clear selection"))
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        for _ in range(2):
            select_all.click()
            self.assertTrue(all(dialog.table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(2)))
        clear_selection.click()
        self.assertTrue(all(dialog.table.item(row, 0).checkState() == Qt.CheckState.Unchecked
            for row in range(2)))
        select_all.click()
        self.assertTrue(all(dialog.table.item(row, 0).checkState() == Qt.CheckState.Checked
            for row in range(2)))

    def test_named_and_custom_languages_keep_api_codes(self):
        dialog = self.dialog()
        self.assertEqual(dialog._config().target, "ko")
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData("ja"))
        self.assertEqual(dialog._config().target, "ja")
        dialog.target_combo.setEditText("en-GB")
        dialog._save_settings()
        self.assertEqual(self.settings.value("translation/languages/target"), "en-GB")
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("claude"))
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("openai"))
        self.assertEqual(dialog._config().target, "en-GB")

    def test_direct_edits_auto_select_row_and_allow_manual_review(self):
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("직접 번역")
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog._apply()
        self.assertEqual(approved_translation(dialog.selected_cues[0]), "직접 번역")
        self.assertEqual(dialog.selected_cues[0]["translation"]["provider"], "manual")

    def test_apply_commits_active_multiline_cell_editor(self):
        dialog = self.dialog()
        dialog.show()
        dialog.review_panel.show()
        self.app.processEvents()
        dialog.table.editItem(dialog.table.item(0, 3))
        self.app.processEvents()
        editor = next(editor for editor in dialog.table.findChildren(QPlainTextEdit) if editor.isVisible())
        editor.setPlainText("아직 편집 중인 번역\n둘째 줄")
        dialog._apply()
        self.assertEqual(approved_translation(dialog.selected_cues[0]), "아직 편집 중인 번역\n둘째 줄")

    def test_review_deletion_and_empty_new_rows_have_clear_selection(self):
        self.cues = apply_translations(self.cues, ["안녕"], TranslationConfig("google")) + [
            {"start": 4, "end": 8, "text": "Next"}]
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("")
        dialog._check_all(True)
        self.assertEqual(dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Checked)
        self.assertIn("1", dialog.review_summary.text().split("·")[-1])
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            dialog._apply()
        self.assertNotIn("translation", dialog.selected_cues[0])
        self.assertEqual(dialog.selected_cues[1], self.cues[1])

    def test_return_in_settings_does_not_default_to_translation(self):
        dialog = self.dialog()
        self.assertTrue(all(not button.autoDefault() for button in dialog.findChildren(QPushButton)))

    def test_oversized_editor_input_keeps_dialog_open(self):
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("x" * 64001)
        dialog._apply()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.selected_cues, self.cues)
        self.assertIn("too long", dialog.status_label.text())

    def test_style_preview_updates_and_mode_values_are_preserved(self):
        source = Source(SourceType.LYRICS, "Lyrics", font_size=32,
            subtitle_translation_style={"font_size_offset": -6, "color": "#00FF00", "font_weight": 500})
        dialog = LyricsTranslationStyleDialog(source, self.translator)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.preview.font().pixelSize(), 26)
        dialog.size_mode.setCurrentIndex(1)
        dialog.size.setValue(50)
        self.assertEqual(dialog.preview.font().pixelSize(), 50)
        dialog.size_mode.setCurrentIndex(0)
        self.assertEqual(dialog.size.value(), -6)
        dialog.size_mode.setCurrentIndex(1)
        self.assertEqual(dialog.size.value(), 50)
        dialog._reset()
        self.assertEqual(dialog.preview.font().pixelSize(), 28)
        self.assertEqual(source.subtitle_translation_style["font_size_offset"], -6)

    def test_editing_saved_mixed_language_rows_keeps_each_rows_language(self):
        first = apply_translations(self.cues, ["안녕"], TranslationConfig("google", target="ko"))
        second = apply_translations([{**self.cues[0], "start": 4, "end": 8}], ["こんにちは"],
            TranslationConfig("deepl", target="ja"))
        self.cues = first + second
        dialog = self.dialog()
        dialog.table.item(1, 3).setText("やあ")
        dialog.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        dialog._apply()
        self.assertEqual(dialog.selected_cues[1]["translation"]["language"], "ja")
        self.assertEqual(dialog.selected_cues[1]["translation"]["provider"], "deepl")
        self.assertEqual(dialog.selected_cues[0], first[0])

    def test_style_and_visibility_are_independent_and_roundtrip(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_line_styles=[{}, {"color": "#FF0000"}])
        dialog = LyricsTranslationStyleDialog(source, self.translator)
        self.addCleanup(dialog.deleteLater)
        dialog.size_mode.setCurrentIndex(1)
        dialog.size.setValue(40)
        dialog.enabled.setChecked(False)
        saved = source.to_dict()
        saved.update(dialog.settings())
        restored = Source.from_dict(saved)
        self.assertEqual(restored.subtitle_translation_style["font_size"], 40)
        self.assertFalse(restored.subtitle_translation_enabled)
        self.assertEqual(restored.subtitle_line_styles, [{}, {"color": "#FF0000"}])
        self.assertTrue(source.subtitle_translation_enabled)

    def test_ttml_translations_do_not_take_over_word_highlighting(self):
        from app.preview.frame_state import lyrics_display_groups, resolve_lyrics_cue_state, resolve_lyrics_word_reveal
        cues = [{"start": 0, "end": 5, "text": "Long", "format": "ttml", "words": [
                    {"start": 0, "end": 5, "text": "Long", "offset": 0}]},
                {"start": 1, "end": 3, "text": "Short", "format": "ttml", "words": [
                    {"start": 1, "end": 3, "text": "Short", "offset": 0}]}]
        cues = apply_translations(cues, ["긴 가사", "짧은 가사"], TranslationConfig("google"))
        track = PlaylistTrack("", "Overlap", duration_seconds=6, lyrics=cues)
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_karaoke_enabled=True)
        state = resolve_lyrics_cue_state(track, 2, 0, 0, "none", .36, whole_lines=True)
        groups, index = lyrics_display_groups(track, state, translations=True)
        self.assertEqual(groups[index]["text"], "Long\n긴 가사\nShort\n짧은 가사")
        self.assertEqual(groups[index]["display_line_positions"], (0, -1, 0, -1))
        reveal = resolve_lyrics_word_reveal(track, source, 2, state, None)
        self.assertEqual(len(reveal), 4)
        self.assertAlmostEqual(reveal[0], 1.6)
        self.assertAlmostEqual(reveal[2], 2.5)
        self.assertEqual(track.lyrics[1]["words"][0]["offset"], 0)

    def test_typed_model_id_overrides_loaded_model_and_cancel_waits_for_worker(self):
        dialog = self.dialog()
        dialog.model_combo.addItem("Display name", "model-one")
        dialog.model_combo.setEditText("model-two")
        self.assertEqual(dialog._config().model, "model-two")
        def work(cancel, progress):
            cancel.wait(2)
            raise TranslationCancelled()
        dialog._start("translate", work)
        dialog.reject()
        self.wait(dialog)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.selected_cues, self.cues)

    def test_canvas_translation_style_is_not_original_second_line_style(self):
        cues = apply_translations([{**self.cues[0], "text": "Original\nSecond"}], ["번역문"], TranslationConfig("google"))
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=400, font_size=24,
            subtitle_animation="none", subtitle_context_lines=0, subtitle_next_lines=0,
            subtitle_intro_enabled=False, subtitle_line_styles=[{}, {"color": "#FF0000"}],
            subtitle_translation_style={"color": "#00FF00", "font_size": 18, "font_weight": 400})
        scene = CanvasScene()
        scene.set_artboard_size(600, 400)
        item = SourceItem(source)
        scene.addItem(item)
        recorded = {}
        original = item._draw_text
        def observe(painter, rect, flags, text):
            recorded[text] = painter.pen().color().name()
            return original(painter, rect, flags, text)
        track = PlaylistTrack("", "Track", duration_seconds=4, lyrics=cues)
        with patch.object(item, "_draw_text", side_effect=observe):
            frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=1, transparent=True)
        self.assertFalse(frame.isNull())
        self.assertEqual(recorded["Second"], "#ff0000")
        self.assertEqual(recorded["번역문"], "#00ff00")
        self.assertEqual(item._subtitle_line_style_indices, ())
        source.subtitle_translation_enabled = False
        recorded.clear()
        with patch.object(item, "_draw_text", side_effect=observe):
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=1, transparent=True)
        self.assertNotIn("번역문", recorded)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_persisted_credentials_are_windows_encrypted(self):
        credentials = TranslationCredentials(self.settings)
        credentials.save("google", {"key": "secret-value"}, remember=True)
        self.settings.sync()
        self.assertNotIn("secret-value", Path(self.settings.fileName()).read_text(encoding="utf-8"))
        _SESSION.clear()
        self.assertEqual(credentials.load("google")["key"], "secret-value")
        credentials.clear("google")
        self.assertEqual(credentials.load("google"), {})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_translation_settings_cannot_remove_persistent_chatgpt_session(self):
        dialog = self.dialog()
        dialog.credentials.save("chatgpt", {"access_token": "fake-access", "remember": True}, remember=True)
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("chatgpt"))
        self.assertTrue(dialog.remember_check.isHidden())
        dialog.remember_check.setChecked(False)
        dialog._save_settings()
        _SESSION.clear()
        self.assertTrue(dialog.credentials.load("chatgpt")["remember"])


if __name__ == "__main__":
    unittest.main()
