"""Contract for mandatory Rust prerequisites in developer-only workflows."""

from __future__ import annotations

from importlib import metadata
from types import SimpleNamespace
import subprocess
import sys
import runpy
import unittest
from unittest.mock import patch

from scripts import rust_prerequisites as req


class RustPrerequisiteTests(unittest.TestCase):
    def test_missing_rustc_blocks_source_debugging_and_builds(self):
        with patch.object(req.shutil, "which", return_value=None):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "rustc"):
                req.require_rust_toolchain()

    def test_missing_cargo_is_rejected(self):
        with patch.object(req.shutil, "which", side_effect=[
            "C:/cargo/rustc.exe", None,
        ]), patch.object(req.subprocess, "run", return_value=subprocess.CompletedProcess(
            args=["rustc", "--version"], returncode=0, stdout="rustc 1.90.0", stderr="",
        )):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "cargo"):
                req.require_rust_toolchain()

    def test_rustc_failure_is_rejected(self):
        with patch.object(req.shutil, "which", return_value="rustc"), patch.object(
            req.subprocess, "run", return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="invalid toolchain"
            )
        ):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "invalid toolchain"):
                req.require_rust_toolchain()

    def test_valid_toolchain_passes(self):
        with patch.object(req.shutil, "which", side_effect=["rustc", "cargo"]), patch.object(
            req.subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["rustc"], 0, "rustc 1.90.0", ""),
                subprocess.CompletedProcess(["cargo"], 0, "cargo 1.90.0", ""),
            ],
        ):
            self.assertEqual(set(req.require_rust_toolchain()), {"cargo", "rustc"})

    def test_missing_or_outdated_native_module_blocks_developer_launch(self):
        expected = req.required_native_version()
        with patch.object(req.metadata, "version", side_effect=metadata.PackageNotFoundError()):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "setup_rust_native.py"):
                req.require_native_extension()
        with patch.object(req.metadata, "version", return_value="0.0.0"):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "does not match"):
                req.require_native_extension()
        mocked = SimpleNamespace(reduce_spectra=lambda: None, pcm_rms=lambda: None)
        with patch.object(req.metadata, "version", return_value=expected), patch.object(
            req.importlib, "import_module", return_value=mocked
        ):
            with self.assertRaisesRegex(req.RustPrerequisiteError, "pcm_waveform"):
                req.require_native_extension()

    def test_matching_wheel_and_api_pass(self):
        expected = req.required_native_version()
        mocked = SimpleNamespace(**{name: (lambda: None) for name in req.REQUIRED_NATIVE_FUNCTIONS})
        with patch.object(req.metadata, "version", return_value=expected), patch.object(
            req.importlib, "import_module", return_value=mocked
        ):
            req.require_native_extension()

    def test_main_script_exits_before_loading_qt_when_rust_is_absent(self):
        with patch.object(
            req, "require_developer_environment",
            side_effect=req.RustPrerequisiteError("rustc missing"),
        ), patch.object(sys, "frozen", False, create=True):
            with self.assertRaisesRegex(SystemExit, "rustc missing"):
                runpy.run_path(str(req.PROJECT_ROOT / "main.py"), run_name="__main__")

    def test_test_runner_returns_error_when_rust_is_absent(self):
        from scripts import run_tests
        with patch.object(
            req, "require_developer_environment",
            side_effect=req.RustPrerequisiteError("cargo missing"),
        ):
            self.assertEqual(run_tests.main(), 2)

    def test_check_can_be_used_for_toolchain_only(self):
        with patch.object(req, "require_rust_toolchain") as rust, patch.object(
            req, "require_native_extension"
        ) as native:
            req.require_developer_environment(require_native=False)
            rust.assert_called_once_with()
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
