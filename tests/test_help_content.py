"""The User Guide's content: every tab filled, links resolve, screenshots exist, F1 targets exist."""

from __future__ import annotations

import ast
import gc
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.dialogs.help_content import HELP_TAB_IDS, help_tabs, help_topics, topic_images  # noqa: E402
from app.dialogs.help_dialog import HelpDialog, install_help_shortcut  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HELP_IMAGES = ROOT / "app" / "resources" / "help"


class HelpContentTests(unittest.TestCase):
    def test_both_languages_have_the_same_topics_on_every_tab(self) -> None:
        korean, english = help_topics(True), help_topics(False)
        self.assertEqual([t.identifier for t in korean], [t.identifier for t in english])
        self.assertEqual([t.tab for t in korean], [t.tab for t in english])
        self.assertEqual([tab.identifier for tab in help_tabs(True)], list(HELP_TAB_IDS))
        for tab in HELP_TAB_IDS:
            with self.subTest(tab=tab):
                self.assertGreaterEqual(sum(1 for t in korean if t.tab == tab), 5)
        identifiers = [t.identifier for t in korean]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_every_topic_is_tagged_and_every_link_and_screenshot_resolves(self) -> None:
        for korean, language in ((True, "ko"), (False, "en")):
            topics = help_topics(korean)
            identifiers = {t.identifier for t in topics}
            for topic in topics:
                with self.subTest(topic=topic.identifier, language=language):
                    self.assertTrue(topic.tags)
                    for target in topic.related + tuple(re.findall(r"topic:([a-z0-9_]+)", topic.body)):
                        self.assertIn(target, identifiers)
                    for image in topic_images(topic):
                        self.assertTrue((HELP_IMAGES / language / f"{image}.png").is_file(), image)

    def test_every_screenshot_is_used(self) -> None:
        used = {image for topic in help_topics(True) for image in topic_images(topic)}
        for language in ("ko", "en"):
            self.assertEqual({path.stem for path in (HELP_IMAGES / language).glob("*.png")}, used)

    def test_f1_targets_in_the_code_are_real_topics(self) -> None:
        """Every string literal passed as an F1 context names an existing tab or topic."""
        identifiers = {t.identifier for t in help_topics(True)}
        tabs = set(HELP_TAB_IDS)
        topic_lists = {"_STEP_HELP_TOPICS", "_TAB_HELP_TOPICS"}
        for path in (ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id in topic_lists for target in node.targets):
                    for constant in ast.walk(node.value):
                        if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                            with self.subTest(path=path.name, topic=constant.value):
                                self.assertIn(constant.value, identifiers)
                if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "install_help_shortcut"
                        and len(node.args) >= 2 and isinstance(node.args[1], ast.Tuple)):
                    tab, topic = (element.value for element in node.args[1].elts)
                    with self.subTest(path=path.name, tab=tab, topic=topic):
                        self.assertIn(tab, tabs)
                        self.assertIn(topic, identifiers)



class HelpWindowLifetimeTests(unittest.TestCase):
    """A closed window must not be kept alive by its F1 or search connections.

    A leaked dialog is only deleted inside ~QApplication, which crashed the
    interpreter at exit on Windows.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_new_feature_topics_are_searchable_and_render_their_screenshots(self) -> None:
        from app.utils.i18n import Language, Translator

        translator = Translator()
        for language, terms in (
            (Language.KOREAN, ("음악 반응", "거리별 흐림", "간주", "줄별 스타일", "스냅", "ID3")),
            (Language.ENGLISH, ("music reaction", "distance falloff", "interlude", "per-line styles", "snap", "ID3")),
        ):
            translator.set_language(language)
            dialog = HelpDialog(translator)
            try:
                for term, identifier in zip(terms, ("music_reaction", "lyrics_transition", "lyrics_intro",
                                                     "lyrics_line_styles", "timeline", "mp3_metadata")):
                    with self.subTest(language=language, topic=identifier):
                        dialog.search_edit.setText(term)
                        dialog.select_tab("track" if identifier == "mp3_metadata" else "canvas")
                        self.assertIn(identifier, dialog.visible_topic_ids())
                        dialog.select_topic(identifier)
                        self.assertEqual(dialog.current_topic_id, identifier)
                        self.assertTrue(dialog.browser.images)
                        self.assertIn(dialog._topic(identifier).title, dialog.browser.toPlainText())
            finally:
                dialog.close()
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_customization_dialogs_f1_open_their_own_updated_topics(self) -> None:
        from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
        from app.dialogs.lyrics_line_style_dialog import LyricsLineStyleDialog
        from app.dialogs.mp3_metadata_editor_dialog import Mp3MetadataEditorDialog
        from app.models.source import Source, SourceType
        from app.utils.i18n import Translator

        translator = Translator()
        source = Source(SourceType.LYRICS, "Lyrics", fill_color="#00000000")
        for cls, arguments, tab, topic in (
            (LyricsAnimationDialog, (source, translator), "canvas", "lyrics_transition"),
            (LyricsLineStyleDialog, (source, translator), "canvas", "lyrics_line_styles"),
            (Mp3MetadataEditorDialog, (translator,), "track", "mp3_metadata"),
        ):
            with self.subTest(dialog=cls.__name__):
                dialog = cls(*arguments)
                try:
                    shortcuts = dialog.findChildren(QShortcut)
                    self.assertEqual(len(shortcuts), 1)
                    with patch("app.dialogs.help_dialog.open_help") as open_help:
                        shortcuts[0].activated.emit()
                        open_help.assert_called_once_with(dialog, translator, tab, topic)
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_closed_help_dialog_is_released(self) -> None:
        dialog = HelpDialog(None, tab="preview")
        dialog.search_edit.setText("volume")
        dialog.close()
        reference = weakref.ref(dialog)
        del dialog
        gc.collect()
        self.assertIsNone(reference())

    def test_f1_shortcut_does_not_keep_its_window_alive(self) -> None:
        from PySide6.QtWidgets import QDialog, QTabWidget

        class Window(QDialog):
            def __init__(self) -> None:
                super().__init__()
                self.tabs = QTabWidget(self)
                install_help_shortcut(self, lambda window: ("other", ("settings",)[window.tabs.currentIndex() + 1]))

        dialog = Window()
        dialog.close()
        reference = weakref.ref(dialog)
        del dialog
        gc.collect()
        self.assertIsNone(reference())


if __name__ == "__main__":
    unittest.main()


class RecentHelpCoverageTests(unittest.TestCase):
    def test_recent_topics_exist_in_both_languages(self):
        for korean in (True, False):
            topics = {topic.identifier: topic for topic in help_topics(korean)}
            for identifier in ('lyrics_wrapping', 'lyrics_online_search', 'source_decoration', 'status_warnings'):
                self.assertIn(identifier, topics)
                self.assertTrue(topics[identifier].body)
            body = topics['lyrics_files'].body
            self.assertNotIn('LiriQo', body)
            for provider in ('KuGou', 'AMLL', 'LRCLIB'):
                self.assertIn(provider, body)
