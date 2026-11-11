# Native Qt Canvas paint time by source type (2026-11-12)

## Evidence from PR #126

Four actual RTX 5060 30s 1920x1080 60fps reports (1,800 frames,
5,400 Qt band captures) compared unchanged automatic against skipping
empty keyframe ExitStack contexts, A-B-B-A.

| Metric | Baseline mean | Candidate mean |
|---|---:|---:|
| Full CLI wall | **80.0601s** | 80.9230s |
| Qt state setup | **16.2952s** | 16.7001s |
| Qt paint stage | **23.7075s** | 24.0474s |
| Native stream | **72.1477s** | 73.3590s |

Candidate **skipped 16,200 identity contexts per export** in its
3 Canvas Z bands and still had no meaningful full-export speedup.
This demonstrates that Python ExitStack bookkeeping is not the
dominant bottleneck for that project. PR #126 was closed unmerged.
Even after earlier per-band diagnostics (#123), the 24s aggregate
Qt paint stage cannot identify expensive source *types*.

## Developer-only opt-in profiling

`--gpu-profile-qt-paint` requires explicit
`--gpu-profile automatic` in `tools.native_gpu_full_export`.
It opts the *detached project frame generator* into a
`_ProfiledNativeSourceItem` subclass of the production `SourceItem`,
rather than touching production `SourceItem.paint` or sharing global
profiling data. The subclass calls `super().paint` with the exact
original arguments and uses `monotonic()` around it in a `finally`
block.

The report gains a bounded
`native_report.raster_cache.qt_canvas_source_type_paint` mapping from
**generic SourceType enum values** (not track/title/source IDs/text)
to `{calls, seconds}`. Both counters are aggregated inside the
existing GUI-thread-only Qt renderer. Any thrown paint exception
continues to propagate normally, so the profile cannot conceal bugs.
The report does not retain QImages or record media, identities, or
lyrics text.

A `paint()` call includes the specific Python/Qt renderer work
(including lyric layout or pixmap draws), but does **not** include the
QGraphicsScene background/fill, compositor, scene traversal, QPainter
setup, paint-related native Qt overhead outside the callback, or
the Python frame-state setup. Source-type categories add up to at most
the known SourceType registry; their cumulative `seconds` should
normally be below the existing `qt_canvas_paint_stage_seconds` wall
metric. Instrumentation itself adds Python timing overhead and must
**never be used as evidence that profiling accelerates production**.

Example single 5-second diagnostic (developer Windows host only):

```powershell
python -m tools.native_gpu_full_export `
  --project "C:\\Users\\wlqor\\Desktop\\giggles.pvsproj" `
  --output "C:\\Users\\wlqor\\Desktop\\mms-profile-qt-type-5s.mp4" `
  --report "C:\\Users\\wlqor\\Desktop\\mms-profile-qt-type-5s.json" `
  --native-exe "C:\\Users\\wlqor\\Desktop\\playlist_project\\native\\gpu_export_core\\target\\release\\d3d11_qt_canvas_stream_export.exe" `
  --fps 60 --scale 1.5 --smoke-seconds 5 `
  --gpu-profile automatic --gpu-profile-qt-paint
```

The sample size of 5 seconds may not include late lyrics or transitions;
it is for bottleneck discovery, not an end-to-end performance A/B.
The full 30 seconds remain appropriate for deciding optimization
adoption. The CLI `--gpu-profile-qt-paint` flag changes **only
instrumentation**, not frame pixels, NAL, NVENC, audio or UI settings.

## Safety and merge criteria

- Real headless Qt pixel-parity test: separate scenes, actual
  SourceType.SHAPE + dynamic SourceType.TEXT, multiple animation
  timestamps, 1.5 scale; bytes of normal/diagnostic captures match.
  Tests verify that Qt really invokes the subclass paint override and
  counts SourceType categories.
- Real audio Canvas export-frame test: 30 frames, bounded metrics,
  preservation of the original ProjectDocument, category paint
  seconds not exceeding the aggregate paint stage (with tolerance).
- CLI validation: the profiler cannot be selected on any other profile
  or implicit GUI default.
- All **latest** Windows serial/Qt/lyrics/GPU and audio Canvas checks
  must succeed, no conflicts or review blockers. Since ordinary
  production constructs only unmodified SourceItem objects, this
  optional diagnostic may be squash-merged after these correctness
  gates without a hardware speedup claim.

After merge, run the single short developer diagnostic once and
inspect the per-type paint costs. The next **behavior-changing** paint
optimization must be general-purpose, safe for all SourceTypes, tested
for raw pixel/audio parity and show a repeatable full-export speedup
before it can be merged.
