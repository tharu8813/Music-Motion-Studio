"""Settings tabs keep edited values and fit without horizontal clipping."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.project_settings_dialog import ProjectSettingsDialog
from app.models.project import ProjectSettings
from app.services.app_settings_service import AppSettings
from app.services.theme_service import Theme
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Language, Translator


class SettingsLayoutTests(unittest.TestCase):
    def test_maintenance_explanations_fit_their_wrapped_height(self):
        app = QApplication.instance() or QApplication([])
        apply_studio_style(app)
        translator = Translator()
        for language in (Language.KOREAN, Language.ENGLISH):
            translator.set_language(language)
            dialog = SettingsDialog(AppSettings(), language, Theme.DARK, translator)
            try:
                dialog.show()
                dialog.tabs.setCurrentIndex(5)
                for width in (820, 720):
                    dialog.resize(width, 520)
                    app.processEvents()
                    for hint in (dialog.integrity_hint, dialog.program_reinstall_hint, dialog.reset_hint):
                        self.assertGreaterEqual(hint.height(), hint.heightForWidth(hint.width()))
            finally:
                dialog.close()

    def test_tabs_preserve_values_and_fit_in_both_languages(self):
        app = QApplication.instance() or QApplication([])
        apply_studio_style(app)
        translator = Translator()
        original = translator.language
        try:
            for language in (Language.KOREAN, Language.ENGLISH):
                translator.set_language(language)
                settings = SettingsDialog(AppSettings(), language, Theme.AUTO, translator)
                project = ProjectSettingsDialog(ProjectSettings(), translator, QPixmap())
                try:
                    settings.crf_spin.setValue(19)
                    project.title_edit.setText("Edited project")
                    project.transition_crossfade_radio.setChecked(True)
                    project.crossfade_seconds_spin.setValue(5.5)
                    for dialog in (settings, project):
                        dialog.resize(dialog.minimumWidth(), dialog.minimumHeight())
                        dialog.show()
                        for index in range(dialog.tabs.count()):
                            dialog.tabs.setCurrentIndex(index)
                            app.processEvents()
                            page = dialog.tabs.widget(index)
                            self.assertLessEqual(page.widget().width(), page.viewport().width())
                    self.assertEqual(settings.app_settings.crf, 19)
                    project._accept()
                    self.assertEqual(project.selected_settings.title, "Edited project")
                    self.assertEqual(project.selected_settings.crossfade_seconds, 5.5)
                    self.assertEqual(project.selected_settings.transition_mode, "crossfade")
                finally:
                    settings.close()
                    project.close()
        finally:
            translator.set_language(original)


if __name__ == "__main__":
    unittest.main()
