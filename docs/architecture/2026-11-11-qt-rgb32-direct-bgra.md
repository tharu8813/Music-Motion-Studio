# Avoid redundant Qt RGB32 -> straight BGRA copies in native GPU export

## RTX 5060 measurements (same input project and 1800 frames)

The user's identical-source-sha `giggles.pvsproj`, 1920x1080 / 60 FPS / first 30s:

| Version | Native FPS | Total seconds | Qt capture seconds | BGRA worker seconds |
| --- | ---: | ---: | ---: | ---: |
| #109 main automatic parallel | **26.767** | **74.407** | **36.945** | 19.038 |
| #110 Rust buffer recycling | 22.144 | 89.432 | 52.541 | 29.143 |
| #111 cached Qt scene traversal | 23.739 | 83.859 | 44.958 | 24.531 |

Both follow-up branches passed Windows CI but **neither demonstrated speedup**. Neither was merged. These single-run comparisons can reflect machine load or thermal conditions, so do not attribute slower Qt capture to one code change without repeated alternating baseline runs.

## Change (#112)

`straight_bgra_bytes(QImage)` always called `image.convertToFormat(Format_ARGB32)`. That was needed for transparent Qt `Format_ARGB32_Premultiplied` images to un-premultiply the glyph alpha, but the full-resolution base Canvas is drawn into `Format_RGB32` and already has B,G,R,0xFF bytes on little-endian Windows. Likewise `Format_ARGB32` stores straight B,G,R,A already. For these **two** formats, skip the extra full-frame Qt color-format conversion and read `constBits()` directly. Still make one immutable `bytes` copy for the current sender ownership and unchanged SHA-256 checks. All other image formats continue to use `convertToFormat(Format_ARGB32)`.

No changes to actual Qt painting, premultiplied/straight alpha semantics, schema/sha/transport, FFmpeg path or available GUI export choices. No new user option. Native output contract remains lossless.

## Validation

Tests compare the fast implementation byte-for-byte against a separately computed Qt `convertToFormat(Format_ARGB32)` reference for RGB32, ARGB32, ARGB32_Premultiplied, and Grayscale8 in several dimensions, using Qt painting with transparency. A second test captures a genuine opaque scene base and a translucent antialiased text band; both must produce canonical BGRA bytes. Existing GPU packet, TTML, ROI, canvas and Windows deployment workflows must pass.

**Performance unverified** until benchmarked on the same RTX 5060, 1920x1080, 60 FPS, 30s with the same source SHA. Compare to #109's 26.767FPS / 74.407s and especially `raster_cache.cpu_parallel_bgra_work_seconds`, `frame_production_seconds` and end-to-end wall clock. Those phase times overlap; do not add them.
