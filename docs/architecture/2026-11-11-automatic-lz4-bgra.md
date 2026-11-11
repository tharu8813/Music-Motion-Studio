# Automatic lossless LZ4 BGRA transport (binary-v3)

## Baseline and purpose

RTX 5060, `giggles.pvsproj`, 1920×1080 / 60 FPS / first 30 seconds, 1800 frames:

| Run | Native FPS | Full export | SHA-256 on sender | OS pipe writes | IPC bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| PR #106 | 23.940 | 82.31s | not split | not split | 48.945GB |
| PR #108 | 23.685 | 83.49s | 25.04s | 32.80s | 48.945GB |

The ~1% variation is not meaningful acceleration. The v2 plane-reuse optimization has
only 242 referenced planes in this workload and eliminates *Rust-side copies*, not wire bytes.
It cannot improve the dominant 48.945 GB IPC payload very much.

## New automatic transport

For production Rust GPU exports with `lz4` installed and native v3 support,
the existing automatic optimization profile uses the **binary-v3** protocol.
This is an internal implementation detail; the user chooses only FFmpeg or Rust GPU.

1. Sender SHA-256 checks the **original BGRA** bytes, maintaining frame identity.
2. If the plane references the previous frame, transmit no pixel bytes as before.
3. Otherwise, check four distributed 16 KiB samples before attempting LZ4.
   Only compress planes >=128 KiB where the sample indicates strong compressibility.
   If the final block is not at least 10% smaller, send raw bytes.
4. Add `wire_bytes` and `compression` metadata per plane. The v3 header payload
   counts the actual sent bytes, not the decoded BGRA length.
5. Rust strictly bounds the encoded size, decodes the LZ4 block into an
   exactly sized BGRA buffer and **revalidates SHA-256 of the decoded pixels**
   using the existing pixel parser. The GPU texture receives unchanged pixels.
6. Legacy v1/v2 input decoders remain intact. Python without `lz4` falls back
   to v2; packaged Windows releases *require* the pinned `lz4==4.4.5` wheel.

## Safety and diagnostics

- Reject compressed size >= uncompressed size, truncated input, invalid algorithm,
  any version/PTS/geometry mismatch and malformed LZ4 blocks.
- Reused frames require `compression=previous` and zero wire bytes, still
  validate previous stream key/dimensions/checksum.
- Bound each decoded plane by the same 3840×2160×4 limit; no decoded pixels
  enter the renderer before full SHA-256 validation.
- New native stream JSON counters: `frame_lz4_cpu_seconds`,
  `frame_lz4_planes`, `frame_raw_source_bytes`,
  `frame_wire_payload_bytes`, `frame_wire_saved_bytes`.
  `frame_transport_bytes` includes protocol headers.
- CI coverage: Python byte-for-byte raw fallback, v3 LZ4 round trip, repeat
  references; Rust v3 corrupted/hash-forged/truncated payload and v2/v3 version checks.
  No GUI optimization choice has been added.


## Follow-up RTX 5060 real-hardware measurement: LZ4 regression

The user successfully exported all 1,800 frames of the same 1080p
30-second clip using v3; the former frame-1 previous-packet error is fixed.

| Metric | v2 baseline | v3 with always-on LZ4 |
| --- | ---: | ---: |
| Native pipeline FPS | 23.685 | **17.575** |
| Full CLI wall clock | 83.49s | **109.49s** |
| Wire bytes | 48.945GB | **3.057GB** |
| Sender SHA-256 duration | 25.04s | 25.71s |
| LZ4 sender CPU duration | 0 | **20.85s** |
| OS pipe write duration | 32.80s | 37.63s |
| Total Python send/write duration | ~61.65s | **87.15s** |
| Native frame receive duration | - | 93.97s |

Despite ~93.8% fewer wire bytes, compression increased elapsed time:
`pipe_syswrite_seconds` includes receiver backpressure, so shrinking the
byte stream doesn't guarantee shorter writes or faster end-to-end execution.

### 2026-11-11 revision: CPU parallelism + automatic wire selection

- Parallelize hashing and eligible LZ4 compression across **up to 3**
  immutable BGRA planes with one bounded, per-export worker pool. Never
  invoke Qt painting off its owner thread.
- Keep SHA-256 verification and all v3 wire metadata; results are inserted
  in original layer order, deterministically.
- During the first **64 frames**, alternate LZ4 and raw v3 packets. Compare
  per-frame sender wall time per uncompressed MB, skipping frames below
  1 MiB of changed BGRA. The export only retains LZ4 if measured median
  sender latency is at least **10% faster** than uncompressed; otherwise
  v3 continues with raw planes automatically. Sparse or inconclusive
  calibration chooses raw.
- This is **one automatic** transport policy, not another user setting.
  The Rust worker already supports compressed, raw, and previous-frame
  references mixed inside one binary-v3 stream.
- New report metrics: `frame_lz4_auto_choice`,
  `frame_lz4_probe_samples`, `frame_sha256_wall_seconds`,
  `frame_lz4_wall_seconds`. The original SHA/LZ4 CPU-second values now
  sum concurrent thread times; **only the new wall-second fields** track
  elapsed latency directly.
- Unit tests compare complete serial vs parallel packets, adaptive choices
  for slower/faster LZ4, and v3 transitions from LZ4 to raw and references.

**Unverified gain:** the prior 17.575FPS result is measured; this revision
still requires Windows CI and a second identical RTX 5060 A/B export
before claiming any throughput increase. The automatic fallback is designed
to avoid retaining the demonstrated large LZ4 regression.

## Real hardware test

Pull the updated source **and rebuild the Rust native release executable**.
This changes both sides of the pipe; an old Rust worker does not understand v3.

Use the existing 1080p short smoke test script with
`--smoke-seconds 30 --fps 60 --scale 1.5 --gpu-profile automatic` and
the same `giggles.pvsproj`. Compare the new reported wire bytes and
`native_stream_wall_fps` against 48.945 GB / 23.685FPS. Inspect the MP4,
especially lyrics, alpha, transitions, timestamp continuity and audio.
If compression CPU cost offsets the saved pipe writes, adjust the **automatic
threshold** based on this measurement rather than exposing another GUI toggle.

This protocol is intentionally versioned: v2 consumers reject v3, so always rebuild the native worker together with the Python sender.
