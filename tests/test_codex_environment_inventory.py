import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

spec = importlib.util.spec_from_file_location("inventory", Path(__file__).parents[1] / "scripts/check_codex_environment.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InventoryTests(unittest.TestCase):
    def test_preserves_disabled_and_never_exports_secrets_or_mutates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills/hatch-pet/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("instructions", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(f'''[[skills.config]]
path = '{skill.as_posix()}'
enabled = false
[plugins.disabled]
enabled = false
[mcp_servers.example]
command = "DO_NOT_EXECUTE"
[mcp_servers.example.env]
TOKEN = "SECRET_VALUE"
''', encoding="utf-8")
            before = config.read_bytes()
            first = module.inventory(root, root / "missing")
            self.assertEqual(first, module.inventory(root, root / "missing"))
            self.assertEqual(before, config.read_bytes())
            self.assertTrue(first["skills"][0]["explicitly_disabled"])
            self.assertFalse(first["plugins"][0]["enabled"])
            self.assertIsNone(first["mcp_servers"][0]["enabled"])
            for secret in ("SECRET_VALUE", "TOKEN", "DO_NOT_EXECUTE"):
                self.assertNotIn(secret, json.dumps(first))

    def test_missing_config_is_not_runtime_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = module.inventory(Path(directory), Path(directory) / "missing")
            self.assertFalse(result["config_present"])
            self.assertEqual(result["skills"], [])
            self.assertIn("not verified", result["scope"])


if __name__ == "__main__":
    unittest.main()
