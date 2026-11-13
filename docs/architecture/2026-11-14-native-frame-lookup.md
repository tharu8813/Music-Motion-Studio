# P1: Frozen native export frame lookup

`resolve_project_frame` previously rebuilt its enabled-track list,
track-ID dictionary, track position and intro/outro animation durations
from the whole source list for **every CFR output frame**. The Rust GPU
exporter already deep-copies its input project and never intentionally
mutates that snapshot during export.

`build_native_frame_lookup` now precomputes all four values once for a
frozen native export. The generator passes this bounded in-memory lookup
to each frame; other callers, including live or ad-hoc frame inspectors,
continue using the unchanged dynamic lookup behavior.

Tests compare the cached state dictionary to the old result across a
fractional track boundary and final frame of a three-hour playlist and
assert `_animation_durations` is never invoked inside the cached frame
loop. The original frame presentation clock, layer order, lyric timing
and output pixels are unchanged.

This removes repeated Python metadata scans. Actual 1080p60 RTX 5060
performance gains are not yet measured and require real hardware
A-B-B-A timing and full decoded media validation.
