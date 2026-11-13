# P1 A-B-B-A benchmark verdict semantics (2026-11-14)

A-B-B-A reports historically wrote `"verdict":"pass"` whenever decoded
media **matched**; it never meant that B improved performance. This was
misleading in a measured failure case. The legacy field and CLI return code
are preserved for existing automation, but new additive fields state the
distinction explicitly:

- `verdict_scope = decoded_video_audio_pts_parity_only`: compatibility
  field for the entire decoded 1,800-frame video, audio and PTS sequence
- `performance_verdict = faster | equal | slower`: *observation* based on
  the A/A versus B/B mean wall time, not a statistical significance claim
- `candidate_mean_delta_seconds`: B average minus A average (positive
  means B is slower)
- `automatic_promotion_decision`: reject if media mismatched or B was
  equal/slower; otherwise requires a second representative project,
  repeatability and appropriate resource/pixel validation before promotion
- When native metrics are present: per-trial `frame_transport_bytes`,
  `alpha_trim_band_timing`, and summary average transfer volumes, reduction
  percentage, mean candidate alpha scan/copy costs

**User RTX 5060 1080p60 alpha_trim outcome** (30s / 1,800 frames):

| Trial | Method | Total wall |
|---|---|---:|
| A1 | Original automatic | 52.87653s |
| B1 | Automatic + exact alpha trim | 63.73668s |
| B2 | Automatic + exact alpha trim | 63.55592s |
| A2 | Original automatic | 57.16109s |

A mean 55.0188s, B mean 63.6463s, B **15.68% slower** (+8.6275s)
although wire fell 30.6265GB to 23.7031GB (~22.61%).
The candidate scanned alpha for 1,800 frames (~7s wall) and copied
the compact regions (~2.9s wall). These are overlapping stage timings;
do not add them to export wall time or claim causality solely from
counters. Complete decoded video/PCM/PTS parity passed without mismatch.

**Decision: reject alpha trim for automatic mode.** Keep the opt-in
experiment for reproducibility. Optimizations must beat the entire
export wall clock, not only shrink transported bytes.
