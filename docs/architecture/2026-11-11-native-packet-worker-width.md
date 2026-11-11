# Native packet SHA/LZ4 worker-pool width experiment (2026-11-11)

## Why this candidate

On the same RTX 5060, 30s 1920x1080 60fps (1800 frames) reports,
Python sender SHA-256 digested **50,568,840,000 bytes** of BGRA,
taking about **33–36 cumulative task seconds** and **15–17 measured
hash-stage wall seconds** with the current three-worker packet pool.
Layer workers only touch immutable BGRA byte buffers, not Qt scenes.
End-to-end export elapsed roughly 90–100 seconds in recent trials,
with noticeable workstation drift.

Increasing SHA task parallelism may reduce critical-path waiting when
the project contains four or more large, independent bands. Conversely,
more simultaneous hashing can saturate memory bandwidth and interfere
with Qt rasterization, pipe writes or Rust. **No speed gain is assumed.**

## Experiment only

The GUI and `automatic` default continue to use exactly **three**
SHA/LZ4 packet workers, matching current behavior. The developer CLI
may explicitly select a bounded pool size **1..6**:

```powershell
python -m tools.native_gpu_full_export --project C:\\path\\project.pvsproj `
  --output C:\\path\\candidate.mp4 --report C:\\path\\candidate.json `
  --native-exe C:\\path\\d3d11_qt_canvas_stream_export.exe `
  --fps 60 --scale 1.5 --smoke-seconds 30 --gpu-profile automatic `
  --gpu-packet-workers 5
```

Without `--gpu-packet-workers`, no pooling behavior changes. A custom
value is rejected outside the explicit `automatic` developer profile.
The native report now includes `frame_packet_workers` so A/B results
cannot be mislabeled. The same executor already hashes and optionally
LZ4-compresses independent full BGRA layers; no new executor, BGRA copy,
frame cache or unbounded queue is introduced.

This experiment does **not** modify SHA-256 integrity checks, v1/v2/v3
transport, codec, Qt painting, frame ordering, scene state, playlist
audio, GPU D3D11 or NVENC, or the UI. Native receiver reconstructs
every plane and checks matching SHA before accepting the frame. Worker
task order is preserved by `executor.map`.

## Verification

Windows serial CI and real audio/Canvas packet tests must pass before
any Windows hardware benchmark. Tests verify the default remains three,
invalid counts fail closed, developer profile is required, and a
five-layer 320×180 mock-worker stream with 12 frames preserves exact
SHA-256, correct ordered ACK, previous base references and frame count.

**First hardware gate:** on the same Windows machine, use a small
process-local SHA-256 microbenchmark alternating pool sizes 3 and 5 over
several independently owned 1080p BGRA byte buffers, then compare
end-to-end A-B-B-A 30-second 1080p60 exports only if a plausible,
reproducible microbenchmark advantage exists. The normal 3-worker
case and the candidate 5-worker case must use the **same PR head,
Python, Qt, Rust encoder, source hash, project, FPS, scale, audio,
environment and unique output names**. Compare actual full CLI elapsed,
native stream wall, per-frame hash wall, and output SHA/decoded
video+audio parity. Control for warmup/scheduling and machine load.

Do not infer a full-export improvement from task wall time alone:
hashing overlaps Canvas painting and native IPC, and pool width may
increase contention. Do not merge this candidate based only on CI or
one favorable timing; close without merge if benefit is too small,
inconsistent or accompanied by parity errors. No source/project-specific
rules should be hardcoded into the normal automatic policy.
