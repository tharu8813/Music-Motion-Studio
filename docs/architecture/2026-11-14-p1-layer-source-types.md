# P1: Identify high-wire native GPU packet layers by source type

A user RTX 5060 export profile for `giggles` (2026-11-14)
completed 1,800 frames at 1280x720 and sent ~10.39 GB of
uncompressed/adaptively packed layer payload. The `frame_layer_transport`
report shows `layer:1` carrying ~6.32 GB (~61% of total),
but previous reports could not attribute this layer to a Canvas band
or dynamic effect.

`native_report.raster_cache.frame_layer_source_types` now describes
every stream key `base`, `layer:0`, etc. with:
- `kind`: `qt_canvas` or `dynamic`;
- `source_types`: source enum strings (e.g. `lyrics`,
  `audio_visualizer`, `particle_overlay`);
- `source_count` for that band.

The dictionary order matches **the final packet order**, after Canvas
band splitting, translated covers or GPU text splits. It is built once
from the detached export's immutable stack and written only after the
frame generator exits, not per frame. It deliberately excludes source
IDs, titles, media paths and project names.

This is a profiling enhancement only: no Canvas pixels, layer order,
binary MMSB packets, SHA-256, GPU shaders, NVENC behavior or output MP4
files change.

The next user 1080p60 run can correlate
`native_report.frame_layer_transport["layer:1"].wire_bytes`
with
`native_report.raster_cache.frame_layer_source_types["layer:1"]`
and prioritize the largest **real** source category. A single
720p instrumented clip is not evidence of a measured speedup.
