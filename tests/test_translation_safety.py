"""Recovery, deletion, honest readiness and Windows model publication regressions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Timer
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.services.local_translation_service import LocalTranslationModels, _retry_install_io
from app.services.lyrics_translation_service import (LyricsTranslationService, TranslationConfig,
    TranslationError, TranslationCancelled, apply_translations)
from app.services.translation_draft import TranslationDraft
from app.utils.i18n import Translator
from tests.test_local_translation import package_bytes


class TranslationSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("translation/provider", "openai")
        self.settings.setValue("translation/config/openai/model", "test-model")
        self.cues = [{"text": "First", "start": 1, "end": 3}, {"text": "Second", "start": 4, "end": 6}]
        self.dialogs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for dialog in self.dialogs:
            if getattr(dialog, "worker", None):
                dialog.workflow.cancel()
                self.until(lambda: dialog.worker is None)
            dialog.deleteLater()
        self.app.processEvents()

    def until(self, condition):
        end = time.monotonic() + 4
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def dialog(self, cues=None, **kwargs):
        dialog = LyricsTranslationDialog(cues if cues is not None else self.cues, Translator(),
            settings=self.settings, draft_key="same-track", **kwargs)
        dialog.key_edit.setText("secret-fixture")
        self.dialogs.append(dialog)
        return dialog

    def test_apply_then_cancel_track_restores_translation_from_disk(self):
        track = PlaylistTrack("", "Test track", lyrics=deepcopy(self.cues))
        details = TrackDetailsDialog(track, Translator())
        self.dialogs.append(details)
        def create(cues, translator, parent, **kwargs):
            dialog = LyricsTranslationDialog(cues, translator, parent, settings=self.settings, **kwargs)
            self.dialogs.append(dialog)
            def execute():
                dialog.table.item(0, 3).setText("최종 저장 전 번역")
                dialog._apply()
                return dialog.result()
            dialog.exec = execute
            return dialog
        with patch("app.dialogs.lyrics_translation_dialog.LyricsTranslationDialog", side_effect=create):
            details._translate_lyrics()
        self.assertEqual(details.selected_lyrics[0]["translation"]["text"], "최종 저장 전 번역")
        details.reject()
        self.assertEqual(track.lyrics, self.cues)
        settings_reopened = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        restored = LyricsTranslationDialog(track.lyrics, Translator(), settings=settings_reopened, draft_key=track.id)
        self.dialogs.append(restored)
        self.assertTrue(restored.draft.restored)
        self.assertEqual(restored.table.item(0, 3).text(), "최종 저장 전 번역")
        self.assertTrue(restored.apply_button.isEnabled())

    def test_blank_and_duplicate_original_deletions_require_explicit_confirmation(self):
        cues = apply_translations(self.cues, ["기존 1", "기존 2"], TranslationConfig("gemini"))
        for edit in ("", "First"):
            dialog = self.dialog(cues)
            dialog.table.item(0, 3).setText(edit)
            with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning:
                dialog._apply()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertEqual(dialog.selected_cues, cues)
            self.assertEqual(warning.call_args.args[-1], QMessageBox.StandardButton.Cancel)
            with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
                dialog._apply()
            self.assertNotIn("translation", dialog.selected_cues[0])
            self.assertEqual(dialog.selected_cues[1], cues[1])

    def test_repeated_staging_recovers_latest_edits_after_track_cancel(self):
        first = self.dialog()
        first.table.item(0, 3).setText("첫 적용")
        first._apply()
        second = self.dialog(first.selected_cues)
        second.table.item(0, 3).setText("두 번째 적용")
        second._apply()
        recovered = self.dialog(self.cues)
        self.assertTrue(recovered.draft.restored)
        self.assertEqual(recovered.table.item(0, 3).text(), "두 번째 적용")
        external = apply_translations(self.cues, ["다른 작업에서 저장한 번역", None], TranslationConfig("manual"))
        independent = self.dialog(external)
        self.assertFalse(independent.draft.restored)
        self.assertEqual(independent.table.item(0, 3).text(), "다른 작업에서 저장한 번역")

    def test_unchecked_blank_does_not_warn_or_delete_existing_translation(self):
        cues = apply_translations(self.cues, ["기존 1", "기존 2"], TranslationConfig("gemini"))
        dialog = self.dialog(cues)
        dialog.table.item(0, 3).setText("")
        dialog.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning") as warning:
            dialog._apply()
        warning.assert_not_called()
        self.assertEqual(dialog.selected_cues, cues)

    def test_common_languages_survive_provider_mode_switches_and_restart(self):
        self.settings.setValue("translation/config/openai/source", "fr")
        self.settings.setValue("translation/config/openai/target", "ja")
        self.settings.setValue("translation/config/claude/source", "en")
        self.settings.setValue("translation/config/claude/target", "ko")
        dialog = self.dialog()
        self.assertEqual((dialog._config().source, dialog._config().target), ("fr", "ja"))
        dialog.source_combo.setEditText("en-GB")
        dialog.target_combo.setEditText("zh-Hant")
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("claude"))
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("local"))
        self.assertEqual((dialog._config().source, dialog._config().target), ("en-GB", "zh-Hant"))
        dialog.reject()
        restored = self.dialog()
        self.assertEqual((restored._config().source, restored._config().target), ("en-GB", "zh-Hant"))
        self.assertEqual(self.settings.value("translation/config/claude/target"), "ko")

    def test_follow_is_opt_in_and_can_be_disabled_during_translation(self):
        dialog = self.dialog()
        review = dialog.review_panel
        def work(cancel, progress):
            cancel.wait(3)
            raise TranslationCancelled()
        dialog._start("translate", work)
        with patch.object(dialog.table, "scrollToItem") as scroll:
            review._activity({"stage": "working", "rows": [1]})
            scroll.assert_not_called()
            review.follow_check.setChecked(True)
            review._activity({"stage": "working", "rows": [1]})
            self.assertEqual(scroll.call_count, 1)
            review.follow_check.setChecked(False)
            review._activity({"stage": "working", "rows": [0]})
            self.assertEqual(scroll.call_count, 1)
        dialog.workflow.cancel()
        self.until(lambda: dialog.worker is None)

    def test_readiness_distinguishes_settings_model_listing_and_changed_credentials(self):
        dialog = self.dialog()
        self.assertIn("미확인", dialog.readiness_label.text())
        dialog.service_dialog.record_connection(True, ["test-model"])
        dialog._refresh_summary()
        self.assertIn("5분", dialog.readiness_label.text())
        dialog.key_edit.setText("new-credential")
        self.assertIn("미확인", dialog.readiness_label.text())
        dialog.service_dialog.record_connection(True, ["another-model"])
        dialog._refresh_summary()
        self.assertIn("조회 목록에 없습니다", dialog.readiness_label.text())
        dialog.service_dialog.record_connection(False)
        dialog._refresh_summary()
        self.assertIn("실패", dialog.readiness_label.text())
        with patch("time.monotonic", return_value=time.monotonic() + 301):
            dialog._refresh_summary()
            self.assertIn("미확인", dialog.readiness_label.text())

    def test_deleting_draft_resets_edits_without_resaving_or_removing_track_data(self):
        cues = apply_translations(self.cues, ["저장된 번역", None], TranslationConfig("gemini"))
        dialog = self.dialog(cues)
        dialog.table.item(0, 3).setText("수정 초안")
        dialog.review_panel.save_draft()
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            dialog.review_panel.discard_draft()
        self.assertTrue(self.settings.contains(dialog.draft.key))
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            dialog.review_panel.discard_draft()
        QTest.qWait(600)
        self.assertFalse(self.settings.contains(dialog.draft.key))
        self.assertEqual(dialog.table.item(0, 3).text(), "저장된 번역")
        dialog.reject()
        self.assertFalse(self.settings.contains(dialog.draft.key))
        self.assertEqual(cues[0]["translation"]["text"], "저장된 번역")

    def test_delete_is_scoped_to_track_and_legacy_drafts_do_not_leak_credentials(self):
        dialog = self.dialog()
        dialog.table.item(0, 3).setText("초안")
        dialog.review_panel.save_draft()
        other = TranslationDraft(self.cues, self.settings, identity="different-track")
        other.rows[0].text, other.has_result, other.dirty = "다른 곡", True, True
        other.save()
        legacy = json.loads(self.settings.value(dialog.draft.key))
        legacy["version"] = 1
        legacy["rows"][0]["config"].update(key="leaked-key", endpoint="private-url", instructions="private-prompt")
        self.settings.setValue(dialog.draft.key, json.dumps(legacy))
        recovered = self.dialog()
        recovered.review_panel.save_draft()
        payload = self.settings.value(dialog.draft.key)
        for secret in ("leaked-key", "private-url", "private-prompt", "secret-fixture"):
            self.assertNotIn(secret, payload)
        dialog.draft.discard()
        self.assertFalse(self.settings.contains(dialog.draft.key))
        self.assertTrue(self.settings.contains(other.key))

    def test_delete_all_track_recovery_versions_then_new_edits_autosave(self):
        first = self.dialog()
        first.table.item(0, 3).setText("첫 적용")
        first._apply()
        second = self.dialog(first.selected_cues)
        second.table.item(0, 3).setText("다음 수정")
        second.review_panel.save_draft()
        with patch("app.dialogs.translation_review_panel.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            second.review_panel.discard_draft()
        self.assertFalse(self.settings.contains(first.draft.key))
        self.assertFalse(self.settings.contains(second.draft.key))
        second.table.item(0, 3).setText("삭제 후 새 편집")
        self.until(lambda: self.settings.contains(second.draft.key))
        self.assertIn("삭제 후 새 편집", self.settings.value(second.draft.key))

    def test_save_failure_keeps_editor_open_and_reports_error(self):
        dialog = self.dialog()
        dialog.show()
        dialog.table.item(0, 3).setText("메모리 안의 번역")
        with patch.object(dialog.draft, "_sync", side_effect=TranslationError("Cannot save draft")):
            dialog._apply()
            self.assertEqual(dialog.selected_cues, self.cues)
            dialog.reject()
            self.assertTrue(dialog.isVisible())
            self.assertIn("Cannot save", dialog.status_label.text())
        dialog.reject()


class ModelPublicationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.models = LocalTranslationModels(self.root / "models")
        self.archive = self.root / "test.argosmodel"
        self.archive.write_bytes(package_bytes())

    @staticmethod
    def locked_error():
        error = PermissionError("file locked")
        error.winerror = 5
        return error

    def test_concurrent_installations_publish_one_complete_model(self):
        barrier = Barrier(2)
        def progress(value, total):
            if value == total:
                barrier.wait(3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.models.install, self.archive, progress=progress) for _ in range(2)]
            results = [future.result(8) for future in futures]
        self.assertEqual(results[0].id, results[1].id)
        self.assertEqual(len(self.models.installed()), 1)
        self.assertTrue((results[0].directory / "LICENSE.txt").is_file())
        self.assertFalse(list(self.models.root.glob(".install-*")))

    def test_separate_processes_can_install_the_same_package(self):
        code = ("from app.services.local_translation_service import LocalTranslationModels; "
                "import sys; print(LocalTranslationModels(sys.argv[1]).install(sys.argv[2]).id)")
        def install():
            return subprocess.run([sys.executable, "-c", code, str(self.models.root), str(self.archive)],
                                  capture_output=True, timeout=10)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: install(), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(results[0].stdout, results[1].stdout)
        self.assertEqual(len(self.models.installed()), 1)
        self.assertFalse(list(self.models.root.glob(".install-*")))

    def test_cleanup_only_removes_owned_staging_directory(self):
        model = self.models.install(self.archive)
        with self.assertRaises(ValueError):
            self.models._cleanup_stage(model.directory)
        with self.assertRaises(ValueError):
            self.models._cleanup_stage(self.root / ".install-outside")
        self.assertTrue(model.directory.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows sharing/access errors")
    def test_transient_permission_error_retries_and_cancellation_interrupts_wait(self):
        operation = Mock(side_effect=[self.locked_error(), "published"])
        self.assertEqual(_retry_install_io(operation), "published")
        cancel = Event()
        def fail():
            cancel.set()
            raise self.locked_error()
        operation = Mock(side_effect=fail)
        with self.assertRaises(TranslationCancelled):
            _retry_install_io(operation, cancel)
        self.assertEqual(operation.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows native file handles")
    def test_native_file_handle_reproduces_rename_denial_and_release_recovers(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle, timer, errors = None, None, []
        original = Path.rename
        def rename(source, target):
            try:
                return original(source, target)
            except PermissionError as error:
                errors.append(error.winerror)
                raise
        def progress(value, total):
            nonlocal handle, timer
            if value == total:
                path = next(self.models.root.glob(".install-*/en_ko/model/model.bin"))
                handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                timer = Timer(.2, lambda: kernel.CloseHandle(handle))
                timer.start()
        try:
            with patch.object(Path, "rename", rename):
                model = self.models.install(self.archive, progress=progress)
        finally:
            if timer:
                timer.join(2)
        self.assertTrue(any(code in {5, 32, 33} for code in errors))
        self.assertTrue(model.directory.is_dir())
        self.assertFalse(list(self.models.root.glob(".install-*")))

    @unittest.skipUnless(os.name == "nt", "Windows sharing/access errors")
    def test_persistent_denial_reports_access_failure_and_preserves_installed_model(self):
        previous = self.models.install(self.archive)
        second = self.root / "second.argosmodel"
        second.write_bytes(package_bytes(source="ja"))
        with patch.object(Path, "rename", side_effect=self.locked_error()), \
                patch("app.services.local_translation_service.Event"):
            with self.assertRaisesRegex(TranslationError, "Cannot access model installation"):
                self.models.install(second)
        self.assertEqual([model.id for model in self.models.installed()], [previous.id])
        self.assertFalse(list(self.models.root.glob(".install-*")))


if __name__ == "__main__":
    unittest.main()
