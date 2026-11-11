# Native Canvas empty-keyframe context optimization (experimental, 2026-11-12)

## Evidence and scope

The last BGRA converter-side SHA prehash A-B-B-A (PR #125) moved
33,899,817,600 BGRA bytes of hashing into already-active conversion workers,
and lowered sender SHA-stage wall time 25.6%, but did **not** produce a
repeatable total MP4 improvement: baseline CLI averaged 114.77s, candidate
113.28s with severe 26s baseline run-order drift. Decoded video+audio
hashes matched. That experiment was closed unmerged.

Qt Canvas remains a larger target: across recent 1800-frame / 5400-raster
30s 1080p60 exports, Canvas setup took 21–31s cumulative and QPainter
painting 33–40s cumulative. Three Z bands required repeated per-source
animation state preparation even where no stored keyframe curves exist.

## Exactly what changes (opt-in only)

In `CanvasSnapshot.capture_track`, each SourceItem currently evaluates
keyframe values and unconditionally registers `keyframe_source(item, {})`
in an `ExitStack`. For empty curves and no editor blur rebind,
`keyframe_source` is a true identity context: it yields the original
Source with no property, pixmap or geometry changes.

The developer-only `--gpu-skip-empty-keyframes` flag (requires
`--gpu-profile automatic`) bypasses **only that no-op context creation**.
Keyframes with any evaluated value, or editor keyframe pose with a blur
filter in `_editor_keyframe_values`, always use the original
`keyframe_source` path. Per-frame text templates, lyrics, keyframe
evaluation itself, geometry, source visibility, QPainter, source order,
native transport, Rust D3D11/NVENC and audio all remain untouched.

The regular automatic/GUI/FFmpeg capture path has the same behavior as
before. There is no user-facing performance toggle, frame cache,
worker-count change or project-specific special case.

Opt-in output additionally accounts for the number of actual bypassed
identity contexts as `native_report.raster_cache.qt_canvas_band_timing`
`[base|layer:N].empty_keyframe_context_skips`. This is a small integer
per current generic Z band and has no side effects on pixels or keyframe
evaluation. In normal mode the count remains zero. Benchmark reports can
thus prove that the fast path ran on the selected project.

Example candidate (developer worktree):

```powershell
python -m tools.native_gpu_full_export --project C:\\path\\project.pvsproj `
  --output C:\\path\\optimized.mp4 --report C:\\path\\optimized.json `
  --native-exe C:\\path\\d3d11_qt_canvas_stream_export.exe `
  --fps 60 --scale 1.5 --smoke-seconds 30 `
  --gpu-profile automatic --gpu-skip-empty-keyframes
```

Omit the final flag to run the baseline on the **same PR HEAD**.

## Safety and adoption gate

Tests capture a real Qt shape base, animated text-clock layer and actual
global X keyframe layer across multiple PTS. The experiment preserves
**exact BGRA packet bytes**, raster cache/reuse counts and the keyframed
SourceItem context, while eliminating no-op context entries on other
layers. A separate test exercises an editor blur pose with empty values
and ensures the full rebind path still runs. CLI tests enforce that
normal automatic mode remains off and nonautomatic profile overrides
fail before opening any project.

Before hardware testing require all latest Windows CI (serial Qt,
lyrics, keyframe preview, GPU H264 and real Canvas/audio) success and
no review conflicts.

Run A-B-B-A with the same SHA-pinned PR head, Python, Qt, Rust worker,
source-project hash, 1800 CFR frames and auto GPU profile.
Compare complete CLI wall time, Qt per-band state setup and paint
time, sender SHA, native pipe, decoded video/audio parity and process
memory. The normal 3-packet-worker setting stays unchanged.
Substage timers overlap and must not be summed.

Do **not** merge this experiment for the mere reduction in Python
ExitStack allocations or a small noisy timing difference. If full
MP4 latency does not reliably improve, close the PR without merging
and preserve existing behavior.
