# Native LZ4 per-layer cost diagnostics (2026-11-11)

## Measured motivation

On the same RTX 5060 1920x1080, 60fps, 1800-frame project, one ordered A/B
of the experimental selective `layer:0` LZ4 mode found:

| Measurement | Existing automatic | LZ4 only on `layer:0` |
|---|---:|---:|
| Full CLI wall | 94.8292s | 101.8124s |
| Native stream elapsed | 86.3813s | 93.6529s |
| BGRA wire payload | 48,273,919,909 bytes | 34,078,145,433 bytes |
| LZ4 task time total | 0.3850s | 3.8440s |

The source SHA, output 1920x1080/60fps/1800 frames, worker version and profile
matched. Even though `layer:0` shrank from 14,929,920,000 to 65,402,169
bytes when always compressed, overall export was **7.36% slower in that single
trial**. Earlier automatic runs were substantially faster (73.72 seconds), so
machine load/run order matter. No universal performance claim follows from
these one-off numbers.

The exploratory compression-policy PR was closed unmerged. The costs it
exposed are still useful as neutral diagnostics across all existing projects,
without changing compression choices.

## Deliberately narrow change

This diagnostics-only PR extends the existing `frame_chunks` timing dictionary
with `lz4_layer_cost` per **existing stream key**. It does NOT add selectable
compression modes, alter frame bytes, modify `AdaptiveCompressionBudget`,
change the default GUI or developer CLI, or touch D3D11/Rust decoder, codecs,
FFmpeg/audio, layer ordering, SHA-256 or ACK handling.

Final `native_report.frame_lz4_layer_cost` contains up to eight entries.
An entry is recorded only when the current global LZ4 policy actually
**attempts** to compress a non-reused plane. Each entry includes:

- `attempted_planes`: attempted non-reused planes for that stream.
- `source_bytes`: uncompressed bytes considered.
- `wire_bytes`: output bytes from that codec attempt, including raw fallback.
- `saved_bytes`: source minus output.
- `task_elapsed_seconds`: sum of CPU compression-task elapsed durations.
- `compressed_planes`: attempts that selected LZ4 rather than raw.

The per-layer task times can overlap each other and Qt/Rust work; do NOT
add them to infer total wall time. `source_bytes` here applies only to
attempted planes and is intentionally distinct from the existing
`frame_layer_transport.source_bytes`, which counts all planes including
reused ones. No large BGRA bytes are retained in diagnostics.

## Verification

- Unit tests compare generated packet bytes **exactly** with and without
  timings for v3 raw/LZ4/previous mixed inputs.
- Verify that raw fallback costs zero saved bytes, repeated planes do not
  create new compression attempts, and per-layer sums agree with existing
  global LZ4 counters.
- Verify report persistence with a bounded Python mock native receiver
  that checks per-frame SHA-256, packet order and ACKs.
- Require both the Windows serial build and actual audio Canvas packet
  CI before squash merge. CI success indicates no behavior regression,
  **not** a runtime speed improvement.

The next optimization proposal should be backed by multiple balanced A/B
runs and representative project types, rather than selected-layer wire
bytes alone.
