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
from math import isfinite


# These fields must match across all four runs to support a fair comparison.
MATCH_FIELDS = (
    "benchmark", "backend", "preview_quality", "width", "height",
    "video_count", "target_fps", "render_path", "transport_exercised",
    "lyrics", "integrated", "known_fixture_beats", "track_count",
    "analysis_workers", "python", "qt", "numpy", "platform",
    "gpu_backend", "gpu_active_at_end", "proxy_count",
    "warmup_seconds", "requested_seconds", "frame_time_percentile_method",
)

# Each expression is a scalar obtained from one benchmark run. Lower CPU
# percentage / latency / dropped-frame rate is better; higher real scene FPS
# is better. Repeated callbacks are NOT counted as new scene FPS.
# Only existing opt-in profiling spans. Values are inclusive wall time in
# seconds, not additive CPU work or GPU execution time. A stage is reported
# only if every run captured usable samples for that exact stage.
STAGE_METRICS = (
    ("canvas.capture_seconds", "Canvas frame capture"),
    ("canvas.qt_scene_paint_seconds", "Qt scene painting"),
    ("preview.gpu_ordered_canvas_seconds", "GPU-ordered Canvas preparation"),
    ("preview.texture_prepare_upload_seconds", "GPU texture preparation / upload"),
    ("preview.gl_texture_upload_cpu_seconds", "OpenGL upload submission"),
    ("preview.gl_submission_seconds", "OpenGL paint submission"),
    ("preview.submission_to_swap_seconds", "Submission to Qt frame swap"),
    ("preview.gui_refresh_lateness_seconds", "GUI refresh scheduling lateness"),
    ("video.to_image_seconds", "QVideoFrame to QImage conversion"),
    ("video.cpu_filter_seconds", "CPU video frame filtering"),
    ("video.cpu_scale_seconds", "CPU video scaling"),
    ("video.cpu_color_blur_seconds", "CPU video color and blur"),
)

METRICS = (
    ("presented_scene_fps", "fps", True),
    ("scene_frame_time_p50_ms", "ms", False),
    ("scene_frame_time_p95_ms", "ms", False),
    ("heartbeat_max_ms", "ms", False),
    ("mean_parent_cpu_percent_of_one_core", "percent of one core", False),
    ("rss_peak_bytes", "bytes", False),
    ("frame_buffer_peak_bytes", "bytes", False),
)


def _valid_duration(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and isfinite(value) and value > 0
    )


def _read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    if data.get("benchmark") != "real_video_preview":
        raise ValueError(f"{path}: not a real_video_preview measurement")
    if not _valid_duration(data.get("seconds")):
        raise ValueError(f"{path}: invalid measurement duration")
    if data.get("integrated") and not data.get("blended_audio_ready"):
        raise ValueError(f"{path}: integrated AutoMix was not ready during measurement")
    return data


def _spread_percent(values: list[float]) -> float | None:
    average = mean(values)
    return round((max(values) - min(values)) * 100 / abs(average), 3) if average else None


def _valid_scale_range(bounds: tuple[object, object]) -> bool:
    lower, upper = bounds
    return (
        all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and isfinite(value) and value > 0
            for value in (lower, upper)
        )
        and lower <= upper
    )


def _stage_percentile(run: dict, stage: str) -> tuple[float, int, int | None] | None:
    """Extract an opt-in profile P95 sample without inventing missing stages."""
    profile = run.get("profile")
    if not isinstance(profile, dict):
        return None
    sample = profile.get(stage)
    if not isinstance(sample, dict):
        return None
    count, p95 = sample.get("count"), sample.get("recent_p95")
    if (
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        or isinstance(p95, bool) or not isinstance(p95, (int, float))
        or not isfinite(p95) or p95 < 0
    ):
        return None
    # Lifetime calls and the bounded window used for recent_p95 differ in
    # long runs. Older profile JSON lacks recent_count: report it as unknown.
    recent_count = sample.get("recent_count")
    if recent_count is not None and (
        isinstance(recent_count, bool)
        or not isinstance(recent_count, int)
        or not 1 <= recent_count <= count
    ):
        return None
    return float(p95) * 1000.0, count, recent_count


def _stage_latency_report(runs: list[dict]) -> dict[str, dict]:
    """P95 of per-call spans, never the sum of overlapping nested timers."""
    stages: dict[str, dict] = {}
    for stage, label in STAGE_METRICS:
        samples = [_stage_percentile(run, stage) for run in runs]
        if any(sample is None for sample in samples):
            continue
        # The order of runs is A1, B1, B2, A2.
        durations = [sample[0] for sample in samples]
        before, after = [durations[0], durations[3]], [durations[1], durations[2]]
        baseline_mean, candidate_mean = mean(before), mean(after)
        stages[stage] = {
            "label": label,
            "unit": "ms",
            "measurement": "inclusive per-call recent P95 wall time",
            "baseline_p95_mean_ms": round(baseline_mean, 4),
            "candidate_p95_mean_ms": round(candidate_mean, 4),
            "candidate_change_percent": (
                round((candidate_mean - baseline_mean) * 100 / baseline_mean, 3)
                if baseline_mean else None
            ),
            "baseline_pair_spread_percent": _spread_percent(before),
            "candidate_pair_spread_percent": _spread_percent(after),
            "sample_counts_abba": [sample[1] for sample in samples],
            "recent_sample_counts_abba": [sample[2] for sample in samples],
        }
    return stages


def _video_pts_absolute_report(runs: list[dict]) -> dict | None:
    """Report magnitude of eligible video PTS drift, not a CPU/GPU stage."""
    samples = [
        _stage_percentile(run, "video.eligible_pts_abs_error_seconds")
        for run in runs
    ]
    if any(sample is None for sample in samples):
        return None
    # A1, B1, B2, A2: keep the same pairing as all other A/B metrics.
    durations = [sample[0] for sample in samples]
    before, after = [durations[0], durations[3]], [durations[1], durations[2]]
    baseline_mean, candidate_mean = mean(before), mean(after)
    return {
        "unit": "ms",
        "measurement": "eligible video-frame absolute PTS-to-target error recent P95",
        "baseline_p95_mean_ms": round(baseline_mean, 4),
        "candidate_p95_mean_ms": round(candidate_mean, 4),
        "candidate_change_percent": (
            round((candidate_mean - baseline_mean) * 100 / baseline_mean, 3)
            if baseline_mean else None
        ),
        "baseline_pair_spread_percent": _spread_percent(before),
        "candidate_pair_spread_percent": _spread_percent(after),
        "sample_counts_abba": [sample[1] for sample in samples],
        "recent_sample_counts_abba": [sample[2] for sample in samples],
    }


def _rss_window_growth_report(runs: list[dict]) -> dict | None:
    """Compare in-window process RSS change, without inferring memory leaks."""
    bounds = [
        (row.get("rss_start_bytes"), row.get("rss_end_bytes")) for row in runs
    ]
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool)
        or not isfinite(value) or value < 0
        for start, end in bounds for value in (start, end)
    ):
        return None
    # Negative delta (memory released during playback) is legitimate.
    changes = [end - start for start, end in bounds]
    baseline, candidate = [changes[0], changes[3]], [changes[1], changes[2]]
    baseline_mean, candidate_mean = mean(baseline), mean(candidate)
    return {
        "unit": "bytes",
        "measurement": "RSS end minus start within active preview measurement window",
        "growth_bytes_abba": changes,
        "baseline_mean_bytes": round(baseline_mean, 4),
        "candidate_mean_bytes": round(candidate_mean, 4),
        # A percent change is misleading if baseline memory growth is
        # negative or zero, so return an absolute difference in bytes.
        "candidate_minus_baseline_mean_bytes": round(
            candidate_mean - baseline_mean, 4,
        ),
        "baseline_pair_spread_bytes": abs(baseline[1] - baseline[0]),
        "candidate_pair_spread_bytes": abs(candidate[1] - candidate[0]),
    }


def compare(baseline: list[dict], candidate: list[dict]) -> dict:
    """Compare exactly two baseline and two candidate runs in A1-B1-B2-A2."""
    if len(baseline) != 2 or len(candidate) != 2:
        raise ValueError("A-B-B-A needs two baseline and two candidate measurements")
    runs = [baseline[0], candidate[0], candidate[1], baseline[1]]
    for index, row in enumerate(runs):
        if row.get("benchmark") != "real_video_preview" or not _valid_duration(row.get("seconds")):
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
    # The benchmark records actual elapsed wall time (which may drift
    # slightly from --seconds when the GUI event loop stalls). Disallow
    # materially different runs: P95 and RSS peaks depend on sample length.
    elapsed = [row["seconds"] for row in runs]
    if max(elapsed) > min(elapsed) * 1.10:
        raise ValueError("Incompatible measured durations across A-B-B-A runs")

    scale_ranges = [
        (row.get("canvas_scale_min"), row.get("canvas_scale_max")) for row in runs
    ]
    valid_scale_ranges = all(_valid_scale_range(bounds) for bounds in scale_ranges)
    # Four absent fields used to compare equal (None == None), incorrectly
    # claiming matched canvas quality for benchmark records without scale data.
    quality_scale_equal = (
        valid_scale_ranges
        and all(value == scale_ranges[0] for value in scale_ranges[1:])
    )
    metrics: dict[str, dict] = {}
    for field, unit, higher_is_better in METRICS:
        before = [row.get(field) for row in baseline]
        after = [row.get(field) for row in candidate]
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not isfinite(value)
            for value in before + after
        ):
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
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and isfinite(value) and value >= 0
        for value in before_drops + after_drops
    ):
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
        "valid_canvas_scale_ranges": valid_scale_ranges,
        "same_effective_canvas_scale": quality_scale_equal,
        "metrics": metrics,
        "profile_stage_p95_ms": _stage_latency_report(runs),
        "video_pts_abs_p95_ms": _video_pts_absolute_report(runs),
        "rss_window_growth": _rss_window_growth_report(runs),
        "performance_only": True,
        "notes": [
            "No FPS improvement or regression is claimed without inspecting measured differences and noise.",
            "Matching settings and platform strings do not prove the same physical GPU or background workload.",
            "Missing or invalid adaptive canvas scale readings cannot establish equal-quality FPS comparisons."
            if not valid_scale_ranges else
            "Changed adaptive canvas scale confounds equal-quality FPS comparisons."
            if not quality_scale_equal else
            "Recorded adaptive canvas scale ranges match, but image-pixel parity must be tested separately.",
            "GPU utilization samples, if present, describe the whole device, not the application.",
            "A/B timing must exclude other concurrent benchmark or background-intensive tasks.",
            "Requested duration and warm-up must match; actual measurement lengths must remain within 10%.",
            "Profile stage P95 values are inclusive per-call wall times: nested stages overlap and must not be summed.",
            "OpenGL submission times are not GPU execution times; compare stage counts and per-run spread before assigning bottlenecks.",
            "Video PTS absolute P95 covers only eligible decoded frames, not rejected frames or final A/V audio synchronization.",
            "RSS window growth is an in-run difference, not proof of a leak; compare repeats and long-run trends separately.",
            "P95 uses bounded recent samples (up to 1024), not necessarily all cumulative calls. Missing recent counts are unknown, never zero.",
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
