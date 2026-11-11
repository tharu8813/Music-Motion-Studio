# Bounded 3-worker BGRA conversion experiment (November 11, 2026)

## Evidence

The same RTX 5060 / 1920×1080 / 60 FPS / 1,800-frame / 30-second project was tested consecutively on the same desktop, source SHA256 `a56ca40e382791bc700b5b4e2a55c4f764981de56c046e3a48c3319c37412cbb`:

| Metric | main (PR #109) | PR #112 RGB32 |
|---|---:|---:|
| Native stream throughput | 24.4085 FPS | 24.7321 FPS |
| Complete CLI wall | 81.313 s | 80.564 s |
| BGRA worker accumulated wall | 22.873 s | 19.422 s |
| BGRA worker peak concurrency | 1 | 2 |
| GUI Qt Canvas capture | 43.889 s | 41.212 s |
| Total frame production | 58.238 s | 56.527 s |
| Sender SHA-256 wall | 14.645 s | 13.692 s |
| Wire bytes | 48,276,988,328 | 48,276,988,328 |
| Frames, PTS | 1800/1800, monotonic | 1800/1800, monotonic |

PR #112 was merged because Windows Canvas tests passed and the A/B pair showed a modest throughput improvement plus a 15.1% reduction in total BGRA conversion work. **This is a single A/B pair and not a statistically robust speedup claim.** Earlier independent runs show substantial natural fluctuation.

## This experiment

The `automatic` profile currently permits only two in-flight large `QImage` -> immutable BGRA `bytes` converter tasks; these conversions are detached from the Qt paint thread and may run concurrently with GUI painting and sender hashing.

On >=8 logical CPU systems, try a **third** worker and corresponding semaphore slot to increase overlap for complex multi-layer frames. Do not affect lower-core configurations or the existing `cpu_parallel` and other profiles; these remain on two workers. Do not add any UI optimization choices. All non-large image conversion, Qt thread ownership, output geometry, SHA-256 checking, native GPU, FFmpeg and wire format rules remain unchanged.

The third slot can retain a further ~31.6 MiB 4K QImage conversion input; the cap remains bounded at three (about 95 MiB of simultaneous 4K BGRA input buffers, not counting their immutable results or encoder buffers). Do not submit unbounded futures. Never wait for a worker slot on the Qt GUI thread: inline when at capacity.

The native raster report exposes `cpu_parallel_bgra_worker_limit` alongside measured `cpu_parallel_bgra_peak_workers` and summed `cpu_parallel_bgra_work_seconds`.

## Tests

- Mock 4 logical CPUs, verify `automatic` remains at 2 workers, nonblocking third task.
- Mock 20 logical CPUs, verify automatic starts 3 workers simultaneously; fourth submission immediately falls back inline.
- Mock 20 logical CPUs with legacy `cpu_parallel`, verify unchanged 2-worker cap.
- Compare all converted byte sequences against canonical straight BGRA, ensure thread pool fully shuts down, retain existing pixel-parity/glyph/ROI and Windows CI.

## Decision gate

**Do not assume this experiment will accelerate the actual export**. Additional workers may increase DRAM bandwidth contention against Qt scene painting and SHA-256 hashing. Run another matched baseline vs PR 30-second test with the same project after Windows CI; compare FPS, total CLI seconds, Qt capture time, BGRA worker wall sum, peak workers and output frame counts. Close without merging if end-to-end results do not support a speedup.
