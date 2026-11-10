# Rust GPU process-scoped performance diagnostics (Windows)

The normal editor still exposes **FFmpeg** and **Rust GPU** only. This
diagnostic does not add any user optimization switches or change video pixels.

## Why measure processes, not the entire machine?

`tools/export_bottleneck_profile.py` samples the requested editor/CLI process
and its recursive children. CPU utilization is reported as **logical CPU core
equivalents**, with categories `editor`, `canvas_workers`, `rust_gpu`,
`ffmpeg`, and `other`. A value of 1.0 represents approximately one fully
busy logical CPU core, not 1% of the whole PC.

On Windows, `tools/windows_gpu_process_counters.py` reads the OS
`GPU Engine(*)\Utilization Percentage` performance counter. It stores **only
instances matching PIDs from that process tree**, separated by engine
(`3d`, `copy`, `videoencode`, etc.). This can distinguish Qt painting,
GPU uploads/copy work, and NVENC more accurately than the GPU-wide Task
Manager percentage. GPU Engine values are not overall GPU utilization:
different engines can execute concurrently and grouped values may exceed 100%.

No unrelated-process CPU/GPU data is saved. GPU sampling is unavailable on
non-Windows systems and can also be unavailable on Windows without supported
GPU drivers/performance counters; missing counters must **not** be treated as
zero utilization. Device-wide `nvidia-smi` readings are not taken by default.

## One 30-second local Rust GPU smoke run

Run in PowerShell from the repo root, after updating `main` and building the
Rust executable for the same commit. Use an output/report filename that does
**not** already exist.

```powershell
git switch main
git pull --ff-only
python -m pip install psutil
New-Item -ItemType Directory -Force build | Out-Null
python tools/export_bottleneck_profile.py --output build/gpu-process-auto.json -- python -m tools.native_gpu_full_export `
  --project "C:\Users\wlqor\Desktop\giggles.pvsproj" `
  --output "C:\Users\wlqor\Desktop\giggles-gpu-process-auto.mp4" `
  --native-exe ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe" `
  --fps 60 --scale 1 --smoke-seconds 30 `
  --gpu-profile automatic --report build/gpu-native-auto.json
```

`--gpu-profile` is **developer CLI A/B only**, never a GUI choice. This
generates a 30-second clip, not a whole-project video. It uses the first track,
so it may not characterize the most expensive animated scenes elsewhere.

After the process exits, inspect:

- `build/gpu-process-auto.json`: timestamped CPU/RAM by process type, PIDs
  from the actual export process tree, and **per-PID GPU engine** metrics.
  Summary has `observed_cpu_core_seconds`,
  `process_gpu_engine_mean_percent_by_category`, and
  `process_gpu_engine_peak_percent_by_category`.
- `build/gpu-native-auto.json`: native `frame_production_seconds`,
  `frame_pipe_write_seconds`, `frame_ack_wait_seconds`,
  `native_stream_wall_fps`, plus `raster_cache` Qt/inline BGRA counters.
  The complete export and native stream timings can overlap; do not add them.
- Produced MP4: confirm frame timing, lyrics and output quality.

For an existing UI export, use the process's **own PID** (the editor, not a
generic `python.exe` from another project):

```powershell
python tools/export_bottleneck_profile.py --pid 12345 --duration 60 --output build/gpu-process-ui.json
```

Attach mode provides process CPU/RAM/GPU, but cannot turn on the editor's
Python stage timers retroactively. For stage timing use the fresh CLI export
above. If the GUI is already rendering, attaching does not interrupt it.

## Interpreting a result

- High `editor` CPU-core load with low Rust `videoencode` and high
  `frame_production_seconds`: inspect Qt/Python frame generation.
- Large `frame_pipe_write_seconds`: inspect frame bytes, hashing, pipe
  backpressure, and native packet validation.
- Large `frame_ack_wait_seconds`: inspect D3D11 texture submission, NV12
  slots, and NVENC synchronization.
- Sustained high `rust_gpu` `videoencode`: encoding may be the limit;
  confirm by timing counters before changing encoding parameters.

Send the two JSON reports and the actual observed FPS after reviewing them
for local paths/media metadata. They are more actionable than screenshots
of total CPU/GPU use.
