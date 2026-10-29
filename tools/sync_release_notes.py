#!/usr/bin/env python3
"""Synchronize reviewed user-friendly notes to existing GitHub Releases.

Only release bodies are changed. Release tags, titles, assets and publication
settings are not modified. Existing descriptions are backed up before writes.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = "tharu8813/Playlist-Canvas"
NOTES_DIR = Path("docs/release-notes")
BACKUP_PATH = Path("release-note-backups/original-release-notes.json")
API_BASE = f"https://api.github.com/repos/{REPO}"


def api(method, url, payload=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "playlist-canvas-release-note-sync",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} failed ({err.code}): {body[:800]}") from err


def load_notes():
    notes = {}
    for path in sorted(NOTES_DIR.glob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        if content.get("schema_version") != 1:
            raise ValueError(f"{path}: unknown schema version")
        for tag, body in content["release_notes"].items():
            if tag in notes:
                raise ValueError(f"Duplicate tag: {tag}")
            if not isinstance(body, str) or not body.strip().startswith("# Playlist Canvas"):
                raise ValueError(f"Empty or invalid release notes: {tag}")
            notes[tag] = body.strip() + "\n"
    if len(notes) != 19:
        raise ValueError(f"Expected 19 reviewed releases, got {len(notes)}")
    return notes


def list_releases():
    releases = []
    page = 1
    while True:
        batch = api("GET", f"{API_BASE}/releases?per_page=100&page={page}")
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return {release["tag_name"]: release for release in releases}


def append_summary(mode, changed, unchanged, missing):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(
                f"## Release notes: {mode}\n\n"
                f"- Updated / would update: {len(changed)}\n"
                f"- Already identical: {len(unchanged)}\n"
                f"- Not found: {len(missing)}\n\n"
            )
            for tag in changed:
                output.write(f"- {tag}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "apply"], default="check")
    args = parser.parse_args()

    notes = load_notes()
    releases = list_releases()
    missing = sorted(set(notes) - set(releases))
    if missing:
        append_summary(args.mode, [], [], missing)
        raise RuntimeError(f"Releases not found. No modifications made: {missing}")

    # Preserve all previous descriptions before making a single change.
    BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup = {
        "repository": REPO,
        "release_notes": {
            tag: {
                "release_id": releases[tag]["id"],
                "name": releases[tag]["name"],
                "body": releases[tag]["body"],
            }
            for tag in notes
        },
    }
    BACKUP_PATH.write_text(
        json.dumps(backup, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    changed, unchanged = [], []
    for tag, body in notes.items():
        current = (releases[tag].get("body") or "").strip()
        if current == body.strip():
            unchanged.append(tag)
            print(f"SKIP {tag}: already matches")
            continue
        if args.mode == "apply":
            api("PATCH", f"{API_BASE}/releases/{releases[tag]['id']}", {"body": body})
            print(f"UPDATED {tag}")
        else:
            print(f"WOULD UPDATE {tag}")
        changed.append(tag)

    append_summary(args.mode, changed, unchanged, missing)
    print(f"Mode={args.mode}; changes={len(changed)}; unchanged={len(unchanged)}; backup={BACKUP_PATH}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
