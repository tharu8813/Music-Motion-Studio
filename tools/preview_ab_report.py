"""Read-only A-B-B-A report for real preview benchmark JSON files.

This compares recorded preview performance, not image correctness or hardware
capabilities. It never runs the renderer, alters preview settings or exports.
Run four identical benchmark fixtures in A1, B1, B2, A2 order and supply the
corresponding files as --baseline A1 A2 --candidate B1 B2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean


# These fields must match across all four runs to support a fair comparison.
MATCH_FIELDS = (
    "benchmark", "backend", "preview_quality", "width", "height",
    "video_count", "target_fps", "render_path", "transport_exercised",
    "lyrics", "integrated", "known_fixture_beats", "track_count",
    "analysis_workers", "python", "qt", "numpy", "platform",
    "gpu_backend", "gpu_active_at_end", "proxy_count",
)

# Each expression is a scalar obtained from one benchmark run. Lower CPU
# percentage / latency / dropped-frame rate is better; higher real scene FPS
# is better. Repeated callbacks are NOT counted as new scene FPS.
METRICS = (
    ("presented_scene_fps", "fps", True),
    ("scene_frame_time_p50_ms", "ms", False),
    ("scene_frame_time_p95_ms", "ms", False),
    ("heartbeat_max_ms", "ms", False),
    ("mean_parent_cpu_percent_of_one_core", "percent of one core", False),
    ("rss_peak_bytes", "bytes", False),
    ("frame_buffer_peak_bytes", "bytes", False),
)


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    if data.get("benchmark") != "real_video_preview":
        raise ValueError(f"{path}: not a real_video_preview measurement")
    if not isinstance(data.get("seconds"), (int, float)) or data["seconds"] <= 0:
        raise ValueError(f"{path}: invalid measurement duration")
    if data.get("integrated") and not data.get("blended_audio_ready"):
        raise ValueError(f"{path}: integrated AutoMix was not ready during measurement")
    return data


def _spread_percent(values: list[float]) -> float | None:
    average = mean(values)
    return round((max(values) - min(values)) * 100 / abs(average), 3) if average else None


def compare(baseline: list[dict], candidate: list[dict]) -> dict:
    """Compare exactly two baseline and two candidate runs in A1-B1-B2-A2."""
    if len(baseline) != 2 or len(candidate) != 2:
        raise ValueError("A-B-B-A needs two baseline and two candidate measurements")
    runs = [baseline[0], candidate[0], candidate[1], baseline[1]]
    for index, row in enumerate(runs):
        if row.get("benchmark") != "real_video_preview" or not isinstance(row.get("seconds"), (float, int)) or row["seconds"] <= 0:
            raise ValueError(f"run {index + 1}: invalid real_video_preview result")
        if row.get("integrated") and not row.get("blended_audio_ready"):
            raise ValueError(f"run {index + 1}: AutoMix not ready")
    mismatches = {
        field: [row.get(field) for row in runs]
        for field in MATCH_FIELDS
        if any(row.get(field) != runs[0].get(field) for row in runs[1:])
    }
    if mismatches:
        raise ValueError("Incompatible benchmark configurations: " + json.dumps(
            mismatches, ensure_ascii=False, sort_keys=True,
        ))

    scale_ranges = [
        (row.get("canvas_scale_min"), row.get("canvas_scale_max")) for row in runs
    ]
    quality_scale_equal = all(value == scale_ranges[0] for value in scale_ranges[1:])
    metrics: dict[str, dict] = {}
    for field, unit, higher_is_better in METRICS:
        before = [row.get(field) for row in baseline]
        after = [row.get(field) for row in candidate]
        if any(not isinstance(value, (int, float)) for value in before + after):
            continue
        before_mean = mean(before)
        after_mean = mean(after)
        metrics[field] = {
            "unit": unit,
            "higher_is_better": higher_is_better,
            "baseline_mean": round(before_mean, 4),
            "candidate_mean": round(after_mean, 4),
            "candidate_change_percent": round((after_mean - before_mean) * 100 / before_mean, 3)
            if before_mean else None,
            "baseline_pair_spread_percent": _spread_percent(before),
            "candidate_pair_spread_percent": _spread_percent(after),
        }

    # Normalize decoder pressure by elapsed wall time. A 10 second run should
    # not count as more dropped frames simply because it ran longer.
    before_drops = [row.get("decode_drops") for row in baseline]
    after_drops = [row.get("decode_drops") for row in candidate]
    if all(isinstance(value, (int, float)) for value in before_drops + after_drops):
        a = [row["decode_drops"] / row["seconds"] for row in baseline]
        b = [row["decode_drops"] / row["seconds"] for row in candidate]
        a_mean, b_mean = mean(a), mean(b)
        metrics["decode_drops_per_second"] = {
            "unit": "drops/s", "higher_is_better": False,
            "baseline_mean": round(a_mean, 4),
            "candidate_mean": round(b_mean, 4),
            "candidate_change_percent": round((b_mean - a_mean) * 100 / a_mean, 3)
            if a_mean else None,
            "baseline_pair_spread_percent": _spread_percent(a),
            "candidate_pair_spread_percent": _spread_percent(b),
        }

    return {
        "method": "A1-B1-B2-A2 paired preview measurement",
        "matched_configuration": {key: runs[0].get(key) for key in MATCH_FIELDS},
        "canvas_scale_ranges_abba": scale_ranges,
        "same_effective_canvas_scale": quality_scale_equal,
        "metrics": metrics,
        "performance_only": True,
        "notes": [
            "No FPS improvement or regression is claimed without inspecting measured differences and noise.",
            "Matching settings and platform strings do not prove the same physical GPU or background workload.",
            "Changed adaptive canvas scale confounds equal-quality FPS comparisons."
            if not quality_scale_equal else
            "Recorded adaptive canvas scale ranges match, but image-pixel parity must be tested separately.",
            "GPU utilization samples, if present, describe the whole device, not the application.",
            "A/B timing must exclude other concurrent benchmark or background-intensive tasks.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, nargs=2, required=True, metavar=("A1", "A2"))
    parser.add_argument("--candidate", type=Path, nargs=2, required=True, metavar=("B1", "B2"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = compare(
            [_read(path) for path in args.baseline],
            [_read(path) for path in args.candidate],
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
