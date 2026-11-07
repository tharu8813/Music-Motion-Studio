"""Regression tests for project-based and collision-free export names."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.services.export_file_naming import available_export_path, project_export_stem


class ExportFilenameTests(unittest.TestCase):
    def test_project_title_is_used_without_losing_spaces_or_korean(self):
        self.assertEqual(project_export_stem("여름 플레이리스트"), "여름 플레이리스트")
        self.assertEqual(project_export_stem("My / Mix?"), "My _ Mix_")
        self.assertEqual(project_export_stem("Untitled Project"), "playlist")
        self.assertEqual(project_export_stem("CON"), "_CON")

    def test_number_suffix_starts_at_two_and_fills_first_gap(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            root = folder / "Project.mp4"
            self.assertEqual(available_export_path(root), root)
            root.touch()
            self.assertEqual(available_export_path(root), folder / "Project (2).mp4")
            (folder / "Project (2).mp4").touch()
            (folder / "Project (4).mp4").touch()
            self.assertEqual(available_export_path(root), folder / "Project (3).mp4")

    def test_extension_and_user_filename_are_preserved(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("Named Project.srt", "Named Project.lrc", "Custom mix.flac"):
                original = folder / name
                original.touch()
                self.assertEqual(
                    available_export_path(original),
                    original.with_name(f"{original.stem} (2){original.suffix}"),
                )


if __name__ == "__main__":
    unittest.main()
