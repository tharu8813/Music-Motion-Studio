"""Account form, encrypted restart persistence and cancellable lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from app.dialogs.openai_account_dialog import OpenAIAccountDialog
from app.dialogs.settings_dialog import SettingsDialog
from app.services.app_settings_service import AppSettings
from app.services.chatgpt_auth_service import ChatGPTAuthService
from app.services.lyrics_translation_service import TranslationCancelled
from app.services.theme_service import Theme
from app.services.translation_credentials import TranslationCredentials, _SESSION
from app.utils.i18n import Language, Translator


class OpenAIAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "account.ini"
        self.settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        self.credentials = TranslationCredentials(self.settings)
        self.auth = ChatGPTAuthService(self.credentials, self.settings)
        _SESSION.clear()

    def until(self, condition):
        end = time.monotonic() + 3
        while not condition() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_legacy_session_is_migrated_and_readable_after_process_restart(self):
        self.credentials.save("chatgpt", {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "email": "test@example.com", "expires_at": time.time() + 3600,
            "scope": "chatgpt.tokens.use.direct", "remember": False})
        dialog = OpenAIAccountDialog(Translator(), auth=self.auth)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.email.text(), "test@example.com")
        self.assertTrue(self.credentials.load("chatgpt")["remember"])
        self.assertNotIn("fixture-access", self.path.read_text(encoding="utf-8"))
        code = "from PySide6.QtCore import QSettings; from app.services.translation_credentials import TranslationCredentials; import sys; p=TranslationCredentials(QSettings(sys.argv[1],QSettings.Format.IniFormat)).load('chatgpt'); assert p['access_token']=='fixture-access' and p['remember'] is True"
        child = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr.decode(errors="replace"))

    def test_account_form_exposes_status_without_tokens(self):
        self.credentials.save("chatgpt", {"access_token": "fixture-access", "remember": True,
            "email": "test@example.com", "expires_at": time.time() + 3600, "scope": "chatgpt.tokens.use.direct"}, remember=True)
        dialog = OpenAIAccountDialog(Translator(), auth=self.auth)
        self.addCleanup(dialog.deleteLater)
        labels = " ".join(label.text() for label in (dialog.email, dialog.connection, dialog.permission, dialog.session))
        self.assertIn("test@example.com", labels)
        self.assertNotIn("fixture-access", labels)
        self.assertTrue(dialog.verify.isEnabled())
        self.assertTrue(dialog.sign_out.isEnabled())

    def test_close_cancels_and_waits_for_account_worker(self):
        dialog = OpenAIAccountDialog(Translator(), auth=self.auth)
        self.addCleanup(dialog.deleteLater)
        def work(cancel, progress):
            cancel.wait(2)
            raise TranslationCancelled()
        dialog._start("signin", work)
        dialog.reject()
        self.until(lambda: dialog.worker is None)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_settings_has_a_separate_accounts_page(self):
        dialog = SettingsDialog(AppSettings(), Language.KOREAN, Theme.DARK, Translator())
        self.addCleanup(dialog.deleteLater)
        self.assertIs(dialog.tabs.widget(6), dialog.accounts_page)
        self.assertTrue(dialog.openai_account_button.text())
