# BGRA conversion-worker SHA prehash (experimental, 2026-11-12)

## Motivation

Controlled RTX 5060 tests of a SHA-256 worker-pool width increase
(PR #124, 3 -> 5) showed a **10.21% regression in full 30-second MP4
export duration**, despite SHA-stage wall time decreasing by 15.3%.
All decoded video and audio hashes matched. PR #124 was closed unmerged,
and the default three-worker packet pool is retained.

The producer already runs up to two bounded Qt QImage -> immutable BGRA
conversion tasks while the GUI thread paints further frames. The sender
subsequently launches additional SHA-256 tasks over the same immutable
buffers. This proposal attempts to move those SHA passes into the
**existing conversion workers**, before the bytes become cold in memory,
and reuse the resulting digest later in the packet encoder.

## Exact scope

Only an explicit *developer CLI* A/B flag enables this feature:

```powershell
python -m tools.native_gpu_full_export --project C:\\path\\project.pvsproj `
  --output C:\\path\\candidate.mp4 --report C:\\path\\candidate.json `
  --native-exe C:\\path\\d3d11_qt_canvas_stream_export.exe `
  --fps 60 --scale 1.5 --smoke-seconds 30 `
  --gpu-profile automatic --gpu-prehash-bgra
```

Unflagged automatic, GUI and other profiles remain bit-for-bit
unchanged. A precomputed SHA hint can only be returned alongside
immutable `bytes` by the same `straight_bgra_bytes_with_sha256`
conversion worker. Small inline conversions, invariant cached planes,
audio visualizer overlays, and any non-prehashed frame continue
using the existing sender-side hash algorithm. This may be a small
benefit or a regression; no speedup is claimed.

Packet encoder validates the hint is lowercase 64-byte hexadecimal and
the payload type is exactly immutable Python `bytes`. The receiver
**always hashes reconstructed raw/LZ4/previous BGRA independently**
and rejects altered packets or mismatched SHA. The native wire
manifest, schema, protocol v1/v2/v3, reference logic, NVENC and
D3D11 code remain unchanged.

The new opt-in counters
`native_report.frame_sha256_prefetched_planes` and
`native_report.frame_sha256_prefetched_bytes` distinguish
conversion-worker-provided SHA from sender hashing. Existing
`frame_sha256_bytes` remains the number of **logically verified**
bytes across both places, rather than claiming they were never hashed.
The counts track only immutable bytes and do not retain QImages or
frames beyond the established two-packet bound.

## Checks and adoption gate

Unit tests cover:
- identical v1/v2/v3 packet bytes and previous-reference manifests,
  including deliberate failure if a prehashed plane is rehashed by sender;
- invalid hash formats and mutable bytearrays rejected;
- opaque, straight-alpha and premultiplied QImage formats with exact
  BGRA bytes and SHA; deferred Future[bytes|tuple] compatibility;
- real Qt async 640x512 dynamic text layer conversion, packet parity,
  existing three-worker default;
- mock Windows receiver verifies SHA/ACK/order/reuse across 12 frames.

Latest Windows serial/Qt/lyrics/pixel and real-audio Canvas CI must pass
before hardware testing. Then do a controlled 3-worker **same-PR-head**
A-B-B-A 30-second 1080p60 export with automatic profile only, differing
*solely* by `--gpu-prehash-bgra`. Use the same
project/source SHA, Python, Qt, Rust exe and separate output paths.
Compare full CLI elapsed, Qt/CPU-BGRA stage, SHA sender stage, pipe
write and decoded audio/video hash parity; watch peak RAM.

The extra SHA work competes for the existing conversion worker pool and
can delay Qt rendering and native transfers. **Do not merge** if
the full export is not repeatably faster, regardless of sender SHA
wall time. A single project's success is not a guarantee for all
projects. Do not make a user-visible optimizer option.
