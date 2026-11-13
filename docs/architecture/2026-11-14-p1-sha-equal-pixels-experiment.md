# P1 exact BGRA equality before SHA-256 (2026-11-14)

## Why this remains an experiment

Real RTX 5060 1080p60 / 30-second `alpha_trim` A-B-B-A decreased wire
by 22.61%, yet **increased** mean end-to-end wall time by 15.68%.
`p1-summary.verdict=pass` established only decoded video, PCM and PTS
parity. The default automatic profile must therefore remain unchanged.

The existing MMSB v2/v3 sender computes SHA-256 for every newly
allocated BGRA byte buffer. It already skips hashing when an immutable
`bytes` object is *identical* to the previous one for the same stream.
A repeated Qt capture can produce a **new** `bytes` object with **exactly
the same** pixels, forcing an unnecessary full SHA-256. This opt-in
candidate first checks full immutable-byte equality against the
immediately previous stream layer. An exact match reuses its previous
SHA-256 and existing native previous-frame reference. A difference
anywhere reverts to the original SHA-256. Receiver-side validation,
manifest SHA-256, layer geometry, opacity, transforms and wire version
are unchanged. No sampled hashes or lossy shortcuts.

An equality comparison can cost more than hashing when pixels change
often or only at the tail of large buffers. This may regress performance.
Do not enable `reuse_equal_pixels` in `automatic` or GUI based on a
reduced `frame_sha256_bytes` counter alone.

## Real hardware acceptance

From a clean main containing this PR, use a NEW output directory per run:

```powershell
python -m tools.native_gpu_p1_benchmark `
  --project "$env:USERPROFILE\Desktop\giggles.pvsproj" `
  --native-exe ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe" `
  --output-dir ".\p1-sha-equal-giggles" `
  --scale 1.5 --width 1920 --height 1080 --fps 60 --seconds 30 `
  --experiment equal_pixels
```

The benchmark runs A1 (automatic), B1/B2 (automatic + exact equality),
then A2 (automatic); the same binary, project and profile are used.
Read `p1-summary.json` fields `verdict` (entire decoded media parity),
`performance_verdict` (actual whole-export speed), and
`automatic_promotion_decision`. The report additionally exposes
`baseline_mean_sha256_bytes`, `candidate_mean_sha256_bytes`,
`candidate_mean_sha256_equality_reused_bytes` when available.
In per-run native report, `frame_sha256_equality_checked_bytes`
is the **logical size of inputs tested**, not a measured number of
memory bytes loaded by `memcmp`. `frame_sha256_equality_seconds`
is the CPU sender comparison wall cost and overlaps other stages.

Use a second representative project containing dynamic lyrics, text and
audio overlays, with 1920x1080 output geometry confirmed in advance.
Promote to automatic only after BOTH projects show reproducible
end-to-end gains, no video/audio/PTS mismatches, and stable Windows
resource usage. A slower/equal/mismatched outcome means **reject**;
the experimental implementation can remain for reproducibility.
No Windows GPU performance claim is made by unit tests or hosted CI.
