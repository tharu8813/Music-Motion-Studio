"""Inspector field sections work on their own, without building the whole SourceInspector."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QPushButton  # noqa: E402

from app.inspector.editors.audio_level_meter_editor import LevelMeterSection  # noqa: E402
from app.inspector.editors.audio_visualizer_editor import VisualizerSection  # noqa: E402
from app.inspector.editors.base import TypographySection, TYPE_SPECIFIC_FIELD_KEYS  # noqa: E402
from app.inspector.editors.lyrics_editor import LyricsSection  # noqa: E402
from app.inspector.editors.now_playing_editor import NowPlayingSection  # noqa: E402
from app.inspector.editors.particle_overlay_editor import ParticleSection  # noqa: E402
from app.inspector.editors.track_list_editor import TrackListSection  # noqa: E402
from app.models.source import Source, SourceType  # noqa: E402


def _spin(minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    return spin


class LyricsSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.section = LyricsSection(_spin, QPushButton)

    def test_every_field_is_a_source_attribute_with_a_label_row_and_help(self) -> None:
        source = Source(SourceType.LYRICS, "Lyrics")
        self.assertEqual([key for key, _ in LyricsSection.ROWS], list(self.section.widgets))
        for key in self.section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, TYPE_SPECIFIC_FIELD_KEYS)
            self.assertIn(key, LyricsSection.LABELS)
            self.assertIn(key.removeprefix("subtitle_"), LyricsSection.HELP)

    def test_fill_then_edit_reports_model_updates(self) -> None:
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_animation="rise", subtitle_timing_offset=-0.5)
        self.section.fill(source, set_color=lambda _button, _value: None)
        self.assertEqual(self.section.widgets["subtitle_animation"].currentData(), "rise")
        self.assertEqual(self.section.widgets["subtitle_timing_offset"].value(), -0.5)

        updates: list[tuple[str, object]] = []
        self.section.connect(lambda key, value: updates.append((key, value)))
        self.section.widgets["subtitle_animation"].setCurrentIndex(0)
        self.section.widgets["subtitle_line_spacing"].setValue(12)
        with patch("sys.excepthook") as slot_error:
            self.section.widgets["subtitle_accent_enabled"].click()
            slot_error.assert_not_called()
        self.assertEqual(updates, [("subtitle_animation", "glow"), ("subtitle_line_spacing", 12.0),
                                  ("subtitle_accent_enabled", True)])

    def test_bindings_kinds_and_previous_line_dependency(self) -> None:
        kinds = {key: kind for key, (_path, _widget, kind) in self.section.bindings().items()}
        self.assertEqual(kinds.pop("subtitle_animation"), "combo")
        self.assertEqual(kinds.pop("subtitle_accent_enabled"), "check")
        self.assertEqual(kinds.pop("subtitle_karaoke_enabled"), "check")
        self.assertEqual(kinds.pop("subtitle_accent_color"), "color")
        self.assertEqual(kinds.pop("subtitle_karaoke_pending_mode"), "combo")
        self.assertEqual(kinds.pop("subtitle_karaoke_pending_color"), "color")
        self.assertEqual(set(kinds.values()), {"spin"})
        self.assertIsInstance(self.section.widgets["subtitle_animation"], QComboBox)

        hidden = self.section.hidden_when_off(Source(SourceType.LYRICS, "L", subtitle_context_lines=0))
        self.assertEqual(hidden, {
            "subtitle_previous_opacity": False, "subtitle_previous_blur": False,
            "subtitle_accent_color": False,
            "subtitle_accent_enabled": True,
            "subtitle_karaoke_offset": False,
            "subtitle_karaoke_pending_mode": False, "subtitle_karaoke_pending_color": False,
            "subtitle_karaoke_pending_brightness": False,
        })

    def test_subtitle_transition_shortcut_and_duration_follow_selection(self) -> None:
        from unittest.mock import patch
        from app.inspector.source_inspector import SourceInspector
        from app.services.source_store import SourceStore
        from app.ui.design_system import apply_studio_style
        from app.utils.i18n import Language, Translator

        apply_studio_style(self.app)
        translator = Translator()
        translator.set_language(Language.KOREAN)
        store = SourceStore()
        inspector = SourceInspector(store, translator)
        self.addCleanup(inspector.deleteLater)
        self.addCleanup(inspector.close)
        inspector.resize(380, 800)
        inspector.show()
        lyrics = Source(SourceType.LYRICS, "Lyrics", subtitle_animation="none")
        store.add(lyrics)
        self.app.processEvents()
        effect = inspector.lyrics.widgets["subtitle_animation"]
        duration = inspector.lyrics.widgets["subtitle_animation_duration"]
        self.assertEqual(inspector._field_categories["subtitle_animation"], "animation")
        self.assertFalse(duration.isEnabled())
        self.assertFalse(inspector.lyrics_transition_button.isHidden())
        with patch("app.inspector.source_inspector.LyricsAnimationDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = 0
            inspector.lyrics_transition_button.click()
            dialog_type.assert_called_once_with(lyrics, translator, inspector)
        with patch("app.inspector.source_inspector.LyricsAnimationDialog") as dialog_type:
            dialog_type.return_value.exec.return_value = 1
            dialog_type.return_value.settings.return_value = {
                "subtitle_flow_direction": "left",
                "subtitle_role_styles": {"next": {"color": "#55AACC", "scale": 1.2}},
            }
            inspector.lyrics_animation_button.click()
            self.assertEqual(lyrics.subtitle_flow_direction, "left")
            self.assertEqual(lyrics.subtitle_role_styles["next"]["scale"], 1.2)
        effect.setCurrentIndex(effect.findData("rise"))
        self.assertTrue(duration.isEnabled())
        duration.setValue(0.65)
        restored = Source.from_dict(lyrics.to_dict())
        self.assertEqual(restored.subtitle_animation, "rise")
        self.assertAlmostEqual(restored.subtitle_animation_duration, 0.65)
        inspector.lyrics.widgets["subtitle_karaoke_enabled"].click()
        self.assertTrue(lyrics.subtitle_karaoke_enabled)
        inspector.lyrics.widgets["subtitle_karaoke_offset"].setValue(0.2)
        restored = Source.from_dict(lyrics.to_dict())
        self.assertEqual(restored.subtitle_karaoke_offset, 0.2)
        self.assertTrue(inspector._field_visibility["subtitle_karaoke_pending_brightness"])
        pending_mode = inspector.lyrics.widgets["subtitle_karaoke_pending_mode"]
        pending_mode.setCurrentIndex(pending_mode.findData("custom"))
        self.assertEqual(lyrics.subtitle_karaoke_pending_mode, "custom")
        self.assertTrue(inspector._field_visibility["subtitle_karaoke_pending_color"])
        self.assertFalse(inspector._field_visibility["subtitle_karaoke_pending_brightness"])
        effect.setCurrentIndex(effect.findData("none"))
        store.add(Source(SourceType.TEXT, "Text"))
        self.assertFalse(inspector._field_visibility["subtitle_karaoke_enabled"])
        self.assertFalse(inspector._field_visibility["subtitle_karaoke_pending_mode"])
        self.assertTrue(inspector.lyrics_transition_button.isHidden())
        self.assertTrue(inspector._sections[("animation", "sub_transition")].isHidden())
        store.select(lyrics.id)
        self.assertFalse(duration.isEnabled())
        effect.setCurrentIndex(effect.findData("glow"))
        self.assertTrue(duration.isEnabled())
        self.assertAlmostEqual(duration.value(), 0.65)
        translator.set_language(Language.ENGLISH)
        self.assertEqual(duration.suffix(), " s")
        self.assertIn("Subtitle transition", inspector.lyrics_transition_button.text())


class TypographySectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fields_fill_edit_and_follow_their_toggles(self) -> None:
        section = TypographySection(_spin)
        source = Source(SourceType.TEXT, "T", text_case="upper", text_letter_spacing=2.5)
        self.assertEqual({key for key, _ in TypographySection.ROWS}, set(section.widgets))
        for key in section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, TypographySection.LABELS)
            self.assertIn(key.removeprefix("text_"), TypographySection.HELP)
        section.retranslate(korean=True)
        section.fill(source)
        self.assertEqual(section.widgets["text_case"].currentText(), "대문자")
        self.assertEqual(section.widgets["text_letter_spacing"].value(), 2.5)
        updates: list[tuple[str, object]] = []
        section.connect(
            lambda key, value: updates.append((key, value)),
            apply_mixed_checkbox=lambda _key, _checked: None,
        )
        section.widgets["text_italic"].click()
        self.assertEqual(updates, [("text_italic", True)])
        self.assertEqual(section.hidden_when_off(source), {
            "text_shadow_glyph": False, "text_gradient": False,
        })


class LevelMeterSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.section = LevelMeterSection(_spin, QPushButton)

    def test_every_field_is_a_source_attribute_with_a_label_row(self) -> None:
        source = Source(SourceType.AUDIO_LEVEL_METER, "Meter")
        self.assertEqual({key for key, _ in LevelMeterSection.ROWS}, set(self.section.widgets))
        for key in self.section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, LevelMeterSection.LABELS)
        kinds = {key: kind for key, (_path, _widget, kind) in self.section.bindings().items()}
        self.assertEqual(kinds["level_meter_show_peak"], "check")
        self.assertEqual(kinds["level_meter_high_color"], "color")
        self.assertEqual(kinds["level_meter_segments"], "spin")

    def test_legacy_led_mode_is_shown_as_stereo_with_led_style(self) -> None:
        colors: dict[str, str] = {}
        self.section.fill(
            Source(SourceType.AUDIO_LEVEL_METER, "Old", level_meter_mode="led", level_meter_low_color="#112233"),
            set_color=lambda button, value: colors.__setitem__(button, value),
        )
        self.assertEqual(self.section.widgets["level_meter_mode"].currentData(), "stereo")
        self.assertEqual(self.section.widgets["level_meter_style"].currentData(), "led")
        self.assertEqual(colors[self.section.widgets["level_meter_low_color"]], "#112233")

    def test_checkbox_and_colour_button_report_through_their_own_callbacks(self) -> None:
        updates: list[tuple[str, object]] = []
        mixed: list[tuple[str, bool]] = []
        picked: list[str] = []
        self.section.connect(
            lambda key, value: updates.append((key, value)),
            choose_color=lambda key, _button: picked.append(key),
            apply_mixed_checkbox=lambda key, checked: mixed.append((key, checked)),
        )
        self.section.widgets["level_meter_show_peak"].click()
        self.section.widgets["level_meter_mid_color"].click()
        self.assertEqual(updates, [("level_meter_show_peak", True)])
        self.assertEqual(mixed, [("level_meter_show_peak", True)])
        self.assertEqual(picked, ["level_meter_mid_color"])
        hidden = self.section.hidden_when_off(Source(SourceType.AUDIO_LEVEL_METER, "M", level_meter_show_peak=False))
        self.assertEqual(hidden, {"level_meter_peak_hold": False, "level_meter_peak_decay": False})


class TrackListSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.section = TrackListSection(_spin, QPushButton)

    def test_fields_bindings_and_automatic_count(self) -> None:
        source = Source(SourceType.TRACK_LIST, "List", track_list_count=0, track_list_marker="dot")
        self.assertEqual({key for key, _ in TrackListSection.ROWS}, set(self.section.widgets))
        for key in self.section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, TrackListSection.LABELS)
            self.assertIn(key.removeprefix("track_list_"), {**TrackListSection.HELP, "style": None})
        kinds = [kind for _path, _widget, kind in self.section.bindings().values()]
        self.assertEqual({kind: kinds.count(kind) for kind in set(kinds)},
                         {"combo": 3, "spin": 5, "check": 4, "color": 3})

        self.section.retranslate(korean=True)
        self.section.fill(source, set_color=lambda _button, _value: None)
        self.assertEqual(self.section.widgets["track_list_count"].text(), "자동")
        self.assertEqual(self.section.widgets["track_list_marker"].currentText(), "점 ●")
        self.assertIn("track_list_count", self.section.notes(korean=True))


class NowPlayingSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_exit_field_edits_its_differently_named_model_attribute(self) -> None:
        section = NowPlayingSection(_spin)
        source = Source(SourceType.NOW_PLAYING, "NP", now_playing_exit_animation="zoom")
        for key in section.widgets:
            self.assertTrue(hasattr(source, section.attribute(key)), key)
            self.assertIn(key, NowPlayingSection.LABELS)
        self.assertEqual(section.bindings()["now_playing_exit_animation"][2], "combo")

        section.retranslate(korean=True)
        section.fill(source)
        self.assertEqual(section.widgets["now_playing_exit"].currentText(), "줌")
        updates: list[tuple[str, object]] = []
        section.connect(lambda key, value: updates.append((key, value)))
        section.widgets["now_playing_exit"].setCurrentIndex(0)
        self.assertEqual(updates, [("now_playing_exit_animation", "fade")])


class ParticleSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fields_fill_and_colour_button(self) -> None:
        section = ParticleSection(_spin, QPushButton)
        source = Source(SourceType.PARTICLE_OVERLAY, "P", particle_style="bokeh", particle_seed=42,
                        particle_secondary_color="#112233")
        self.assertEqual({key for key, _ in ParticleSection.ROWS}, set(section.widgets))
        for key in section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, ParticleSection.LABELS)
            self.assertIn(key.removeprefix("particle_"), {**ParticleSection.HELP, "style": None})
        colors: dict[object, str] = {}
        section.retranslate(korean=True)
        section.fill(source, set_color=lambda button, value: colors.__setitem__(button, value))
        self.assertEqual(section.widgets["particle_style"].currentText(), "보케")
        self.assertEqual(section.widgets["particle_seed"].value(), 42)
        self.assertEqual(colors, {section.widgets["particle_secondary_color"]: "#112233"})


class VisualizerSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_rows_split_in_two_spots_cover_every_field_once(self) -> None:
        section = VisualizerSection(_spin)
        added: list[tuple[str, str | None]] = []

        def add_row(_form, key, _widget, section=None):
            added.append((key, section))

        section.add_rows(add_row, None, VisualizerSection.EARLY_KEYS)
        self.assertEqual([key for key, _ in added], list(VisualizerSection.EARLY_KEYS))
        section.add_rows(add_row, None, VisualizerSection.LATE_KEYS)
        self.assertEqual(added, list(VisualizerSection.ROWS))

        source = Source(SourceType.AUDIO_VISUALIZER, "V", visualizer_noise_gate=0.012)
        for key in section.widgets:
            self.assertTrue(hasattr(source, key), key)
            self.assertIn(key, VisualizerSection.LABELS)
        section.fill(source)
        self.assertEqual(section.widgets["visualizer_noise_gate"].text(), "0.012")  # 3 decimals kept
        self.assertEqual(set(section.notes(korean=False)), {"visualizer_min_level", "visualizer_curve"})


if __name__ == "__main__":
    unittest.main()
