# Rust GPU export engine: native compositor milestone 1

## Problem we are solving

On one user's NVIDIA RTX 5060 and i5-14600KF, a 1080p60 NVENC export
used approximately 80% CPU but only 21% overall GPU. The original
`CanvasSnapshot.capture()` uses CPU `QPainter(QImage)`. FFmpeg's CPU
`overlay` filter then composites the raw QImage-derived streams before the
NVENC hardware engine compresses the final image. Merely translating those
operations from Python to Rust cannot make the rasterization and compositing
GPU-resident.

The user measured the previous experimental OpenGL framebuffer path at
**0.15656 s versus 0.08721 s CPU**, for the same 30 synthetic 1080p Canvas
frames, with significant pixel differences. CUDA FFmpeg overlay was only
**~1.9% faster** on its separate short synthetic benchmark, with a different
pixel format and without scene/lyrics fidelity validation. Neither of those
paths should become a production default.

## This milestone: actual Rust GPU compute compositor

`native/gpu_export_core` is an **independent** Rust crate using `wgpu`
with Direct3D 12 or Vulkan graphics backends. It does not require an
application-level Qt OpenGL context. This is a new backend core, **not a
replacement of the existing PyO3 native audio module (v0.3.0)**.

Implemented:
- Select a real discrete or integrated hardware GPU; decline CPU/WARP
  software adapters rather than reporting misleading GPU acceleration.
- Validate opaque RGBA8 base and up to 32 straight-alpha rectangular
  overlays, bounds, byte lengths and maximum frame memory.
- Upload base and overlay bytes, run a real WGSL GPU compute shader for
  alpha-over composition. Overlay passes ping-pong between two GPU buffers
  without intermediate downloads.
- Retain source/Z-order across layers, support negative placement offsets
  and clipping, and read back to CPU only **once after all overlay passes**.
- Compare byte-for-byte to a separate deterministic Rust CPU reference
  using identical integer rounding; include reference unit tests.
- Fail or report unsupported hardware. No silent GPU-to-CPU substitution
  inside this experimental core.

This is a meaningful **GPU-resident composition prototype**, but its input
and output are still CPU RAM because the existing Canvas draws QImage
pixels. It is **not** a zero-copy GPU-to-NVENC device frame.

## Run locally on the actual RTX 5060

Requires Rust, Cargo and Visual Studio C++ Build Tools. With
`C:\Users\wlqor\Desktop\playlist_project` as working directory:

```powershell
git pull
cargo test --manifest-path native/gpu_export_core/Cargo.toml
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_probe -- --width 1920 --height 1080 --frames 30 --output rust-gpu-compositor-local.json
```

`rust-gpu-compositor-local.json` reports the adapter and backend, GPU
availability, CPU-reference and GPU (upload+compute+readback) per-frame
median times, and strict output parity. The benchmark does **not** modify
projects or the exported videos. A successful compute test and lower elapsed
time on a synthetic frame are insufficient to activate a new renderer by
default.

Windows GitHub-hosted runners may expose only a software GPU: in that case
the CLI outputs `status: gpu_unavailable`. That result is **not** an
RTX 5060 throughput measurement. `cargo test` still validates CPU
reference, shape/bounds safety and the native compilation. The optional
GPU test runs only when real hardware exists.

## Limitations / explicit non-goals

- No Qt SourceType/TTML/KRC/keyframe/particle translation yet.
- No arbitrary shaped masks, blur, rotation, clipping paths or dynamic
  per-pixel effects.
- The overlay input requires straight RGBA8 and an **opaque** base.
  Qt's `Format_ARGB32_Premultiplied` needs an explicit conversion or a
  differently specified shader; silently interpreting premultiplied as
  straight alpha would break colors.
- No connection to the running FFmpeg exporter, no native D3D11 texture
  sharing, no NVENC hardware frame upload, and no change in default codecs.
- GPU buffer creation and final readback still occur for each prototype
  frame. Subsequent milestones must retain the textures/pipelines across
  frames and eliminate final CPU readback.
- The current pixel equality requirement is for this prototype's *Rust
  integer compositing contract*, **not proof** of parity with all Qt
  QPainter and FFmpeg overlay behaviors.

## Next milestones / promotion gates

1. Benchmark the actual RTX 5060 GPU probe. Record GPU API, device name,
   frame size, readback costs and the exact no-readback GPU-compute timing
   separately.
2. Add an immutable GPU render-command scene model covering the Python
   export timeline, layer sorting, lyrics, text atlas, media textures,
   transforms and color effects. Version and verify a portable command
   format against established golden frames. Keep the editor UI in Qt.
3. Implement **persistent shared GPU textures and frame synchronization**
   with native Direct3D12/D3D11 interop and compatible FFmpeg/libavcodec
   NVENC hardware frames. Never emit QImage bytes if the encoder can
   consume a shared texture/surface on the same device.
4. Benchmark full **223-second, 1920×1080/60** exports on the same
   project against original pipeline. Require: output-frame count and
   timestamps, audio stream integrity, visual/alpha fidelity, graceful
   cancellation and device-loss cleanup, no excessive VRAM growth, and
   shorter overall export wall time (not a higher task-manager GPU bar).

**Release decision:** production exporter remains unchanged. This Rust GPU
core is for validated migration and proof that native GPU composition works,
not a promise of a finished GPU-centric export engine.


## Milestone 2: persistent GPU session and no-readback composition

The first RTX 5060 Vulkan test produced **3.8738ms** median on the Rust
CPU reference versus **8.07595ms** on the initial GPU implementation, for
1080p/2 layers and 30 frames. The GPU path included creating buffers,
uploading both overlays and the opaque base, dispatching compute and
downloading the final RGBA result on *every frame*. GPU compute alone
was never separately measured in that result.

`GpuCompositor::prepare_session(...)` is now an additional experimental API:

- Upload each static RGBA overlay once, with one uniform geometry buffer
  and persistent bind group per layer.
- Allocate and retain the base, intermediate ping-pong accumulation buffers,
  and a reusable final readback buffer.
- Use `update_base(...)` and `update_overlay(index, ...)` only when input
  bytes change. New dimensions or layer geometry require a new session.
- `compose_gpu_only()` dispatches all GPU compositing passes and waits for
  completion, **without downloading the result to CPU memory**.
- `compose_and_readback()` performs the same composition but downloads
  the final frame once, for strict byte-for-byte reference verification.

The original `GpuCompositor::composite()` remains available for a control
measurement. The standalone probe now reports three distinct GPU timings:

1. `gpu_median_seconds`: legacy GPU allocation/upload/compute/readback.
2. `persistent_readback_median_seconds`: preallocated GPU resources with
   one required final CPU readback.
3. `persistent_gpu_only_median_seconds`: preallocated GPU resources and
   completed GPU dispatch without CPU readback.

The last number **excludes per-frame input uploads**, which matters for
animated overlays. It is a GPU-queue-completion wall-clock measurement,
not a hardware timestamp query. This experiment does not yet connect GPU
storage buffers directly to NVENC hardware frames, cannot render lyrics
or other Qt Canvas effects, and is not enabled in real exports.

On the RTX 5060, reproduce all three paths:

```powershell
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_probe -- --width 1920 --height 1080 --frames 30 --output rust-gpu-compositor-persistent.json
```

**Promotion criteria:** Actual GPU pixel equality, successful dynamic base
and overlay updates, no GPU buffer leaks over long exports, and same-project
end-to-end NVENC time reduction after future native GPU-frame interop.
