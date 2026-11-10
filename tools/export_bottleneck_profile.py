"""Measure export CPU process-tree load, GPU encoder activity, and stage timings.

Use with an opt-in PLAYLIST_CANVAS_PROFILE session. This standalone tool has
no runtime dependency on the editor; install psutil only for diagnostics.
The default capture uses only GPU Engine counters belonging to the export process tree.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time


CATEGORIES = ("editor", "canvas_workers", "rust_gpu", "ffmpeg", "other")
# Record only the settings that control export optimization. Do not include
# environment secrets, file paths, token values or media metadata.
OPTIMIZATION_SETTINGS = (
    "PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB",
    "PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE",
    "PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH",
    "PLAYLIST_CANVAS_DISABLE_FIXED_OVERLAY_EVAL",
    "PLAYLIST_CANVAS_DISABLE_OPACITY_KEYFRAME_ROI",
    "PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE",
    "PLAYLIST_CANVAS_DISABLE_CFR_SAMPLE_PRUNING",
    "PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE",
    "PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE",
)


def run_configuration(environment: dict[str, str] | None = None) -> dict:
    values = os.environ if environment is None else environment
    return {
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "optimization_environment": {
            name: values.get(name)
            for name in OPTIMIZATION_SETTINGS
        },
    }

STAGES = (
    "export.total_wall_seconds",
    "export.completed_wall_seconds",
    "export.incomplete_wall_seconds",
    "export.audio_prepare_seconds",
    "export.bass_prepare_seconds",
    "export.piped_capture_session_seconds",
    "export.capture_session_seconds",
    "export.frame_generation_seconds",
    "canvas.scene_render_seconds",
    "canvas.qt_scene_paint_seconds",
    "export.frame_queue_seconds",
    "export.frame_delivery_seconds",
    "export.frame_pixel_prepare_seconds",
    "export.frame_pipe_write_seconds",
    "export.renderer_seconds",
    "ffmpeg.process_wall_seconds",
)


def classify_process(name: str, command: list[str], root: bool = False) -> str:
    """Categorize by observable process identity, not total machine CPU."""
    if root:
        return "editor"
    executable = Path(name or "").name.lower()
    if executable in {"ffmpeg.exe", "ffmpeg"}:
        return "ffmpeg"
    if executable in {"d3d11_qt_canvas_stream_export.exe", "d3d11_qt_canvas_stream_export"}:
        return "rust_gpu"
    arguments = " ".join(command).lower()
    if "multiprocessing.spawn" in arguments or "spawn_main(" in arguments or "--multiprocessing-fork" in arguments:
        return "canvas_workers"
    # On Windows the Python shim selected by a virtualenv can spawn a second
    # python.exe that actually runs main.py. Do not count its CPU as "other".
    if executable in {"python.exe", "pythonw.exe", "python"} and any(
        argument.strip('"').replace("\\", "/").lower().rsplit("/", 1)[-1] == "main.py"
        for argument in command
    ):
        return "editor"
    return "other"


def parse_gpu_output(raw: str) -> list[dict[str, float | int]]:
    """Parse nvidia-smi device-level CSV counters, omitting unsupported rows."""
    devices: list[dict[str, float | int]] = []
    for line in raw.splitlines():
        fields = [part.strip().replace(" %", "").replace(" MiB", "") for part in line.split(",")]
        if len(fields) != 4:
            continue
        try:
            values = [float(field) for field in fields]
        except ValueError:
            continue
        devices.append({
            "index": int(values[0]),
            "gpu_percent": values[1],
            "encoder_percent": values[2],
            "memory_mib": values[3],
        })
    return devices


def query_nvidia_gpu() -> list[dict[str, float | int]] | None:
    """Optional device-wide utilization. No GPU or unsupported query is not an error."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,utilization.encoder,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_gpu_output(result.stdout) if result.returncode == 0 else None


def load_stage_profiles(path: Path) -> dict[str, object]:
    """Combine editor and unique worker timing files without adding nested phases."""
    paths = [path, *sorted(path.parent.glob(f"{path.stem}.worker-*{path.suffix}"))]
    stages: dict[str, dict[str, float | int]] = {}
    # Stream-level evidence is kept separate from the inclusive totals. It
    # maps a stable Canvas band key (e.g. layer:0) to its source worker data.
    streams: dict[str, dict[str, object]] = {}
    parent_queues: dict[str, dict] = {}
    parent_pruning: dict[str, dict] = {}
    read_files = 0
    for filename in paths:
        try:
            data = json.loads(filename.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
            continue
        read_files += 1
        context = data.get("context")
        if isinstance(context, dict):
            queue_info = context.get("parent_canvas_queues")
            if isinstance(queue_info, dict):
                for key, info in queue_info.items():
                    if isinstance(key, str) and isinstance(info, dict):
                        if key == "base" or (key.startswith("layer:") and key[6:].isdigit()):
                            parent_queues[key] = info
            pruning_info = context.get("parent_cfr_pruning")
            if isinstance(pruning_info, dict):
                for key, info in pruning_info.items():
                    if isinstance(key, str) and isinstance(info, dict):
                        if key == "base" or (key.startswith("layer:") and key[6:].isdigit()):
                            parent_pruning[key] = info
            key = context.get("stream_key")
            if isinstance(key, str) and (key == "base" or (
                    key.startswith("layer:") and key[6:].isdigit())):
                streams[key] = {
                    "worker_pid": data.get("pid"),
                    "width": context.get("stream_width"),
                    "height": context.get("stream_height"),
                    "fps": context.get("output_fps"),
                    "pipe_fps": context.get("pipe_fps", context.get("output_fps")),
                    "submitted_states": context.get("submitted_states"),
                    "written_frames": context.get("written_frames"),
                    "worker_completed": context.get("worker_completed"),
                    "writer_io": context.get("writer_io", {}),
                    "stage_timings": {
                        name: {
                            "calls": metric.get("count"),
                            "inclusive_wall_seconds": metric.get("total"),
                        }
                        for name, metric in data["metrics"].items()
                        if name in STAGES and isinstance(metric, dict)
                    },
                }
        for name, metric in data["metrics"].items():
            if name not in STAGES or not isinstance(metric, dict):
                continue
            summary = stages.setdefault(name, {"calls": 0, "inclusive_wall_seconds": 0.0})
            summary["calls"] += int(metric.get("count", 0))
            summary["inclusive_wall_seconds"] += float(metric.get("total", 0.0))
    for key, queue in parent_queues.items():
        if key in streams:
            streams[key]["parent_job_queue"] = queue
    for key, stats in parent_pruning.items():
        if key in streams:
            streams[key]["cfr_pruning"] = stats
    return {"profile_files": read_files, "stages": stages, "streams": streams}


def summarize(samples: list[dict], profiles: dict) -> dict:
    """Summaries describe observed load, never an automatic bottleneck verdict."""
    core_seconds = {kind: 0.0 for kind in CATEGORIES}
    peak_rss = 0.0
    gpu_encoder: list[float] = []
    gpu_usage: list[float] = []
    gpu_engine_samples = 0
    gpu_engine_sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    gpu_engine_peaks: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for sample in samples:
        delta = float(sample.get("interval_seconds", 0))
        for kind, cores in sample.get("cpu_cores", {}).items():
            if kind in core_seconds:
                core_seconds[kind] += float(cores) * delta
        peak_rss = max(peak_rss, sum(sample.get("rss_mib", {}).values()))
        for gpu in sample.get("gpu") or []:
            gpu_encoder.append(float(gpu["encoder_percent"]))
            gpu_usage.append(float(gpu["gpu_percent"]))
        process_gpu = sample.get("process_gpu_engines")
        if process_gpu is not None:
            gpu_engine_samples += 1
            categories = sample.get("process_categories", {})
            for pid, engines in process_gpu.items():
                category = categories.get(pid)
                if category not in CATEGORIES:
                    continue
                for engine, value in engines.items():
                    gpu_engine_sums[category][engine] += float(value)
                    gpu_engine_peaks[category][engine] = max(
                        gpu_engine_peaks[category][engine], float(value),
                    )
    return {
        "observed_cpu_core_seconds": {key: round(value, 2) for key, value in core_seconds.items()},
        "peak_process_tree_rss_mib": round(peak_rss, 1),
        "device_gpu_mean_percent": round(sum(gpu_usage) / len(gpu_usage), 1) if gpu_usage else None,
        "device_nvenc_mean_percent": round(sum(gpu_encoder) / len(gpu_encoder), 1) if gpu_encoder else None,
        "process_gpu_engine_samples": gpu_engine_samples,
        "process_gpu_engine_mean_percent_by_category": {
            category: {engine: round(total / gpu_engine_samples, 2)
                       for engine, total in engines.items()}
            for category, engines in gpu_engine_sums.items() if gpu_engine_samples
        },
        "process_gpu_engine_peak_percent_by_category": {
            category: {engine: round(value, 2) for engine, value in engines.items()}
            for category, engines in gpu_engine_peaks.items()
        },
        "timing_files": profiles["profile_files"],
        "stage_timings": profiles["stages"],
        "canvas_streams": profiles.get("streams", {}),
    }


def assess_capture(target_exit_code: int | None, samples: list[dict],
                   profiles: dict) -> tuple[str, str | None]:
    """Distinguish a real export from a failed or idle app launch.

    The sampler may miss short FFmpeg processes; known export stage timers
    provide a second signal. GPU utilization alone is never proof of export.
    """
    if target_exit_code is not None and target_exit_code != 0:
        return ("target_failed",
                f"Target application exited with code {target_exit_code}. "
                "Its startup error is shown above; no valid export benchmark was recorded. "
                "Check the selected Python environment and install project dependencies.")
    observed_ffmpeg = any(
        sample.get("process_counts", {}).get("ffmpeg", 0) > 0
        or sample.get("process_counts", {}).get("rust_gpu", 0) > 0
        for sample in samples
    )
    stage_names = profiles.get("stages", {})
    observed_export_timing = any(
        name.startswith(("export.", "ffmpeg.")) for name in stage_names
    )
    if observed_ffmpeg or observed_export_timing:
        return "export_observed", None
    return ("no_export_observed",
            "No FFmpeg or Rust GPU process or export timing was observed. The samples may "
            "only describe app startup or idle GPU activity; export metrics are not validated.")


def observe_tree(psutil, root, last_cpu: dict, interval: float) -> dict:
    """One process-tree sample. CPU is measured in logical core equivalents."""
    cpu_cores = defaultdict(float)
    rss_mib = defaultdict(float)
    counts = defaultdict(int)
    process_categories: dict[str, str] = {}
    try:
        members = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        members = []
    for process in members:
        try:
            identity = (process.pid, process.create_time())
            cpu = process.cpu_times()
            elapsed_cpu = cpu.user + cpu.system
            previous = last_cpu.get(identity, elapsed_cpu)
            last_cpu[identity] = elapsed_cpu
            name = process.name()
            category = classify_process(name, process.cmdline(), process.pid == root.pid)
            cpu_cores[category] += max(0.0, elapsed_cpu - previous) / max(interval, 0.001)
            rss_mib[category] += process.memory_info().rss / (1024 ** 2)
            counts[category] += 1
            process_categories[str(process.pid)] = category
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return {
        "cpu_cores": {kind: round(cpu_cores[kind], 3) for kind in CATEGORIES},
        "rss_mib": {kind: round(rss_mib[kind], 2) for kind in CATEGORIES},
        "process_counts": {kind: counts[kind] for kind in CATEGORIES},
        "process_categories": process_categories,
    }


def collect(psutil, pid: int, seconds: float | None, interval: float,
            samples: list[dict] | None = None) -> tuple[list[dict], str]:
    """Measure only the selected editor PID and recursive export children."""
    from tools.windows_gpu_process_counters import WindowsGpuProcessCounters

    root = psutil.Process(pid)
    start = time.monotonic()
    previous_tick = start
    last_cpu: dict = {}
    samples = [] if samples is None else samples
    next_gpu_at = start
    gpu_sampler = WindowsGpuProcessCounters() if os.name == "nt" else None
    try:
        while True:
            tick = time.monotonic()
            expired = seconds is not None and tick - start >= seconds
            try:
                alive = root.is_running() and root.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                alive = False
            if expired or not alive:
                return samples, "duration_limit" if expired else "process_exited"
            delta = max(0.001, tick - previous_tick)
            sample = observe_tree(psutil, root, last_cpu, delta)
            sample["elapsed_seconds"] = round(tick - start, 3)
            sample["interval_seconds"] = round(delta, 3)
            # No machine-wide GPU measurements: only PIDs actually observed
            # inside the editor's process tree are passed to Windows PDH.
            sample["gpu"] = None
            sample["process_gpu_engines"] = None
            if tick >= next_gpu_at:
                if gpu_sampler is not None:
                    sample["process_gpu_engines"] = gpu_sampler.sample({
                        int(process_id) for process_id in sample["process_categories"]
                    })
                next_gpu_at = time.monotonic() + 1.0
            samples.append(sample)
            previous_tick = tick
            time.sleep(interval)
    finally:
        if gpu_sampler is not None:
            gpu_sampler.close()


def resolve_app_command(command: list[str], interpreter: str | None = None) -> list[str]:
    """Run bare 'python' with the profiler's interpreter, including its venv.

    On Windows the executable found by CreateProcess for a bare name can
    differ from the Python command selected by the interactive shell. Keep
    explicit paths and non-Python commands exactly as the caller provided.
    """
    if not command:
        return command
    if command[0].lower() not in {"python", "python.exe"}:
        return command
    return [interpreter or sys.executable, *command[1:]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="Attach to a running editor (stage timers need prior opt-in)")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds")
    parser.add_argument("--interval", type=float, default=0.5, help="Process sampling interval (default: 0.5s)")
    parser.add_argument("--output", type=Path, default=Path("build/export-bottleneck.json"))
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Use '-- python main.py' or '-- app.exe'")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    command = resolve_app_command(command)
    if (args.pid is None) == (not bool(command)):
        parser.error("Provide either --pid or an app command after '--'.")
    if args.interval < 0.1 or (args.duration is not None and args.duration <= 0):
        parser.error("Interval must be >= 0.1s and duration must be positive.")

    try:
        import psutil
    except ImportError:
        print("Install the diagnostic-only dependency: python -m pip install psutil", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    timings = args.output.with_suffix(".timings.json")
    process = None
    if command:
        # Prevent old worker files from being counted in a subsequent run.
        if timings.exists() or next(timings.parent.glob(f"{timings.stem}.worker-*{timings.suffix}"), None):
            timings = timings.with_name(f"{timings.stem}.{os.getpid()}-{int(time.time())}{timings.suffix}")
        env = os.environ.copy()
        env.pop("PLAYLIST_CANVAS_PROFILE_ROOT_PID", None)
        env["PLAYLIST_CANVAS_PROFILE"] = str(timings.resolve())
        print(f"Launching editor with: {command[0]}", flush=True)
        process = subprocess.Popen(command, env=env)
        pid = process.pid
        print(f"Profiling editor PID {pid}. Export a project, then close the editor to flush timings.", flush=True)
    else:
        pid = args.pid
        print(f"Sampling running editor PID {pid}. Stage timings require PLAYLIST_CANVAS_PROFILE at launch.")

    samples: list[dict] = []
    try:
        samples, ended = collect(psutil, pid, args.duration, args.interval, samples=samples)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
        print(f"Cannot inspect the target process: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        ended = "interrupted"
    if process is not None and process.poll() is not None:
        process.wait()
    target_exit_code = process.returncode if process is not None else None

    profiles = load_stage_profiles(timings)
    capture_status, warning = assess_capture(target_exit_code, samples, profiles)
    report = {
        "schema": 2,
        "gpu_measurement": "windows_pdh_process_gpu_engine" if os.name == "nt" else "unavailable_non_windows",
        "capture_status": capture_status,
        "target_exit_code": target_exit_code,
        "collected_at": datetime.now().astimezone().isoformat(),
        "target_pid": pid,
        "ended": ended,
        "run_configuration": run_configuration(),
        "samples": samples,
        "summary": summarize(samples, profiles),
        "limitations": [
            "GPU measurements are filtered to the observed editor and child-process PIDs only.",
            "GPU Engine utilization reports separate engines, not total GPU%; sums may exceed 100%.",
            "Missing Windows GPU Engine counters are reported as unavailable, never as 0%.",
            "Process CPU and memory samples may miss short-lived work; process classification is heuristic.",
            "Stage timers are inclusive wall time, may overlap and must never be summed as elapsed time.",
            "Attach mode cannot enable code timings retroactively; editor/worker timing files appear on flush.",
        ],
    }
    if warning:
        report["warning"] = warning
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Export diagnostics written: {args.output}")
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    if capture_status == "export_observed":
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return 1 if capture_status == "target_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
