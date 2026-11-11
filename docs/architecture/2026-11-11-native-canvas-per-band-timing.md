# Native Qt Canvas capture costs by band (2026-11-11)

## Hardware evidence and next question

The most recent balanced 30-second RTX 5060 1920x1080 60fps A-B-B-A
against the experimental reuse of preselected ROI SourceItems (PR #122)
did **not** establish a performance gain:

| Metric | Baseline mean | Candidate mean |
|---|---:|---:|
| Complete CLI | 96.763s | 99.953s |
| Qt Canvas capture | 52.873s | 54.716s |
| Native stream | 87.634s | 90.640s |

The individual candidate runs varied between 93.72s and 106.18s.
Both produced all 1800 frames; raster cache reported 5400 Qt
rasterizations and 1800 partial captures. PR #122 was closed unmerged.

The broad `qt_canvas_capture_seconds` total is useful but cannot say
whether a source-independent optimization should target the base,
animated lyric band, a small transparent band, or another band. This
diagnostic PR answers that question without altering painting.

## New, bounded report

For the detached export only, `native_report.raster_cache.qt_canvas_band_timing`
maps current **generic stream key** (`base`, `layer:N`) to:

- `captures`: successful uncached QPainter captures of that stream.
- `reuse_hits`: requests served directly from the export-local invariant
  BGRA cache, which have zero new Qt paint cost.
- `capture_seconds`: elapsed Qt `capture_track` calls, excluding later
  BGRA conversions and native pipe I/O.
- `state_setup_seconds`, `paint_stage_seconds`,
  `state_restore_seconds`: existing nonoverlapping internal timers.
- `partial_captures`: calls whose `capture_track` used an exact
  transparent ROI instead of painting the full source area.

Only at most eight canvas stream keys are tracked. A dynamic GPU texture
is **not** a Canvas band and is not included in these numbers. Empty or
invariant cached streams naturally have fewer Qt captures.

## Safety and accounting

This change only aggregates **existing** timing fields and reports
counts. It does not change any pixel, animation, lyric, Qt QPainter,
scene iteration, cache validity, frame bytes, FFmpeg, D3D11, Rust NVENC,
IPC or GUI setting. Per-band `capture_seconds` sums to the original
`qt_canvas_capture_seconds`; likewise each internal stage sums to
the original global stage time. These stage timers belong to Qt and may
overlap worker encode/sending; do not sum them with the native stream
time to estimate export wall duration.

Tests verify exact BGRA parity before and after instrumentation across
static base reuse plus animated text bands, per-band timer sums,
correct counts and cleanup. Require the complete latest Windows
serial/Qt/lyrics workflow and real audio Canvas packet workflow to
succeed before squash merging diagnostics. CI success does not
establish any speed improvement.

## Subsequent measurement-driven work

Run the standard 30-second 1080p60 project once on `main` after merge,
retrieve `qt_canvas_band_timing`, and identify whether time accrues
primarily to scene state setup or QPainter paint for specific band types.
Do not hardcode a source name, visualizer, song or one project into the
automatic optimizer. Any future behavior-changing optimization must
pass isolated real Qt pixel/PTS/lyrics/audio tests and its own hardware
A-B-B-A before merge.
