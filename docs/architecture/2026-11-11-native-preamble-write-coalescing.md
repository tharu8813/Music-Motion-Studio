# Native MMSB frame preamble write coalescing (2026-11-11)

## Scope

This change is deliberately independent of any project name, source identity,
Z index, particles, lyrics, or output resolution.

The producer previously issued separate `stream.write()` calls for the 28-byte
MMSB header and its bounded UTF-8 JSON manifest. `write_frame()` now combines
these **two small, contiguous wire segments** into one preamble write while
leaving `frame_chunks()`, the metadata bytes, all BGRA plane payloads,
checksums, V1/V2/V3 wire formats, reuse, LZ4, per-frame ACKs, output pixels,
and Rust decoding untouched.

The preamble allocation is bounded by the existing 64 KiB manifest limit plus
28 header bytes. No full-frame BGRA buffers are concatenated or copied.
The existing partial-write loop still handles short writes and zero-progress
failures exactly as before.

## Motivation and evidence

The same-project 30-second 1920x1080/60fps automatic export (1800 frames) reported
`frame_pipe_syswrite_calls=12358` and `frame_wire_payload_bytes=48273919909`.
One successful preamble write instead of two can remove roughly one writer
call per frame when the OS accepts the small preamble in one operation.

This is a **small general-purpose call-count optimization**, not a 48 GB BGRA
payload reduction. The impact on elapsed time is unknown. In particular,
`frame_pipe_syswrite_seconds` includes native worker/backpressure interactions;
lower call counts do not prove faster rendering.

## Validation

- Run the Windows serial Python test workflow, including
  `tests.test_native_frame_transport`.
- Run native packet/pixel-parity and real audio Canvas tests.
- Test code checks byte-for-byte equality against `frame_chunks()` for
  v1, v2 and v3 including previous-plane reuse, short writes and unchanged
  counters.
- For hardware evaluation, compare the exact same project/clip, FPS,
  resolution, native worker and automatic profile before/after. Record
  `complete_cli_wall_seconds`, `native_stream_elapsed_seconds`,
  `frame_pipe_syswrite_calls`, `frame_pipe_syswrite_seconds`,
  `frame_wire_payload_bytes`, and pixel/audio/lyrics parity.

Do not claim a speedup or mark a broader transport optimization complete based
solely on passing CI. Larger improvements to dynamic/Canvas layer payload size
require their own separately measured, safe changes.
