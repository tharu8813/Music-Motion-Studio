"""Read-only inventory; never starts Codex/MCP or reads authentication files."""

import argparse
import json
import os
from pathlib import Path
import tomllib


def inventory(codex_home, agents_skills):
    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    disabled = {
        Path(row["path"]).resolve()
        for row in config.get("skills", {}).get("config", [])
        if row.get("enabled") is False and isinstance(row.get("path"), str)
    }
    roots = [codex_home / "skills", codex_home / "skills" / ".system", agents_skills]
    skills = []
    for root in roots:
        if root.is_dir():
            for skill in sorted(root.glob("*/SKILL.md")):
                skills.append({"name": skill.parent.name, "source": str(root),
                               "explicitly_disabled": skill.resolve() in disabled,
                               "runtime_exposure": "not_checked"})
    # Only allowlisted fields leave this function. No commands, args, env or tokens.
    return {
        "codex_home": str(codex_home),
        "config_present": config_path.is_file(),
        "skill_roots": [{"path": str(root), "exists": root.is_dir()} for root in roots],
        "skills": skills,
        "plugins": [{"id": name, "enabled": row.get("enabled") if isinstance(row.get("enabled"), bool) else None, "runtime_exposure": "not_checked"}
                    for name, row in sorted(config.get("plugins", {}).items())],
        "mcp_servers": [{"id": name, "enabled": row.get("enabled") if isinstance(row.get("enabled"), bool) else None, "runtime_exposure": "not_checked"}
                        for name, row in sorted(config.get("mcp_servers", {}).items())],
        "scope": "disk_inventory_only; effective thread config and calls not verified",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    parser.add_argument("--agents-skills", type=Path, default=Path.home() / ".agents" / "skills")
    args = parser.parse_args()
    try:
        result = inventory(args.codex_home, args.agents_skills)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        # Parser exceptions can contain source lines: never echo them.
        parser.exit(1, "Inventory failed: unreadable or unsupported config; no config contents printed.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
