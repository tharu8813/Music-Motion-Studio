# P1 Qt scene-render capture timing (developer-only)

## Why

The user's RTX 5060 5-second / 1920x1080 / 60fps / 300-frame diagnostic
(`p1-qt-detail-20261114-230126.json`) measured 12.1971s CLI wall time,
8.0285s Qt Canvas capture, 6.1663s Qt paint, and 1.5453s state setup.
The two uploaded JSONs are byte-identical and are **one run, not a repeated A/B**.

The same diagnostic measured SourceItem paint categories (text 0.9026s,
lyrics 0.7970s, background 0.4796s, album_cover 0.4076s, progress_bar
0.1597s). Their total is not the entire Qt paint stage: QGraphicsScene,
QImage buffer preparation, clipping, and band setup have their own costs.
The new counters narrow this distinction before choosing any behavior-changing
optimization.

## Scope and output

Existing developer-only `--gpu-profile automatic --gpu-profile-qt-paint`
now adds `native_report.raster_cache.qt_canvas_capture_stage_costs`, with
bounded `{calls, seconds}` counters:

- `image_prepare_seconds`: QImage allocation/reuse/fill or background-image copy.
- `scene_render_seconds`: CPU `QGraphicsScene.render` through the existing
  QPainter, **including** SourceItem paint calls and their text substages.
- `capture_total_seconds`: CanvasSnapshot.capture around render, item
  selection/masking and restoration. It **includes both** previous counters.

The counters measure actual QImage CPU paths; OpenGL or prepared-band paths
may omit a substage. Do not infer `scene_render - SourceItem sum` as exact
framework overhead without accounting for Qt internal work and timer costs.
All counters are final-report only. There are no source titles, lyrics, paths,
IDs, images, or per-frame records.

No GUI, production automatic behavior, renderer output, alpha-trim, equal-pixel
reuse, packet format, Rust, FFmpeg or NVENC changes. The default/automatic
report has no added key. Developer profiling introduces timing overhead and
cannot establish a speed gain.

## User hardware validation

After all required CI succeeds and the diagnostic-only PR is merged, use the
existing 5-second command with both state and paint profiling:

```powershell
cd C:\Users\wlqor\Desktop\playlist_project
git switch main
git pull --ff-only
$native = (Resolve-Path ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe").Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
python -m tools.native_gpu_full_export \`
  --project "$env:USERPROFILE\Desktop\giggles.pvsproj" \`
  --native-exe $native \`
  --output ".\p1-scene-detail-$stamp.mp4" \`
  --report ".\p1-scene-detail-$stamp.json" \`
  --scale 1.5 --fps 60 --smoke-seconds 5 \`
  --gpu-profile automatic --gpu-profile-qt-state --gpu-profile-qt-paint
```

If a behavior-changing candidate is later proposed, require RTX 5060
1920x1080/60fps/30s two-project A-B-B-A and full decoded video, PCM and PTS
parity before considering automatic promotion. Previous `alpha_trim` and
`equal_pixels` performance candidates stay rejected.

## Verification requirements

- Real FFmpeg + Qt 30-frame profiling report has positive scene and buffer
  counters, bounded by the containing paint stage.
- The exact same real project exports equal resolved BGRA/native frames with
  developer profiling on versus ordinary automatic off.
- Ordinary automatic has no new report key; report aggregation remains final-only.
- All required Windows and real-audio/native Canvas CI checks must finish
  success before squash merge. A queued/running check blocks merging.
