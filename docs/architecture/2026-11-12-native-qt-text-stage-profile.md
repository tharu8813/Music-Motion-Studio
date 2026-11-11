# Qt Canvas text-rendering stage profile (2026-11-12)

## Source-backed motivation

An opt-in 5-second 1920x1080 60fps / 300-frame RTX 5060
diagnostic on `main` after PR #127 reported:

| Qt SourceType | Paint calls | Accumulated SourceItem.paint | Mean per call |
|---|---:|---:|---:|
| text | 1,498 | 1.6371507s | 1.093ms |
| lyrics | 293 | 0.5964265s | 2.036ms |
| background | 300 | 0.2833904s | 0.945ms |
| album_cover | 300 | 0.2243971s | 0.748ms |
| progress_bar | 300 | 0.0320725s | 0.107ms |

Total Qt paint stage: **4.3001126s**; the direct SourceItem
paint callbacks sum to ~2.7734372s, with ~1.5266754s outside
these callbacks (scene traversal/compositing/QPainter/Qt and
measurement gaps). Text + lyrics account for **~51.9%** of
the Qt paint stage. Canvas setup: 2.4348738s; the final
5-second CLI wall was 15.9508387s. The same short export moved
7,344,713,357 frame-transport bytes via binary-v3.

These measurements identify text paint as a candidate, but do
**not** say whether font construction, text template resolution,
text layout, vector outlines or QPainter draw calls dominate.
Some paths already cache results. Blindly caching them again
risks pixel differences for animation, text effect, and kerning.

## Additional diagnostic, normal export unchanged

The existing `--gpu-profile automatic --gpu-profile-qt-paint`
developer-only path now also records exclusive calls and inclusive
wall seconds from a few existing `SourceItem` methods in the same
`_ProfiledNativeSourceItem` subclass:

- `font`: `text_font`, QFont creation and typography properties
- `template`: `_render_text` including known placeholder labeling
- `draw_text`: `_draw_text` including glyph outline/glow/draw
- `layout_path`: `_text_layout_path` glyph shaping/vector contours
- `glyph_shadow`: `_draw_glyph_shadow` shadow/glow raster and cache

The bounded report mapping is
`native_report.raster_cache.qt_canvas_text_stage_costs` with
`{SourceType: {stage: {calls, seconds}}}`. No text, lyrics, source
IDs, titles, media paths or other content appear in it. Each
source/category/stage bucket is updated on the same GUI thread.

**IMPORTANT**: Stage names are inclusive nested timings.
For example `draw_text` includes `layout_path` and
`glyph_shadow`; sum of substage durations would double count
work. Compare each stage with the outer
`qt_canvas_source_type_paint[type].seconds` and infer residual.
The clock wrappers add profiling overhead; these wall seconds
must **not** be compared to an uninstrumented export as an
optimization result. No production `SourceItem` method, frame
pixels, Qt painter, decoder, GPU compositing, protocol or user
configuration is changed.

## Test and adoption gate

Latest CI must run real Qt dynamic text pixel parity across
timestamps and exercise a nonzero glyph stroke/vector layout
on two independent scenes; same exact output BGRA bytes are
required. A real 30-frame dynamic-audio export test confirms
the stage counters are actually emitted, positive when invoked,
and bounded by each type's parent paint time (with small
measurement margin). Windows build, translation/keyframe
regression and actual Canvas/audio packets must all complete
success. This diagnostics-only PR can be squash-merged after
checks, without a hardware speedup claim.

Only after merging, run the same **one 5-second**
developer RTX diagnostic on the verified new main. The
usual output JSON now contains text stages; no new CLI
flags or UI controls are necessary. Use the measured
dominant text substage to design a separate **experimental**
optimization. Require full-project same-commit
30s/1080p60 A-B-B-A *and decoded video/audio SHA parity*
before any behavior-changing change is merged.
