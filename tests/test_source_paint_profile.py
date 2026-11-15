"""Profile-only Canvas text/lyric paint timing with preserved renderer behavior."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.canvas import source_item
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.models.source_registry import source_registry


class SourcePaintProfilingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_opt_in_records_text_and_lyrics_without_source_identifiers(self) -> None:
        for kind in (SourceType.TEXT, SourceType.LYRICS):
            with self.subTest(kind=kind):
                source = Source(kind, "Sensitive title", text="Do not log this lyric")
                item = SourceItem(source)
                rendered = []
                def fake_renderer(current, painter, option, widget):
                    rendered.append(current.source.source_type)
                with (
                    patch.object(source_item, "PROFILE_ENABLED", True),
                    patch.object(source_item.PROFILE, "observe") as observe,
                    patch.object(source_registry.get(kind), "renderer", new=fake_renderer),
                ):
                    item.paint(None, None)
                self.assertEqual(rendered, [kind])
                self.assertIs(item.source, source)
                observe.assert_called_once()
                stage, elapsed = observe.call_args.args
                self.assertEqual(stage, f"canvas.source_{kind.value}_paint_seconds")
                self.assertGreaterEqual(elapsed, 0.0)
                self.assertNotIn("Sensitive", stage)
                self.assertNotIn("lyric", stage if kind is SourceType.TEXT else "ignored")

    def test_normal_paint_and_unrelated_sources_have_no_timing_overhead(self) -> None:
        for kind, enabled in ((SourceType.TEXT, False), (SourceType.LYRICS, False),
                              (SourceType.IMAGE, True)):
            with self.subTest(kind=kind, enabled=enabled):
                source = Source(kind, "Example")
                item = SourceItem(source)
                called = []
                def fake_renderer(*_args):
                    called.append(True)
                with (
                    patch.object(source_item, "PROFILE_ENABLED", enabled),
                    patch.object(source_item.PROFILE, "observe") as observe,
                    patch.object(source_registry.get(kind), "renderer", new=fake_renderer),
                ):
                    item.paint(None, None)
                self.assertEqual(called, [True])
                self.assertIs(item.source, source)
                observe.assert_not_called()

    def test_profiled_exception_restores_source_and_records_wall_time(self) -> None:
        source = Source(SourceType.LYRICS, "Example")
        item = SourceItem(source)
        def fail_renderer(*_args):
            raise RuntimeError("renderer failed")
        with (
            patch.object(source_item, "PROFILE_ENABLED", True),
            patch.object(source_item.PROFILE, "observe") as observe,
            patch.object(source_registry.get(SourceType.LYRICS), "renderer", new=fail_renderer),
        ):
            with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                item.paint(None, None)
        self.assertIs(item.source, source)
        observe.assert_called_once()
        self.assertEqual(observe.call_args.args[0], "canvas.source_lyrics_paint_seconds")


if __name__ == "__main__":
    unittest.main()
