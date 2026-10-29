from __future__ import annotations

from pathlib import Path
import re
import unittest

from app import __version__
from app.utils.subprocess_utils import hidden_process_kwargs


ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_installer_sources_only_the_packaged_distribution(self) -> None:
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        # Inno predefines SourcePath as the script directory. Reusing it as
        # an override accidentally includes the repository and virtualenv.
        self.assertNotIn("#ifndef SourcePath", installer)
        self.assertIn('#define AppSourcePath "dist\\Music Motion Studio"', installer)
        self.assertIn('Source: "{#AppSourcePath}\\*";', installer)

    def test_pyinstaller_does_not_bundle_development_ffmpeg(self) -> None:
        specification = (ROOT / "playlist_canvas.spec").read_text(encoding="utf-8")
        self.assertNotIn("bundled_ffmpeg", specification)
        self.assertIn("app_icon.ico", specification)
        self.assertIn('name="Music Motion Studio"', specification)
        self.assertIn('"sitecustomize", "usercustomize"', specification)

    def test_every_ffmpeg_subprocess_uses_hidden_window_options(self) -> None:
        paths = (
            "app/ffmpeg/managed_installer.py",
            "app/renderer/ffmpeg_renderer.py",
            "app/renderer/python_visualizer.py",
            "app/renderer/static_video_stream.py",
            "app/services/playlist_service.py",
            "app/dialogs/settings_dialog.py",
        )
        for relative_path in paths:
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            process_calls = source.count("subprocess.run(") + source.count("subprocess.Popen(")
            self.assertGreater(process_calls, 0, relative_path)
            self.assertEqual(
                source.count("hidden_process_kwargs()"), process_calls, relative_path
            )

    def test_windows_hidden_process_options_disable_console_windows(self) -> None:
        options = hidden_process_kwargs()
        self.assertIn("creationflags", options)
        self.assertIn("startupinfo", options)
        self.assertNotEqual(int(options["creationflags"]), 0)
        startup_info = options["startupinfo"]
        self.assertNotEqual(startup_info.dwFlags, 0)  # type: ignore[attr-defined]

    def test_gitignore_keeps_runtime_ffmpeg_source_tracked(self) -> None:
        ignore_file = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/ffmpeg/", ignore_file.splitlines())
        self.assertNotIn("ffmpeg/", ignore_file.splitlines())

    def test_release_icon_exists(self) -> None:
        icon = ROOT / "app" / "resources" / "app_icon.ico"
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 1_000)

    def test_project_file_icon_exists(self) -> None:
        png = ROOT / "app" / "resources" / "project_file_icon.png"
        icon = ROOT / "app" / "resources" / "project_file_icon.ico"
        self.assertTrue(png.is_file())
        self.assertGreater(png.stat().st_size, 1_000)
        self.assertTrue(icon.is_file())
        self.assertGreater(icon.stat().st_size, 1_000)

    def test_windows_icons_have_native_small_and_high_dpi_sizes(self) -> None:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        expected = {16, 20, 24, 32, 40, 48, 64, 96, 128, 256}
        for name in ("app_icon", "project_file_icon"):
            with self.subTest(name=name):
                icon = QIcon(str(ROOT / "app/resources" / f"{name}.ico"))
                self.assertEqual({size.width() for size in icon.availableSizes()}, expected)
                for size in expected:
                    self.assertFalse(icon.pixmap(size, size).isNull())

    def test_noncommercial_license_is_applied_to_repository_build_and_installer(self) -> None:
        license_path = ROOT / "LICENSE.txt"
        license_text = license_path.read_text(encoding="utf-8")
        specification = (ROOT / "playlist_canvas.spec").read_text(encoding="utf-8")
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Playlist Canvas Source-Available Noncommercial Share-Alike License 1.0", license_text)
        self.assertIn("Noncommercial Software Modification and Distribution", license_text)
        self.assertIn("Same License and Source Availability", license_text)
        self.assertIn("Ownership and Use of Output", license_text)
        self.assertIn("Attribution for Output is optional", license_text)
        self.assertIn("이 영상은 Playlist Canvas를 이용해 제작되었습니다.", license_text)
        self.assertIn("https://github.com/tharu8813/Playlist-Canvas", license_text)
        self.assertIn('project_root / "LICENSE.txt"', specification)
        self.assertIn('#define LicenseFilePath "LICENSE.txt"', installer)
        self.assertIn('Source: "LICENSE.txt"; DestDir: "{app}"', installer)
        self.assertIn("Source-Available Noncommercial Share-Alike License 1.0", readme)
        self.assertIn("선택 사항이며 라이선스 의무가 아닙니다", readme)
        self.assertIn("이 영상은 Music Motion Studio를 이용해 제작되었습니다.", readme)

    def test_release_version_uses_four_numeric_parts(self) -> None:
        parts = __version__.split(".")
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(part.isdigit() for part in parts))

    def test_windows_and_python_packaging_versions_are_synchronized(self) -> None:
        parts = ", ".join(__version__.split("."))
        version_info = (ROOT / "windows_version_info.txt").read_text(
            encoding="utf-8"
        )
        packaging = (ROOT / "PACKAGING.md").read_text(encoding="utf-8")
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        lock_file = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8")
        self.assertIn(f"filevers=({parts})", version_info)
        self.assertIn(f"prodvers=({parts})", version_info)
        self.assertIn('StringStruct("FileVersion", "' + __version__ + '")', version_info)
        self.assertIn(f'#define MyAppVersion "{__version__}"', installer)
        self.assertIn(f'#define MyAppFileVersion "{__version__}"', installer)
        self.assertIn("Python 3.14", packaging)
        self.assertIn("python314.dll", packaging)
        self.assertIn("Python 3.14", lock_file.splitlines()[0])
        self.assertNotIn("python312.dll", packaging)

    def test_ci_uses_the_documented_python_runtime(self) -> None:
        version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
        workflow = (ROOT / ".github/workflows/deployment.yml").read_text(encoding="utf-8")
        packaging = (ROOT / "PACKAGING.md").read_text(encoding="utf-8")
        lock_file = (ROOT / "requirements-lock.txt").read_text(encoding="utf-8")
        self.assertIn("python-version-file: '.python-version'", workflow)
        self.assertIn(f"Python {version}", packaging)
        self.assertIn(f"CPython {version}", lock_file)

    def test_documented_setup_names_and_badge_match_the_app_version(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        packaging = (ROOT / "PACKAGING.md").read_text(encoding="utf-8")
        setup_names = re.findall(r"Music Motion Studio-([\d.]+)-setup\.exe", readme + packaging)
        self.assertTrue(setup_names)
        self.assertEqual(set(setup_names), {__version__})
        self.assertIn(f"badge/version-{__version__}-", readme)

    def test_release_lock_pins_every_package_the_spec_bundles(self) -> None:
        specification = (ROOT / "playlist_canvas.spec").read_text(encoding="utf-8")
        declared = re.search(r"AUTOMIX_PACKAGES = \(([^)]*)\)", specification)
        self.assertIsNotNone(declared)
        modules = [*re.findall(r'"([\w.]+)"', declared.group(1)), "onnxruntime", "numpy"]
        self.assertIn("sonara", modules)  # the parse found the real list
        distributions = {"sklearn": "scikit-learn"}  # import name -> pip name
        pinned = {
            line.split("==")[0].strip().lower().replace("_", "-")
            for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        }
        missing = [
            module for module in modules
            if distributions.get(module, module).lower().replace("_", "-") not in pinned
        ]
        self.assertEqual(missing, [])

    def test_installer_registers_pvsproj_file_association(self) -> None:
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        self.assertIn("ChangesAssociations=yes", installer)
        self.assertIn('Software\\Classes\\.pvsproj', installer)
        self.assertIn('ValueData: "PlaylistCanvas.Project"', installer)
        self.assertIn('PlaylistCanvas.Project\\DefaultIcon', installer)
        self.assertIn('#define ProjectIconPath "app\\resources\\project_file_icon.ico"', installer)
        self.assertIn('DestName: "project_file_icon.ico"', installer)
        self.assertIn('ValueData: "{app}\\project_file_icon.ico,0"', installer)
        self.assertIn('PlaylistCanvas.Project\\shell\\open\\command', installer)
        self.assertIn('""%1""', installer)
        self.assertIn('Applications\\{#PreviousAppName}.exe"; Flags: deletekey', installer)

    def test_renaming_keeps_upgrade_identity_and_preserves_existing_shortcuts(self) -> None:
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        self.assertIn('{{28b780ab-d9ec-420a-88d8-17d364505722}', installer)
        self.assertIn('UsePreviousAppDir=yes', installer)
        self.assertIn('UsePreviousGroup=no', installer)
        # A previous desktop link must be remembered before InstallDelete removes it.
        self.assertIn('function InitializeSetup(): Boolean;', installer)
        self.assertIn('HadDesktopShortcut :=', installer)
        self.assertIn('Result := HadDesktopShortcut and', installer)

    def test_updates_use_program_files_and_only_restart_after_success(self) -> None:
        installer = (ROOT / "setup.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=admin", installer)
        self.assertIn(r"DefaultDirName={commonpf}\{#MyAppName}", installer)
        self.assertNotIn("UnInstallOldVersion", installer)
        self.assertNotIn("taskkill", installer)
        self.assertIn("WaitForSingleObject(ParentHandle, 120000)", installer)
        self.assertIn("Flags: nowait runasoriginaluser; Check: IsAutomaticUpdate", installer)
        self.assertIn("--resume-update", installer)
        self.assertIn("{param:UPDATEREADY|}", installer)
        self.assertIn("SaveStringToFile(ReadyPath", installer)
        self.assertNotIn('Type: filesandordirs; Name: "{app}"', installer)
        self.assertIn('Type: dirifempty; Name: "{app}"', installer)


if __name__ == "__main__":
    unittest.main()
