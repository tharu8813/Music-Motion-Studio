# P1 1080p60 Alpha-trim A/B: candidate, not a production toggle

## Measured reason

The user's 2026-11-14 `giggles` 30s, 60fps, 1920×1080 RTX 5060
report: 1,800 frames in 44.557s, 42.09 stream fps.
Qt capture 29.002s, SHA-256 19.524 CPU seconds (11.136s wall),
NVENC encode 3.833s, shared packet decode 14.255s. These stage
timers **overlap**. Native BGRA transfer was 30.627GB.
`layer:1` (album_cover + lyrics + text) carried 14.549GB (47.5%);
the background base carried 13.465GB (44.0%).
Only **transparent non-base layers** are eligible for alpha trim.

## Experiment

The existing exact-alpha-pixel crop in
`app.preview.native_canvas_layer_packets._nontransparent_crop`
is *not* enabled by the automatic GUI export profile. It is already
tested with moving outlined/blurred lyrics. This PR makes it accessible
from the developer-only CLI for a fully isolated A-B-B-A comparison.
It does NOT silently enable a slower/untested optimization.

`tools.native_gpu_full_export --gpu-profile automatic --gpu-trim-transparent-bands`
selects the existing opt-in behavior. It requires automatic profile;
it has **no user setting or UI control**, cannot change the base layer,
and always preserves every nonzero-alpha source pixel.

```powershell
python -m tools.native_gpu_p1_benchmark `
  --experiment alpha_trim `
  --project "C:\Users\wlqor\Desktop\giggles.pvsproj" `
  --native-exe ".\native\gpu_export_core\target\release\d3d11_qt_canvas_stream_export.exe" `
  --output-dir ".\build\p1-alpha-trim-1080-unique" `
  --scale 1.5 --width 1920 --height 1080 --fps 60 --seconds 30
```

- A1, A2: normal automatic production profile.
- B1, B2: identical configuration plus *only* alpha-trim enabled.
- All four render real 30-second MP4s, full 1800-frame video PTS
  checks, decoded video SHA-256 and decoded stereo PCM SHA-256.
- The benchmark preflight refuses to run when project Canvas dimensions
  multiplied by scale do not equal the requested resolution.
- Per-band `raster_cache.alpha_trim_band_timing` reports attempts,
  cropped frames, source/candidate BGRA bytes, alpha scan wall seconds
  and copy wall seconds. These diagnostics count attempts rather than
  making a misleading performance claim from wire-byte savings alone.
  Existing `compact_canvas_bytes_saved` also includes partial-band ROI.

Only consider promoting the automatic trim path if **all media parity
checks pass**, a repeated 1080p test shows a stable overall speedup
(including scan/copy overhead), and cancellation, lyric overlap and
layout tests still pass. Until then, unchanged automatic is the
production default. Rollback is immediate: omit the flag.
