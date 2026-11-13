import unittest
from pathlib import Path

from app.services.project_persistence_service import (
    default_project_path,
    is_legacy_project_path,
)


class ProjectPersistenceServiceTests(unittest.TestCase):
    def test_default_path_sanitizes_title_and_uses_requested_directory(self) -> None:
        path = default_project_path("  My / Playlist?  ", Path("exports"))
        self.assertEqual(path, Path("exports/My _ Playlist_.pvsproj"))

    def test_empty_title_has_stable_fallback(self) -> None:
        self.assertEqual(default_project_path("!!!", Path("exports")).name, "___.pvsproj")

    def test_windows_device_names_are_not_used_as_project_paths(self) -> None:
        for title in ("CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            with self.subTest(title=title):
                filename = default_project_path(title, Path("exports")).name
                self.assertTrue(filename.startswith("_"), filename)
                self.assertTrue(filename.endswith(".pvsproj"))

    def test_long_titles_have_bounded_stable_distinct_filenames(self) -> None:
        prefix = "가" * 130
        first = default_project_path(prefix + "first", Path("exports"))
        second = default_project_path(prefix + "second", Path("exports"))
        self.assertLessEqual(len(first.stem), 120)
        self.assertNotEqual(first, second)
        self.assertEqual(first, default_project_path(prefix + "first", Path("exports")))
        self.assertTrue(first.suffix == ".pvsproj")

    def test_legacy_format_detection_is_path_only(self) -> None:
        self.assertTrue(is_legacy_project_path(Path("old.project.json")))
        self.assertTrue(is_legacy_project_path(Path("old.JSON")))
        self.assertFalse(is_legacy_project_path(Path("new.pvsproj")))
        self.assertFalse(is_legacy_project_path(None))


if __name__ == "__main__":
    unittest.main()
