# Native BGRA opaque-base word validation experiment (2026-11-11)

## Motivation

The native Rust packet parser independently verifies each plane's SHA-256,
then checks that every pixel of the **base** stream has alpha 255 before
submitting the frame to D3D11. The previous validator iterated in four-byte
BGRA pixel units and checked one alpha byte per pixel.

A 1920x1080 BGRA base has 2,073,600 pixels (8,294,400 bytes) per frame.
At 1800 frames, the base alpha check scans roughly 14.93 GB of data per
30-second export, independently of whether later overlays are small or
compressible. This makes it a source-independent CPU optimization candidate,
not a project-specific special case.

## Change

- Use a safe `chunks_exact(8)` scan, a `u64::from_le_bytes` word, and
  alpha-bit mask `0xff00_0000_ff00_0000` to validate two BGRA pixels
  per comparison.
- Validate one possible remaining four-byte pixel exactly as before.
- Reject malformed standalone buffers whose byte size is not a multiple of 4.
- No `unsafe`, pointer casts, unaligned loads, or architecture-specific
  intrinsics. `from_le_bytes` makes the byte semantics architecture-neutral.
- No changes to SHA-256, metadata validation, Qt rendering, frame ordering,
  LZ4, the native transport protocol, D3D11, NVENC, output colors, or alpha
  blending. The same checker also handles plain v1 and referenced v2/v3
  frames, after their existing checksum validation.
- Tests compare word-wide checks against the former four-byte scalar predicate
  across odd/even pixel counts, every possible invalid-alpha position, and
  several 1080p frame boundaries. Tests additionally forge a matching SHA
  for a transparent base to prove the packet parser still rejects it.

## Verification gate

The Windows CI must compile the Rust crate, run packet validation tests and
real D3D11/NVENC pixel/lyrics/audio parity checks. CI is a **safety gate**, not
evidence of speed: the current Rust compiler may already auto-vectorize the
old four-byte loop, and a word-wide check can be equal or slower.

First compare the old scalar predicate with the new wide predicate using
the same **release-mode** binary and same CPU, alternating run order:

```powershell
cargo test --release --manifest-path native/gpu_export_core/Cargo.toml `
  opaque_bgra_scalar_vs_wide_cpu_benchmark -- --ignored --nocapture
```

The ignored test prints four alternating repetitions of 100 opaque
1920x1080 frames per predicate. It deliberately has **no timing pass/fail
assertions** and does not run in CI by default. It is a microbenchmark, not
proof that the complete Qt/Python-to-Rust export gets faster.

Before adopting this as a production optimization, run identical 30-second
1080p60 exports on the **same Qt/Python head** with baseline native worker
versus candidate native worker, in balanced A/B order. Use new MP4/JSON paths;
compare `complete_cli_wall_seconds`, `native_stream_elapsed_seconds`,
`frame_receive_seconds`, `frame_sha256_seconds`,
`frame_pipe_write_seconds`, frame counts and full decoded visual/audio parity.
These counters overlap; do not sum them to estimate the speedup.

The original producer, Rust input format, and renderer remain interoperable;
if there is no repeatable end-to-end improvement, close this experiment
without merge.
