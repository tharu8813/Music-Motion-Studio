"""Unified translation/editing, separate settings, and resumable drafts."""
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTableWidget

from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.services.lyrics_translation_service import LyricsTranslationService, TranslationConfig, TranslationCancelled, TranslationError, apply_translations
from app.services.translation_connection import available_models
from app.utils.i18n import Translator


class TranslationWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("translation/provider", "openai")
        self.settings.setValue("translation/config/openai/model", "chosen-model")
        self.cues = [{"text": "First verse", "start": 1}, {"text": "Second verse", "start": 5},
                     {"text": "Final verse", "start": 9}]
        self.dialogs = []
        self.addCleanup(self.cleanup_windows)

    def cleanup_windows(self):
        for dialog in self.dialogs:
            if dialog.worker:
                dialog.workflow.cancel()
                self.until(lambda: dialog.worker is None)
            dialog.deleteLater()
        self.app.processEvents()

    def until(self, condition):
        end = time.monotonic() + 5
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def dialog(self, **kwargs):
        service = kwargs.pop("service", LyricsTranslationService(lambda url, body=None, headers=None, **kw:
            json.dumps({"translations": [{"id": row["id"], "text": "번역 " + str(row["id"])}
                        for row in json.loads(body["input"][0]["content"])["lyrics"]]})))
        dialog = LyricsTranslationDialog(self.cues, Translator(), settings=self.settings, service=service,
                                         draft_key="track-1", **kwargs)
        dialog.service_dialog.key_edit.setText("private-test-key")
        self.dialogs.append(dialog)
        return dialog

    def test_translation_and_review_share_one_window_and_settings_are_optional(self):
        dialog = self.dialog()
        dialog.show()
        self.app.processEvents()
        self.assertFalse(dialog.service_dialog.isVisible())
        self.assertTrue(dialog.review_panel.isVisible())
        self.assertFalse(dialog.review_panel.isWindow())
        self.assertIs(dialog.table.window(), dialog)
        self.assertFalse(hasattr(dialog, "review_button"))
        for widget in (dialog.key_edit, dialog.endpoint_edit, dialog.instructions_edit, dialog.model_combo):
            self.assertFalse(widget.isVisibleTo(dialog))
        dialog.translate_button.click()
        self.until(lambda: dialog.worker is None)
        self.assertFalse(dialog.service_dialog.isVisible())
        self.assertTrue(dialog.review_panel.isVisible())
        self.assertEqual(dialog.review_panel.table.item(2, 3).text(), "번역 2")

    def test_missing_credentials_guides_to_settings_then_keeps_languages(self):
        dialog = self.dialog()
        dialog.show()
        dialog.key_edit.clear()
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData("ja"))
        dialog.workflow.translate()
        self.assertIsNone(dialog.worker)
        self.assertTrue(dialog.service_dialog.isVisible())
        dialog.key_edit.setText("restored-key")
        dialog.service_dialog.accept()
        self.assertEqual(dialog._config().target, "ja")
        dialog.workflow.translate()
        self.until(lambda: dialog.worker is None)
        self.assertTrue(dialog.review_panel.isVisible())

    def test_saved_draft_is_bound_to_track_and_source_and_excludes_credentials(self):
        dialog = self.dialog()
        dialog.instructions_edit.setPlainText("Private instruction")
        dialog.workflow.translate()
        self.until(lambda: dialog.worker is None)
        dialog.table.item(1, 3).setText("다듬은 번역\n둘째 줄")
        dialog.table.item(2, 0).setCheckState(Qt.CheckState.Unchecked)
        dialog.review_panel.save_button.click()
        dialog.reject()
        restored = self.dialog()
        self.assertTrue(restored.draft.restored)
        self.assertEqual(restored.table.item(1, 3).text(), "다듬은 번역\n둘째 줄")
        self.assertEqual(restored.table.item(2, 0).checkState(), Qt.CheckState.Unchecked)
        stored = str(self.settings.value(dialog.draft.key))
        self.assertNotIn("private-test-key", stored)
        self.assertNotIn("Private instruction", stored)
        self.cues[0]["text"] = "Changed original"
        other = self.dialog()
        self.assertFalse(other.draft.restored)
        self.assertEqual(other.table.item(1, 3).text(), "")

    def test_selective_retry_keeps_global_ids_edits_selection_and_provenance(self):
        self.cues = apply_translations(self.cues, ["이전 0", "이전 1", "이전 2"],
                                      TranslationConfig("gemini", target="ja", model="old-model"))
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("내가 수정한 번역")
        dialog.table.item(2, 0).setCheckState(Qt.CheckState.Unchecked)
        from PySide6.QtWidgets import QMessageBox
        with patch("app.services.translation_workflow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            dialog.workflow.translate([1])
        self.until(lambda: dialog.worker is None)
        self.assertEqual([row.text for row in dialog.draft.rows], ["내가 수정한 번역", "번역 1", "이전 2"])
        self.assertEqual(dialog.draft.rows[0].config.provider, "gemini")
        self.assertEqual(dialog.draft.rows[1].config.provider, "openai")
        self.assertFalse(dialog.draft.rows[2].checked)
        dialog.review_panel._apply()
        self.assertEqual(dialog.selected_cues[0]["translation"]["language"], "ja")
        self.assertEqual(dialog.selected_cues[1]["translation"]["model"], "chosen-model")
        self.assertEqual(dialog.selected_cues[2], self.cues[2])
        self.assertTrue(self.settings.contains(dialog.draft.key))

    def test_service_settings_exposes_one_contextual_check_action_per_provider(self):
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = []
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog

        self.assertEqual(services._provider, "openai")
        self.assertTrue(services.models_button.isHidden())
        self.assertEqual(services.test_button.text(), "연결 확인")
        services.provider_combo.setCurrentIndex(services.provider_combo.findData("claude"))
        self.assertEqual(services.test_button.text(), "연결 확인")
        self.assertTrue(services.models_button.isHidden())
        services.provider_combo.setCurrentIndex(services.provider_combo.findData("chatgpt"))
        self.assertEqual(services.test_button.text(), "모델 목록 확인")
        self.assertTrue(services.models_button.isHidden())

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local_llm"))
        self.until(lambda: dialog.worker is None)
        self.assertEqual(services.test_button.text(), "설치 모델 새로고침")
        self.assertTrue(services.models_button.isHidden())
        self.assertFalse(services.llm_manager_button.isHidden())
        self.assertEqual(services.llm_manager_button.text(), "모델 관리·설치…")

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local"))
        self.assertEqual(services.test_button.text(), "언어 모델 확인")
        self.assertTrue(services.models_button.isHidden())
        self.assertTrue(services.llm_manager_button.isHidden())

    def test_service_contextual_check_button_preserves_existing_operations(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        with patch.object(services, "_start") as start:
            services.test_button.click()
            self.assertEqual(start.call_args.args[0], "connection")
        services.provider_combo.setCurrentIndex(services.provider_combo.findData("chatgpt"))
        with patch.object(services, "_load_models") as load_models:
            services.test_button.click()
            load_models.assert_called_once_with()
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local"))
        services.local_pair_note.setText("No model installed")
        with patch.object(services, "_refresh_local_models") as refresh:
            services.test_button.click()
            refresh.assert_called_once()
        self.assertEqual(services.status_label.text(), "No model installed")

    def test_service_readiness_hint_matches_editor_and_updates_after_key_changes(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        self.assertIn("연결 미확인", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())
        self.assertEqual(services.connection_hint.textFormat(), Qt.TextFormat.PlainText)

        services.record_connection(True, ["chosen-model"])
        dialog._refresh_summary()
        self.assertIn("연결·모델 확인됨", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

        services.key_edit.setText("rotated-test-credential")
        self.assertIn("연결 미확인", services.connection_hint.text())
        services.record_connection(False)
        dialog._refresh_summary()
        self.assertIn("연결 실패", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

    def test_service_readiness_refreshes_immediately_after_model_lookup(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        self.assertIn("연결 미확인", services.connection_hint.text())
        dialog.workflow.start("models", lambda cancel, progress: [
            ("chosen-model", "Chosen model")])
        self.until(lambda: dialog.worker is None)
        self.assertEqual(services.verified_connection(),
                         (True, frozenset({"chosen-model"})))
        self.assertIn("연결·모델 확인됨", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

    def test_service_readiness_shows_missing_local_llm_without_failed_connection(self):
        self.settings.setValue("translation/provider", "ollama")
        self.settings.setValue("translation/config/ollama/model", "missing:3b")
        service = Mock(provider="ollama", base="http://localhost:11434")
        service.discover.return_value = []
        dialog = self.dialog(local_llm_factory=lambda *args: service)
        self.until(lambda: dialog.worker is None and service.discover.called)
        services = dialog.service_dialog
        services.record_connection(True, [])
        dialog._refresh_summary()
        self.assertIn("모델 미설치", services.connection_hint.text())
        self.assertNotIn("연결 실패", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

    def test_service_readiness_matches_editor_during_model_management(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        with patch.object(services, "manager", create=True) as manager:
            manager.isVisible.return_value = True
            dialog._refresh_summary()
            self.assertIn("모델 관리를 마친 뒤", dialog.readiness_label.text())
            self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())
            self.assertFalse(dialog.translate_button.isEnabled())
            manager.isVisible.return_value = False
            dialog._refresh_summary()
            self.assertIn("연결 미확인", services.connection_hint.text())
            self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

    def test_service_readiness_matches_editor_without_editable_lyrics(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        with patch.object(dialog.review_panel, "_editable_rows", set()):
            dialog._refresh_summary()
            self.assertIn("번역할 가사가 없습니다", dialog.readiness_label.text())
            self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())
            self.assertFalse(dialog.translate_button.isEnabled())
        dialog._refresh_summary()
        self.assertIn("연결 미확인", services.connection_hint.text())
        self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())

    def test_empty_lyrics_state_has_priority_over_open_model_manager(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        with patch.object(services, "manager", create=True) as manager, \
                patch.object(dialog.review_panel, "_editable_rows", set()):
            manager.isVisible.return_value = True
            dialog._refresh_summary()
            self.assertIn("번역할 가사가 없습니다", dialog.readiness_label.text())
            self.assertEqual(services.connection_hint.text(), dialog.readiness_label.text())


    def test_switching_provider_clears_unrelated_connection_error(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        services.status_label.setText("Old provider connection failed")
        services.provider_combo.setCurrentIndex(services.provider_combo.findData("claude"))
        self.assertEqual(services._provider, "claude")
        self.assertEqual(services.status_label.text(), "")
        self.assertEqual(services.provider_combo.currentData(), "claude")

    def test_missing_saved_local_nmt_model_never_silently_selects_another(self):
        self.settings.setValue("translation/provider", "local_nmt")
        self.settings.setValue("translation/config/local_nmt/model", "removed-package")
        local_models = Mock()
        local_models.for_pair.return_value = [
            SimpleNamespace(id="available-package", name="English → Korean · available")]
        dialog = self.dialog(service=LyricsTranslationService(local_models=local_models))
        services = dialog.service_dialog
        self.assertEqual(services._provider, "local_nmt")
        self.assertEqual(services.model_combo.count(), 1)
        self.assertEqual(services.model_combo.currentIndex(), -1)
        self.assertEqual(services._model_value(), "")
        self.assertIn("전에 선택한", services.local_pair_note.text())
        self.assertIn("직접 선택", services.local_pair_note.text())

        services.model_combo.setCurrentIndex(0)
        self.assertEqual(services._model_value(), "available-package")

    def test_local_nmt_initial_setup_still_selects_only_available_model(self):
        self.settings.setValue("translation/provider", "local_nmt")
        local_models = Mock()
        local_models.for_pair.return_value = [
            SimpleNamespace(id="only-package", name="English → Korean · installed")]
        dialog = self.dialog(service=LyricsTranslationService(local_models=local_models))
        self.assertEqual(dialog.service_dialog._model_value(), "only-package")
        self.assertIn("모델 파일 확인됨", dialog.service_dialog.local_pair_note.text())

    def test_local_nmt_language_change_requires_explicit_different_model(self):
        self.settings.setValue("translation/provider", "local_nmt")
        self.settings.setValue("translation/config/local_nmt/model", "en-ko-package")
        local_models = Mock()
        def installed_for_pair(source, target):
            if (source, target) == ("en", "ko"):
                return [SimpleNamespace(id="en-ko-package", name="English → Korean")]
            if (source, target) == ("en", "ja"):
                return [SimpleNamespace(id="en-ja-package", name="English → Japanese")]
            return []
        local_models.for_pair.side_effect = installed_for_pair
        dialog = self.dialog(service=LyricsTranslationService(local_models=local_models))
        services = dialog.service_dialog
        self.assertEqual(services._model_value(), "en-ko-package")
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData("ja"))
        self.assertEqual(services.model_combo.count(), 1)
        self.assertEqual(services.model_combo.currentIndex(), -1)
        self.assertEqual(services._model_value(), "")
        self.assertIn("직접 선택", services.local_pair_note.text())
        services.model_combo.setCurrentIndex(0)
        self.assertEqual(services._model_value(), "en-ja-package")

    def test_provider_switch_save_failure_restores_selection_and_preserves_values(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        self.assertEqual(services._provider, "openai")
        self.assertEqual(services.provider_combo.currentData(), "openai")
        services.instructions_edit.setPlainText("Keep the original instructions")
        previous_key = services.key_edit.text()
        with patch.object(services, "_save_settings",
                          side_effect=TranslationError("Settings directory is read-only")):
            services.provider_combo.setCurrentIndex(services.provider_combo.findData("claude"))
        self.assertEqual(services._provider, "openai")
        self.assertEqual(services.provider_combo.currentData(), "openai")
        self.assertEqual(services.instructions_edit.toPlainText(), "Keep the original instructions")
        self.assertEqual(services.key_edit.text(), previous_key)
        self.assertEqual(self.settings.value("translation/provider"), "openai")
        self.assertIn("read-only", services.status_label.text())
        # Retry works after the filesystem permission issue has been fixed.
        services.provider_combo.setCurrentIndex(services.provider_combo.findData("claude"))
        self.assertEqual(services._provider, "claude")
        self.assertEqual(services.provider_combo.currentData(), "claude")

    def test_mode_switch_credentials_save_failure_restores_mode_without_rebuilding_form(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        original_provider = services._provider
        original_model = services._model_value()
        original_key = services.key_edit.text()
        self.assertEqual(dialog.mode_combo.currentData(), "cloud_llm")
        with patch.object(services.credentials, "save",
                          side_effect=TranslationError("Credential store denied access")):
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local"))
        self.assertEqual(dialog.mode_combo.currentData(), "cloud_llm")
        self.assertEqual(services._provider, original_provider)
        self.assertEqual(services.provider_combo.currentData(), original_provider)
        self.assertEqual(services._model_value(), original_model)
        self.assertEqual(services.key_edit.text(), original_key)
        self.assertIn("Credential store denied access", services.status_label.text())
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local"))
        self.assertEqual(dialog.mode_combo.currentData(), "local")
        self.assertEqual(services._provider, "local_nmt")


    def test_model_probe_discards_result_when_credentials_change_mid_request(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        services.record_connection(True, ["chosen-model"])
        previous_connection = services.connection
        selected_model = services._model_value()
        entered, release = Event(), Event()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            return [("stale-model", "Stale model")]

        dialog.workflow.start("models", work)
        self.until(entered.is_set)
        services.key_edit.setText("a-new-api-key")
        release.set()
        self.until(lambda: dialog.worker is None)

        self.assertEqual(services._model_value(), selected_model)
        self.assertNotIn("stale-model", [
            services.model_combo.itemData(i) for i in range(services.model_combo.count())])
        self.assertEqual(services.connection, previous_connection)
        self.assertIsNone(services.verified_connection())
        self.assertIn("변경되어", dialog.workflow.status)

    def test_failed_probe_cannot_mark_changed_endpoint_disconnected(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        services.record_connection(True, ["chosen-model"])
        previous_connection = services.connection
        entered, release = Event(), Event()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            raise TranslationError("Old endpoint refused connection")

        dialog.workflow.start("connection", work)
        self.until(entered.is_set)
        services.endpoint_edit.setText("https://new-endpoint.example/v1")
        release.set()
        self.until(lambda: dialog.worker is None)

        self.assertEqual(services.connection, previous_connection)
        self.assertIsNone(services.verified_connection())
        self.assertIn("변경되어", dialog.workflow.status)
        self.assertNotIn("Old endpoint refused connection", dialog.workflow.status)

    def test_cancelled_model_probe_never_marks_connection_verified(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        entered, release = Event(), Event()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            return [("should-not-appear", "Cancelled result")]

        dialog.workflow.start("models", work)
        self.until(entered.is_set)
        dialog.workflow.cancel()
        release.set()
        self.until(lambda: dialog.worker is None)
        self.assertIsNone(services.connection)
        self.assertNotIn("should-not-appear", [
            services.model_combo.itemData(i) for i in range(services.model_combo.count())])

    def test_current_model_probe_still_updates_model_list_and_connection(self):
        dialog = self.dialog()
        services = dialog.service_dialog
        dialog.workflow.start("models", lambda cancel, progress: [("fresh-model", "Fresh model")])
        self.until(lambda: dialog.worker is None)

        self.assertIn("fresh-model", [
            services.model_combo.itemData(i) for i in range(services.model_combo.count())])
        self.assertEqual(services.verified_connection(), (True, frozenset({"fresh-model"})))


    def test_settings_close_during_translation_keeps_live_editor_and_main_close_cancels(self):
        entered = Event()
        def transport(*args, cancel=None, **kwargs):
            entered.set()
            cancel.wait(4)
            raise TranslationCancelled()
        dialog = self.dialog(service=LyricsTranslationService(transport))
        dialog.show()
        dialog.review_panel.show()
        dialog.workflow.translate()
        self.until(entered.is_set)
        self.assertTrue(dialog.review_panel.isVisible())
        dialog.service_dialog.show()
        dialog.service_dialog.close()
        self.assertFalse(dialog.worker.cancel.is_set())
        self.assertTrue(dialog.isVisible())
        dialog.close()
        self.until(lambda: dialog.worker is None)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.selected_cues, self.cues)

    def test_failure_retry_filter_and_edit_tracking_preserve_previous_text(self):
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("보존할 편집")
        dialog.workflow.service = LyricsTranslationService(lambda *args, **kwargs: (_ for _ in ()).throw(TranslationError("Disconnected")))
        from PySide6.QtWidgets import QMessageBox
        with patch("app.services.translation_workflow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            dialog.workflow.translate([1])
        self.until(lambda: dialog.worker is None)
        review = dialog.review_panel
        self.assertEqual(review.table.item(0, 3).text(), "보존할 편집")
        self.assertEqual(dialog.draft.rows[1].status, "failed")
        review.filter_combo.setCurrentIndex(review.filter_combo.findData("failed"))
        self.assertTrue(review.table.isRowHidden(0))
        self.assertFalse(review.table.isRowHidden(1))
        review.filter_combo.setCurrentIndex(review.filter_combo.findData("edited"))
        self.assertFalse(review.table.isRowHidden(0))
        self.assertTrue(review.table.isRowHidden(1))

    def test_real_second_batch_failure_keeps_first_batch_results(self):
        self.cues = [{"text": f"Line {i}", "start": i} for i in range(21)]
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            rows = json.loads(body["input"][0]["content"])["lyrics"]
            calls.append([row["id"] for row in rows])
            if len(calls) == 2:
                raise TranslationError("Second batch disconnected")
            return json.dumps({"translations": [
                {"id": row["id"], "text": f"번역 {row['id']}"} for row in rows]})
        dialog = self.dialog(service=LyricsTranslationService(transport))
        dialog.workflow.translate()
        self.until(lambda: dialog.worker is None)
        self.assertEqual([len(x) for x in calls], [20, 1])
        self.assertEqual(dialog.draft.rows[19].text, "번역 19")
        self.assertEqual(dialog.draft.rows[19].status, "complete")
        self.assertEqual(dialog.draft.rows[20].status, "failed")
        self.assertFalse(dialog.draft.rows[20].checked)
        self.assertTrue(dialog.draft.has_result)
        self.assertIn("Second batch disconnected", dialog.workflow.status)

    def test_failed_translation_retains_finished_rows_and_ephemeral_llm_diagnostics(self):
        dialog = self.dialog()
        review = dialog.review_panel
        raw = '{"translations":[{"id":0,"text":"첫번째 번역"}]}'
        def work(cancel, progress):
            dialog.worker.activity.emit({"stage": "working", "rows": [0, 1, 2]})
            dialog.worker.activity.emit({"stage": "diagnostic", "provider": "ollama",
                "model": "qwen2.5-coder:7b", "rows": [0, 1, 2], "raw": raw,
                "reason": "Missing rows 2-3", "status": "failed"})
            dialog.worker.activity.emit({"stage": "complete", "row": 0, "text": "첫번째 번역"})
            dialog.worker.activity.emit({"stage": "failure", "rows": [1, 2],
                "reason": "Missing rows 2-3"})
            raise TranslationError("Missing rows 2-3")
        dialog.workflow._rows = [0, 1, 2]
        dialog.workflow.start("translate", work)
        self.until(lambda: dialog.worker is None)
        self.assertEqual(dialog.table.item(0, 3).text(), "첫번째 번역")
        self.assertEqual(dialog.draft.rows[0].status, "complete")
        self.assertTrue(dialog.draft.rows[0].checked)
        self.assertEqual([dialog.draft.rows[i].status for i in (1, 2)], ["failed", "failed"])
        self.assertTrue(all(not dialog.draft.rows[i].checked for i in (1, 2)))
        self.assertEqual(dialog.draft.rows[1].detail, "Missing rows 2-3")
        self.assertTrue(review.inspect_button.isEnabled())
        self.assertIn(raw, review._diagnostics[0]["raw"])
        self.assertIn("1줄", dialog.workflow.status)
        stored = str(self.settings.value(dialog.draft.key))
        self.assertIn("첫번째 번역", stored)
        self.assertNotIn(raw, stored)
        self.assertNotIn("qwen2.5-coder:7b", stored)

    def test_selective_cancel_restores_draft_checks_and_edit_flags(self):
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("첫째 편집")
        dialog.table.item(1, 3).setText("둘째 편집")
        dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        before = [(row.text, row.checked, row.status, row.edited) for row in dialog.draft.rows]
        entered = Event()
        def work(cancel, progress):
            dialog.worker.activity.emit({"stage": "complete", "row": 1, "text": "임시 결과"})
            entered.set()
            cancel.wait(4)
            raise TranslationCancelled()
        dialog.workflow._rows = [1]
        dialog.workflow.start("translate", work)
        self.until(lambda: entered.is_set() and dialog.table.item(1, 3).text() == "임시 결과")
        dialog.cancel_request_button.click()
        self.until(lambda: dialog.worker is None)
        self.assertEqual([(row.text, row.checked, row.status, row.edited) for row in dialog.draft.rows], before)

    def test_cancelled_translation_ignores_delayed_activity_and_progress(self):
        dialog = self.dialog()
        entered, release = Event(), Event()
        progress_seen = []
        dialog.workflow.progress_changed.connect(lambda value, total: progress_seen.append((value, total)))
        initial = [(row.text, row.checked, row.status) for row in dialog.draft.rows]

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            # Simulate callbacks already queued by an API request during cancel.
            progress(1, 3)
            dialog.worker.activity.emit({"stage": "complete", "row": 0, "text": "late result"})
            dialog.worker.activity.emit({"stage": "diagnostic", "raw": "late response"})
            return None

        dialog.workflow._rows = [0]
        dialog.workflow.start("translate", work)
        self.until(entered.is_set)
        dialog.workflow.cancel()
        release.set()
        self.until(lambda: dialog.worker is None)
        self.assertEqual(progress_seen, [])
        self.assertEqual(dialog.workflow._received, {})
        self.assertEqual(dialog.workflow.diagnostics, [])
        self.assertEqual([(row.text, row.checked, row.status) for row in dialog.draft.rows], initial)
        self.assertEqual(dialog.workflow.operation, "")
        self.assertIn("취소", dialog.workflow.status)

    def test_finished_translation_drops_delayed_callbacks(self):
        dialog = self.dialog()
        progress_seen = []
        dialog.workflow.progress_changed.connect(lambda value, total: progress_seen.append((value, total)))
        dialog.workflow.start("translate", lambda cancel, progress: None)
        self.until(lambda: dialog.worker is None)
        dialog.workflow._activity({"stage": "complete", "row": 0, "text": "orphaned result"})
        dialog.workflow._progress(1, 3)
        self.assertEqual(dialog.workflow._received, {})
        self.assertEqual(progress_seen, [])
        self.assertEqual(dialog.workflow.operation, "")

    def test_main_close_waits_for_model_manager_worker(self):
        entered = Event()
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [{"id": "qwen3:4b", "size": 0, "parameters": "4B", "quantization": "Q4"}]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local_llm"))
        self.until(lambda: dialog.worker is None and dialog.model_combo.count() == 1)
        dialog.service_dialog._open_llm_manager()
        manager = dialog.service_dialog.manager
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        def memory(*args):
            entered.set()
            args[-1].wait(4)
            raise TranslationCancelled()
        local.memory.side_effect = memory
        manager._memory(True)
        self.until(entered.is_set)
        dialog.show()
        dialog.close()
        self.until(lambda: manager.worker is None and not dialog.isVisible())
        self.assertTrue(manager._closing)

    def test_escape_and_resize_are_local_to_each_window(self):
        dialog = self.dialog()
        dialog.show()
        for window in (dialog.service_dialog, dialog.advanced_dialog):
            window.show()
            window.resize(window.minimumSize())
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            QTest.keyClick(window, Qt.Key.Key_Escape)
            self.assertFalse(window.isVisible())
            self.assertTrue(dialog.isVisible())

    def test_advanced_controls_and_style_use_sibling_windows(self):
        callback = Mock()
        dialog = self.dialog(style_callback=callback)
        dialog.advanced_dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog.instructions_edit.isVisible())
        self.assertFalse(dialog.endpoint_edit.isVisible())
        dialog.advanced_dialog.model_edit.setFocus()
        dialog.advanced_dialog.model_edit.selectAll()
        QTest.keyClicks(dialog.advanced_dialog.model_edit, "manual-model")
        self.assertEqual(dialog._config().model, "manual-model")
        dialog.review_panel.style_button.click()
        callback.assert_called_once_with(dialog)

    def test_api_connection_discovery_never_sends_lyrics(self):
        transport = Mock(return_value={"data": [{"id": "model-a"}]})
        config = TranslationConfig("openai", key="credential")
        self.assertEqual(available_models(transport, config, Event()), [("model-a", "model-a")])
        self.assertEqual(transport.call_args.args, ("https://api.openai.com/v1/models",))
        self.assertNotIn("body", transport.call_args.kwargs)

    def test_style_applies_to_chosen_canvas_source_without_modifying_lyrics(self):
        from app.models.source import Source, SourceType
        from app.ui.main_window import MainWindow
        from types import SimpleNamespace
        store = Mock()
        sources = [Source(SourceType.LYRICS, "First"), Source(SourceType.LYRICS, "Second")]
        store.sources.return_value = sources
        owner = SimpleNamespace(store=store, translator=Translator())
        editor = Mock()
        editor.exec.return_value = QDialog.DialogCode.Accepted
        editor.settings.return_value = {"subtitle_translation_enabled": False}
        with patch("PySide6.QtWidgets.QInputDialog.getItem", return_value=("2. Second", True)), \
                patch("app.dialogs.lyrics_translation_style_dialog.LyricsTranslationStyleDialog", return_value=editor):
            MainWindow._edit_translation_style_from_review(owner, None)
        store.update.assert_called_once_with(sources[1].id, subtitle_translation_enabled=False)
        self.assertNotIn("translation", self.cues[0])


if __name__ == "__main__":
    unittest.main()
