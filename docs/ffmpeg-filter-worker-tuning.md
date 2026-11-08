# Multilayer FFmpeg filter worker tuning

## Motivation

The existing FFmpeg encoder worker allocation for 720p and 1080p/30 or 60 FPS
could use eight filter workers on a sufficiently parallel CPU. For a
**linear** stream of repeated `overlay` filters, extra filter threads can
add contention without increasing overlay parallelism. Encoding threads are
still managed independently by the selected libx264 encoder.

## New narrow fast path

For a live, constant-frame-rate Canvas with **two or more piped transparent
layers**, no external video clips or Python visualizers, software **libx264**
encoding, 720p-to-1080p resolution and either **30 or 60 FPS**, the filter
worker count is capped at two. The existing work-mode and CPU bounds still
apply: the Stable mode stays at one worker.

All other export scenarios, including 4K, HEVC, GPU encoding, prepared
concat videos, source clips with masks, and unusual frame rates, retain the
previous filter thread allocation.

For a same-project comparison, restart from the same project with identical
FFmpeg/codec/export settings. Disable the new cap with:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_FFMPEG_FILTER_WORKER_TUNING = "1"
.\.venv\Scripts\python.exe main.py
```

Remove the environment variable in a fresh process to measure the tuned
path:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_FFMPEG_FILTER_WORKER_TUNING -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe main.py
```

Use `PLAYLIST_CANVAS_PROFILE`, `tools/export_bottleneck_profile.py` and
`tools/export_ab_verify.py` for whole-project timing, FFmpeg process metrics,
decoded frame/sample comparison and encoded audio validation.

## Windows experimental evidence

The A/B benchmark uses the application's own layered FFmpeg filter graph,
a deterministic moving source, two static alpha overlays, and **identical
output frames**, confirmed through decoded frame hashes across all thread
counts. Run order is alternated to reduce measurement bias.

An initial Windows GitHub Actions run found:

| Synthetic case | 8 filter workers | 2 filter workers | Measured kernel+encode result |
| --- | ---: | ---: | --- |
| 720p30, 90 frames, x264 veryfast | 0.4201 s | 0.4103 s | ~2.3% faster |
| 1080p30, 60 frames, x264 veryfast | 0.6527 s | 0.6375 s | ~2.3% faster |
| 1080p30, filter only | 0.1696 s | 0.1399 s | ~17.5% faster |

Source: https://github.com/tharu8813/Music-Motion-Studio/actions/runs/42519559181

A repeated second Windows run using 1080p60/90 frames and x264 veryfast
measured **0.8053 s** with eight filter workers versus **0.7905 s** with
two workers (~1.8% faster). Filter-only timing was 0.1752 s versus
0.1555 s. Pixel hashes matched.

Source: https://github.com/tharu8813/Music-Motion-Studio/actions/runs/42519806139

**Limitations:** These are synthetic, short FFmpeg-only microbenchmarks on
GitHub Windows runners. Full video export overlaps Qt Canvas rendering,
multiple worker pipes, audio and FFmpeg encoding, and may benefit differently
depending on codec, preset, resolution and computer. The opt-out lets the
same representative user project establish whether the cap is truly faster.
Never add inclusive per-process timing counters to infer whole-export time.
