# P1 Qt Canvas background and lyric setup substage profile (2026-11-14)

## Why

Actual RTX 5060 5-second (1920×1080, 60fps, 300 frames) developer
`--gpu-profile-qt-state` report showed **9.0098s** full CLI time.
The same profile recorded Qt Canvas **5.3941s** capture,
**4.1769s** paint and **1.0061s** state setup (these stages
overlap the native transport/worker; do not add them to CLI time).
SourceType state setup was:

| SourceType | Calls | Accumulated seconds |
|---|---:|---:|
| background | 300 | 0.55448 |
| lyrics | 300 | 0.24513 |
| text | 1500 | 0.14523 |
| progress_bar | 300 | 0.02182 |
| album_cover | 300 | 0.01624 |

This indicates **where to inspect**, not the amount that can be
removed. For example, album artwork and ambient background already
have bounded caches; blindly adding another cache or skipping
per-frame resets risks flicker, source-state leaks and output drift.
The existing `--gpu-profile-qt-paint` independently times Qt item
paint and text/glyph subcalls. The paint stage remains the larger
total CPU Canvas stage.

## Developer-only extension

No additional UI or config option: the existing
`--gpu-profile automatic --gpu-profile-qt-state` now also reports,
only in the final `native_report.raster_cache` snapshot:

```json
{
  "qt_canvas_state_detail_costs": {
    "background": {"album_art": {"calls": 300, "seconds": 0.0}},
    "lyrics": {"cue_state": {"calls": 300, "seconds": 0.0}}
  }
}
```

The sample numbers above are **illustrative schema**, not measured
stage values. Stages time the existing background album-art/ambient
artwork setup and lyric cue state/layout block, respectively.
They are **inclusive within each corresponding SourceType state
setup bucket**, so never add their duration to the outer parent
category. A project's non-album background may not emit `album_art`.
No source IDs, lyric text, titles, paths, pixel bytes or
objects are stored in the report.

Profile timing adds overhead. All default automatic and GUI paths
retain the original state evaluation and painting, and the new
stage counters are disabled. No native packet, encoder, source
order, or GUI setting is changed.

## One more developer diagnostic on Windows

For a single 5-second capture, collect both state and paint data
in one report, with distinct output/report paths (never overwrite):

```powershell
cd C:\Users\wlqor\Desktop\playlist_project
git switch main
git pull --ff-only
$native = (Resolve-Path ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe").Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
python -m tools.native_gpu_full_export `
  --project "$env:USERPROFILE\Desktop\giggles.pvsproj" `
  --native-exe $native `
  --output ".\p1-qt-detail-$stamp.mp4" `
  --report ".\p1-qt-detail-$stamp.json" `
  --scale 1.5 --fps 60 --smoke-seconds 5 `
  --gpu-profile automatic --gpu-profile-qt-state --gpu-profile-qt-paint
```

The report identifies whether album-art refresh or lyric state
work is the more promising setup target; paint source categories
and `qt_canvas_text_stage_costs` will also locate the largest
paint substage.

These **instrumented** wall times are not an optimization result.
Any future behavior-changing acceleration needs a separate
30s/1080p60 2-project A-B-B-A on the RTX 5060 with same-commit
total-wall comparison, full decoded video and PCM SHA-256 parity,
and every PTS identical. Previous `alpha_trim` and
`equal_pixels` automatic-promotion decisions remain REJECT.

## Verification

- Existing profiler gate still rejects implicit or non-automatic
  developer CLI profiles.
- Real FFmpeg/Qt/audio 30-frame test captures real album artwork
  and lyrics, compares all resolved BGRA frames with the original
  automatic output, checks input project immutability, and asserts
  both substage buckets are positive and bounded by their parent
  SourceType state category.
- Ordinary automatic native report must never contain stage data.
- Latest Windows build, isolated Python, cover capture, native
  real-audio Canvas packet, and any added GPU validation jobs
  must all finish success before squash merge.
