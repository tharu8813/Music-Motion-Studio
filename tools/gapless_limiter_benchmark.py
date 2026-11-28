"""Compare identical-input true-peak limiter and FLAC stages in ABBA order.

Command-level synthetic benchmark; no application settings or user audio.
This does not change Preview or Export defaults.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.renderer.loudness import normalization_filter

RATE = 48000
FRAME_BYTES = 8


def order(round_index: int) -> tuple[str, ...]:
    return ("auto", "single", "single", "auto") if round_index % 2 == 0 else (
        "single", "auto", "auto", "single"
    )


def parse_measurement(log: str) -> tuple[float, float]:
    """Parse the final ebur128 summary, not the per-frame log."""
    summary = log.rsplit("Summary:", 1)[-1]
    integrated = re.search(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS", summary)
    peak = re.search(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s+dBFS", summary)
    if "Summary:" not in log or integrated is None or peak is None:
        raise RuntimeError("No finite integrated loudness / true-peak summary")
    return float(integrated.group(1)), float(peak.group(1))


def run(executable: str, seconds: int, rounds: int) -> dict:
    if min(seconds, rounds) < 1:
        raise ValueError("Positive duration and round count required")

    def ff(*args, loglevel="error"):
        return subprocess.run(
            [executable, "-hide_banner", "-loglevel", loglevel, "-nostdin", "-y",
             *map(str, args)],
            check=True, capture_output=True, timeout=300,
        )

    def measure(input_args):
        before = perf_counter()
        result = ff(*input_args, "-af", "ebur128=peak=true:framelog=quiet",
                    "-f", "null", "-", loglevel="info")
        return parse_measurement(result.stderr.decode(errors="replace")), perf_counter() - before

    def identity(path):
        size = path.stat().st_size
        if size % FRAME_BYTES:
            raise RuntimeError("Decoded audio is not frame aligned")
        with path.open("rb") as stream:
            return {"frames": size // FRAME_BYTES, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}

    report = {
        "ffmpeg": ff("-version").stdout.decode(errors="replace").splitlines()[0],
        "logical_cpus": os.cpu_count(), "sample_rate": RATE,
        "seconds": seconds, "rounds": rounds, "fixtures": {},
        "scope": "Same raw stereo PCM per fixture. Null output measures filter plus sink/conversion, not pure filter CPU. FLAC measures combined filter and encoding; timings overlap conceptually and must not be subtracted as isolated CPU costs. Command-level only; not GUI readiness.",
    }
    with TemporaryDirectory(prefix="mms-limiter-abba-") as directory:
        root = Path(directory)
        for fixture, signal in (
            ("linear", f"anoisesrc=color=pink:amplitude=0.25:seed=10:r={RATE}:d={seconds}"),
            ("limiter", f"aevalsrc=0.015*sin(2*PI*440*t)+0.7*lt(mod(t\\,2)\\,0.0001):s={RATE}:d={seconds}"),
        ):
            raw = root / "input.f32"
            ff("-f", "lavfi", "-i", signal, "-ac", "2", "-ar", RATE,
               "-c:a", "pcm_f32le", "-f", "f32le", raw)
            pcm = identity(raw)
            if pcm["frames"] != RATE * seconds:
                raise RuntimeError("Fixture duration mismatch")
            input_args = ["-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", str(raw)]
            (integrated, peak), measurement_seconds = measure(input_args)
            chain = normalization_filter(integrated, peak)
            if ("alimiter=" in chain) != (fixture == "limiter"):
                raise RuntimeError(f"Unexpected filter for {fixture}: {chain}")
            rows = []
            baseline = None
            for repeat in range(rounds):
                for variant in order(repeat):
                    options = [] if variant == "auto" else ["-filter_threads", "1"]
                    output = root / "audio.flac"
                    decoded = root / "decoded.f32"
                    before = perf_counter()
                    ff(*options, *input_args, "-af", chain, "-ar", RATE, "-ac", "2",
                       "-c:a", "pcm_s16le", "-f", "null", "-")
                    null_seconds = perf_counter() - before
                    before = perf_counter()
                    ff(*options, *input_args, "-af", chain, "-c:a", "flac",
                       "-compression_level", "0", "-ar", RATE, "-ac", "2", output)
                    flac_seconds = perf_counter() - before
                    ff("-i", output, "-c:a", "pcm_f32le", "-f", "f32le", decoded)
                    actual = identity(decoded)
                    if actual["frames"] != pcm["frames"]:
                        raise RuntimeError(f"Output sample count changed: {fixture} {variant}")
                    stats, _ = measure(["-i", str(output)])
                    if baseline is None:
                        baseline = (actual, stats)
                    if (actual, stats) != baseline:
                        raise RuntimeError(f"Output PCM or loudness changed: {fixture} {variant}")
                    row = {"round": repeat + 1, "variant": variant,
                           "null_seconds": null_seconds, "flac_seconds": flac_seconds,
                           "decoded": actual, "output_lufs": stats[0], "output_dbtp": stats[1]}
                    rows.append(row)
                    print(json.dumps({"fixture": fixture, **row}), flush=True)
                    output.unlink()
                    decoded.unlink()
            report["fixtures"][fixture] = {
                "input": pcm, "input_lufs": integrated, "input_dbtp": peak,
                "measurement_seconds": measurement_seconds, "filter": chain, "runs": rows,
                "median_seconds": {
                    variant: {
                        stage: statistics.median(row[stage] for row in rows if row["variant"] == variant)
                        for stage in ("null_seconds", "flac_seconds")
                    } for variant in ("auto", "single")
                },
            }
            raw.unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg"))
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--report", type=Path, default=Path("gapless-limiter-abba.json"))
    args = parser.parse_args()
    if not args.ffmpeg or min(args.seconds, args.rounds) < 1:
        parser.error("FFmpeg and positive seconds/rounds required")
    result = run(str(Path(args.ffmpeg).resolve()), args.seconds, args.rounds)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({name: fixture["median_seconds"] for name, fixture in result["fixtures"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
