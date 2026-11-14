"""Regression coverage for standalone source palette help translations."""

from __future__ import annotations

import unittest

from app.models.source import SourceType
from app.ui.source_palette_help import source_hover_help, source_palette_summary


class SourcePaletteHelpTests(unittest.TestCase):
    def test_all_registered_source_types_have_nonempty_help_in_both_languages(self):
        for source in SourceType:
            for korean in (False, True):
                with self.subTest(source=source, korean=korean):
                    summary = source_palette_summary(source, korean)
                    purpose, properties = source_hover_help(source, korean)
                    self.assertTrue(summary)
                    self.assertTrue(purpose)
                    self.assertTrue(properties)

    def test_labels_preserve_existing_localization_and_source_specific_settings(self):
        self.assertEqual(
            source_palette_summary(SourceType.LYRICS, True), "실시간 가사와 자막"
        )
        self.assertEqual(
            source_palette_summary(SourceType.AUDIO_VISUALIZER, False),
            "Frequency-reactive effect",
        )
        self.assertIn(
            "타이밍 보정", source_hover_help(SourceType.LYRICS, True)[1]
        )
        self.assertIn(
            "attack", source_hover_help(SourceType.AUDIO_VISUALIZER, False)[1]
        )


if __name__ == "__main__":
    unittest.main()
