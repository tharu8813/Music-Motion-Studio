"""CI may shorten Markdown-only PRs, but must fail closed for source changes."""
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.classify_ci_changes import changed_paths, classify, is_docs_only, validate_docs


class CIClassificationTests(unittest.TestCase):
    def test_only_docs_markdown_is_eligible(self):
        self.assertTrue(is_docs_only(["docs/README.md", "docs/architecture/note.md"]))
        for paths in ([], ["README.md"], ["docs/tool.py"], ["docs/data.json"],
                      ["docs/architecture/rust-gpu-export-core.md"],
                      ["docs/../app/code.md"], ["/docs/README.md"],
                      ["docs/README.md", "app/main.py"],
                      ["docs/README.md", ".github/workflows/deployment.yml"],
                      ["docs\\README.md"]):
            with self.subTest(paths=paths):
                self.assertFalse(is_docs_only(paths))

    def test_non_pr_and_unknown_diff_require_full_ci(self):
        for event in ("push", "workflow_dispatch", ""):
            with patch("scripts.classify_ci_changes.changed_paths") as diff:
                self.assertFalse(classify(Path("."), event, "a" * 40))
                diff.assert_not_called()
        with patch("scripts.classify_ci_changes.changed_paths",
                   side_effect=subprocess.CalledProcessError(1, "git")):
            self.assertFalse(classify(Path("."), "pull_request", "a" * 40))
        with self.assertRaises(ValueError):
            changed_paths(Path("."), "HEAD~1")

    def test_real_git_diff_includes_code_removal_on_rename(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root).decode().strip()
            git("init", "-q")
            git("config", "user.name", "CI")
            git("config", "user.email", "ci@example.invalid")
            (root / "source.py").write_text("print('hello')\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            (root / "docs").mkdir()
            (root / "source.py").rename(root / "docs" / "renamed.md")
            git("add", "-A")
            git("commit", "-qm", "rename code")
            paths = changed_paths(root, base)
            self.assertCountEqual(paths, ["source.py", "docs/renamed.md"])
            self.assertFalse(is_docs_only(paths))
            base = git("rev-parse", "HEAD")
            (root / "docs" / "renamed.md").write_text("# Updated\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "docs only")
            self.assertTrue(classify(root, "pull_request", base))

    def test_documentation_validation_does_not_silently_pass_conflicts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs").mkdir()
            file = root / "docs" / "note.md"
            file.write_text("# Heading\nNormal Markdown.  \n", encoding="utf-8")
            validate_docs(root, ["docs/note.md", "docs/deleted.md"])
            file.write_text("<<<<<<< HEAD\ncontent\n=======\nother\n>>>>>>> branch\n",
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_docs(root, ["docs/note.md"])
            file.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                validate_docs(root, ["docs/note.md"])


if __name__ == "__main__":
    unittest.main()
