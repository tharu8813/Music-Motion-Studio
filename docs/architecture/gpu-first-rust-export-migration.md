# GPU-first / Rust export engine migration (RTX 5060, 1080p60)

## Starting point

A representative user machine (Intel i5-14600KF, NVIDIA RTX 5060 8GB)
reported 80% CPU and 21% Windows overall GPU use during **H.264 NVENC**
encoding of a 1920x1080, 60 FPS, 3m43s video. The encoder was already on the
GPU; the main remaining path is **Qt CPU scene rasterization and FFmpeg software
compositing**.

Important: overall GPU utilization is not the same as the Task Manager
`Video Encode`, `3D`, `Compute`, and `Copy` engine utilization. A low
overall GPU percentage does not prove NVENC is unused. The target is **shorter
end-to-end export wall time at identical quality and timing**, not making the
GPU meter read 100%.

## Export pipeline today

1. Python manages the compiled track timeline and Canvas sources, audio
   features, lyrics, animation, and multiple capture workers.
2. `QGraphicsScene.render(QPainter(QImage))` does the bulk of frame painting on
   CPU. A 1080p RGB32 frame is about 7.9MiB.
3. The Windows named-pipe transport streams CPU-memory RGB32/RGBA frames to
   FFmpeg, and the FFmpeg `overlay` graph composites on the CPU.
4. NVENC receives final frames and handles GPU hardware encoding. The
   `libx264` software encoder is also available.

The earlier Rust 0.3 native extension accelerates FFT frequency bands, PCM RMS
and waveform analysis, and an optional BGR24 transport experiment. Those
kernels **do not** replace Qt Canvas frame drawing or FFmpeg compositing.

## Milestone 1: experiment with Qt OpenGL offscreen paint (this PR)

`app/preview/gpu_canvas_capture.py` owns an optional, per-worker OpenGL
context, framebuffer and QPainter device. When explicitly enabled it runs the
existing Qt scene drawing commands on the OpenGL paint engine instead of the
CPU QImage raster paint engine.

```powershell
# Rebuild is NOT necessary; the existing native 0.3.0 Rust wheel is unchanged.
git pull
$env:PLAYLIST_CANVAS_EXPERIMENTAL_GL_EXPORT = "1"
.\.venv\Scripts\python.exe main.py
```

**WARNING:** This path is experimental and off by default. Qt OpenGL
antialiasing, fonts and blending can differ from QImage rasterization.
`QOpenGLFramebufferObject.toImage()` still downloads the GPU frame into
CPU memory before the established rawvideo pipe uploads it to NVENC. The
result may be *slower* than the CPU rasterizer. A context/driver failure
within Python disables it and falls back to the normal CPU renderer; a
catastrophic GPU driver crash cannot be recovered by Python.

Run the non-destructive local probe with the exact NVIDIA driver:

```powershell
.\.venv\Scripts\python.exe tools/benchmark_gpu_canvas_capture.py --width 1920 --height 1080 --frames 30 --repeats 3 --output gpu-canvas-local.json
```

The JSON reports pixel deviation, renderer availability, CPU/GPU render time
and GPU->CPU readback cost. Do not promote the experimental backend to a
default based only on a successful offscreen context. Check project frames
with TTML lyrics, gradients, clipping, image effects, particles, transitions
and both transparent/opaque Canvas bands.

Windows GitHub-hosted runners may not offer functional OpenGL; CI must report
that as `opengl_unavailable`, not mislabel it a passed GPU speed benchmark.

## Milestone 2: Rust-owned scene / timeline commands

Move immutable draw commands, Z-order, coalesced schedules, frame timing and
value-only animation calculations into a separate Rust model that can be
compiled and tested against the existing Python source-of-truth. Keep Qt
editor widgets and UI event dispatch in Python. A compiled command buffer
must handle all existing SourceType variants, lyric cue overlaps, KRC/TTML
word timing, custom keyframes, and mixed audio-driven effects **before**
replacing current capture paths. A Rust port of bookkeeping alone will not
materially increase GPU usage.

## Milestone 3: resident GPU compositor and native encoder interop

Use a persistent Rust-controlled D3D11/D3D12 or CUDA graphics backend that
renders textures, layer transforms, text glyph atlases, blur, alpha and other
effects **on the GPU**. The renderer must share or map hardware video frame
surfaces into NVENC or libavcodec hardware frame contexts, instead of
reading them back to `QImage` and piping rawvideo through system memory.

Alternatively, restricted, verified FFmpeg `overlay_cuda` hardware graphs can
accelerate composition of compatible YUV frame formats. This is not an
automatic replacement for the current RGBA overlay: CUDA format restrictions
require extra conversion, and the resulting pixel values may differ.

An optional CPU renderer remains for devices without a compatible GPU and for
unsupported source effects.

## Promotion gates

- Hardware support: actual Windows RTX 5060, NVIDIA driver, OpenGL/D3D11/CUDA
  and the installed FFmpeg build.
- Fidelity: reference CPU frames at start, mid-track, overlaps and transitions;
  compare geometry, RGB, alpha, words and timestamps. No stealthy change in
  subtitle and motion semantics.
- Timing: identical projects, resolution, FPS, codec, bitrate/preset and audio
  settings, measuring end-to-end duration rather than only GPU use.
- Reliability: 10-minute+ exports, cancellation, out-of-memory, device loss,
  worker process cleanup and GPU fallback.
- Performance: measure CPU process usage, per-GPU-engine use (3D/Copy/Video
  Encode), VRAM high-water mark, write bandwidth, FFmpeg filter, GPU
  compositor, GPU readback and NVENC times.

An unchanged NVENC encoder is not evidence of a GPU-first renderer. Likewise,
a Rust source-code percentage is not a performance metric; prioritize moving
the pixel-generating hot path and eliminating round-trips.
