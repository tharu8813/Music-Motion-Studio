"""Unified translation/editing, separate settings, and resumable drafts."""
import json
from pathlib import Path
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
        self.assertFalse(self.settings.contains(dialog.draft.key))

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
