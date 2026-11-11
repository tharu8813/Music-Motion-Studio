# Qt Canvas detached-export scene enumeration reuse

## Why this follows the RTX 5060 measurements

Same 1920×1080, 60FPS, 30-second clip, 1,800 frames from
`giggles.pvsproj` (identical source SHA-256):

| Metric | Last validated main (#109) | Experimental PR #110 |
| --- | ---: | ---: |
| Native FPS | 26.767 | 22.144 |
| Full CLI seconds | 74.407 | 89.432 |
| Frame production seconds | 48.570 | 68.981 |
| Qt canvas capture seconds | 36.945 | 52.541 |
| SHA sender wall seconds | 13.854 | 16.351 |
| Wire bytes | 48.277GB | 48.277GB |
| Frames/PTS | 1800/1800 | 1800/1800 |

**Caution:** PR #110 changed Rust allocation logic, whereas measured Qt capture
time increased +15.6 seconds. The observed regression cannot confidently
be attributed to that change without repeat matched A/B runs. Therefore PR
#110 stays unmerged despite green CI until a causal and performance review.

The export's `NativeRasterCache` already preselects members for each source
Z-band. However, `CanvasSnapshot.capture_track` still calls
`_dynamic_band_capture_rect` without that ready band list, which forces
`QGraphicsScene.items()` on every partial Z-band capture. The lower-level
`CanvasSnapshot.capture` also enumerates `scene.items()` whenever it hides
out-of-band sources. A 1,800-frame, 3-band export can trigger thousands of
redundant scene-wide enumerations in this hot path.

## Bounded changes

- In the detached immutable native scene, cache a tuple of **all**
  `SourceItem` objects once per export. Include initially hidden items; a
  source may be visible in a subsequent timeline frame.
- Pass this tuple only via optional internal keyword arguments to
  `CanvasSnapshot.capture_track` and `CanvasSnapshot.capture`.
- `CanvasSnapshot.capture` still checks **live** `isVisible()` and live
  `source.z_index` before temporarily hiding out-of-band sources, and
  restores visibility on exit, preserving clipping and Z composition.
- Reuse the existing already-filtered `source_items` list for calculating
  partial Z-band capture rectangles, retaining current motion/bounds checks.
- All other preview/FFmpeg/diagnostic calls have unchanged default arguments
  and retain their previous scene traversal code.
- No GPU engine changes, no new settings, no frame buffers retained by the
  cache beyond short-lived Qt object references already owned by the export.

## Verification

`tests.test_native_layer_cache` compares dynamic 3-band Qt captures at
multiple timeline positions against the original uncached painter. A
`scene.items` spy asserts that the optimized captures do **not** enumerate
the full QGraphicsScene on each frame/band. Existing scene parity,
TTML/keyframe, partial ROI and compact-band tests must all pass.

**Performance is not yet claimed.** PR #111 must pass Windows CI first and
then be benchmarked on the user's RTX 5060 against #109's 26.767 FPS /
74.407 seconds. Record Qt canvas capture and frame production times, not
just end-to-end FPS. All phase duration counters can overlap.
