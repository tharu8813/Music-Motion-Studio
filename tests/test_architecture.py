"""Architecture rules: keep boundaries that were deliberately untangled from coming back.

These read source code only (no Qt), so they are fast and run everywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

APP = Path(__file__).resolve().parents[1] / "app"
MAIN_WINDOW = APP / "ui" / "main_window.py"


def _runtime_imports(path: Path) -> set[str]:
    """Modules imported outside ``if TYPE_CHECKING:`` blocks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    type_only: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            type_only.update(id(child) for child in ast.walk(node))
    modules = set()
    for node in ast.walk(tree):
        if id(node) in type_only:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


class ArchitectureRuleTests(unittest.TestCase):
    def test_model_and_shared_process_boundaries_do_not_import_widgets(self) -> None:
        paths = [*APP.joinpath("models").glob("*.py"), APP / "ffmpeg" / "process.py",
                 APP / "preview" / "overlay_frame_cache.py",
                 APP / "controllers" / "source_layout_controller.py",
                 APP / "services" / "project_session.py"]
        for path in paths:
            with self.subTest(path=path.name):
                imports = _runtime_imports(path)
                self.assertFalse(any(module.startswith(("app.ui", "app.dialogs", "PySide6.QtWidgets"))
                                     for module in imports), imports)

    def test_nothing_in_app_imports_main_window_at_runtime(self) -> None:
        # Controllers take the window as a parameter; importing main_window back
        # (e.g. to let tests patch "app.ui.main_window.X") couples them to it.
        offenders = [
            str(path.relative_to(APP.parent))
            for path in APP.rglob("*.py")
            if path != MAIN_WINDOW and "app.ui.main_window" in _runtime_imports(path)
        ]
        self.assertEqual(offenders, [])

    def test_qthread_teardown_lives_only_in_the_shared_helper(self) -> None:
        # The DeferredDelete sequence fixed real Windows 0xC0000409 crashes; a
        # second copy can silently drift from the verified one.
        offenders = [
            str(path.relative_to(APP.parent))
            for path in APP.rglob("*.py")
            if "DeferredDelete" in path.read_text(encoding="utf-8")
            and path.name != "qt_worker_lifecycle.py"
        ]
        self.assertEqual(offenders, [])

    def test_main_window_does_not_own_controller_runtime_state(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        for field in (
            # ExportOrchestrator.frames
            "_export_frame_staging", "_export_frame_index", "_export_capture_count",
            "_export_frame_cache", "_export_frame_metrics", "_export_png_pipeline",
            "_last_export_frame_metrics",
            # ProjectController.save_*
            "_project_save_worker", "_project_save_context", "_project_save_succeeded",
            # ProjectSession and AutosaveController own editing/worker state.
            "_project_dirty", "_project_change_serial", "_history_ready",
            "_history_restoring", "_history_applying", "_autosave_worker",
        ):
            with self.subTest(field=field):
                self.assertNotIn(f"self.{field}", source)


if __name__ == "__main__":
    unittest.main()
