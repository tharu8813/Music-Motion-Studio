"""Run test modules in isolated Python processes.

Qt multimedia and worker-thread state can outlive a unittest module. Running
each module in a fresh process prevents that state from affecting later
modules and gives every module an explicit timeout.

Modules run one at a time on each worker. Deterministic CI shards run on separate
Windows runners, because main-window tests share Qt recovery files and
registry-backed QSettings. Do not run those modules concurrently on one host.
The default command remains a full serial run for local reproducibility.

    python scripts/run_tests.py                      # everything
    python scripts/run_tests.py automix project      # modules whose name contains a filter
    python scripts/run_tests.py test_x --repeat 20   # hunt an order/race-dependent failure
    python scripts/run_tests.py --report times.json  # per-module timing for CI artifacts
    python scripts/run_tests.py main_window           # CI shard: main-window GUI
    python scripts/run_tests.py --exclude main_window # remaining modules
    python scripts/run_tests.py --exclude main_window --shard-index 0 --shard-count 3
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import perf_counter


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def discover_modules(tests_directory: Path, filters: list[str], excluded: list[str]) -> list[str]:
    """Deterministic partition with no imports or test execution at discovery time."""
    return sorted(
        f"tests.{path.stem}"
        for path in tests_directory.glob("test_*.py")
        if (not filters or any(name in path.stem for name in filters))
        and not any(name in path.stem for name in excluded)
    )


def partition_modules(modules: list[str], index: int, count: int) -> list[str]:
    """Divide a deterministic module list without dropping or duplicating tests."""
    if count < 1 or not 0 <= index < count:
        raise ValueError("Shard index must be in [0, shard count).")
    return modules[index::count]


def parse_unittest_slowest_tests(output: str) -> list[dict[str, object]]:
    """Read Python 3.12+ unittest --durations output without inventing timings."""
    marker = "Slowest test durations"
    if marker not in output:
        return []
    rows: list[dict[str, object]] = []
    for raw_line in output.split(marker, 1)[1].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("-"):
            continue
        found = re.fullmatch(r"(\d+(?:\.\d+)?)s\s+(.+)", line)
        if not found:
            if rows:
                break
            continue
        rows.append({"test": found.group(2), "seconds": float(found.group(1))})
    return rows


def main() -> int:
    # This process prints captured Unicode too, not just the child processes.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("filters", nargs="*", help="only run modules whose name contains one of these")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--repeat", type=int, default=1, help="run each selected module this many times")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME",
                        help="exclude modules whose filename contains NAME (repeatable)")
    parser.add_argument("--shard-index", type=int, default=0,
                        help="zero-based shard index")
    parser.add_argument("--shard-count", type=int, default=1,
                        help="number of independent shards")
    parser.add_argument("--verbose", action="store_true",
                        help="include the active test name in captured timeout diagnostics")
    parser.add_argument("--report", type=Path, help="write per-run status and seconds as JSON")
    args = parser.parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("Shard index must be in [0, shard count).")

    root = Path(__file__).resolve().parents[1]
    # Run the repository test/debug harness only with a working Rust toolchain
    # and current Python native wheel. Imported test modules remain testable
    # independently so isolated negative prerequisite tests can use mocks.
    sys.path.insert(0, str(root))
    from scripts.rust_prerequisites import (
        RustPrerequisiteError, require_developer_environment,
    )
    try:
        require_developer_environment()
    except RustPrerequisiteError as error:
        print(f"[Rust prerequisite] {error}", file=sys.stderr)
        return 2
    modules = discover_modules(root / "tests", args.filters, args.exclude)
    if not modules:
        print("No test modules match the given filters.")
        return 1
    modules = partition_modules(modules, args.shard_index, args.shard_count)
    if not modules:
        print("This test shard is empty; check the CI matrix.")
        return 1
    print(f"Shard {args.shard_index + 1}/{args.shard_count}: {len(modules)} modules", flush=True)
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Korean log lines otherwise hit the locale codec (cp949) and kill the
    # output reader, dropping exactly the text a failure report needs.
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    failures: list[str] = []
    runs: list[dict[str, object]] = []
    started = perf_counter()

    for module in modules * max(1, args.repeat):
        begin = perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", module, "-v" if args.verbose else "-q",
                 *(["--durations", "5"] if sys.version_info >= (3, 12) else [])],
                cwd=root,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired as error:
            seconds = perf_counter() - begin
            print(f"TIMEOUT {module} (>{args.timeout:g}s)", flush=True)
            output = (_text(error.stdout) + _text(error.stderr)).strip()
            if output:  # what it was doing when it hung
                print(output[-8000:], flush=True)
            failures.append(module)
            runs.append({"module": module, "status": "timeout", "seconds": round(seconds, 2)})
            continue

        seconds = perf_counter() - begin
        status = "fail" if completed.returncode else "pass"
        summary = re.search(r"Ran (\d+) tests?", completed.stderr)
        skipped = re.search(r"\bskipped=(\d+)", completed.stderr)
        slow_tests = parse_unittest_slowest_tests(completed.stderr)
        runs.append({
            "module": module, "status": status, "seconds": round(seconds, 2),
            "returncode": completed.returncode,
            "tests": int(summary.group(1)) if summary else 0,
            "skipped": int(skipped.group(1)) if skipped else 0,
            "slowest_tests": slow_tests,
        })
        if completed.returncode:
            print(f"FAIL    {module} ({seconds:.1f}s, exit {completed.returncode})", flush=True)
            output = (completed.stdout + completed.stderr).strip()
            if output:
                print(output, flush=True)
            failures.append(module)
        else:
            print(f"PASS    {module} ({seconds:.1f}s)", flush=True)
            if seconds >= 10.0 and slow_tests:
                detail = ", ".join(
                    f"{row['test']} {row['seconds']:.2f}s" for row in slow_tests[:3]
                )
                print(f"  Slow test cases: {detail}", flush=True)

    total = len(runs)
    print(f"\n{total - len(failures)}/{total} module runs passed in {perf_counter() - started:.0f}s")
    print(f"Tests: {sum(run.get('tests', 0) for run in runs)}, skipped: {sum(run.get('skipped', 0) for run in runs)}")
    slowest = sorted(runs, key=lambda run: run["seconds"], reverse=True)[:5]
    print("Slowest: " + ", ".join(f"{run['module'].removeprefix('tests.')} {run['seconds']}s" for run in slowest))
    if args.report is not None:
        args.report.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    if failures:
        print("Failed modules:")
        print("\n".join(f"- {module}" for module in dict.fromkeys(failures)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
