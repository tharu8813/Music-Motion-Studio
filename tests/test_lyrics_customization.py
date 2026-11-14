"""Motion, role styling, serialization and live-preview regression checks."""

from copy import deepcopy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from app.animation.lyrics import context_style_at_distance, lyric_progress, role_style, stagger_progress
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.models.playlist import PlaylistTrack
from app.models.source import SUBTITLE_ANIMATIONS, Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportTimelinePlanner
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Language, Translator


class LyricsCustomizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def source(self, **options):
        return Source(SourceType.LYRICS, "Lyrics", width=600, height=320,
                      fill_color="#00000000", font_size=24, font_weight=400,
                      subtitle_animation_duration=0.6, subtitle_previous_blur=0,
                      subtitle_current_scale=1, subtitle_glow_strength=0,
                      subtitle_role_styles={
                          "previous": {"color": "#FF0000", "opacity": 0.8},
                          "current": {"color": "#00FF00", "opacity": 0.8},
                          "next": {"color": "#0000FF", "opacity": 0.8},
                      }, **options)

    def track(self, multiline=False):
        return PlaylistTrack("", "Track", duration_seconds=8, lyrics=[
            {"start": 0, "end": 2, "text": "Previous"},
            {"start": 2, "end": 4, "text": "Current A\nCurrent B" if multiline else "Current"},
            {"start": 4, "end": 6, "text": "Next"},
            {"start": 6, "end": 8, "text": "Last"},
        ])

    def record(self, source, seconds, multiline=False):
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        original = deepcopy(source.to_dict())
        recorded = {}
        draw = item._draw_text

        def observe(painter, rect, flags, text):
            center = painter.worldTransform().map(rect.center())
            recorded[text] = (center.x(), center.y(), painter.opacity(), painter.pen().color().name(),
                              int(painter.font().weight()), painter.font().italic())
            draw(painter, rect, flags, text)

        with patch.object(item, "_draw_text", side_effect=observe):
            frame = CanvasSnapshot.capture_track(scene, self.track(multiline), 1, 1, 0,
                                                 elapsed_seconds=seconds, transparent=True)
        self.assertFalse(frame.isNull())
        self.assertEqual(source.to_dict(), original)
        self.assertEqual(item._subtitle_transition_raw, 1)
        self.assertEqual(item._subtitle_cue_layout, ())
        return recorded, frame

    def test_directions_preserve_multiline_cue_order_and_per_line_colors(self):
        for direction in ("up", "down", "left", "right"):
            with self.subTest(direction=direction):
                rows, _ = self.record(self.source(subtitle_flow_direction=direction,
                    subtitle_line_styles=[{}, {"color": "#0000FF"}]), 2.8, True)
                self.assertLess(rows["Current A"][1], rows["Current B"][1])
                self.assertEqual(rows["Previous"][3], "#ffffff")
                self.assertEqual(rows["Current A"][3], "#ffffff")
                self.assertEqual(rows["Current B"][3], "#0000ff")
                self.assertEqual(rows["Next"][3], "#ffffff")
                axis = 0 if direction in ("left", "right") else 1
                self.assertGreater(rows["Next"][axis], rows["Previous"][axis]) if direction in ("up", "left") else (
                    self.assertLess(rows["Next"][axis], rows["Previous"][axis])
                )
                before, _ = self.record(self.source(subtitle_flow_direction=direction), 1.9999, True)
                after, _ = self.record(self.source(subtitle_flow_direction=direction), 2.0001, True)
                for text in ("Previous", "Current A", "Current B"):
                    for position in (0, 1):
                        self.assertAlmostEqual(before[text][position], after[text][position], delta=0.05)

    def test_advanced_size_and_opacity_preserve_per_line_typography_in_old_projects(self):
        basic = Source(SourceType.LYRICS, "Lyrics", width=600, height=320,
                       fill_color="#00000000", font_size=24, font_weight=400,
                       subtitle_animation="none", subtitle_accent_enabled=False,
                       subtitle_line_styles=[{}, {"color": "#0000FF", "font_weight": 700,
                                                  "italic": True, "font_size_offset": -4}])
        old_role = {"color": "#FF0000", "font_weight": 900, "italic": False,
                    "font_size": 90, "scale": 1.2, "opacity": 0.8, "blur": 0}
        source = Source.from_dict(basic.to_dict() | {
            "subtitle_advanced_categories": ["styles"],
            "subtitle_advanced_settings": {"subtitle_role_styles": {
                role: dict(old_role) for role in ("previous", "current", "next")}},
        })
        rows, _ = self.record(source, 2.8, True)
        self.assertEqual(rows["Current A"][3:6], ("#ffffff", 400, False))
        self.assertEqual(rows["Current B"][3:6], ("#0000ff", 700, True))
        self.assertAlmostEqual(rows["Current B"][2], 0.8)
        for role, style in source.resolved_lyrics().subtitle_role_styles.items():
            self.assertEqual(style, {"scale": 1.2, "opacity": 0.8, "blur": 0}, role)
        from app.canvas.renderers.lyrics_renderer import _line_style
        for role in ("previous", "current", "next"):
            style = _line_style(source.resolved_lyrics(), role, source.subtitle_line_styles[1])
            self.assertAlmostEqual(style["size"], 24)
            self.assertEqual(style["color"], "#0000FF")
            self.assertEqual(style["font_weight"], 700)
            self.assertTrue(style["italic"])

    def test_effects_have_different_motion_and_all_animate(self):
        glow = self.source(subtitle_animation="glow")
        rise = self.source(subtitle_animation="rise")
        self.assertNotEqual(lyric_progress(glow, 0.25), lyric_progress(rise, 0.25))
        glow_rows, _ = self.record(glow, 2.15)
        rise_rows, _ = self.record(rise, 2.15)
        self.assertNotAlmostEqual(glow_rows["Current"][1], rise_rows["Current"][1])
        for effect in SUBTITLE_ANIMATIONS:
            with self.subTest(effect=effect):
                source = self.source(subtitle_animation=effect)
                _, first = self.record(source, 2.12)
                _, last = self.record(source, 2.7)
                if effect == "none":
                    self.assertEqual(first, last)
                else:
                    self.assertNotEqual(first, last)

    def test_stagger_order_moves_cues_at_different_times_and_settles(self):
        source = self.source(subtitle_animation="cascade", subtitle_motion_easing="linear",
                             subtitle_stagger=0.6, subtitle_next_lines=2)
        self.assertGreater(stagger_progress(source, 0.4, 2, 2, 3), stagger_progress(source, 0.4, 3, 2, 3))
        near_first, _ = self.record(source, 2.24)
        source.subtitle_stagger_order = "far_first"
        far_first, _ = self.record(source, 2.24)
        self.assertNotAlmostEqual(near_first["Next"][1], far_first["Next"][1])
        for text in ("Previous", "Current"):
            self.assertEqual(near_first[text], far_first[text])
        source.subtitle_stagger = 0
        together, _ = self.record(source, 2.24)
        for text in ("Previous", "Current"):
            self.assertEqual(near_first[text], together[text])
        source.subtitle_stagger = 0.6
        for slot in (2, 3):
            self.assertEqual(stagger_progress(source, 0, slot, 2, 3), 0)
            self.assertEqual(stagger_progress(source, 1, slot, 2, 3), 1)
        _, finished = self.record(source, 2.6)
        _, settled = self.record(source, 2.8)
        self.assertEqual(finished, settled)

    def test_context_cues_fade_and_blur_by_distance_in_every_direction(self):
        source = self.source(subtitle_animation="none", subtitle_context_lines=3, subtitle_next_lines=3)
        for role in ("previous", "next"):
            source.subtitle_role_styles[role].update(opacity=0.2, blur=6)
        source.subtitle_role_styles["current"]["opacity"] = 1
        previous, current = role_style(source, "previous"), role_style(source, "current")
        near = context_style_at_distance(previous, current, 1, 3, source.subtitle_previous_distance_fade)
        far = context_style_at_distance(previous, current, 3, 3, source.subtitle_previous_distance_fade)
        self.assertAlmostEqual(near["blur"], 2)
        self.assertGreater(near["opacity"], far["opacity"])
        at_current = context_style_at_distance(previous, current, 0, 3, source.subtitle_previous_distance_fade)
        self.assertEqual(at_current["blur"], 0)
        self.assertEqual(at_current["opacity"], 1)
        track = PlaylistTrack("", "Track", duration_seconds=16, lyrics=[
            {"start": index * 2, "end": (index + 1) * 2, "text": f"Cue {index}"}
            for index in range(8)
        ])
        raster = SourceItem._lyric_blur_pixmap
        for direction in ("up", "down", "left", "right"):
            with self.subTest(direction=direction):
                source.subtitle_flow_direction = direction
                radii = {}

                def observe(item, line, color, radius, *args):
                    radii[line] = radius
                    return raster(item, line, color, radius, *args)

                with patch.object(self, "track", return_value=track), patch.object(
                    SourceItem, "_lyric_blur_pixmap", autospec=True, side_effect=observe,
                ):
                    _, gradient = self.record(source, 6.8)
                    for cue, radius in ((2, 2), (1, 4), (0, 6), (4, 2), (5, 4), (6, 6)):
                        self.assertAlmostEqual(radii[f"Cue {cue}"], radius)
                    source.subtitle_previous_distance_fade = 0
                    _, uniform = self.record(source, 6.8)
                    self.assertNotEqual(gradient, uniform)
                    self.assertTrue(all(radii[f"Cue {cue}"] == 6 for cue in (0, 1, 2)))
                    self.assertAlmostEqual(radii["Cue 4"], 2)
                    source.subtitle_previous_distance_fade = 1
                    source.subtitle_next_distance_fade = 0
                    _, next_uniform = self.record(source, 6.8)
                    self.assertNotEqual(gradient, next_uniform)
                    self.assertTrue(all(radii[f"Cue {cue}"] == 6 for cue in (4, 5, 6)))
                    self.assertAlmostEqual(radii["Cue 2"], 2)
                    source.subtitle_next_distance_fade = 1

    def test_blur_preserves_fractional_radii_without_a_sharp_duplicate(self):
        peaks = []
        for radius in (0.6, 0.8, 1.0):
            pixmap, margin = SourceItem._blurred_raster(
                lambda painter: painter.fillRect(QRectF(16, 16, 1, 1), QColor("white")),
                QSizeF(32, 32), radius, 1,
            )
            peaks.append(pixmap.toImage().pixelColor(16 + int(margin), 16 + int(margin)).alpha())
        self.assertGreater(peaks[0], peaks[1])
        self.assertGreater(peaks[1], peaks[2])
        item = SourceItem(self.source())
        flags = Qt.AlignmentFlag.AlignCenter
        first, _ = item._lyric_blur_pixmap("Blur", QColor("white"), 0.6, 200, 60, 1, flags)
        second, _ = item._lyric_blur_pixmap("Blur", QColor("white"), 0.8, 200, 60, 1, flags)
        self.assertNotEqual(first.cacheKey(), second.cacheKey())
        source = self.source()
        source.subtitle_role_styles["previous"]["blur"] = 2
        source.subtitle_role_styles["next"]["blur"] = 2
        rows, frame = self.record(source, 2.8)
        self.assertEqual(set(rows), {"Current"})
        self.assertFalse(frame.isNull())

    def test_solid_lyric_blur_matches_rgba_blur_at_fractional_and_hidpi_radii(self):
        import numpy as np
        from app.video.frame_filter import _rgb_pixels

        for color in (QColor("white"), QColor("#C54789"), QColor(83, 162, 215, 120)):
            for radius, ratio in ((0.6, 1), (3.25, 1), (5, 2)):
                with self.subTest(color=color.name(), alpha=color.alpha(), radius=radius, ratio=ratio):
                    def paint(painter):
                        painter.setPen(color)
                        painter.drawText(QRectF(0, 0, 180, 60), Qt.AlignmentFlag.AlignCenter, "가사 Blur")

                    reference, margin = SourceItem._blurred_raster(paint, QSizeF(180, 60), radius, ratio)
                    optimized, optimized_margin = SourceItem._blurred_raster(
                        paint, QSizeF(180, 60), radius, ratio, solid_color=color,
                    )
                    self.assertEqual(margin, optimized_margin)
                    before, after = reference.toImage(), optimized.toImage()
                    expected = _rgb_pixels(before).astype(np.int16)
                    actual = _rgb_pixels(after).astype(np.int16)
                    self.assertTrue(np.array_equal(expected[:, :, 3], actual[:, :, 3]))
                    self.assertLessEqual(int(np.abs(expected - actual).max()), 1)

    def test_missing_lyrics_message_does_not_inherit_cue_blur_or_opacity(self):
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=320,
                        fill_color="#00000000", font_size=24, font_weight=400)
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("", "Instrumental", duration_seconds=8)
        roles = {role: {"blur": 8, "opacity": 0.2, "color": "#FF0000"}
                 for role in ("previous", "current", "next")}
        for fallback in (source.subtitle_fallback, "Instrumental track\nNo lyrics"):
            with self.subTest(fallback=fallback):
                source.subtitle_fallback = fallback
                source.subtitle_advanced_categories = []
                reference = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=3)
                source.subtitle_advanced_categories = ["styles"]
                source.subtitle_advanced_settings = {"subtitle_role_styles": roles}
                original = deepcopy(source.to_dict())
                with patch.object(item, "_lyric_blur_pixmap", wraps=item._lyric_blur_pixmap) as blur:
                    preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=3)
                    blur.assert_not_called()
                self.assertEqual(preview, reference)
                self.assertEqual(source.to_dict(), original)
                sample = min(ExportTimelinePlanner.build([track], [source], 30),
                             key=lambda sample: abs(sample.elapsed_seconds - 3))
                capturer = ExportCanvasCapturer(scene, [track], 8, set(), [(None, None)],
                    lambda image, duration, _key: (image, duration), lambda: None, lambda *_: None)
                self.assertEqual(capturer.capture_stream(sample, "base")[0], preview)
                self.assertEqual(source.to_dict(), original)
        # The same wording in a real timed cue still receives its lyric style,
        # including upcoming blur before the cue starts.
        track.lyrics = [{"start": 4, "end": 8, "text": source.subtitle_fallback}]
        for elapsed in (1, 6):
            with patch.object(item, "_lyric_blur_pixmap", wraps=item._lyric_blur_pixmap) as blur:
                CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=elapsed)
                self.assertTrue(blur.called)

    def test_settings_roundtrip_validation_and_export_motion_frames(self):
        source = self.source(subtitle_animation="cascade", subtitle_flow_direction="right")
        self.assertEqual(Source.from_dict(source.to_dict()).to_dict(), source.to_dict())
        for key, invalid in (("subtitle_flow_direction", "diagonal"), ("subtitle_motion_easing", "invalid"),
                             ("subtitle_anchor", 2), ("subtitle_stagger", float("nan")),
                             ("subtitle_previous_distance_fade", -0.1),
                             ("subtitle_previous_distance_fade", float("nan")),
                             ("subtitle_next_distance_fade", 1.1),
                             ("subtitle_next_distance_fade", float("nan")),
                             ("subtitle_role_styles", {"next": {"opacity": 2}}),
                             ("subtitle_role_styles", {"next": {"scale": True}}),
                             ("subtitle_role_styles", {"next": {"unknown": 1}})):
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: invalid})
        track = self.track()
        samples = ExportTimelinePlanner.build([track], [source], 30)
        moving = [sample for sample in samples if 2 <= sample.elapsed_seconds < 2.6]
        self.assertGreaterEqual(len(moving), 15)
        scene = CanvasScene()
        scene.set_artboard_size(600, 320)
        scene.addItem(SourceItem(source))
        capturer = ExportCanvasCapturer(
            scene, [track], 8, {source.id}, [(None, None)],
            lambda *_args: None, lambda: None, lambda *_args: None,
        )
        first_key = capturer._source_state_key(source, moving[1])
        last_key = capturer._source_state_key(source, moving[-2])
        self.assertNotEqual(first_key, last_key)

    def test_live_editor_keeps_changes_in_draft_and_stops_on_cancel(self):
        translator = Translator()
        translator.set_language(Language.KOREAN)
        source = self.source()
        before = source.to_dict()
        dialog = LyricsAnimationDialog(source, translator)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog._timer.isActive())
        self.assertFalse(dialog.controls["subtitle_animation"].isEnabled())
        for category in dialog.category_toggles.values():
            category.setChecked(True)
        dialog.preview_count.setValue(7)
        advanced = dialog.settings()["subtitle_advanced_settings"]
        self.assertEqual(advanced["subtitle_context_lines"], source.subtitle_context_lines)
        self.assertEqual(advanced["subtitle_next_lines"], source.subtitle_next_lines)
        self.assertEqual(dialog._preview_source.subtitle_context_lines, 3)
        self.assertEqual(dialog._preview_source.subtitle_next_lines, 3)
        self.assertGreater(dialog._scene.artboard_rect.height(), 300)
        self.assertIn("♪", dialog._preview_texts[len(dialog._preview_texts) // 2])
        dialog.controls["subtitle_previous_distance_fade"].setValue(0.65)
        dialog.controls["subtitle_animation"].setCurrentIndex(dialog.controls["subtitle_animation"].findData("cascade"))
        dialog.controls["subtitle_flow_direction"].setCurrentIndex(dialog.controls["subtitle_flow_direction"].findData("left"))
        dialog.role.setCurrentIndex(dialog.role.findData("next"))
        self.assertTrue(dialog.controls["subtitle_previous_distance_fade"].isHidden())
        self.assertFalse(dialog.controls["subtitle_next_distance_fade"].isHidden())
        dialog.controls["subtitle_next_distance_fade"].setValue(0.35)
        dialog.role_scale.setValue(1.4)
        dialog.role_opacity.setValue(0.4)
        dialog.role_blur.setValue(2)
        dialog._render_preview(0.3)
        self.assertFalse(dialog.preview.pixmap().isNull())
        values = dialog.settings()["subtitle_advanced_settings"]
        self.assertEqual(values["subtitle_previous_distance_fade"], 0.65)
        self.assertEqual(values["subtitle_next_distance_fade"], 0.35)
        self.assertEqual(values["subtitle_role_styles"]["next"]["scale"], 1.4)
        self.assertEqual(values["subtitle_role_styles"]["next"], {"scale": 1.4, "opacity": 0.4, "blur": 2})
        Source.from_dict(source.to_dict() | dialog.settings())
        dialog.reject()
        self.assertFalse(dialog._timer.isActive())
        self.assertEqual(source.to_dict(), before)

    def test_advanced_categories_preserve_basics_and_match_rendered_effective_values(self):
        source = self.source(subtitle_animation="none")
        dialog = LyricsAnimationDialog(source, Translator())
        self.addCleanup(dialog.close)
        dialog.category_toggles["motion"].setChecked(True)
        dialog.category_toggles["layout"].setChecked(True)
        dialog.controls["subtitle_animation"].setCurrentIndex(dialog.controls["subtitle_animation"].findData("zoom"))
        dialog.controls["subtitle_animation_duration"].setValue(1.5)
        dialog.controls["subtitle_next_lines"].setValue(4)
        dialog.category_toggles["layout"].setChecked(False)
        self.assertFalse(hasattr(dialog, "advanced_enabled"))
        self.assertEqual(dialog.settings()["subtitle_advanced_categories"], ["motion"])
        self.assertTrue(dialog.controls["subtitle_animation_duration"].isEnabled())
        self.assertFalse(dialog.controls["subtitle_next_lines"].isEnabled())
        saved = Source.from_dict(source.to_dict() | dialog.settings())
        effective = saved.resolved_lyrics()
        self.assertEqual(saved.subtitle_animation, "none")
        self.assertEqual(effective.subtitle_animation, "zoom")
        self.assertEqual(effective.subtitle_animation_duration, 1.5)
        self.assertEqual(effective.subtitle_next_lines, source.subtitle_next_lines)
        _, advanced_frame = self.record(saved, 2.4)
        _, effective_frame = self.record(effective, 2.4)
        self.assertEqual(advanced_frame, effective_frame)
        samples = ExportTimelinePlanner.build([self.track()], [saved], 30)
        transition = [sample for sample in samples if 2.1 <= sample.elapsed_seconds <= 3.4]
        self.assertGreater(len(transition), 30)
        scene = CanvasScene(); scene.addItem(SourceItem(saved))
        capturer = ExportCanvasCapturer(scene, [self.track()], 8, set(), [(None, None)],
                                       lambda *_: None, lambda: None, lambda *_: None)
        self.assertNotEqual(capturer._source_state_key(saved, transition[0]),
                            capturer._source_state_key(saved, transition[-1]))
        item = scene.items()[0]
        with patch("app.preview.canvas_snapshot.resolve_lyrics_cue_state", side_effect=RuntimeError("evaluation failed")):
            with self.assertRaises(RuntimeError):
                CanvasSnapshot.capture_track(scene, self.track(), 1, 1, 0, elapsed_seconds=2.4)
        self.assertIs(item.source, saved)
        restored = Source.from_dict(saved.to_dict())
        restored.subtitle_advanced_categories = []
        self.assertIs(restored.resolved_lyrics(), restored)
        self.assertEqual(restored.subtitle_animation, "none")
        reopened = LyricsAnimationDialog(restored, Translator())
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.controls["subtitle_animation_duration"].value(), 1.5)
        self.assertFalse(reopened.category_toggles["layout"].isChecked())
        legacy = source.to_dict()
        legacy.pop("subtitle_advanced_categories")
        legacy.pop("subtitle_advanced_settings")
        migrated = Source.from_dict(legacy)
        self.assertEqual(migrated.subtitle_advanced_categories, ["styles"])
        self.assertEqual(migrated.subtitle_advanced_settings["subtitle_role_styles"], source.subtitle_role_styles)
        large = Source.from_dict(source.to_dict() | {"subtitle_advanced_categories": ["styles"],
                                "subtitle_advanced_settings": {"subtitle_role_styles": {"current": {"scale": 3}}}})
        Source.from_dict(large.resolved_lyrics().to_dict())
        for key, bad in (("subtitle_advanced_categories", "motion"),
                         ("subtitle_advanced_categories", ["unknown"]),
                         ("subtitle_advanced_settings", {"width": 1}),
                         ("subtitle_advanced_settings", {"subtitle_animation_duration": float("nan")}),
                         ("subtitle_advanced_settings", {"subtitle_role_styles": {"next": {"blur": 99}}})):
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: bad})

    def test_advanced_inspector_locks_only_owned_categories_and_restores_on_disable(self):
        from app.inspector.source_inspector import SourceInspector
        from app.services.source_store import SourceStore
        store, translator = SourceStore(), Translator()
        translator.set_language(Language.KOREAN)
        inspector = SourceInspector(store, translator)
        self.addCleanup(inspector.close)
        source = self.source(subtitle_animation="rise")
        store.add(source)
        store.update(source.id, subtitle_advanced_categories=["motion"],
                     subtitle_advanced_settings={"subtitle_animation": "zoom", "subtitle_animation_duration": 1.5})
        self.assertFalse(inspector.lyrics.widgets["subtitle_animation"].isEnabled())
        self.assertFalse(inspector.lyrics.widgets["subtitle_animation_duration"].isEnabled())
        self.assertTrue(inspector.lyrics.widgets["subtitle_next_lines"].isEnabled())
        self.assertIn("속성 탭에서 설정할 수 없습니다", inspector.lyrics_mode_note.text())
        self.assertIn("고급 모드", inspector.lyrics.widgets["subtitle_animation"].toolTip())
        self.assertEqual(inspector.lyrics.widgets["subtitle_animation"].currentData(), "rise")
        inspector._update("subtitle_animation_duration", 0.1)
        self.assertEqual(source.subtitle_animation_duration, 0.6)
        store.update(source.id, subtitle_advanced_categories=["styles", "layout"])
        self.assertTrue(inspector.lyrics.widgets["subtitle_animation"].isEnabled())
        self.assertFalse(inspector.lyrics.widgets["subtitle_previous_opacity"].isEnabled())
        self.assertNotIn("subtitle_previous_blur", inspector.lyrics.widgets)
        self.assertFalse(inspector.text_alignment_combo.isEnabled())
        self.assertTrue(inspector.font_size_spin.isEnabled())
        translator.set_language(Language.ENGLISH)
        self.assertIn("Advanced mode", inspector.lyrics_mode_note.text())
        store.update(source.id, subtitle_advanced_categories=[])
        self.assertTrue(inspector.lyrics.widgets["subtitle_previous_opacity"].isEnabled())
        self.assertTrue(inspector.text_alignment_combo.isEnabled())
        self.assertEqual(source.subtitle_animation_duration, 0.6)

    def test_inactive_advanced_drafts_do_not_change_preview_or_export(self):
        basic = Source(SourceType.LYRICS, "Lyrics", width=600, height=320,
                       fill_color="#00000000", font_size=24, subtitle_animation="none",
                       subtitle_intro_enabled=False, subtitle_intro_midtrack=False)
        draft = {
            "subtitle_animation": "cascade", "subtitle_animation_duration": 2,
            "subtitle_flow_direction": "right", "subtitle_context_lines": 4,
            "subtitle_next_lines": 4, "subtitle_anchor": 0.1,
            "subtitle_role_styles": {"current": {"color": "#FF0000", "scale": 2, "blur": 5}},
            "subtitle_intro_enabled": True, "subtitle_intro_midtrack": True,
        }
        saved = Source.from_dict(basic.to_dict() | {"subtitle_advanced_settings": draft})
        self.assertIs(saved.resolved_lyrics(), saved)
        dialog = LyricsAnimationDialog(saved, Translator())
        self.addCleanup(dialog.close)
        self.assertFalse(any(toggle.isChecked() for toggle in dialog.category_toggles.values()))
        for key in dialog.SETTINGS:
            self.assertEqual(getattr(dialog._preview_source, key), getattr(basic, key), key)
        for toggle in dialog.category_toggles.values():
            toggle.setChecked(True)
        for toggle in dialog.category_toggles.values():
            toggle.setChecked(False)
        saved = Source.from_dict(saved.to_dict() | dialog.settings())
        for key in dialog.SETTINGS:
            self.assertEqual(getattr(saved.resolved_lyrics(), key), getattr(basic, key), key)
            self.assertEqual(getattr(dialog._preview_source, key), getattr(basic, key), key)
        self.assertEqual(self.record(saved, 2.4)[1], self.record(basic, 2.4)[1])
        track = self.track()
        self.assertEqual(ExportTimelinePlanner.build([track], [saved], 30),
                         ExportTimelinePlanner.build([track], [basic], 30))
        self.assertEqual(saved.subtitle_advanced_settings["subtitle_animation"], draft["subtitle_animation"])

    def test_transition_options_have_localized_tooltips_and_category_state(self):
        for language in (Language.KOREAN, Language.ENGLISH):
            translator = Translator(); translator.set_language(language)
            dialog = LyricsAnimationDialog(self.source(), translator)
            self.addCleanup(dialog.close)
            for key, widget in dialog.controls.items():
                self.assertTrue(widget.toolTip(), key)
                self.assertTrue(widget.accessibleDescription(), key)
            for widget in (dialog.role, dialog.role_scale, dialog.role_opacity, dialog.role_blur,
                           dialog.preview_count, dialog.intro_preview_mode,
                           *dialog.category_toggles.values()):
                self.assertTrue(widget.toolTip())
            for category in dialog.category_toggles.values():
                category.setChecked(False)
            reopened = LyricsAnimationDialog(Source.from_dict(self.source().to_dict() | dialog.settings()), translator)
            self.addCleanup(reopened.close)
            self.assertFalse(any(toggle.isChecked() for toggle in reopened.category_toggles.values()))

    def test_blur_tab_saves_effects_and_non_intro_tabs_share_sample_clock(self):
        source = self.source()
        dialog = LyricsAnimationDialog(source, Translator())
        self.addCleanup(dialog.close)
        edge = dialog.controls["subtitle_karaoke_edge_blur"]
        self.assertEqual((edge.value(), edge.maximum()), (15, 50))
        edge.setValue(50)
        blur_mode = dialog.controls["subtitle_karaoke_blur_mode"]
        blur_mode.setCurrentIndex(blur_mode.findData("long"))
        self.assertTrue(dialog.controls["subtitle_karaoke_blur_min_duration"].isEnabled())
        for name, default, value in (("fade_in", 0.10, 0.4), ("fade_out", 0.32, 0.75)):
            control = dialog.controls[f"subtitle_karaoke_glow_{name}"]
            self.assertEqual(control.value(), default)
            control.setValue(value)
        settings = dialog.settings()
        restored = Source.from_dict(source.to_dict() | settings)
        self.assertEqual(restored.subtitle_karaoke_edge_blur, 50)
        self.assertEqual(restored.subtitle_karaoke_blur_mode, "long")
        self.assertEqual(restored.subtitle_karaoke_glow_fade_in, 0.4)
        self.assertEqual(restored.subtitle_karaoke_glow_fade_out, 0.75)
        dialog.preview_karaoke.setChecked(True)
        dialog.preview_word_duration.setValue(1.2)
        track, _ = dialog._preview_track()
        self.assertEqual(track.lyrics[0]["format"], "ttml")
        self.assertAlmostEqual(track.lyrics[0]["words"][0]["end"], 1.2)
        dialog.preview_unit.setCurrentIndex(dialog.preview_unit.findData("character"))
        dialog.preview_character_duration.setValue(0.4)
        track, _ = dialog._preview_track()
        self.assertAlmostEqual(track.lyrics[0]["words"][0]["end"], 0.4)
        self.assertEqual(dialog.settings(), settings)
        with patch.object(dialog, "_render_preview", wraps=dialog._render_preview) as render:
            dialog.tabs.setCurrentIndex(1)
            dialog.tabs.setCurrentIndex(dialog.tabs.count() - 1)
            render.assert_not_called()
            dialog.tabs.setCurrentIndex(dialog._intro_tab)
            self.assertTrue(dialog.preview_timing.isHidden())
            dialog.tabs.setCurrentIndex(0)
            self.assertFalse(dialog.preview_timing.isHidden())
            self.assertEqual(render.call_count, 2)


if __name__ == "__main__":
    unittest.main()
