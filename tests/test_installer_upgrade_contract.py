from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/windows-installer-acceptance.yml"
SCRIPT = ROOT / "scripts/verify_installer_upgrade_smoke.ps1"


class PreviousInstallerUpgradeContractTests(unittest.TestCase):
    def test_published_installer_version_is_pinned_before_execution(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("/releases/download/1.4.0.0/Music.Motion.Studio-1.4.0.0-setup.exe", workflow)
        self.assertIn("ec41c04dbdaf507affe5dbee4385c71eef0987d513af069b43b39ed60cdacb3d", workflow)
        self.assertIn("Get-FileHash -LiteralPath $download -Algorithm SHA256", workflow)
        self.assertIn("if ($actualSha256 -ne $expectedSha256)", workflow)
        self.assertIn("- 'scripts/verify_installer_upgrade_smoke.ps1'", workflow)
        self.assertIn("- 'tests/test_installer_upgrade_contract.py'", workflow)
        self.assertIn("-PreviousSetupPath $env:MMS_PREVIOUS_INSTALLER", workflow)

    def test_upgrade_runs_only_on_disposable_runner_and_checks_preservation(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$env:CI -ne 'true'", script)
        self.assertIn("$env:RUNNER_TEMP", script)
        self.assertIn("$env:GITHUB_RUN_ID", script)
        self.assertIn("Expected published 1.4.0.0 launcher", script)
        self.assertIn("Upgrade deleted a user-owned marker", script)
        self.assertIn("Uninstall deleted a user-owned marker", script)
        self.assertIn("verify_distribution.py", script)
        self.assertIn("Registry::HKEY_CLASSES_ROOT\\PlaylistCanvas.Project\\shell\\open\\command", script)
        self.assertIn("--smoke-test", script)


if __name__ == "__main__":
    unittest.main()
