from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/windows-installer-acceptance.yml"
SCRIPT = ROOT / "scripts/verify_installer_upgrade_smoke.ps1"


class PreviousInstallerUpgradeContractTests(unittest.TestCase):
    def test_published_installer_version_is_pinned_before_execution(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("/releases/download/1.4.0.1/Music.Motion.Studio-1.4.0.1-setup.exe", workflow)
        self.assertIn("4e62606f43b9c1d551cef585640ac286577154ca1f1a4e68b56b6f1edaf11563", workflow)
        self.assertIn("Get-FileHash -LiteralPath $download -Algorithm SHA256", workflow)
        self.assertIn("if ($actualSha256 -ne $expectedSha256)", workflow)
        self.assertIn("- 'scripts/verify_installer_upgrade_smoke.ps1'", workflow)
        self.assertIn("- 'tests/test_installer_upgrade_contract.py'", workflow)
        self.assertIn("-PreviousSetupPath $env:MMS_PREVIOUS_INSTALLER", workflow)
        self.assertIn("-ExpectedPreviousVersion '1.4.0.1'", workflow)
        self.assertIn("-ExpectedCurrentVersion $currentVersion", workflow)
        self.assertIn("from app import __version__", workflow)
        self.assertNotIn("/releases/download/1.4.0.0/", workflow)

    def test_upgrade_runs_only_on_disposable_runner_and_checks_preservation(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$env:CI -ne 'true'", script)
        self.assertIn("$env:RUNNER_TEMP", script)
        self.assertIn("$env:GITHUB_RUN_ID", script)
        self.assertIn("Expected published $ExpectedPreviousVersion launcher", script)
        self.assertIn("if ($previousFileVersion -ne $ExpectedPreviousVersion)", script)
        self.assertIn("if ($upgradedVersion -ne $ExpectedCurrentVersion)", script)
        self.assertIn("ValidatePattern", script)
        self.assertIn("Upgrade deleted a user-owned marker", script)
        self.assertIn("Uninstall deleted a user-owned marker", script)
        self.assertIn("verify_distribution.py", script)
        self.assertIn("Registry::HKEY_CLASSES_ROOT\\PlaylistCanvas.Project\\shell\\open\\command", script)
        self.assertIn("--smoke-test", script)


if __name__ == "__main__":
    unittest.main()
