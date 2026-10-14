from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from main import open_initial_workspace, project_path_from_arguments


class ApplicationLaunchTests(unittest.TestCase):
    def test_explorer_project_argument_is_resolved_even_with_spaces(self) -> None:
        with TemporaryDirectory(prefix="playlist canvas launch ") as raw_directory:
            project = Path(raw_directory) / "My Playlist.PVSPROJ"
            project.write_bytes(b"placeholder")

            resolved = project_path_from_arguments([
                "--ignored-option", "notes.txt", str(project),
            ])

        self.assertEqual(resolved, project.resolve())

    def test_missing_or_non_project_arguments_are_ignored(self) -> None:
        self.assertIsNone(project_path_from_arguments([
            "--smoke-test", "missing.pvsproj", "cover.png",
        ]))

    def test_explicit_project_bypasses_startup_chooser_and_keeps_app_open(self) -> None:
        project = Path("Explorer project.pvsproj").resolve()
        window = MagicMock()
        window.open_project_path.return_value = False

        self.assertTrue(open_initial_workspace(window, project))
        window.open_project_path.assert_called_once_with(project)
        window.show_startup_dialog.assert_not_called()

    def test_normal_launch_still_uses_startup_chooser(self) -> None:
        window = MagicMock()
        window.show_startup_dialog.return_value = False

        self.assertFalse(open_initial_workspace(window, None))
        window.show_startup_dialog.assert_called_once_with()
        window.open_project_path.assert_not_called()

    def test_update_restart_restores_its_snapshot_without_a_chooser(self) -> None:
        window = MagicMock()
        snapshot = Path("한글 작업/update.recovery.json")
        self.assertTrue(open_initial_workspace(window, snapshot, update_recovery=snapshot))
        window._offer_recovery.assert_called_once_with(snapshot_path=snapshot, confirm=False)
        window.show_startup_dialog.assert_not_called()
        window.open_project_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
