"""Independent gaps inside timed lyrics and between cue groups."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.canvas.renderers.lyrics_renderer import lyric_cue_extent, lyric_wrapped_rows
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.inspector.editors.lyrics_editor import LyricsSection
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.lyrics_translation_service import TranslationConfig, apply_translations, display_cue
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class LyricsSpacingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def scene(self, *, translated=False, **changes):
        settings = dict(width=900, height=650, font_size=24, subtitle_current_scale=1,
            subtitle_intro_enabled=False, subtitle_intro_midtrack=False, subtitle_animation="none",
            subtitle_context_lines=1, subtitle_next_lines=1, subtitle_previous_blur=0,
            subtitle_glow_strength=0, subtitle_line_spacing=14, subtitle_cue_spacing=14,
            subtitle_role_styles={role: {"opacity": .8, "blur": 0, "scale": 1}
                for role in ("previous", "current", "next")})
        settings.update(changes)
        source = Source(SourceType.LYRICS, "Lyrics", **settings)
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        texts = ("First", "Current", "Next") if translated else (
            "First A\nFirst B", "Current A\nCurrent B", "Next A\nNext B")
        cues = [{"start": i * 2, "end": (i + 1) * 2, "text": text} for i, text in enumerate(texts)]
        if translated:
            cues = apply_translations(cues, ["이전 번역", "현재 번역", "다음 번역"], TranslationConfig("google"))
        track = PlaylistTrack("", "Lyrics", duration_seconds=6, lyrics=cues)
        return scene, item, track

    def capture(self, scene, item, track, seconds=3, scale=1):
        rows = {}
        draw = item._draw_text
        def observe(painter, rect, flags, text):
            rows[text] = painter.worldTransform().mapRect(rect)
            draw(painter, rect, flags, text)
        before = deepcopy(item.source.to_dict())
        with patch.object(item, "_draw_text", side_effect=observe):
            frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=seconds, output_scale=scale,
                playlist_tracks=[track], playlist_duration_seconds=6, timeline_seconds=seconds)
        self.assertEqual(item.source.to_dict(), before)
        return rows, frame

    def test_inner_line_and_cue_gaps_change_independently(self):
        for wrap in (False, True):
            gaps = []
            for line, cue in ((8, 32), (28, 32), (8, 62)):
                scene, item, track = self.scene(subtitle_auto_wrap=wrap,
                    subtitle_line_spacing=line, subtitle_cue_spacing=cue)
                rows, _ = self.capture(scene, item, track)
                gaps.append((rows["Current B"].center().y() - rows["Current A"].center().y(),
                    rows["Next A"].center().y() - rows["Current B"].center().y()))
            self.assertAlmostEqual(gaps[1][0] - gaps[0][0], 20)
            self.assertAlmostEqual(gaps[1][1], gaps[0][1])
            self.assertAlmostEqual(gaps[2][0], gaps[0][0])
            self.assertAlmostEqual(gaps[2][1] - gaps[0][1], 30)

    def test_original_and_translation_stay_closer_than_cue_groups(self):
        for direction in ("up", "down", "left", "right"):
            for wrap in (False, True):
                with self.subTest(direction=direction, wrap=wrap):
                    scene, item, track = self.scene(translated=True,
                        subtitle_flow_direction=direction, subtitle_auto_wrap=wrap)
                    rows, _ = self.capture(scene, item, track)
                    gap = rows["현재 번역"].center().y() - rows["Current"].center().y()
                    height = item._lyric_line_height()
                    self.assertLess(gap, height)
                    self.assertGreater(gap, height - item.source.subtitle_line_spacing)
                    if direction == "up":
                        self.assertAlmostEqual(rows["Next"].center().y() - rows["현재 번역"].center().y(), height)
                    elif direction == "down":
                        self.assertAlmostEqual(rows["Current"].center().y() - rows["다음 번역"].center().y(), height)
                    extent = lyric_cue_extent(item, display_cue(track.lyrics[1]))
                    self.assertAlmostEqual(extent * height, height + gap)

    def test_wrapped_original_and_translation_use_the_same_compact_geometry(self):
        scene, item, track = self.scene(translated=True, subtitle_auto_wrap=True,
            subtitle_wrap_to_source=False, subtitle_wrap_width=145,
            subtitle_line_spacing=24, subtitle_cue_spacing=45)
        track.lyrics[1] = apply_translations([{**track.lyrics[1], "text": "Alpha beta gamma delta epsilon"}],
            ["하나 둘 셋 넷 다섯 여섯 일곱"], TranslationConfig("google"))[0]
        rows, _ = self.capture(scene, item, track)
        original = lyric_wrapped_rows(item, track.lyrics[1]["text"], 0)
        translated = lyric_wrapped_rows(item, track.lyrics[1]["translation"]["text"], -1)
        self.assertGreater(len(original), 1)
        self.assertGreater(len(translated), 1)
        height = item._lyric_line_height()
        for parts in (original, translated):
            for first, second in zip(parts, parts[1:]):
                self.assertAlmostEqual(rows[second[0]].center().y() - rows[first[0]].center().y(),
                    height - item.source.subtitle_line_spacing)
        gap = rows[translated[0][0]].center().y() - rows[original[-1][0]].center().y()
        self.assertLess(gap, height)
        self.assertGreater(gap, height - item.source.subtitle_line_spacing)

    def test_legacy_settings_migrate_once_and_new_values_roundtrip(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_line_spacing=35)
        self.assertEqual(source.subtitle_cue_spacing, 35)
        saved = source.to_dict()
        saved.pop("subtitle_cue_spacing")
        saved["subtitle_advanced_categories"] = ["layout"]
        saved["subtitle_advanced_settings"] = {"subtitle_line_spacing": 42}
        restored = Source.from_dict(saved)
        self.assertEqual(restored.subtitle_cue_spacing, 35)
        self.assertEqual(restored.resolved_lyrics().subtitle_cue_spacing, 42)
        store = SourceStore()
        store.add(restored)
        store.update_component(restored.id, cue_spacing=60)
        self.assertEqual(restored.subtitle_line_spacing, 35)
        store.update_component(restored.id, line_spacing=7)
        self.assertEqual(restored.subtitle_cue_spacing, 60)
        self.assertEqual(Source.from_dict(restored.to_dict()).to_dict(), restored.to_dict())
        for value in (-1, 121, True, "30", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Source.from_dict(restored.to_dict() | {"subtitle_cue_spacing": value})

    def test_layout_dialog_exposes_both_values_without_editing_source(self):
        source = Source(SourceType.LYRICS, "Lyrics")
        original = source.to_dict()
        dialog = LyricsAnimationDialog(source, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.category_toggles["layout"].setChecked(True)
        dialog.controls["subtitle_line_spacing"].setValue(6)
        dialog.controls["subtitle_cue_spacing"].setValue(42)
        result = Source.from_dict(source.to_dict() | dialog.settings()).resolved_lyrics()
        self.assertEqual(result.subtitle_line_spacing, 6)
        self.assertEqual(result.subtitle_cue_spacing, 42)
        self.assertIn("subtitle_cue_spacing", LyricsSection.ADVANCED_BASIC_FIELDS["layout"])
        settings = deepcopy(dialog.settings())
        dialog.preview_multiline.setChecked(False)
        self.assertEqual(dialog.settings(), settings)
        self.assertEqual(source.to_dict(), original)

    def test_auto_context_matches_a_full_strip_at_both_gap_extremes(self):
        for line, cue in ((40, 0), (0, 80)):
            scene, item, track = self.scene(translated=True, height=260, subtitle_auto_wrap=True,
                subtitle_context_lines=-1, subtitle_next_lines=-1, subtitle_animation="glow",
                subtitle_line_spacing=line, subtitle_cue_spacing=cue)
            track.lyrics = apply_translations([
                {"start": index * 2, "end": (index + 1) * 2, "text": f"Original cue {index}"}
                for index in range(12)], [f"번역 가사 {index}" for index in range(12)], TranslationConfig("google"))
            track.duration_seconds = 24
            for seconds in (6, 6.25, 10.1):
                _, frame = self.capture(scene, item, track, seconds)
                with patch("app.preview.canvas_snapshot._effective_lyric_context",
                        side_effect=lambda graphics, cues, index: (index, len(cues) - index - 1)):
                    _, reference = self.capture(scene, item, track, seconds)
                self.assertEqual(frame, reference, (line, cue, seconds))

    def test_translated_cues_do_not_jump_and_preview_matches_export(self):
        for direction in ("up", "down"):
            for wrap in (False, True):
                scene, item, track = self.scene(translated=True, subtitle_auto_wrap=wrap,
                    subtitle_flow_direction=direction, subtitle_animation="glow",
                    subtitle_line_spacing=8, subtitle_cue_spacing=38,
                    subtitle_animation_duration=.5, subtitle_motion_easing="linear")
                capturer = ExportCanvasCapturer(scene, [track], 6, set(), [(None, None)],
                    lambda image, duration, key: RenderFrame(image, duration), lambda: None, lambda *_args: None)
                viewport = QRectF(0, 0, item.source.width, item.source.height)
                for boundary in (2, 4):
                    before, _ = self.capture(scene, item, track, boundary - .001)
                    after, frame = self.capture(scene, item, track, boundary)
                    for text, rect in before.items():
                        if rect.intersects(viewport):
                            self.assertIn(text, after)
                            self.assertAlmostEqual(rect.center().y(), after[text].center().y(), delta=.03)
                    exported = capturer.capture(ExportFrameSample(track, 1, 0, .1, boundary, boundary)).image
                    self.assertEqual(frame, exported)


if __name__ == "__main__":
    unittest.main()
