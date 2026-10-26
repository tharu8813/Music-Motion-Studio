from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.inspector.source_inspector import SourceInspector
from app.models.project import ProjectDocument
from app.models.source import Source, SourceType
from app.models.source_components import SourceComponent, TextComponent
from app.models.source_registry import SourceDefinition, SourceRegistry, source_registry
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class SourceRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_every_type_has_legacy_bindings_and_round_trips(self) -> None:
        for source_type in SourceType:
            with self.subTest(source_type=source_type):
                definition = source_registry.get(source_type.value)
                self.assertIs(definition.type, source_type)
                self.assertTrue(issubclass(definition.component, SourceComponent))
                self.assertIs(definition.source_factory, Source)
                self.assertTrue(callable(definition.renderer))
                self.assertTrue(callable(definition.inspector))
                source = definition.source_factory(source_type, "Registered source")
                encoded = source_registry.serialize(source)
                self.assertEqual(encoded, source.to_dict())
                self.assertEqual(source_registry.deserialize(encoded), Source.from_dict(encoded))
                for version in (1, 2):
                    document = ProjectDocument.from_dict({"version": version, "sources": [encoded]})
                    self.assertEqual(document.to_dict()["sources"], [encoded])

    def test_unknown_duplicate_and_invalid_definitions_are_rejected(self) -> None:
        registry = SourceRegistry()
        with self.assertRaisesRegex(ValueError, "Unregistered"):
            registry.get(SourceType.TEXT)
        definition = SourceDefinition(SourceType.TEXT, TextComponent)
        registry.register(definition)
        self.assertIs(registry.get("text"), definition)
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(definition)
        with self.assertRaises(ValueError):
            registry.register(SourceDefinition("unknown", TextComponent))
        for component in (None, object, TextComponent.from_source(Source(SourceType.TEXT, "Text"))):
            with self.subTest(component=component), self.assertRaisesRegex(ValueError, "SourceComponent"):
                SourceRegistry().register(SourceDefinition(SourceType.TEXT, component))
        for value in ("unknown", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry.get(value)
        for data in ({}, {"source_type": "unknown"}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                registry.deserialize(data)

    def test_legacy_migration_and_validation_still_use_source_codec(self) -> None:
        payload = Source(SourceType.LYRICS, "Lyrics").to_dict()
        payload.update(subtitle_style="neon", subtitle_animation="scroll_up")
        restored = source_registry.deserialize(payload)
        self.assertEqual(restored.outline_color, "#72E8FF")
        self.assertEqual(restored.subtitle_animation, "rise")
        self.assertIn("subtitle_style", payload)
        payload["opacity"] = float("nan")
        with self.assertRaises(ValueError):
            source_registry.deserialize(payload)

    def test_component_changes_are_detached_atomic_and_use_the_registered_codec(self) -> None:
        source = Source(SourceType.VIDEO, "Video", video_paths=["original.mp4"])
        before = source.to_dict()
        paths = ["new.mp4"]
        definition = source_registry.get(SourceType.VIDEO)
        codec = Mock(wraps=Source)
        with patch.object(definition, "serializer", codec):
            changes = source_registry.component_changes(source, paths=paths, speed=2)
        codec.to_dict.assert_called_once_with(source)
        codec.from_dict.assert_called_once()
        paths.append("external.mp4")
        self.assertEqual(changes, {"video_paths": ["new.mp4"], "video_speed": 2})
        self.assertEqual(source.to_dict(), before)
        with self.assertRaises(ValueError):
            source_registry.component_changes(source, paths=["draft.mp4"], speed=0)
        self.assertEqual(source.to_dict(), before)
        self.assertEqual(source_registry.component_changes(source, speed=1), {})

    def test_project_uses_registered_serializer(self) -> None:
        source = Source(SourceType.TEXT, "Title")
        codec = Mock()
        codec.to_dict.return_value = source.to_dict()
        codec.from_dict.return_value = source
        with patch.object(source_registry.get(SourceType.TEXT), "serializer", codec):
            payload = ProjectDocument(sources=[source]).to_dict()
            restored = ProjectDocument.from_dict(payload)
        codec.to_dict.assert_called_once_with(source)
        codec.from_dict.assert_called_once_with(payload["sources"][0])
        self.assertIs(restored.sources[0], source)

    def test_model_only_import_does_not_load_qt(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import app.models.project; "
             "assert not any(m.startswith('PySide6') for m in sys.modules)"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registered_rendering_matches_legacy_pixels_for_every_type(self) -> None:
        def render(item: SourceItem, legacy: bool) -> QImage:
            image = QImage(320, 240, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            try:
                painter.setOpacity(0.6)
                (item._paint_legacy if legacy else item.paint)(painter, None)
            finally:
                painter.end()
            return image

        for source_type in SourceType:
            with self.subTest(source_type=source_type):
                item = SourceItem(Source(source_type, "Source", width=240, height=160, opacity=0.7))
                self.assertEqual(render(item, False), render(item, True))
                # Exercise the shared bitmap path as well as empty placeholders.
                if source_type in SourceInspector.IMAGE_BACKED_TYPES:
                    item._pixmap = QPixmap(40, 40)
                    item._pixmap.fill("#369ABC")
                    self.assertEqual(render(item, False), render(item, True))
                renderer = Mock()
                with patch.object(source_registry.get(source_type), "renderer", renderer):
                    item.paint(None, None)
                renderer.assert_called_once_with(item, None, None, None)
                item.deleteLater()

    def test_registered_inspector_matches_legacy_fields_and_clears_selection(self) -> None:
        inspector = SourceInspector(SourceStore(), Translator())
        try:
            for source_type in SourceType:
                with self.subTest(source_type=source_type):
                    source = Source(source_type, "Source")
                    inspector._update_source_specific_fields(source)
                    actual = dict(inspector._field_visibility)
                    inspector._update_legacy_source_specific_fields(source)
                    self.assertEqual(actual, inspector._field_visibility)
                    editor = Mock()
                    with patch.object(source_registry.get(source_type), "inspector", editor):
                        inspector._update_source_specific_fields(source)
                    editor.assert_called_once_with(inspector, source)
            inspector._update_source_specific_fields(None)
            cleared = dict(inspector._field_visibility)
            inspector._update_legacy_source_specific_fields(None)
            self.assertEqual(cleared, inspector._field_visibility)
        finally:
            inspector.close()
            inspector.deleteLater()


if __name__ == "__main__":
    unittest.main()
