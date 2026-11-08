# Particle overlay frame-rendering optimization

The existing export renderer paints animated particle overlays using
`app/utils/particle_painter.py`. Previously, each painted frame recalculated
four seeded random values for every particle, even though those values depend
only on `(seed, density, particle_index)` and never on export time.

## Optimization

`_particle_static_values(density, seed)` memoizes **immutable** seed-derived
particle parameters using a **64-entry LRU**. Particle size and position are
still computed with the same original arithmetic and the same `time_seconds`
for every frame. Qt/QPainter calls, animation, opacity, glow, alpha and
all FFmpeg/codec settings remain unchanged.

For a diagnostic A/B comparison, run the original (uncached) path with:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_PARTICLE_STATIC_CACHE = "1"
.\.venv\Scripts\python.exe main.py
```

Then remove that environment variable and render the same project:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_PARTICLE_STATIC_CACHE -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe main.py
```

A new process must be launched after changing this switch; the setting is
read once at import. The existing
`tools/export_bottleneck_profile.py` and `tools/export_ab_verify.py`
can compare real end-to-end export wall time and decoded frames.

## Windows evidence

On Windows CI with PySide6 6.11.1 and Python 3.14.10, the following A/B
microbenchmarks drew **120 animated QImage frames, 300 particles per frame**,
three alternating trials per mode:

| Particle style | Uncached median | Cached median | Speedup |
| --- | ---: | ---: | ---: |
| Stars | 1.10367 s | 1.06595 s | 1.035x |
| Noise | 0.28398 s | 0.23646 s | 1.201x |

All particle styles, different timestamps, seed/density variants, and
rectangle variants passed byte-for-byte QImage pixel parity. The test also
checks the cache size bound and confirms that warmed paints no longer call
the seeded random function.

[Windows run and benchmark artifact](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/42518870491)

**Important limitations:** the measurements are for the particle-paint layer
alone, not entire video encoding or file export, and do not predict speedup
for projects without particle overlays. Earlier real-project timings reported
61.3 seconds *cumulative* `canvas.particle_paint_seconds`; that timing
does not mean the optimized export will be faster by 61.3 seconds because
parallel stages and inclusive timers overlap. Full-project A/B measurements
on the same machine/settings are still needed.
