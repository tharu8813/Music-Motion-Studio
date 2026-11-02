"""Local package integrity, CPU inference and visible asynchronous translation."""
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QMessageBox

from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.services.local_translation_service import LocalTranslationModels, LocalTranslationEngine
from app.services.lyrics_translation_service import (PROVIDERS, LLM_PROVIDERS, CLOUD_LLM_PROVIDERS, IncompleteTranslation,
    LyricsTranslationService, TranslationConfig, TranslationCancelled, TranslationError, apply_translations)
from app.services.translation_credentials import _SESSION
from app.utils.i18n import Translator


def package_bytes(*, unsafe=None, top="en_ko/", source="en", target="ko"):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(top + "metadata.json", json.dumps({"from_code": source, "to_code": target,
            "from_name": "English", "to_name": "Korean", "package_version": "test"}))
        archive.writestr(top + "model/model.bin", b"test weights")
        archive.writestr(top + "model/shared_vocabulary.txt", b"test vocabulary")
        archive.writestr(top + "sentencepiece.model", b"test tokenizer")
        archive.writestr(top + "LICENSE.txt", "Test license")
        if unsafe is not None:
            if isinstance(unsafe, str):
                # ZipInfo's Windows constructor normalizes backslashes. Keep
                # the actual untrusted archive spelling for the extraction test.
                raw = ZipInfo("placeholder")
                raw.filename = unsafe
                unsafe = raw
            archive.writestr(unsafe, b"unsafe")
    return buffer.getvalue()


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.models = LocalTranslationModels(self.root / "models")
        self.archive = self.root / "model.argosmodel"
        self.archive.write_bytes(package_bytes())

    def test_import_is_atomic_idempotent_and_preserves_license(self):
        for top in ("en_ko/", ""):
            self.archive.write_bytes(package_bytes(top=top))
            first = self.models.install(self.archive)
            second = self.models.install(self.archive)
            self.assertEqual(first, second)
            self.assertEqual((first.source, first.target), ("en", "ko"))
            self.assertEqual((first.directory / "LICENSE.txt").read_text(), "Test license")
        self.assertEqual(len(self.models.installed()), 2)
        self.assertFalse(any(path.name.startswith(".install-") for path in self.models.root.iterdir()))

    def test_unsafe_and_invalid_packages_do_not_escape_or_install(self):
        for filename in ("../escape.txt", "/escape.txt", "C:/escape.txt", "nested\\escape.txt", "en_ko/NUL", "en_ko/file. "):
            with self.subTest(filename=filename):
                self.archive.write_bytes(package_bytes(unsafe=filename))
                with self.assertRaises(TranslationError):
                    self.models.install(self.archive)
        self.archive.write_bytes(b"not a zip")
        with self.assertRaises(TranslationError):
            self.models.install(self.archive)
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertEqual(self.models.installed(), [])

    def test_symlink_package_entries_are_rejected(self):
        info = ZipInfo("en_ko/link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        self.archive.write_bytes(package_bytes(unsafe=info))
        with self.assertRaises(TranslationError):
            self.models.install(self.archive)

    def test_cancelled_import_or_download_never_installs_or_requests(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(TranslationCancelled):
            self.models.install(self.archive, cancel)
        with patch("app.services.local_translation_service.build_opener", side_effect=AssertionError("Must not request")):
            with self.assertRaises(TranslationCancelled):
                self.models.download({"links": ["https://example.com/model.argosmodel"]}, cancel)
        self.assertEqual(self.models.installed(), [])

    def test_download_installs_only_a_complete_bounded_https_package(self):
        data = package_bytes()
        class Response(BytesIO):
            url = "https://example.com/model.argosmodel"
            headers = {"Content-Length": str(len(data))}
        opener = Mock()
        opener.open.return_value = Response(data)
        events = []
        with patch("app.services.local_translation_service.build_opener", return_value=opener):
            model = self.models.download({"links": [Response.url]}, progress=lambda value, total: events.append((value, total)))
        self.assertEqual((model.source, model.target), ("en", "ko"))
        self.assertEqual(events[-1], (len(data), len(data)))
        self.assertEqual(opener.open.call_args.args[0].get_header("User-agent"), "MusicMotionStudio/1.0")
        short = Response(data[:-10])
        opener.open.return_value = short
        with patch("app.services.local_translation_service.build_opener", return_value=opener):
            with self.assertRaises(TranslationError):
                self.models.download({"links": [Response.url]})
        self.assertEqual(len(self.models.installed()), 1)

    def test_service_has_no_remote_non_llm_adapters(self):
        self.assertEqual(set(PROVIDERS) - LLM_PROVIDERS, {"local_nmt"})
        for provider in ("google", "papago", "deepl", "microsoft", "libretranslate"):
            with self.assertRaises(TranslationError):
                TranslationConfig(provider, key="test", model="test").validate()

    def test_corrupt_installed_metadata_is_ignored_without_breaking_the_dialog(self):
        model = self.models.install(self.archive)
        (model.directory / "metadata.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.models.installed(), [])
        with self.assertRaises(TranslationError):
            self.models.get(model.id)

    def test_cpu_engine_preserves_blank_lines_and_reuses_loaded_model(self):
        model = self.models.install(self.archive)
        result = Mock(hypotheses=[["translated"]])
        translator = Mock()
        translator.translate_batch.return_value = [result]
        tokenizer = Mock()
        tokenizer.encode.return_value = ["source"]
        tokenizer.decode.return_value = "번역"
        fake_ct2 = Mock()
        fake_ct2.Translator.return_value = translator
        fake_sp = Mock()
        fake_sp.SentencePieceProcessor.return_value = tokenizer
        with patch.dict("sys.modules", {"ctranslate2": fake_ct2, "sentencepiece": fake_sp}):
            engine = LocalTranslationEngine(self.models)
            config = TranslationConfig("local_nmt", source="en", target="ko", model=model.id)
            self.assertEqual(engine.translate(["First\n\nLast"], config), ["번역\n\n번역"])
            self.assertEqual(engine.translate(["Next", "이미 한국어"], config), ["번역", None])
            fake_ct2.Translator.assert_called_once()
            self.assertEqual(fake_ct2.Translator.call_args.kwargs["device"], "cpu")
            self.assertEqual(translator.translate_batch.call_count, 3)
            with self.assertRaises(TranslationError):
                engine.translate(["First"], TranslationConfig("local_nmt", source="ja", target="ko", model=model.id))

    def test_local_results_and_activity_are_delivered_one_cue_at_a_time(self):
        cancel, events = Event(), []
        engine = Mock()
        engine.translate.return_value = ["번역"]
        service = LyricsTranslationService(lambda *args, **kwargs: self.fail("No translation API"), local_engine=engine)
        config = TranslationConfig("local_nmt", source="en", target="ko", model="test")
        self.assertEqual(service.translate([{"text": "First"}, {"text": "Next"}], config,
            activity=events.append), ["번역", "번역"])
        self.assertEqual([event["stage"] for event in events], ["working", "complete", "working", "complete"])
        engine.translate.side_effect = lambda *args: (cancel.set(), ["번역"])[1]
        with self.assertRaises(TranslationCancelled):
            service.translate([{"text": "First"}, {"text": "Next"}], config, cancel)

    def test_empty_local_output_retries_then_marks_only_the_missing_line(self):
        model = self.models.install(self.archive)
        translator, tokenizer = Mock(), Mock()
        translator.translate_batch.side_effect = [[Mock(hypotheses=[[value]])] for value in ("", "", "translated")]
        tokenizer.encode.return_value = ["source"]
        tokenizer.decode.side_effect = lambda tokens: tokens[0]
        with patch.dict("sys.modules", {"ctranslate2": Mock(Translator=Mock(return_value=translator)),
                "sentencepiece": Mock(SentencePieceProcessor=Mock(return_value=tokenizer))}):
            engine = LocalTranslationEngine(self.models)
            result = engine.translate(["Missing\nNext", "이미 한국어\n♪\n\u200b\ufeff\u2060"],
                TranslationConfig("local_nmt", source="en", target="ko", model=model.id))
        self.assertEqual(result, [IncompleteTranslation("\ntranslated", (1,)), None])
        self.assertEqual([call.kwargs["beam_size"] for call in translator.translate_batch.call_args_list], [4, 1, 4])

    def test_retry_can_recover_a_blank_beam_and_preserves_target_language_lines(self):
        model = self.models.install(self.archive)
        translator, tokenizer = Mock(), Mock()
        translator.translate_batch.side_effect = [[Mock(hypotheses=[[value]])] for value in ("", "번역")]
        tokenizer.encode.return_value = ["source"]
        tokenizer.decode.side_effect = lambda tokens: tokens[0]
        with patch.dict("sys.modules", {"ctranslate2": Mock(Translator=Mock(return_value=translator)),
                "sentencepiece": Mock(SentencePieceProcessor=Mock(return_value=tokenizer))}):
            result = LocalTranslationEngine(self.models).translate(["\ufeffHello\u200b\n이미 한국어\n♪"],
                TranslationConfig("local_nmt", source="en", target="ko", model=model.id))
        self.assertEqual(result, ["번역\n이미 한국어\n♪"])
        tokenizer.encode.assert_called_once_with("Hello", out_type=str)

    def test_incomplete_cue_does_not_abort_later_cues_or_report_no_op(self):
        engine, events = Mock(), []
        engine.translate.side_effect = [[IncompleteTranslation("", (1,))], ["번역"]]
        service = LyricsTranslationService(local_engine=engine)
        self.assertEqual(service.translate([{"text": "Missing"}, {"text": "Next"}],
            TranslationConfig("local_nmt", source="en", target="ko", model="test"), activity=events.append), ["", "번역"])
        complete = [event for event in events if event["stage"] == "complete"]
        self.assertEqual(complete[0]["missing_lines"], (1,))
        self.assertNotIn("missing_lines", complete[1])

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "work/translation-checks/en-ko.argosmodel").is_file(), "real model not downloaded")
    def test_real_downloaded_model_runs_without_network(self):
        archive = Path(__file__).resolve().parents[1] / "work/translation-checks/en-ko.argosmodel"
        model = self.models.install(archive)
        service = LyricsTranslationService(local_models=self.models)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Offline inference must not request")):
            result = service.translate([{"text": "Hello, world.\n\nWe will meet again."}],
                TranslationConfig("local_nmt", source="en", target="ko", model=model.id))
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].strip())
        self.assertEqual(result[0].splitlines()[1], "")
        self.assertNotEqual(result[0], "Hello, world.\n\nWe will meet again.")

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "work/translation-checks/en-ko.argosmodel").is_file(), "real model not downloaded")
    def test_real_model_skips_invisible_symbol_and_target_lines_and_continues(self):
        archive = Path(__file__).resolve().parents[1] / "work/translation-checks/en-ko.argosmodel"
        model = self.models.install(archive)
        events = []
        service = LyricsTranslationService(local_models=self.models)
        with patch("urllib.request.urlopen", side_effect=AssertionError("Offline inference must not request")):
            result = service.translate([{"text": text} for text in ("\u200b\ufeff\u2060", "♪ ... 123", "이미 한국어",
                "ありがとう", "Hello, world.\n이미 한국어\n\n♪")],
                TranslationConfig("local_nmt", source="en", target="ko", model=model.id), activity=events.append)
        self.assertEqual(result[:3], ["", "", ""])
        self.assertTrue(result[-1].strip())
        self.assertEqual(result[-1].splitlines()[1:], ["이미 한국어", "", "♪"])
        self.assertEqual(len([event for event in events if event["stage"] == "complete"]), 5)


class TranslationActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def until(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def test_modes_are_separate_and_legacy_provider_does_not_fall_back_to_paid_llm(self):
        with TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("translation/provider", "google")
            dialog = LyricsTranslationDialog([{"text": "Hello"}], Translator(), settings=settings)
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.mode_combo.currentData(), "local")
            self.assertEqual(dialog.provider_combo.currentData(), "local_nmt")
            self.assertTrue(dialog.key_edit.isHidden())
            dialog.mode_combo.setCurrentIndex(0)
            self.assertEqual({dialog.provider_combo.itemData(i) for i in range(dialog.provider_combo.count())}, CLOUD_LLM_PROVIDERS)

    def test_live_results_remain_unapplied_and_cancel_restores_prior_review(self):
        with TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            entered, release = Event(), Event()
            def transport(url, body=None, headers=None, **kwargs):
                rows = json.loads(body["input"][0]["content"])["lyrics"]
                if rows[0]["id"] == 20:
                    entered.set()
                    release.wait(3)
                return json.dumps({"translations": [{"id": row["id"], "text": f"번역 {row['id']}"} for row in rows]})
            cues = [{"start": row, "end": row + 1, "text": f"Hello {row}"} for row in range(21)]
            dialog = LyricsTranslationDialog(cues, Translator(), settings=settings, service=LyricsTranslationService(transport))
            self.addCleanup(dialog.deleteLater)
            dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("openai"))
            dialog.key_edit.setText("test")
            dialog.model_combo.setEditText("test")
            try:
                dialog._translate()
                self.until(lambda: entered.is_set() and dialog.table.item(0, 3).text() == "번역 0")
                self.assertIsNotNone(dialog.worker)
                self.assertFalse(dialog.apply_button.isEnabled())
                self.assertEqual(dialog.selected_cues, cues)
                self.assertEqual(dialog.table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
                self.assertIn("번역", dialog.table.item(20, 4).text())
                self.assertTrue(dialog.table.item(20, 2).background().color().isValid())
                dialog._cancel_request()
            finally:
                release.set()
                self.until(lambda: dialog.worker is None)
            self.assertEqual(dialog.table.item(0, 3).text(), "")
            self.assertEqual(dialog.selected_cues, cues)
            self.assertFalse(dialog.apply_button.isEnabled())

    def test_incomplete_local_rows_are_visible_and_preserve_saved_translations_on_apply(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            models = LocalTranslationModels(root / "models")
            archive = root / "model.argosmodel"
            archive.write_bytes(package_bytes())
            models.install(archive)
            settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("translation/provider", "local_nmt")
            cues = apply_translations([{"text": text} for text in ("First", "Missing", "Next")],
                ["이전 1", "이전 2", "이전 3"], TranslationConfig("manual", target="ko"))
            engine = Mock()
            engine.translate.side_effect = [["번역 1"], [IncompleteTranslation("", (1,))], ["번역 3"]]
            dialog = LyricsTranslationDialog(cues, Translator(), settings=settings,
                service=LyricsTranslationService(local_models=models, local_engine=engine))
            self.addCleanup(dialog.deleteLater)
            confirm = patch("app.dialogs.lyrics_translation_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes)
            confirm.start()
            self.addCleanup(confirm.stop)
            dialog._translate()
            self.until(lambda: dialog.worker is None)
            self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
            self.assertIn("미완료", dialog.table.item(1, 4).text())
            self.assertIn("언어 방향", dialog.table.item(1, 4).toolTip())
            self.assertIn("미완료 1", dialog.status_label.text())
            self.assertTrue(dialog.apply_button.isEnabled())
            # A cancelled retry restores the earlier warning and checkbox state.
            entered, release = Event(), Event()
            def blocked(*args):
                entered.set()
                release.wait(3)
                return ["새 번역"]
            engine.translate.side_effect = blocked
            dialog._translate()
            try:
                self.until(entered.is_set)
                dialog._cancel_request()
            finally:
                release.set()
                self.until(lambda: dialog.worker is None)
            self.assertEqual(dialog._incomplete_rows, {1})
            self.assertIn("언어 방향", dialog.table.item(1, 4).toolTip())
            dialog._apply()
            self.assertEqual([cue["translation"]["text"] for cue in dialog.selected_cues], ["번역 1", "이전 2", "번역 3"])
            dialog.table.item(1, 3).setText("직접 보완")
            self.assertEqual(dialog._incomplete_rows, set())
            self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Checked)
            self.assertIn("수정됨", dialog.table.item(1, 4).text())
            self.assertEqual(dialog.table.item(1, 4).toolTip(), "")
