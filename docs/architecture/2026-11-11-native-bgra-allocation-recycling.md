# Recycle submitted CPU BGRA allocations between Rust GPU frames

## Baseline, November 11 2026

User's RTX 5060, 1920×1080 at 60 FPS, first 30 seconds (1,800 frames):

- PR #109: native pipeline **26.767 FPS**, full CLI **74.407 seconds**
- BGRA input (before transport compression): **48,942,663,264 bytes**
- Rust resident uploads: **8,758** covering **48,942,663,264 bytes**
- In the v3 automatic mode, 64 calibration frames choose **raw**
- Sender SHA-256 wall time **13.854s**; Qt scene capture **36.945s**
- Full video completed; timings overlap and cannot be summed

Previous Rust v2/v3 input allocated a fresh `vec![0; plane_bytes]` for **every changed plane**, even when its same-index predecessor had just been submitted to D3D11 and was about to be dropped. The decoder also zero-filled all those new buffers, then immediately overwrote their entire contents from the pipe or LZ4 decoder.

## Implementation

Within the existing `read_frame_with_refs` callback, for each **changed** (not previous-frame referenced) layer:

1. Verify wire version, compression type, encoded payload length and bounds.
2. If the previously submitted frame owns the **same stream key and exact pixel-byte length**, move its BGRA `Vec<u8>` using `std::mem::take`. Otherwise allocate a new bounded buffer as before.
3. Read the full **raw** BGRA payload into that allocation, or fully decode LZ4 into it and require the exact decoded length. Never keep stale pixels as an alternative to parsing.
4. Call the existing common Canvas parser, which computes and verifies the **new frame's SHA-256**, geometry, straight-alpha and opaque-base requirements **after** the overwrite.
5. GPU receives the same validated pixels, Z-order, geometry and PTS as before.

This optimization does not reduce the 48.28GB IPC traffic, Python SHA work, or GPU upload bytes. It targets Rust-side allocations and repeated zero-filled writes for changed frames, potentially up to ~49GB of reinitialization per 30-second export. Its actual FPS impact is **not yet measured** and must not be assumed.

## Verification

- Rust v2 test: after a referenced plane, changed raw BGRA gets the same backing pointer and passes checksum.
- Rust v3 test: raw-v3 changed plane recycles the previous pointer; LZ4 changed plane decodes into a previous raw pointer; forged checksum still fails.
- Existing tests: malformed/truncated LZ4, wrong PTS, wrong geometry/reuse and v1/v2 compatibility.
- GitHub Windows native CI and application full suite must pass before merge.
- Confirm with the same `giggles.pvsproj` 1080p/60FPS/30s export; compare to 26.767 FPS and 74.407s, and inspect output/PTS/video quality.

Retain the FFmpeg and Rust GPU export engines and the single automatic Rust optimization profile.
