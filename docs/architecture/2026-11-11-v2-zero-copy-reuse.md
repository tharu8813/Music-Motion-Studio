# Zero-copy reuse of binary-v2 BGRA frame planes

## Evidence

2026-11-11 RTX 5060, identical 1920×1080 / 60 FPS / 30s / 1800-frame project:

| Metric | Before PR #106 | With PR #106 |
|---|---:|---:|
| Native pipeline FPS | 19.322 | 23.940 |
| Complete export wall | 100.351s | 82.307s |
| BGRA wire bytes | 48,945,364,822 | 48,945,364,822 |
| Reused frame planes | 242 | 242 |
| Reused bytes not sent | 1,626,176,736 | 1,626,176,736 |
| Pipe-write wall (overlapping) | 61.04s | 61.65s |
| ACK wait (overlapping) | 30.42s | 11.83s |

The exact output H264 compressed bytes were unchanged (64,360,192) in both runs, and both completed all 1800 frames. No byte-by-byte video pixel equality comparison was carried out; H264 byte counts alone do not establish output equality.

**Measurement caveat:** User ran the older profiler schema 1, so its GPU percentages represent the **whole device**, not the new PR #105 per-process GPU counters. CPU work is grouped as `other` in this older report.

## Change

After native GPU upload/submission of frame N, the worker no longer requires CPU ownership of its pixel buffers. For a valid `binary-v2` previous-frame reference in frame N+1, move the already-verified `Vec<u8>` from frame N into the next frame with `std::mem::take`, instead of cloning that entire plane. The common packet parser still performs SHA-256 verification on the moved bytes and validates geometry, stream key, opaque base, alpha and ordering exactly as before. The protocol and all user settings remain unchanged.

In this workload this eliminates up to **1.626 GB** of Rust-side repeated plane-memory copying per 30-second export, subject to data shape and repeat count. It **does not** reduce the 48.95GB IPC bytes or guarantee FPS improvement; real 1080p measurements are needed.

## Verification

- Rust core and protocol unit tests, including memory move identity test and malformed reference rejection
- Windows GPU/D3D11 pixel/PTS/encoded frame regression CI
- Windows application packaging and full test suite
- Rebuild Rust release executable before measuring
- Compare with baseline 23.940 FPS / 82.307s, using identical project/scale/fps/clip.

The next priority remains **reducing dynamic layer IPC payload** itself and profiling the 61s overlapping pipe-write span before adopting any compression or image format change.
