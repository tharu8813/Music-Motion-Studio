# P1: Skip no-op keyframe scopes during Qt Canvas capture

## Measurement driving this change

The user-supplied `p1-giggles-20261114-062751.json` report
(2026-11-14; 30s, 1,800 frames, **1280x720**, not 1080p)
records 22.878s CLI wall time and 85.02 native stream FPS.
The Qt Canvas capture accounted for 15.461s, including 5.647s
state setup and 9.260s painting (overlapping other pipeline stages).
Full-frame SHA-256 is also material: 9.504 CPU seconds / 5.755 wall,
and 10.393 GB of wire payload were observed. NVENC itself
reported 2.030 seconds. These are **not additive stage timers**.

The existing font configuration substage measured only 0.017s
for regular text in this sample, so another font-cache change is
not a rational next target.

## Minimal CPU path change

In `CanvasSnapshot.capture_track`, numeric keyframe evaluation returns
an empty dictionary for many ordinary sources. The subsequent
`keyframe_source` context-manager call is a no-op for an unchanged
source except for the **editor blur restore case**. Avoid its creation
and `ExitStack.enter_context` when no numeric curves are active,
while retaining the established scope for animated sources or the
editor state containing a previous blur value.

The Qt scene, render order, lyric positions and native BGRA/MP4
pipeline are unchanged. Targeted Qt tests verify identical raw
raster pixels for static text, animated source restoration, and
editor blur handling.

Performance improvement is a **hypothesis**, not measured from this
single report. The next test should run the same giggles project
at 1080p (1280x720 Canvas needs `--scale 1.5`) on the user's RTX
5060, then compare previous/current revisions under matching
30s/60fps conditions. The P1 A-B-B-A verifier checks all frames
and decoded PCM; an instrumented `--gpu-profile-qt-paint` run
must not be compared directly to an uninstrumented one.
