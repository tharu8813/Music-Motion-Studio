"""Fail-closed native GPU source identity validation.

Directly assembled in-memory ProjectDocument snapshots can bypass
ProjectDocument.from_dict's ID checks; a duplicated ID would otherwise
alias two Canvas layers in the native by-id render path.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models.source import Source, SourceType
from app.renderer.native_project_support import TimelineValidationError, validate_project_for_gpu_capture
from tests.test_native_canvas_project_probe import project_fixture


class NativeGpuSourceIdentityTests(unittest.TestCase):
    def test_duplicate_visible_ids_fail_for_every_policy_before_render(self):
        project = project_fixture()
        duplicate = Source(SourceType.PARTICLE_OVERLAY, "Duplicate reactive layer", z_index=9)
        duplicate.id = project.sources[0].id
        project.sources.append(duplicate)
        for source_policy in ("strict", "dynamic-audio", "canvas-only"):
            with self.subTest(policy=source_policy), self.assertRaisesRegex(
                TimelineValidationError, "duplicate visible source IDs",
            ):
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy=source_policy,
                )

    def test_missing_or_non_string_visible_ids_are_rejected(self):
        for bad_id in ("", "  ", None, 42):
            project = project_fixture()
            project.sources[1].id = bad_id
            with self.subTest(source_id=bad_id), self.assertRaisesRegex(
                TimelineValidationError, "non-empty visible source IDs",
            ):
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                )

    def test_missing_image_background_asset_fails_before_native_capture(self):
        project = project_fixture()
        background = Source(
            SourceType.BACKGROUND, "Wallpaper", background_mode="image",
        )
        project.sources.append(background)
        for policy in ("strict", "dynamic-audio", "canvas-only"):
            with self.subTest(policy=policy), self.assertRaisesRegex(
                TimelineValidationError, "Image asset not found",
            ):
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy=policy,
                )

        with TemporaryDirectory(prefix="native-background-asset-") as temp:
            background.content_path = str(Path(temp) / "missing.png")
            with self.assertRaisesRegex(TimelineValidationError, background.id):
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                )
            Path(background.content_path).write_bytes(b"file exists")
            self.assertEqual(
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                ), [],
            )

    def test_color_or_album_art_background_does_not_need_an_image_path(self):
        for mode in ("color", "album_art"):
            project = project_fixture()
            background = Source(SourceType.BACKGROUND, "Backdrop", background_mode=mode)
            background.content_path = ""
            project.sources.append(background)
            with self.subTest(mode=mode):
                self.assertEqual(
                    validate_project_for_gpu_capture(
                        project, scale=0.25, source_policy="dynamic-audio",
                    ), [],
                )

    def test_hidden_missing_image_background_is_not_an_export_layer(self):
        project = project_fixture()
        background = Source(SourceType.BACKGROUND, "Hidden", background_mode="image")
        background.visible = False
        project.sources.append(background)
        self.assertEqual(
            validate_project_for_gpu_capture(
                project, scale=0.25, source_policy="dynamic-audio",
            ), [],
        )

    def test_hidden_duplicate_does_not_affect_visible_export_capture(self):
        project = project_fixture()
        duplicate = Source(SourceType.VIDEO, "Hidden duplicate video")
        duplicate.id = project.sources[0].id
        duplicate.visible = False
        project.sources.append(duplicate)
        self.assertEqual(
            validate_project_for_gpu_capture(
                project, scale=0.25, source_policy="dynamic-audio",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
