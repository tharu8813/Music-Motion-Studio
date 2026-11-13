# P1: Final-only native GPU raster diagnostics (2026-11-14)

`produce_complete_project_frames` previously rebuilt the entire
`raster_counters` dictionary for **every video frame**, even though the
production export consumes these metrics only after the iterator is fully
closed. This included dozens of counters and copies of nested
`qt_canvas_band_timing`, source-paint and text-stage dictionaries.
For 30s × 60fps the snapshot was rebuilt up to 1,800 times.

The counter snapshot now occurs **once in the generator's finally block**,
including on cooperative cancellation or early iterator close. Live counters
inside `NativeRasterCache` still accumulate once per actual rasterization.
The snapshot of retained bytes and texture counts is captured **before**
`NativeRasterCache.clear()` releases caches. After cleanup the existing
post-join CPU converter totals, Qt timers and the zero retained-bytes
invariant replace the same final fields as before.

No Canvas input, timed lyric state, GPU layer ordering, BGRA packet, Rust
worker protocol, or output MP4 representation changes. The only API
behavior difference is that `raster_counters` remains unpopulated during
an active generator and is populated at iterator exhaustion/close. The
current consumers in `native_project_export.py` and
`tools/native_resident_cache_probe.py` read it **after** iteration.
A regression test verifies the final-only lifecycle, complete Qt paint
details and zero bytes remaining after cleanup.

This removes predictable repeated Python dictionary work but is **not**
proof that a full GPU export is measurably faster. Use a same-machine,
same-project benchmark and decoded media parity before asserting a speedup.
