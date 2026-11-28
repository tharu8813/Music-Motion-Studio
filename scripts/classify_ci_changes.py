"""Conservative PR-only documentation classification for deployment CI.

Unknown, empty, mixed or non-PR changes always require the full Windows suite.
Inspect the actual checked-out PR merge tree against its declared base. Disable
rename detection so a code-to-docs rename cannot conceal removal of source code.
Only ordinary Markdown files under docs/ are eligible, never scripts or assets.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import subprocess

# This document has its own native GPU workflow and is not a lightweight change.
FULL_CHECK_DOCS = {"docs/architecture/rust-gpu-export-core.md"}
CONFLICT_MARKER = re.compile(r"^(?:<{7}(?: |$)|>{7}(?: |$)|={7}$|\|{7}(?: |$))", re.MULTILINE)


def is_docs_only(paths: list[str]) -> bool:
    if not paths:
        return False
    for name in paths:
        path = PurePosixPath(name)
        if (len(path.parts) < 2 or path.parts[0] != "docs"
                or path.is_absolute() or ".." in path.parts
                or "\\" in name or path.suffix != ".md"
                or name in FULL_CHECK_DOCS):
            return False
    return True


def changed_paths(root: Path, base: str) -> list[str]:
    # Accept only a full commit SHA from the event, never arbitrary rev syntax.
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        raise ValueError("Missing or invalid pull request base SHA.")
    subprocess.run(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                   cwd=root, check=True, capture_output=True)
    raw = subprocess.check_output(
        ["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD"],
        cwd=root,
    )
    return [part.decode("utf-8", errors="strict") for part in raw.split(b"\0") if part]


def validate_docs(root: Path, paths: list[str]) -> None:
    for name in paths:
        path = root / name
        if path.is_symlink():
            raise ValueError(f"Documentation symlink is not eligible: {name}")
        if not path.exists():  # A deleted Markdown file has no content to check.
            continue
        if not path.is_file():
            raise ValueError(f"Documentation path is not a file: {name}")
        content = path.read_text(encoding="utf-8")
        if "\0" in content or CONFLICT_MARKER.search(content):
            raise ValueError(f"Invalid Markdown content or conflict marker: {name}")


def classify(root: Path, event: str, base: str) -> bool:
    if event != "pull_request":
        return False
    try:
        paths = changed_paths(root, base)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Cannot establish the change set; require full CI: {error}")
        return False
    if not is_docs_only(paths):
        return False
    # A failing documentation check must fail the job, not become a success.
    validate_docs(root, paths)
    print(f"Validated {len(paths)} documentation change(s).")
    return True


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    docs_only = classify(root, os.environ.get("CI_EVENT", ""), os.environ.get("CI_PR_BASE", ""))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"docs_only={str(docs_only).lower()}\n")
    print("Documentation-only PR" if docs_only else "Full Windows verification required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
