# P1: Qt Canvas state setup by SourceType (2026-11-14)

## Motivation

User RTX 5060, `giggles.pvsproj`, 1920x1080/60fps/30s,
A-B-B-A measurements rejected `alpha_trim` and `equal_pixels`
on total export wall time. Uninstrumented automatic averaged 59.9917s;
Qt Canvas capture accounted for ~40.89s (paint ~30.44s,
state setup ~8.83s). These overlap native pipeline wall time and
must never be summed.

Unlike the existing opt-in SourceItem **paint** profiler, this
developer-only addition profiles each SourceItem's **per-frame state
setup**, including hidden/out-of-timeline skip decisions. It
aggregates `{SourceType.value: {calls, seconds}}` at export completion,
plus separate pre-loop Qt/track setup time. No source names,
file paths, lyric content, Qt object references or captured images are
retained in the reports. No per-frame nested report copies.

The new flag `--gpu-profile-qt-state` requires explicit
`--gpu-profile automatic`. It is disabled by default for every
NativeGpuOptimizations profile, the GUI and normal native exports.
Qt rendering logic, restoration, timeline, encoded pixels, Rust worker
and wire protocol are unchanged.

## Output fields

Only profiled exports include these fields under
`native_report.raster_cache`:

- `qt_canvas_source_type_state_setup`: mapping of generic SourceType
  values to `{calls, seconds}`
- `qt_canvas_state_preloop_seconds`: cumulative time before the
  per-source state setup loops, including canvas and track preparation

The existing `qt_canvas_state_setup_seconds` is inclusive of this
work and the timing instrumentation; an individual category is
not guaranteed to be a standalone removable optimization cost.
Measurements must be compared **within the same instrumented report**,
not as if enabling the profiler speeds up or slows down production.

## RTX diagnostic (one real 5s MP4)

```powershell
cd C:\Users\wlqor\Desktop\playlist_project
git switch main
git pull --ff-only
$native = (Resolve-Path ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe").Path
python -m tools.native_gpu_full_export `
  --project "$env:USERPROFILE\Desktop\giggles.pvsproj" `
  --native-exe $native `
  --output ".\p1-state-profile-5s.mp4" `
  --report ".\p1-state-profile-5s.json" `
  --scale 1.5 --fps 60 --smoke-seconds 5 `
  --gpu-profile automatic --gpu-profile-qt-state
```

Use unique output/report filenames if paths already exist. The 5s
profile is for choosing the next hypothesis, **not a performance
adoption gate**. A later behavior-changing optimization must prove
30s/1080p60 A-B-B-A total-wall improvement across two representative
projects with decoded video/PCM SHA-256 and every PTS identical.

## Safety and checks

- Reject the profiler CLI flag unless explicitly in automatic mode.
- 30 genuine Qt/audio frames: instrumented vs uninstrumented BGRA
  parity, project immutability, valid bounded aggregate counters.
- Uninstrumented native exports do not expose the opt-in counters.
- Full latest Windows build, isolated Python and GPU Canvas packet CI
  must succeed before squash merge.
- Windows CI is not proof of RTX speed or Windows 11 consumer GUI
  acceptance.
