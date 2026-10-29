from __future__ import annotations

import gc
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.dialogs.settings_dialog import SettingsDialog
from app.services.app_settings_service import AppSettings
from app.services.language_pack_service import LanguagePackError, LanguagePackService
from app.services.theme_service import Theme
from app.utils.i18n import Language, Translator


class LanguagePackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setApplicationName("Music Motion Studio Language Pack Tests")
        cls.application.setOrganizationName("Music Motion Studio Tests")

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "metadata": {
                "locale": "ja-JP",
                "name": "Japanese",
                "native_name": "日本語",
                "author": "Test translator",
                "version": "1.0.0",
                "minimum_app_version": "1.0.1",
            },
            "strings": {"save": "保存"},
            "overrides": {
                "Settings": "設定",
                "File {name}": "ファイル {name}",
                "Processed {count} files": "Traité {count} fichiers",
            },
        }

    def test_valid_pack_is_imported_and_external_locale_translates_ui(self) -> None:
        with TemporaryDirectory(prefix="pc-language-pack-") as raw_directory:
            root = Path(raw_directory)
            source = root / "source.json"
            source.write_text(
                json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8"
            )
            service = LanguagePackService(root / "installed")
            pack = service.import_pack(source)
            self.assertEqual(pack.locale, "ja-JP")
            translator = Translator(pack_service=service)
            translator.set_language("ja-JP")
            self.assertEqual(translator.language.value, "ja-JP")
            self.assertEqual(translator.text("save"), "保存")
            self.assertEqual(translator.text("open"), "Open")
            widget = QWidget()
            label = QLabel("Settings", widget)
            translator.translate_widget_tree(widget)
            self.assertEqual(label.text(), "設定")
            dynamic_label = QLabel("Processed 12 files", widget)
            translator.translate_widget_tree(widget)
            self.assertEqual(dynamic_label.text(), "Traité 12 fichiers")
            widget.deleteLater()

    def test_literal_override_must_preserve_placeholders(self) -> None:
        with TemporaryDirectory(prefix="pc-language-pack-invalid-") as raw_directory:
            path = Path(raw_directory) / "invalid.json"
            payload = self._payload()
            payload["overrides"] = {"File {name}": "ファイル"}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(LanguagePackError):
                LanguagePackService(Path(raw_directory) / "installed").load_file(path)

    def test_removed_active_pack_falls_back_to_english(self) -> None:
        with TemporaryDirectory(prefix="pc-language-pack-remove-") as raw_directory:
            root = Path(raw_directory)
            source = root / "source.json"
            source.write_text(json.dumps(self._payload()), encoding="utf-8")
            service = LanguagePackService(root / "installed")
            service.import_pack(source)
            translator = Translator(pack_service=service)
            translator.set_language("ja-JP")
            service.remove_pack("ja-JP")
            translator.refresh_packs()
            self.assertIs(translator.language, Language.ENGLISH)
            self.assertEqual(translator.text("save"), "Save")

    def test_settings_lists_installed_pack_and_management_controls(self) -> None:
        with TemporaryDirectory(prefix="pc-language-pack-settings-") as raw_directory:
            root = Path(raw_directory)
            source = root / "source.json"
            source.write_text(json.dumps(self._payload()), encoding="utf-8")
            service = LanguagePackService(root / "installed")
            service.import_pack(source)
            translator = Translator(pack_service=service)
            dialog = SettingsDialog(
                AppSettings(), Language.ENGLISH, Theme.AUTO, translator
            )
            try:
                index = dialog.language_combo.findData("ja-JP")
                self.assertGreaterEqual(index, 0)
                dialog.language_combo.setCurrentIndex(index)
                self.assertEqual(dialog.selected_language, "ja-JP")
                self.assertTrue(dialog.language_pack_remove_button.isEnabled())
                self.assertIn("Test translator", dialog.language_pack_status.text())
            finally:
                dialog.close()

    def test_closed_settings_dialog_is_not_kept_alive_by_its_own_connections(self) -> None:
        # A leaked dialog is only deleted inside ~QApplication, which crashed
        # the interpreter at exit (0xC0000409 / 0xC0000005).
        dialog = SettingsDialog(AppSettings(), Language.ENGLISH, Theme.AUTO, Translator())
        dialog.close()
        reference = weakref.ref(dialog)
        del dialog
        gc.collect()
        self.assertIsNone(reference())

    def test_default_windows_folder_receives_complete_template(self) -> None:
        with TemporaryDirectory(prefix="pc-language-pack-localappdata-") as raw_directory:
            with patch.dict(os.environ, {"LOCALAPPDATA": raw_directory}), patch(
                "app.services.language_pack_service.sys.platform", "win32"
            ):
                service = LanguagePackService()

            expected_directory = Path(raw_directory) / "PlaylistCanvas" / "languages"
            self.assertEqual(service.directory, expected_directory)
            self.assertTrue(service.template_path.is_file())
            self.assertTrue((expected_directory / "ko.json").is_file())
            self.assertTrue((expected_directory / "en.json").is_file())
            self.assertNotIn("xx-XX", service.packs)
            self.assertNotIn("ko", service.packs)
            self.assertNotIn("en", service.packs)

            payload = json.loads(service.template_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertGreaterEqual(len(payload["strings"]), 30)
            self.assertGreaterEqual(len(payload["overrides"]), 3_000)
            self.assertTrue(all(value == "" for value in payload["strings"].values()))
            self.assertTrue(all(value == "" for value in payload["overrides"].values()))
            self.assertEqual(
                set(payload["overrides"]), set(payload["source_references"])
            )

    def test_blank_template_values_fall_back_to_english(self) -> None:
        template_path = Path("app/resources/language-pack-template.json")
        payload = json.loads(template_path.read_text(encoding="utf-8"))
        payload["metadata"].update({
            "locale": "fr-FR",
            "name": "French",
            "native_name": "Français",
            "author": "Test translator",
        })
        payload["overrides"]["Settings"] = "Paramètres"
        with TemporaryDirectory(prefix="pc-language-pack-blank-") as raw_directory:
            path = Path(raw_directory) / "fr-FR.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            service = LanguagePackService(Path(raw_directory) / "installed")
            pack = service.load_file(path)
            translator = Translator(pack_service=service)
            service.import_pack(path)
            translator.refresh_packs()
            translator.set_language("fr-FR")
            self.assertEqual(pack.overrides["Settings"], "Paramètres")
            self.assertEqual(translator.text("save"), "Save")
            self.assertEqual(translator.literal("Settings"), "Paramètres")

    def test_fully_translated_template_fits_with_headroom(self) -> None:
        # Following the template's own instructions (copy, fill every value)
        # produced ~1.1 MB and was rejected by the old 1 MB cap.
        from app.services.language_pack_service import MAX_PACK_BYTES, MAX_TRANSLATIONS

        payload = json.loads(Path("app/resources/language-pack-template.json").read_text(encoding="utf-8"))
        korean = json.loads(Path("app/resources/ko.json").read_text(encoding="utf-8"))["overrides"]
        payload["metadata"].update({
            "locale": "ja-JP", "name": "Japanese", "native_name": "日本語", "author": "Test translator",
        })
        # Hangul is 3 bytes per character in UTF-8, like CJK: a worst-case-ish full pack.
        payload["overrides"] = {key: korean.get(key) or key for key in payload["overrides"]}
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        # Fail early, long before real packs hit the caps as the template grows.
        self.assertLessEqual(2 * len(raw), MAX_PACK_BYTES)
        self.assertLessEqual(3 * len(payload["overrides"]) // 2, MAX_TRANSLATIONS)
        with TemporaryDirectory(prefix="pc-language-pack-full-") as raw_directory:
            path = Path(raw_directory) / "ja-JP.json"
            path.write_bytes(raw)
            pack = LanguagePackService(Path(raw_directory) / "installed").load_file(path)
        self.assertEqual(pack.locale, "ja-JP")


if __name__ == "__main__":
    unittest.main()
