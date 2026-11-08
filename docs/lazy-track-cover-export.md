# Lazy track-cover construction during Canvas capture

## Background

Each `CanvasSnapshot.capture_track()` call used to call
`extract_track_cover()` whenever a track-aware image or personal-color
source was present. That function may convert a cached QImage into a full-size
QPixmap **on every frame**, even when the existing bounded
`_FILTERED_TRACK_PIXMAP_CACHE` will immediately return the same filtered
QPixmap and never use the newly constructed artwork.

The exported images, source settings, keyframes and FFmpeg encoder are
unchanged. Only the moment at which the full-size cover QPixmap is created
changes.

## Optimization

The cover is now lazily created only when an album-cover/background filtered
pixmap cache miss actually calls the artwork factory. One frame sharing that
cover across multiple sources creates it at most once.

Personal-color-only sources continue to extract their cached palette, but no
longer create an unneeded QPixmap of the cover.

The previous eager path is accessible for comparison by setting:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_LAZY_TRACK_COVER = "1"
.\.venv\Scripts\python.exe main.py
```

To use the optimized default:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_LAZY_TRACK_COVER -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe main.py
```

## Windows verification

[Windows artifact and unit tests](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/42522251305)

The tests passed byte-for-byte Canvas image parity for both eager and lazy
paths, shared album-cover/background artwork, and personal-color-only sources.

A controlled 120-frame, three-trial QGraphicsScene benchmark with two artwork
sources and a 1024x1024 decoded cover reported:

| Metric | Existing eager path | Lazy path |
| --- | ---: | ---: |
| Full-size QPixmap conversions per 120 frames | 120 | 1 |
| Median Canvas painting/capture wall time | 0.19239 s | 0.18219 s |

That is **1.056x** faster (approximately 5.3% less wall time) for this
particular Canvas capture workload.

**Limits:** The benchmark is short, uses a synthetic cached artwork and only
measures Canvas frame generation. It is not an overall MP4 export speedup and
does not predict an equal improvement for projects without album artwork,
which already avoid extracting it.
