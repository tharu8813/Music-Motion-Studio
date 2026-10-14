# Large-playlist preview entry

Preview's track navigator synchronously statted, parsed and decoded every cover
on the GUI thread. Its 512-entry thumbnail cache could not hold a large playlist,
and each partial/final mix replaced the entire navigator and repeated those reads.

`ExportPreviewDialog` now creates text rows immediately. One cancellable
`PreviewCoverWorker` uses the existing QImage thumbnail reader off the GUI thread;
queued results become QPixmap/QIcon only on the GUI thread. Mix and language
updates reuse rows and loaded icons. Closing discards late results and detaches
in-flight reads without waiting for disk I/O; closing before the deferred start
never starts the worker. An unreadable cover does not stop the remaining covers.

`PresentationPlan._window_at` and `visual_segment_at` now bisect their existing
sorted objects directly. Each lookup takes O(log n), without allocating an O(n)
start-time list on every frame. Before-first, gaps and after-last retain their
previous ownership semantics.

## Measurement

The reproducible local harness is `work/benchmark_preview_entry.py`, with results
in `work/preview-entry-benchmark.json`. It compares the previous synchronous
cover-population behavior against current construction with background covers.
Each run clears the thumbnail cache. Fixture: offscreen CPU Qt preview, empty
640×360 canvas, 180-second tracks and distinct 1600-pixel JPEG cover files.

| Tracks | Synchronous cover entry | Background cover entry |
| ---: | ---: | ---: |
| 100 | 915.78 ms | 12.30 ms |
| 1,000 | 7,626.90 ms | 31.98 ms |
| 3,000 | 16,236.74 ms | 61.34 ms |

These are synthetic constructor measurements, not full application startup or
time until all artwork is ready. Thumbnail decoding continues after entry.
FFmpeg, GPU drivers, video sources, lyrics and personal-color visualizer setup
can add separate costs in real projects.

## Verification

The isolated preview/playback/layout/timing suite passed 160 tests across 14
modules. Final checks reran the large-playlist, worker-lifecycle and render-plan
modules, added an actual queued icon-delivery test, and checked AutoMix P1 timing:
35 passed, with two opt-in real-FFmpeg tests skipped. Regression coverage
includes 1,000 tracks with a deliberately blocked cover read while the GUI timer
continues ticking, closing during that read without waiting, row/icon preservation
through mix and language changes, correct jump positions, cover errors, and
10,000-window lookups that reject a whole-playlist scan.

No dependency, project format, export audio policy or packaged executable changed.
