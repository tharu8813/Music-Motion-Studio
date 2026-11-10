# Native Rust SHA-256 validation reuse (2026-11-11)

## Why

In the 30-second 1080p Windows sample, the Qt/Python producer sent
48.95 GB of BGRA data to the Rust D3D11 worker (1800 frames / 60FPS).
`parse_qt_canvas_packet` already SHA-256-validated **each plane**, then
`ResidentLayerCache.update_sized` performed another SHA-256 over the
exact same immutable pixel buffer only to decide whether a GPU upload
was needed. With low NVENC engine utilization, unnecessary CPU passes
were worth eliminating before changing GPU kernel code.

## Change

- Preserve the **already verified 32-byte binary digest** in each
  `CanvasPacketLayer` after the existing full SHA-256 manifest check.
- GPU resident layer cache compares this exact digest rather than
  calculating SHA-256 over every large BGRA plane a second time.
- Reuse the already-parsed `binary-v2` JSON metadata for its second-stage
  parser/validation; it was previously decoded twice per frame.
- Keep the original checksum rejection, stream key validation,
  dimensions, layer count/order, opacity and GPU texture update rules.
  The digest field is crate-private and populated only by the parser
  for real input; mock packets construct the matching digest explicitly.
- Add `resident_layer_cache.digest_reuse_bytes` to the native export
  JSON. This counts bytes no longer hashed a **second** time by Rust.
  It is **not** a reduction in bytes sent over IPC, nor a benchmark
  speedup. Expect a number near the processed planes' total bytes.

No user-facing optimization toggles, no input protocol change (still
`binary-v2`), no frame dropping or different timestamps, and no
NVENC parameter changes.

## Verification

- `cargo test --manifest-path native/gpu_export_core/Cargo.toml`
- `cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export`
- `python scripts/run_tests.py native_gpu_stream native_frame_transport native_project_runtime`
- Windows CI production packet/pixel/lyrics parity tests.
- **Hardware validation still needed:** re-run exactly the same 30-second
  `giggles.pvsproj` clip at 1920x1080 60 FPS with `--scale 1.5`,
  `--gpu-profile automatic` and a newly rebuilt native worker. Compare
  `native_stream_wall_fps`, `frame_pipe_write_seconds`,
  `frame_ack_wait_seconds`, `frame_receive_seconds`,
  `resident_layer_cache.digest_reuse_bytes`, actual MP4 pixels,
  audio and lyric timing. Same source, project and video settings
  are essential; do not claim gains until this comparison.

## Windows baseline (before)

| Metric | 720p | 1080p |
| --- | ---: | ---: |
| native_stream_wall_fps | 40.835 | 19.322 |
| frame_transport_bytes | 18,615,796,691 | 48,945,364,822 |
| frame_receive_seconds | 31.59s | 65.65s |
| frame_pipe_write_seconds | 28.59s | 61.04s |
| frame_ack_wait_seconds | 13.76s | 30.42s |

Timings overlap. Packet input is substantial but total performance
benefit is not predictable from the number of bytes alone.
