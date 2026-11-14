# P1 Qt glyph-shadow raster cache profiling (2026-11-15)

## RTX 5060 source measurement

User-supplied `p1-scene-detail-20261115-021718.json`: completed,
1920x1080/60fps/5s, 300/300 frames, source SHA-256
`672e449b051ba89a52683e4be2b15282f75ab648d74cff00364a2fddb805445f`.

- Full instrumented CLI 12.06599s / native stream 9.93249s (30.20 fps).
- Qt Canvas capture 7.01559s, paint 5.50104s, state setup 1.30895s.
- The latest PR #156 substage profile: 900 QImage preparations 0.89128s,
  `QGraphicsScene.render` 4.04465s, `CanvasSnapshot.capture` 5.06426s.
  The three stages have inclusive/overlapping time and must **not** be added.
- Text: 1,497 paint calls 1.81233s, text draw 1.68777s; within text draw,
  297 glyph-shadow calls 1.56630s (~92.8% of text drawing). This is ~38.7%
  of the scene rendering stage, but still nested in the parent time.
- Album artwork state 0.49098s and lyric cue state 0.51674s (both measured
  inside their SourceType state setup stages).
- Worker NVENC 0.69083s, shared packet decode 2.46427s, frame wire
  5,145,231,173 bytes. Other stage timings can overlap due to pipelining.

This is **one profiling run**, not an A-B-B-A speed result. Per-run timer
overhead, dynamic content and GPU/OS variability prevent a performance claim.

## Why inspect glyph-shadow cache before optimizing?

`SourceItem._draw_glyph_shadow` already memoizes the exact blurred shadow
pixmap in `_text_shadow_cache`, with a 64-entry per-item cap and clear-on-limit
behavior. Repeated work may mean a new text/style/cache key each frame, or it
may be the cost of drawing a cached pixmap. Both require different fixes.

## Developer-only report

The existing `--gpu-profile automatic --gpu-profile-qt-paint` now also
emits one bounded final snapshot:

```json
{
  "qt_canvas_glyph_shadow_cache_costs": {
    "text": {
      "rasterized": {"calls": 1, "seconds": 0.0},
      "reused_or_empty": {"calls": 1, "seconds": 0.0}
    }
  }
}
```

The values are **schema examples**, not RTX measurements. Each entry
aggregates a generic SourceType, not source IDs, text or cache keys.
`rasterized` means the item's shadow-pixmap cache changed size during the
original shadow draw: a new entry was inserted, including the 64-entry
clear-and-insert path. `reused_or_empty` means no new cache entry was
inserted, including valid cache hits and empty glyph paths.

The existing inclusive `qt_canvas_text_stage_costs.text.glyph_shadow`
still measures the same total shadow drawing. The two new buckets partition
that time, and must not be added to their inclusive parent. No extra QPixmap
or pixel data is retained.

**Scope:** only the `_ProfiledNativeSourceItem` development subclass,
with original SourceItem pixels and GUI/automatic worker code untouched.
No automatic cache sizing, rendering, protocol, FFmpeg or NVENC changes.

## Tests and next Windows capture

- Standalone real-QPainter test with two identical shadow draws and one
  changed string: the two identical outputs must have identical BGRA bytes;
  first and third calls rasterize and second reuses the existing shadow.
- The real-audio 30-frame frame-production test checks final-only report
  presence for developer paint profiling and absence in ordinary automatic.
- Windows build, isolated Python groups, real-audio Canvas, cover capture
  and Canvas GPU candidate checks must **all complete success** before
  merge; queued/running or failed checks prohibit merge.

After verified squash merge, run the same 5-second profiler:

```powershell
cd C:\Users\wlqor\Desktop\playlist_project
git switch main
git pull --ff-only
$native = (Resolve-Path ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe").Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
python -m tools.native_gpu_full_export `
  --project "$env:USERPROFILE\Desktop\giggles.pvsproj" `
  --native-exe $native `
  --output ".\p1-shadow-cache-$stamp.mp4" `
  --report ".\p1-shadow-cache-$stamp.json" `
  --scale 1.5 --fps 60 --smoke-seconds 5 `
  --gpu-profile automatic --gpu-profile-qt-state --gpu-profile-qt-paint
```

Performance candidates must still pass **two real projects**,
1080p60/30s RTX 5060 A-B-B-A with full decoded-video/PCM/PTS parity
before any automatic promotion. The previous `alpha_trim` and
`equal_pixels` performance experiments remain rejected.
