# Native LZ4 hardware comparison probe (2026-11-11)

## Why this is a developer experiment, not a production speedup

A 30-second, 1920x1080, 60 FPS native export of the same immutable project
(1800 frames) reported 48,273,919,909 BGRA wire-payload bytes. Automatic
compression calibrated on the first 64 alternating probe frames and chose
`raw`. Its per-layer diagnostic also showed that only 32 particle frames
were sent compressed, with approximately 264 MB saved on that stream.

These results suggest that measuring a **per-layer** policy could be useful,
but the current global choice is based on elapsed sender time. Large wire
savings do not establish faster end-to-end exports.

## Experimental change

`tools.native_gpu_full_export` accepts `--gpu-force-lz4` **only together
with `--gpu-profile automatic`** for a local developer hardware A/B.

- Without the extra flag, there is **zero behavior change**: the same
  `AdaptiveCompressionBudget` performs 64-frame calibration and then selects
  raw or compressed frames.
- With the flag, the v3 sender asks its existing `_compressible` sampling
  and lossless LZ4 encoder to consider each non-reused BGRA layer on every
  frame. Incompressible planes fall back to raw exactly as before.
- The Rust worker, `binary-v3` protocol, raw/LZ4/previous-frame validation,
  SHA-256 checks, pixel data after decompression, renderer, audio, FPS, and
  GPU memory limits are unchanged.
- The experiment is **not exposed in the application GUI** and does not
  silently change ordinary Rust GPU export.
- Missing LZ4 wheel or disabled layer reuse fails closed before starting the
  worker.
- The report marks `frame_lz4_auto_choice=forced_lz4_experiment`. This is not
  the automatic selection and must never be interpreted as an automatic
  speedup result.

## Next verification gate

On the same Windows RTX machine, run two new **30-second** MP4 exports of the
exact same immutable source project using the exact same branch/worker,
`--fps 60 --scale 1.5 --gpu-profile automatic --smoke-seconds 30`.
Set `--gpu-force-lz4` only on the second run. Give each output/report its own
unique filename. Preserve local Git changes and project bytes.

Inspect both reports and actual video/audio/lyrics. At minimum compare:
`source_sha256`, `complete_cli_wall_seconds`,
`native_stream_elapsed_seconds`, `native_stream_wall_fps`,
`frame_lz4_auto_choice`, `frame_lz4_cpu_seconds`,
`frame_lz4_planes`, `frame_layer_transport`,
`frame_wire_payload_bytes`, `frame_pipe_write_seconds`,
`frame_pipe_syswrite_seconds`, and `frame_ack_wait_seconds`.
Timing counters overlap, so never sum them into wall-time speedup.

The next engineering decision must use those hardware results. Keep this
experiment out of the default GUI path until real speed, parity and project
diversity are assessed. Passing Windows CI only validates safety/protocol
integrity, not performance.
