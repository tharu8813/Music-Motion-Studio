"""Run test modules in isolated Python processes.

Qt multimedia and worker-thread state can outlive a unittest module. Running
each module in a fresh process prevents that state from affecting later
modules and gives every module an explicit timeout.

Modules run one at a time on purpose: main-window tests share the Qt test-mode
data folder (recovery files) and their registry-backed QSettings, so parallel
processes would race on them.

    python scripts/run_tests.py                      # everything
    python scripts/run_tests.py automix project      # modules whose name contains a filter
    python scripts/run_tests.py test_x --repeat 20   # hunt an order/race-dependent failure
    python scripts/run_tests.py --report times.json  # per-module timing for CI artifacts
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


def main() -> int:
    # This process prints captured Unicode too, not just the child processes.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("filters", nargs="*", help="only run modules whose name contains one of these")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--repeat", type=int, default=1, help="run each selected module this many times")
    parser.add_argument("--report", type=Path, help="write per-run status and seconds as JSON")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    modules = sorted(
        f"tests.{path.stem}"
        for path in (root / "tests").glob("test_*.py")
        if not args.filters or any(name in path.stem for name in args.filters)
    )
    if not modules:
        print("No test modules match the given filters.")
        return 1
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
                [sys.executable, "-m", "unittest", module, "-q"],
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
        runs.append({
            "module": module, "status": status, "seconds": round(seconds, 2),
            "returncode": completed.returncode,
            "tests": int(summary.group(1)) if summary else 0,
            "skipped": int(skipped.group(1)) if skipped else 0,
        })
        if completed.returncode:
            print(f"FAIL    {module} ({seconds:.1f}s, exit {completed.returncode})", flush=True)
            output = (completed.stdout + completed.stderr).strip()
            if output:
                print(output, flush=True)
            failures.append(module)
        else:
            print(f"PASS    {module} ({seconds:.1f}s)", flush=True)

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
