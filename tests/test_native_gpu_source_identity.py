"""Fail-closed native GPU source identity validation.

Directly assembled in-memory ProjectDocument snapshots can bypass
ProjectDocument.from_dict's ID checks; a duplicated ID would otherwise
alias two Canvas layers in the native by-id render path.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from PySide6.QtGui import QImage

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
            valid = QImage(2, 2, QImage.Format.Format_RGB32)
            valid.fill(0xff304050)
            self.assertTrue(valid.save(background.content_path))
            self.assertEqual(
                validate_project_for_gpu_capture(
                    project, scale=0.25, source_policy="dynamic-audio",
                ), [],
            )

    def test_existing_but_unreadable_images_fail_before_native_capture(self):
        for kind in (SourceType.IMAGE, SourceType.LOGO, SourceType.WATERMARK, SourceType.BACKGROUND):
            with self.subTest(kind=kind), TemporaryDirectory(prefix="bad-canvas-asset-") as temp:
                project = project_fixture()
                path = Path(temp) / "looks-like-image.png"
                path.write_bytes(b"not actually a PNG image")
                source = Source(
                    kind, "Damaged picture", content_path=str(path),
                    **({"background_mode": "image"} if kind is SourceType.BACKGROUND else {}),
                )
                project.sources.append(source)
                with self.assertRaisesRegex(
                    TimelineValidationError, "Image asset cannot be decoded",
                ):
                    validate_project_for_gpu_capture(
                        project, scale=0.25, source_policy="dynamic-audio",
                    )

    def test_valid_png_and_svg_canvas_images_pass_preflight(self):
        with TemporaryDirectory(prefix="valid-canvas-asset-") as temp:
            png = Path(temp) / "valid.png"
            svg = Path(temp) / "valid.svg"
            image = QImage(3, 3, QImage.Format.Format_ARGB32)
            image.fill(0xff112233)
            self.assertTrue(image.save(str(png)))
            svg.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
                '<rect width="8" height="8" fill="#abcdef"/></svg>',
                encoding="utf-8",
            )
            for kind, path in ((SourceType.IMAGE, png), (SourceType.LOGO, svg)):
                with self.subTest(kind=kind):
                    project = project_fixture()
                    project.sources.append(Source(kind, "Valid", content_path=str(path)))
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
