from __future__ import annotations

from dataclasses import fields, replace
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.renderers import lyrics_renderer
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_line_style_dialog import LyricsLineStyleDialog
from app.dialogs.video_source_dialog import VideoSourceDialog
from app.models.project import ProjectDocument
from app.models.source import Source, SourceType
from app.models.source_components import LyricsComponent, VideoComponent
from app.models.source_registry import source_registry
from app.services.history_service import HistoryService
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class SourceComponentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_all_components_map_to_existing_fields_without_changing_storage(self) -> None:
        for source_type in SourceType:
            with self.subTest(source_type=source_type):
                source = Source(source_type, "Source")
                saved = source.to_dict()
                component = source_registry.component_for(source)
                changes = component.to_source_changes()
                self.assertTrue(changes)
                self.assertEqual(len(changes), len(fields(component)))
                self.assertTrue(set(changes) <= set(saved))
                self.assertNotIn("id", changes)
                self.assertNotIn("source_type", changes)
                self.assertNotIn("x", changes)
                self.assertEqual(changes, {key: saved[key] for key in changes})
                for version in (1, 2):
                    document = ProjectDocument.from_dict({"version": version, "sources": [saved]})
                    self.assertEqual(source_registry.component_for(document.sources[0]), component)
                    self.assertEqual(document.to_dict()["sources"], [saved])
                self.assertEqual(source.to_dict(), saved)

    def test_component_edits_publish_once_and_preserve_common_properties(self) -> None:
        cases = {
            SourceType.IMAGE: ({"path": "image.png"}, {"content_path": "image.png"}),
            SourceType.VIDEO: ({"speed": 1.5}, {"video_speed": 1.5}),
            SourceType.TEXT: ({"text": "New title"}, {"text": "New title"}),
            SourceType.SHAPE: ({"kind": "ellipse"}, {"shape_kind": "ellipse"}),
            SourceType.PROGRESS_BAR: ({"value": 0.25}, {"progress_value": 0.25}),
            SourceType.TIME: ({"format": "%current_time%"}, {"time_format": "%current_time%"}),
            SourceType.ALBUM_COVER: ({"fit_mode": "contain"}, {"image_fit_mode": "contain"}),
            SourceType.LOGO: ({"path": "logo.png"}, {"content_path": "logo.png"}),
            SourceType.WATERMARK: ({"path": "mark.png"}, {"content_path": "mark.png"}),
            SourceType.BACKGROUND: ({"mode": "album_art"}, {"background_mode": "album_art"}),
            SourceType.AUDIO_VISUALIZER: ({"bars": 24}, {"visualizer_bars": 24}),
            SourceType.LYRICS: ({"animation": "rise"}, {"subtitle_animation": "rise"}),
            SourceType.TRACK_LIST: ({"count": 3}, {"track_list_count": 3}),
            SourceType.NOW_PLAYING: ({"duration": 4.0}, {"now_playing_duration": 4.0}),
            SourceType.AUDIO_WAVEFORM: ({"line_width": 5.0}, {"visualizer_line_width": 5.0}),
            SourceType.AUDIO_LEVEL_METER: ({"segments": 20}, {"level_meter_segments": 20}),
            SourceType.PARTICLE_OVERLAY: ({"density": 30}, {"particle_density": 30}),
        }
        self.assertEqual(set(cases), set(SourceType))
        for source_type, (changes, legacy) in cases.items():
            with self.subTest(source_type=source_type):
                store = SourceStore()
                source = Source(source_type, "Source", x=123, opacity=0.6, z_index=4)
                store.add(source)
                before = source.to_dict()
                notifications = []
                store.source_changed.connect(notifications.append)
                store.update_component(source.id, **changes)
                self.assertEqual(source.to_dict(), before | legacy)
                self.assertIs(store.get(source.id), source)
                self.assertEqual(notifications, [source])
                store.update_component(source.id, **changes)
                self.assertEqual(len(notifications), 1)

    def test_snapshots_and_list_edits_do_not_share_mutable_state(self) -> None:
        source = Source(SourceType.VIDEO, "Video", video_paths=["a.mp4"])
        snapshot = source_registry.component_for(source)
        self.assertIsInstance(snapshot, VideoComponent)
        snapshot.paths.append("draft.mp4")
        self.assertEqual(source.video_paths, ["a.mp4"])
        serialized = snapshot.to_source_changes()
        serialized["video_paths"].append("other.mp4")
        self.assertEqual(snapshot.paths, ["a.mp4", "draft.mp4"])
        source.video_speed = 2.0
        self.assertEqual(snapshot.speed, 1.0)
        self.assertEqual(source_registry.component_for(source).speed, 2.0)
        edited = replace(snapshot, speed=3.0)
        self.assertEqual(source.video_speed, 2.0)
        self.assertEqual(edited.speed, 3.0)
        store = SourceStore()
        store.add(source)
        paths = ["new.mp4"]
        store.update_component(source.id, paths=paths)
        paths.append("external.mp4")
        self.assertEqual(source.video_paths, ["new.mp4"])
        self.assertEqual(source.video_speed, 2.0)

        # A partial edit must not rewrite unrelated legacy values during validation.
        lyrics = Source(SourceType.LYRICS, "Lyrics", subtitle_animation="fade")
        store.add(lyrics)
        store.update_component(lyrics.id, font_size=32)
        self.assertEqual(lyrics.font_size, 32)
        self.assertEqual(lyrics.subtitle_animation, "fade")

    def test_invalid_edits_are_atomic_and_cannot_change_common_fields(self) -> None:
        source = Source(SourceType.VIDEO, "Video")
        store = SourceStore()
        store.add(source)
        before = source.to_dict()
        notifications = []
        store.source_changed.connect(notifications.append)
        for changes in (
            {"speed": 0}, {"speed": float("nan")},
            {"paths": [7]}, {"paths": ["draft.mp4"], "speed": 9},
            {"x": 1}, {"source_type": SourceType.TEXT}, {"not_a_field": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                store.update_component(source.id, **changes)
            self.assertEqual(source.to_dict(), before)
        store.update_component("missing", speed=2)
        self.assertEqual(notifications, [])

    def test_video_component_validation_is_independent_of_the_source_codec(self) -> None:
        video = VideoComponent.from_source(Source(SourceType.VIDEO, "Video"))
        video.validate("Video")
        for changes in (
            {"timing_mode": "bad"}, {"repeat_mode": "bad"}, {"paths": [3]},
            {"cycle_count": 0}, {"speed": float("nan")}, {"speed": True},
            {"speed": 9}, {"saturation": float("inf")}, {"random_seed": 1.5},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(video, **changes).validate("Video")
        # Legacy flat files retain inactive video properties on non-video sources.
        payload = Source(SourceType.TEXT, "Text").to_dict() | {"video_speed": 0}
        with self.assertRaises(ValueError):
            Source.from_dict(payload)

    def test_lyric_style_validation_is_independent_of_the_source_codec(self) -> None:
        styles = [{"color": "#FF0000", "font_size_offset": 4}]
        roles = {"previous": {"scale": 0.7, "opacity": 0.4, "blur": 2}}
        LyricsComponent.validate_styles(styles, roles, "Lyrics")
        for invalid_styles, invalid_roles in (
            ([{"color": "red"}], {}), ([{"font_size": 20, "font_size_offset": 2}], {}),
            ([{"italic": "true"}], {}), ([{}] * 13, {}),
            ([], {"current": {"scale": float("nan")}}), ([], {"unknown": {}}),
        ):
            with self.subTest(styles=invalid_styles, roles=invalid_roles), self.assertRaises(ValueError):
                LyricsComponent.validate_styles(invalid_styles, invalid_roles, "Lyrics")

    def test_component_edits_survive_history_and_project_round_trip(self) -> None:
        source = Source(SourceType.VIDEO, "Video", video_paths=["a.mp4"])
        store = SourceStore()
        store.add(source)
        history = HistoryService()
        history.reset(ProjectDocument(sources=store.sources()).to_dict())
        store.update_component(source.id, speed=2, paths=["b.mp4"])
        history.commit(ProjectDocument(sources=store.sources()).to_dict())
        restored = ProjectDocument.from_dict(history.undo()).sources[0]
        self.assertEqual(source_registry.component_for(restored).speed, 1)
        self.assertEqual(restored.video_paths, ["a.mp4"])
        restored = ProjectDocument.from_dict(history.redo()).sources[0]
        self.assertEqual(source_registry.component_for(restored).speed, 2)
        self.assertEqual(restored.video_paths, ["b.mp4"])

    def test_lyrics_line_styles_round_trip_and_render_independent_colors(self) -> None:
        styles = [
            {"color": "#FF0000", "font_size": 28.0, "font_weight": 700, "italic": False},
            {"color": "#0000FF", "font_size_offset": -6.0, "font_weight": 400, "italic": True},
        ]
        source = Source(
            SourceType.LYRICS, "Lyrics", width=400, height=180,
            text="FIRST LINE\nSECOND LINE", subtitle_animation="none",
            subtitle_current_line=0, subtitle_current_line_count=2,
            subtitle_current_scale=1.0, subtitle_line_styles=styles,
        )
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.subtitle_line_styles, styles)
        self.assertEqual(source_registry.component_for(restored).line_styles, styles)

        item = SourceItem(restored)
        item._subtitle_line_style_indices = (0, 1)
        image = QImage(400, 180, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            lyrics_renderer.paint_lines(item, painter, QRectF(0, 0, 400, 180))
        finally:
            painter.end()
        red = blue = 0
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                red += color.red() > 160 and color.blue() < 80 and color.alpha() > 20
                blue += color.blue() > 160 and color.red() < 80 and color.alpha() > 20
        self.assertGreater(red, 20)
        self.assertGreater(blue, 20)
        item.deleteLater()

        for invalid in (
            [{"color": "red"}], [{"font_size": 2}], [{"font_size_offset": 101}],
            [{"font_size": 20, "font_size_offset": 2}], [{"font_weight": 42}],
            [{"italic": "yes"}], [{"unknown": True}], [{}] * 13,
        ):
            payload = source.to_dict()
            payload["subtitle_line_styles"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Source.from_dict(payload)

    def test_lyrics_line_style_size_mode_preserves_the_effective_size(self) -> None:
        source = Source(
            SourceType.LYRICS, "Lyrics", font_size=24,
            subtitle_line_styles=[{}, {"font_size_offset": 6.0}],
        )
        dialog = LyricsLineStyleDialog(source, Translator())
        try:
            self.assertEqual(dialog.size_mode.currentData(), "relative")
            self.assertEqual(dialog.font_size.prefix(), "+")
            self.assertEqual(dialog._effective_size(), 30.0)

            dialog.size_mode.setCurrentIndex(dialog.size_mode.findData("absolute"))
            self.assertEqual(dialog.font_size.value(), 30.0)
            self.assertEqual(dialog.styles()[1]["font_size"], 30.0)

            dialog.size_mode.setCurrentIndex(dialog.size_mode.findData("relative"))
            self.assertEqual(dialog.font_size.value(), 6.0)
            self.assertEqual(dialog.styles()[1]["font_size_offset"], 6.0)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_video_dialog_reads_component_without_mutating_source_on_cancel(self) -> None:
        source = Source(SourceType.VIDEO, "Video", video_speed=1.5,
                        video_paths=["a.mp4"], video_repeat_mode="sequence",
                        video_cycle_count=3, video_saturation=0.5)
        before = source.to_dict()
        dialog = VideoSourceDialog(source, False)
        try:
            self.assertEqual(dialog.speed.value(), 1.5)
            self.assertEqual(dialog.cycles.value(), 3)
            self.assertEqual(dialog.saturation.value(), 0.5)
            dialog.speed.setValue(2.0)
            dialog.media_list.addItem("draft.mp4")
            self.assertEqual(dialog.values["video_speed"], 2.0)
            self.assertEqual(dialog.values["video_paths"], ["a.mp4", "draft.mp4"])
            dialog.reject()
            self.assertEqual(source.to_dict(), before)
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
