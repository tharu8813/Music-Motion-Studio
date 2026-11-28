"""Reproducible command-level gapless preview audio benchmark.

No app settings, project files, or source audio are touched. Synthetic fixtures
are created in a temporary folder; output hashes are checked across variants.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg"))
    parser.add_argument("--seconds", type=int, default=60, help="Seconds per synthetic track (8 tracks)")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--report", type=Path, default=Path("gapless-preview-benchmark.json"))
    args = parser.parse_args()
    if not args.ffmpeg or args.seconds < 1 or args.rounds < 1:
        parser.error("FFmpeg and positive duration/round count are required")

    def ff(*options) -> bytes:
        return subprocess.run(
            [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, options)],
            check=True, capture_output=True, timeout=300,
        ).stdout

    report = {"ffmpeg": ff("-version").decode(errors="replace").splitlines()[0],
              "logical_cpus": os.cpu_count(), "track_seconds": args.seconds,
              "track_count": 8, "rounds": args.rounds, "decode": {}, "flac": {}, "loudness": []}
    with TemporaryDirectory(prefix="gapless-benchmark-") as directory:
        root = Path(directory)
        sources = []
        for i in range(8):
            source = root / f"track-{i}.{('wav', 'flac', 'mp3', 'm4a')[i % 4]}"
            ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.25:seed={i + 10}:r=48000:d={args.seconds}",
               "-ac", "2", source)
            sources.append(source)
        parts = [root / f"track-{i}.f32" for i in range(8)]

        def decode(pair):
            source, part = pair
            # MP4 fixture uses an exact whole-second presentation length.
            trim = ["-af", f"atrim=end_sample={args.seconds * 48000}"] if source.suffix == ".m4a" else []
            ff("-threads", "1", "-i", source, "-vn", "-map", "0:a:0", *trim,
               "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", "-f", "f32le", part)

        expected = None
        for repeat in range(args.rounds):
            counts = (1, 2, 4, 6) if repeat % 2 == 0 else (6, 4, 2, 1)
            for count in counts:
                before = perf_counter()
                with ThreadPoolExecutor(max_workers=count) as pool:
                    list(pool.map(decode, zip(sources, parts)))
                elapsed = perf_counter() - before
                hashes = []
                for part in parts:
                    with part.open("rb") as stream:
                        hashes.append(hashlib.file_digest(stream, "sha256").hexdigest())
                if expected is None:
                    expected = hashes
                if hashes != expected:
                    raise RuntimeError("Parallel decoding changed PCM bytes")
                report["decode"].setdefault(str(count), []).append(elapsed)
                print(f"decode workers={count} seconds={elapsed:.3f}", flush=True)

        manifest = root / "playlist.pcm-list"
        manifest.write_text("".join(
            "'" + ("file:" + p.resolve().as_posix()).replace("'", "'\\''") + "'\n" for p in parts
        ), encoding="utf-8", newline="\n")
        input_args = ["-protocol_whitelist", "file,concatf", "-f", "f32le",
                      "-ar", "48000", "-ac", "2", "-i", "concatf:file:" + manifest.resolve().as_posix()]
        expected_audio = None
        for repeat in range(args.rounds):
            before = perf_counter()
            ff(*input_args, "-af", "ebur128=peak=true", "-f", "null", "-")
            report["loudness"].append(perf_counter() - before)
            counts = ("auto", "1", "2", "4") if repeat % 2 == 0 else ("4", "2", "1", "auto")
            for count in counts:
                output = root / "audio.flac"
                thread_args = [] if count == "auto" else ["-threads:a", count]
                before = perf_counter()
                ff(*input_args, "-af", "volume=-3dB", "-c:a", "flac", "-compression_level", "0",
                   *thread_args, "-ar", "48000", "-ac", "2", output)
                elapsed = perf_counter() - before
                # Hash decoded audio, not encoded file bytes or metadata.
                decoded = ff("-i", output, "-c:a", "pcm_f32le", "-f", "hash", "-hash", "sha256", "-")
                if expected_audio is None:
                    expected_audio = decoded
                if decoded != expected_audio:
                    raise RuntimeError("FLAC thread settings changed decoded audio")
                report["flac"].setdefault(count, []).append(elapsed)
                print(f"flac threads={count} seconds={elapsed:.3f}", flush=True)
                output.unlink()
        report["pcm_hashes"] = expected
        report["decoded_flac_hash"] = expected_audio.decode().strip()
    report["median_seconds"] = {
        group: {key: statistics.median(values) for key, values in report[group].items()}
        for group in ("decode", "flac")
    }
    report["median_seconds"]["loudness"] = statistics.median(report["loudness"])
    report["scope"] = "Synthetic command-level stages; constant gain isolates encoding; not full GUI entry or a hardware recommendation."
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["median_seconds"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
