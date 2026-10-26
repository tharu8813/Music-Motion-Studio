from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from app import __version__
from app.models.playlist import PlaylistTrack
from app.models.project import (
    CURRENT_VERSION, CanvasSettings, ProjectDocument, ProjectSettings, migrate_project_payload,
)
from app.models.source import SUBTITLE_ADVANCED_GROUPS, Source, SourceType


class ProjectModelValidationTests(unittest.TestCase):
    def test_legacy_subtitle_master_switch_migrates_without_losing_drafts(self) -> None:
        for enabled, categories in ((True, ["motion"]), (False, [])):
            with self.subTest(enabled=enabled):
                payload = ProjectDocument(sources=[
                    Source(SourceType.LYRICS, "Lyrics", subtitle_animation="rise"),
                    Source(SourceType.TEXT, "Title"),
                ]).to_dict()
                for source in payload["sources"]:
                    source.update(subtitle_advanced_enabled=enabled,
                                  subtitle_advanced_categories=["motion"],
                                  subtitle_advanced_settings={"subtitle_animation": "cascade"})
                original = copy.deepcopy(payload)
                restored = ProjectDocument.from_dict(payload)
                self.assertEqual(payload, original)
                for source in restored.sources:
                    self.assertEqual(source.subtitle_advanced_categories, categories)
                    self.assertEqual(source.subtitle_advanced_settings, {"subtitle_animation": "cascade"})
                    self.assertNotIn("subtitle_advanced_enabled", source.to_dict())
                self.assertEqual(restored.sources[0].resolved_lyrics().subtitle_animation,
                                 "cascade" if enabled else "rise")
                reopened = ProjectDocument.from_dict(restored.to_dict())
                self.assertEqual(reopened.to_dict(), restored.to_dict())
        legacy = Source(SourceType.LYRICS, "Lyrics").to_dict()
        legacy.pop("subtitle_advanced_categories")
        legacy["subtitle_advanced_enabled"] = True
        self.assertEqual(Source.from_dict(legacy).subtitle_advanced_categories, list(SUBTITLE_ADVANCED_GROUPS))
        legacy["subtitle_advanced_enabled"] = "false"
        with self.assertRaises(ValueError):
            Source.from_dict(legacy)

    def test_project_collections_require_arrays(self) -> None:
        for field in ("sources", "groups", "playlist", "content_library"):
            for value in ({}, "", None):
                with self.subTest(field=field, value=value):
                    payload = ProjectDocument().to_dict()
                    payload[field] = value
                    with self.assertRaises(ValueError):
                        ProjectDocument.from_dict(payload)

    def test_valid_document_round_trip(self) -> None:
        document = ProjectDocument(
            sources=[Source(SourceType.TEXT, "Title", text="%title%")],
            playlist=[PlaylistTrack(
                "song.mp3", "Song", duration_seconds=12.5,
                lyrics_timing_offset_seconds=0.35,
                cover_path="cover.png",
            )],
            canvas=CanvasSettings(width=1920, height=1080, snap_enabled=False),
        )
        restored = ProjectDocument.from_dict(document.to_dict())
        self.assertEqual(restored.canvas.width, 1920)
        self.assertFalse(restored.canvas.snap_enabled)
        self.assertEqual(restored.sources[0].text, "%title%")
        self.assertEqual(restored.playlist[0].lyrics_timing_offset_seconds, 0.35)
        self.assertEqual(restored.playlist[0].cover_path, "cover.png")
        self.assertEqual(restored.app_version, __version__)

    def test_ambient_background_controls_round_trip(self) -> None:
        source = Source(
            SourceType.BACKGROUND, "Reactive background",
            background_mode="album_art", background_ambient=True,
            background_ambient_blur=36.0, background_ambient_motion=1.4,
            background_bass_reactive=True, background_bass_strength=0.09,
        )
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.background_ambient_blur, 36.0)
        self.assertEqual(restored.background_ambient_motion, 1.4)
        self.assertTrue(restored.background_bass_reactive)
        self.assertEqual(restored.background_bass_strength, 0.09)

    def test_legacy_project_without_app_version_remains_compatible(self) -> None:
        payload = ProjectDocument().to_dict()
        payload.pop("app_version")
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.app_version, "")

    def test_transition_mode_defaults_to_none(self) -> None:
        self.assertEqual(ProjectSettings().transition_mode, "none")
        payload = ProjectDocument().to_dict()
        del payload["settings"]["transition_mode"]
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.settings.transition_mode, "none")

    def test_transition_mode_round_trips(self) -> None:
        document = ProjectDocument(settings=ProjectSettings(transition_mode="crossfade"))
        restored = ProjectDocument.from_dict(document.to_dict())
        self.assertEqual(restored.settings.transition_mode, "crossfade")

    def test_transition_mode_must_be_a_known_value(self) -> None:
        payload = ProjectDocument().to_dict()
        payload["settings"]["transition_mode"] = "fade_to_black"
        with self.assertRaises(ValueError):
            ProjectDocument.from_dict(payload)

    def test_legacy_automix_enabled_migrates_to_transition_mode(self) -> None:
        payload = ProjectDocument().to_dict()
        del payload["settings"]["transition_mode"]
        payload["settings"]["automix_enabled"] = True
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.settings.transition_mode, "automix")

    def test_legacy_automix_disabled_migrates_to_none(self) -> None:
        payload = ProjectDocument().to_dict()
        del payload["settings"]["transition_mode"]
        payload["settings"]["automix_enabled"] = False
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.settings.transition_mode, "none")

    def test_crossfade_seconds_defaults_and_round_trips(self) -> None:
        self.assertEqual(ProjectSettings().crossfade_seconds, 3.0)
        document = ProjectDocument(settings=ProjectSettings(crossfade_seconds=5.5))
        restored = ProjectDocument.from_dict(document.to_dict())
        self.assertEqual(restored.settings.crossfade_seconds, 5.5)

    def test_crossfade_seconds_is_clamped_to_a_sane_range(self) -> None:
        self.assertEqual(ProjectSettings(crossfade_seconds=0.0).crossfade_seconds, 0.5)
        self.assertEqual(ProjectSettings(crossfade_seconds=999.0).crossfade_seconds, 30.0)

    def test_crossfade_seconds_must_be_a_number(self) -> None:
        payload = ProjectDocument().to_dict()
        payload["settings"]["crossfade_seconds"] = "a lot"
        with self.assertRaises(ValueError):
            ProjectDocument.from_dict(payload)

    def test_project_lyrics_decode_literal_lrc_line_breaks(self) -> None:
        payload = ProjectDocument(
            playlist=[PlaylistTrack(
                "song.mp3", "Song", duration_seconds=10.0,
                lyrics=[{"start": 1.0, "end": 4.0, "text": r"First\nSecond"}],
            )]
        ).to_dict()
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.playlist[0].lyrics[0]["text"], "First\nSecond")

    def test_duplicate_source_ids_are_rejected(self) -> None:
        first = Source(SourceType.TEXT, "First")
        second = Source(SourceType.TEXT, "Second", id=first.id)
        payload = ProjectDocument(sources=[first, second]).to_dict()
        with self.assertRaisesRegex(ValueError, "source IDs must be unique"):
            ProjectDocument.from_dict(payload)

    def test_invalid_canvas_and_non_finite_source_are_rejected(self) -> None:
        payload = ProjectDocument().to_dict()
        payload["canvas"]["width"] = -1
        with self.assertRaisesRegex(ValueError, "Canvas dimensions"):
            ProjectDocument.from_dict(payload)

        payload = ProjectDocument(
            sources=[Source(SourceType.TEXT, "Invalid")]
        ).to_dict()
        payload["sources"][0]["x"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid numeric value"):
            ProjectDocument.from_dict(payload)

        payload = ProjectDocument(
            sources=[Source(SourceType.PARTICLE_OVERLAY, "Too many particles")]
        ).to_dict()
        payload["sources"][0]["particle_density"] = 1_000_000
        with self.assertRaisesRegex(ValueError, "particle_density"):
            ProjectDocument.from_dict(payload)

    def test_invalid_track_timing_is_rejected(self) -> None:
        payload = ProjectDocument(
            playlist=[PlaylistTrack("song.mp3", "Song")]
        ).to_dict()
        payload["playlist"][0]["start_time_seconds"] = -1
        with self.assertRaisesRegex(ValueError, "invalid start time"):
            ProjectDocument.from_dict(payload)

        payload = ProjectDocument(
            playlist=[PlaylistTrack("song.mp3", "Song")]
        ).to_dict()
        payload["playlist"][0]["lyrics_timing_offset_seconds"] = float("nan")
        with self.assertRaisesRegex(ValueError, "lyric timing offset"):
            ProjectDocument.from_dict(payload)

        payload = ProjectDocument(
            playlist=[PlaylistTrack("song.mp3", "Song")]
        ).to_dict()
        payload["playlist"][0]["cover_path"] = 123
        with self.assertRaisesRegex(ValueError, "cover paths"):
            ProjectDocument.from_dict(payload)


class ProjectMigrationTests(unittest.TestCase):
    """Saved files of every supported version, kept as fixtures in tests/data/projects."""

    @staticmethod
    def _fixture(name: str) -> dict:
        path = Path(__file__).parent / "data" / "projects" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_v1_fixture_loads_as_current_model(self) -> None:
        document = ProjectDocument.from_dict(self._fixture("v1.json"))
        self.assertEqual(document.version, CURRENT_VERSION)
        self.assertEqual(document.app_version, "")
        self.assertEqual((document.canvas.width, document.theme, document.language), (1920, "light", "en"))
        self.assertEqual(document.settings.content_mode, "reference")
        self.assertEqual(document.settings.transition_mode, "none")
        self.assertEqual(document.playlist[0].id, "track-1")

    def test_v2_legacy_automix_fixture_migrates_retired_settings(self) -> None:
        document = ProjectDocument.from_dict(self._fixture("v2_legacy_automix.json"))
        self.assertEqual(document.settings.transition_mode, "automix")
        self.assertEqual(document.settings.crossfade_seconds, 4.0)
        saved = document.to_dict()["settings"]
        self.assertNotIn("automix_enabled", saved)
        self.assertNotIn("automix_preset", saved)

    def test_migration_is_pure_and_idempotent(self) -> None:
        for name in ("v1.json", "v2_legacy_automix.json"):
            with self.subTest(fixture=name):
                original = self._fixture(name)
                untouched = copy.deepcopy(original)
                migrated = migrate_project_payload(original)
                self.assertEqual(original, untouched)
                self.assertEqual(migrate_project_payload(migrated), migrated)

    def test_explicit_transition_mode_wins_over_legacy_switch(self) -> None:
        payload = self._fixture("v2_legacy_automix.json")
        payload["settings"]["transition_mode"] = "crossfade"
        self.assertEqual(migrate_project_payload(payload)["settings"]["transition_mode"], "crossfade")

    def test_unknown_versions_and_non_objects_are_rejected(self) -> None:
        for version in (0, 3, 99, -1):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "not supported"):
                ProjectDocument.from_dict({**self._fixture("v1.json"), "version": version})
        with self.assertRaises(ValueError):
            migrate_project_payload([])

    def test_malformed_versions_are_not_coerced_into_supported_versions(self) -> None:
        for version in (True, False, 1.9, 2.0, None, [], {}, "1.9", "invalid"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "integer"):
                ProjectDocument.from_dict({"version": version})
        # Numeric strings were accepted by the legacy loader.
        for version in ("1", "2"):
            self.assertEqual(ProjectDocument.from_dict({"version": version}).version, CURRENT_VERSION)

    def test_save_load_save_is_stable(self) -> None:
        for name in ("v1.json", "v2_legacy_automix.json"):
            with self.subTest(fixture=name):
                saved = ProjectDocument.from_dict(self._fixture(name)).to_dict()
                self.assertEqual(ProjectDocument.from_dict(saved).to_dict(), saved)


if __name__ == "__main__":
    unittest.main()
