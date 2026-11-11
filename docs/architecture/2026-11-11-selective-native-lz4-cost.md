# Per-layer native LZ4 performance isolation (2026-11-11)

## Motivation

The same-source 30-second 1920x1080 60fps A/B compared the shipped automatic
v3 LZ4 decision with forced whole-frame compression:

| Measured item | Automatic | All eligible planes forced LZ4 |
|---|---:|---:|
| Full CLI wall time | 73.7214 s | 87.0448 s |
| Native stream elapsed | 66.6321 s | 79.8919 s |
| BGRA wire payload | 48,273,919,909 B | 3,054,020,502 B |
| LZ4 task elapsed sum (overlapping) | 0.2949 s | 21.9655 s |

The all-plane compressed candidate was **18.1% slower in this single ordered
comparison**, despite about **93.7% fewer BGRA wire bytes**. The right next step
is not a project-specific rule such as `if source.name == "Particles"`, nor
blindly enabling compression globally. The question is whether **one stream**
can benefit without paying for compression of the others.

## Change: no impact on ordinary export

The regular Rust GPU engine continues to create
`NativeGpuOptimizations.for_profile("automatic")` with
`lz4_layer_probe_keys=None`. In this state the existing 64-frame
`AdaptiveCompressionBudget` is unchanged.

The developer CLI can now select any existing protocol stream by generic key:

```powershell
python -m tools.native_gpu_full_export --project C:\\path\\test.pvsproj `
  --output C:\\path\\selected.mp4 --report C:\\path\\selected.json `
  --native-exe C:\\path\\d3d11_qt_canvas_stream_export.exe `
  --fps 60 --scale 1.5 --gpu-profile automatic --smoke-seconds 30 `
  --gpu-lz4-layers layer:0
```

- Accepted keys: `base`, `layer:0` through `layer:6`; comma-separated
  unique keys can select multiple layers.
- This is **developer-only**. Neither the regular app GUI, FFmpeg path nor
  default automatic profile is modified.
- The v3 wire protocol already mixes `raw`, `lz4` and `previous` planes.
  Only non-reused selected layers are eligible to compress. Incompressible
  selected planes still fall back to raw using the existing LZ4 sampler.
  Unselected non-reused planes are raw BGRA. No new Rust decoder or rendering
  path is required.
- An absent/malformed layer key fails closed, and the test requires the
  `automatic` developer profile and installed LZ4 wheel.
- No project filename, source title, Z value or geometry is hardcoded in the
  selection logic. Selection follows only existing packet stream keys.

## New report fields

- `frame_lz4_auto_choice=selected_layers_experiment` clearly distinguishes
  selected-layer probes from automatic decisions.
- `frame_lz4_selected_stream_keys` records the generic selected stream keys.
- `frame_lz4_layer_cost[stream_key]` reports:
  `attempted_planes`, `source_bytes`, `wire_bytes`, `saved_bytes`,
  `task_elapsed_seconds`, and `compressed_planes`.
- These are *per-task elapsed times*, not process CPU seconds. Tasks may run
  in parallel and overlap frame production and pipe backpressure. Do **not**
  add them to produce overall wall time.

The existing per-layer `frame_layer_transport` totals remain unchanged. No
new frame payloads, copies of BGRA planes, persistent source cache, or
FFmpeg/NVENC codec changes were introduced.

## Verification and decision gate

Windows CI must validate the full serial Python tests and real audio Canvas
packets, including mixed raw/LZ4/reuse SHA-256 byte identity, PTS/ACK and
rollback behavior.

For the first hardware test use exactly the same 30-second source project,
output dimensions, FPS, and RTX machine as the previous automatic A/B, but
benchmark the **automatic baseline and selected `layer:0` run on the same new
branch**. Write two unique outputs and JSON reports. Compare the full
`complete_cli_wall_seconds`, `native_stream_elapsed_seconds`, per-layer
codec costs, wire bytes and actual audio/visual parity. If measured gains are
small or unstable, revert to the original global automatic policy.

A source-independent automatic **per-layer** decision can be designed only
after targeted tests show which costs correlate with actual end-to-end
throughput on representative projects. This experimental branch must not be
merged as a default optimization merely because it reduces bytes.
