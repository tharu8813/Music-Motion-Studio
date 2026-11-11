# Native Canvas ROI bounds reuse (2026-11-11)

## Evidence and scope

The recent four-run RTX 5060 test compared a Rust per-pixel opaque-base alpha
validator with a two-pixel predicate. The isolated CPU function ran 1.94x
faster, but overall 30-second 1080p60 export was effectively unchanged
(baseline average 92.447s, wide average 92.656s). That Rust optimization
experiment was closed unmerged. A broader, measurable bottleneck remains
Qt Canvas production and its per-frame scene handling.

In those same four reports, every export produced 1800 frames and performed
**5400 Qt rasterizations**. Native Qt Canvas capture took about 50 seconds
(including state setup and painting, overlapping the sending pipeline);
the native engine automatically used pixel-aligned partial ROI captures on
1800 transparent bands.

## Narrow optimization

Within `CanvasSnapshot.capture_track`, an export with
`band_source_items` already holds the exact visible, ordered SourceItem
subset for this Z band. The partial ROI bounds checker previously called
`scene.items()` **again** to re-enumerate the entire scene and filter by
Z/visibility. This experiment passes the already filtered live
`source_items` to `_dynamic_band_capture_rect`.

- The helper still checks the same **current** item visibility, type,
  Z bounds, geometry animation, shadow padding, anti-alias padding,
  artboard bounds, opacity-only keyframes and motion safety conditions.
- SourceItems are not copied: transforms and source state are live after
  per-frame track updates.
- When `band_source_items` is not provided, the original full-scene
  bounds scan is untouched.
- Native packet bytes, ROI crop alignment, pixel expansion, v1/v2 schema,
  D3D11/NVENC, audio, lyrics, frame ordering, scene paints, and FFmpeg
  export semantics are otherwise unchanged.
- No new GUI setting, no project/source-specific name or hardcoded layout.

A regression test compares the per-band cached bounds against the legacy
full `scene.items()` bounds *at the same moment after track updates*,
including shadows, animated text, a disabled source, three bands,
multiple timestamps and 0.5/1.0/1.5 scales. It verifies exact complete
BGRA packets against uncached/full-scene render after each capture.

## Performance gate

This is a deliberately **experimental** reduction in redundant Qt
enumeration, not a validated export-speed win. A previous, broader
precomputed Z-band exclusion experiment (PR #115) was not adopted because
hardware performance regressed; this narrower change must earn its own
performance evidence.

Require the latest Windows serial Qt tests, real Canvas audio packets,
pixel parity and mergeability checks to pass. Then run a balanced
A-B-B-A same-machine 30s, 1080p, 60fps test comparing original main
against candidate **with the same project, ffmpeg, Rust worker,
Python interpreter, Qt configuration, automatic GPU profile and output
FPS**. Use separate worktrees and output files, capture full wall time,
native stream wall, `raster_cache.qt_canvas_capture_seconds`,
`qt_canvas_state_setup_seconds`, `qt_canvas_paint_stage_seconds`,
`frame_pipe_write_seconds` and packet/image parity.

Subcomponent timers overlap and should not be summed. Only if it
consistently improves meaningful wall time without pixel differences
should the implementation be promoted into main. Otherwise close the
PR without merging and investigate a higher-leverage source-independent
rendering change.
