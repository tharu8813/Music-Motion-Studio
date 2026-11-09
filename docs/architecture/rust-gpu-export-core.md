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


## Milestone 3: animated RGBA input and GPU-resident NV12 export buffer

The RTX 5060 measurements for milestone 2 showed:
- Original per-frame Rust GPU allocation/upload/readback: **7.9758ms**
- Persistent GPU resources with final CPU download: **2.52525ms**
- Unchanged pre-uploaded layers, GPU-only completion: **0.2265ms**

The last measurement excludes per-frame image updates, so it cannot predict
animated lyric/particle exports. This milestone measures that previously
missing cost with an animated base plus a changing overlay:

1. `GpuFrameSession::update_base()` and `update_overlay()` upload
   changed RGBA data to existing GPU buffers.
2. Ordered straight-alpha overlay shader passes stay on the GPU.
3. A second integer WGSL shader converts the finished RGBA buffer into
   **NV12**, a common input format for H.264/NVENC.
4. The output is retained in a **persistent wgpu GPU storage buffer**. A
   CPU readback function exists only for parity and a *separately requested*
   FFmpeg/NVENC integration probe.

The CPU reference `rgba_to_nv12_reference` and GPU shader are specified by
the same integer formula (approximate limited-range BT.709), including
round-to-nearest 2x2 chroma averaging. Sizes require a width divisible
by four, an even height, an opaque base and straight RGBA overlays. The
output buffer contains tightly packed Y bytes then interleaved UV bytes.
This is **not** proven color-exact with FFmpeg `swscale`/Qt; NVENC
compatibility and final H.264 visual fidelity must be verified separately.

The GPU-only method `compose_nv12_gpu_only()` performs the RGBA composition
and GPU NV12 conversion with no CPU readback. It still cannot hand the
storage buffer directly to NVENC: an interoperable D3D11/CUDA hardware
video-frame allocation and synchronization are future work.

### RTX 5060 measurement

```powershell
git pull
cargo test --manifest-path native/gpu_export_core/Cargo.toml
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_dynamic_probe -- --width 1920 --height 1080 --frames 30 --output rust-gpu-dynamic-nv12-local.json
```

Report fields include:
- `cpu_reference_rgba_nv12_median_seconds`: Rust CPU alpha-over+NV12,
  excluding synthetic input generation.
- `gpu_dynamic_rgba_readback_median_seconds`: dynamic uploads + GPU
  RGBA composition + CPU readback.
- `gpu_dynamic_nv12_readback_median_seconds`: dynamic uploads + GPU
  RGBA composition + on-device NV12 + CPU readback.
- `gpu_dynamic_nv12_resident_median_seconds`: dynamic uploads + GPU
  RGBA composition + on-device NV12, with the GPU completion wait but
  **no** download or NVENC encoding.

Only the last two GPU modes include the NV12 transformation; compare
matching work when interpreting results. Every frame in the diagnostic
readback paths is byte-compared to Rust references, not assumed correct.

### Optional actual H.264 NVENC video compatibility check

On a workstation with FFmpeg + NVENC available, explicitly request a
short synthetic MP4:

```powershell
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_dynamic_probe -- --width 1920 --height 1080 --frames 30 --ffmpeg ffmpeg --nvenc-output gpu-nv12-compat-test.mp4 --output rust-gpu-dynamic-nv12-local.json
```

The probe refuses to overwrite an existing video. It validates every
on-device NV12 frame against the Rust reference, then **reads back** that
frame for the FFmpeg `rawvideo/nv12 -> h264_nvenc` CLI. It is an
integration **compatibility** test, not zero-copy and not an export of a real
Music Motion Studio project. Do not compare this probe's total command
duration to the 0.2265ms GPU-only alpha benchmark.

### Automated validation boundaries

- Windows CI compiles the Rust crate and its release diagnostic commands,
  verifies CPU reference and buffer validation, and records unavailable GPUs
  without claiming RTX throughput.
- Mesa lavapipe on Linux CI **executes the actual GPU overlay and NV12
  shaders**, checking byte equality, buffer reuse and changing frames.
  Lavapipe shader correctness is helpful, but not an NVIDIA driver test.
- A real RTX 5060 run remains necessary for native Vulkan/DX12 execution,
  NVENC compatibility and meaningful performance estimates.
- Existing Python Canvas capture, GPU frame transport, TTML/KRC lyrics,
  audio and the release distribution are unchanged.


## Milestone 4: dirty rectangles for dynamic lyrics and Canvas overlays

`GpuFrameSession::update_overlay_rect()` and `update_base_rect()`
update a **tightly packed RGBA8 rectangle** within an existing persistent
GPU buffer. Both methods validate all bounds and bytes **before** any GPU
transfer, so invalid partial updates cannot leave a half-modified frame.
Opaque base patches must have alpha=255. Transparent overlay rectangles may
use any straight-alpha value, including 0 and 255.

Full-width patches require one contiguous `wgpu::Queue::write_buffer`
call. A narrower patch is stored into tightly packed buffer rows, so it
requires **one GPU queue write per dirty row**. The implementation keeps
the previous layer geometry, Z order, GPU bindings, NV12 output memory
and shader pipeline. There is no change to how the full video is encoded.

The separate benchmark compares **real wgpu GPU completion wall times** for
one full overlay upload vs a 128×64 dirty region (or a smaller region on
low-resolution frames) containing identical animation state. The timed path
includes GPU writes and the full RGBA composite + GPU NV12 conversion,
but excludes the readback used for reference-frame parity.

```powershell
git pull
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_dirty_probe -- --width 1920 --height 1080 --frames 30 --output rust-gpu-dirty-local.json
```

Important fields are `full_update_plus_gpu_nv12_median_seconds` and
`dirty_update_plus_gpu_nv12_median_seconds`. The probe validates every
final NV12 frame against an independent Rust reference and reports
`full_overlay_bytes_per_frame` and `dirty_bytes_per_frame`.

**Do not assume that less data equals less time.** The partial path may
issue dozens of small writes instead of a single large transfer, while a
modern GPU can upload large sequential buffers efficiently. A real
RTX 5060 measurement is the acceptance gate. The current default exporter
still paints Qt QImages on the CPU and sends frames to FFmpeg, so its
speed and CPU/GPU utilization have not yet changed.

Dynamic Python/Qt layer invalidation and dirty-rectangle tracking are
not yet wired to this Rust API. The native core is experimental.


## Milestone 5: native D3D11 NV12 video textures (experimental)

A local RTX 5060 FFmpeg compatibility probe succeeded for 1920×1080
H.264/60FPS, producing **120 decoded frames** through the D3D11VA
hardware-frame → NVENC path:

- Software NV12 input → NVENC median **408.303ms**.
- FFmpeg `d3d11va` hardware NV12 frames → NVENC median **438.422ms**
  (about 7.4% slower for this short, CPU-generated synthetic source).
- Hardware-frame consumption is supported, but Rust native texture
  interoperability was **not** tested in that probe.

To progress beyond FFmpeg's internally allocated textures, the Rust
`d3d11_nv12` Windows module now independently creates a native
`DXGI_FORMAT_NV12` GPU `ID3D11Texture2D` from an explicit D3D11
hardware device. It checks NV12 Texture2D support, retains the original
device/context, and manages dedicated CPU upload/readback staging textures.

A validation-only method uploads a tightly packed NV12 frame with
driver-reported **RowPitch** correctly handled for both the Y and
interleaved UV planes. The device copies to a resident GPU texture,
and a separate diagnostic copy reads back the frame byte-for-byte.
Every frame changes both Y and UV; tests reject malformed dimensions
and wrong frame byte lengths. This validates actual native D3D11
allocation/copy and resource lifetime on a supported adapter.

```powershell
git pull
cargo test --manifest-path native/gpu_export_core/Cargo.toml
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nv12_surface_probe -- --width 1920 --height 1080 --frames 20 --output rust-d3d11-nv12-local.json
```

The report contains `status: measured` only when native hardware texture
creation, repeated native GPU copies and exact Y+UV data validation succeed.
In a Windows VM without native D3D11 support, it reports
`d3d11_nv12_hardware_unavailable`. No actual GPU performance improvement
or full Music Motion Studio export speedup is implied.

**This is NOT yet wgpu Vulkan → D3D11 cross-API texture sharing, nor an
in-process FFmpeg `AVHWFramesContext`, nor zero-copy NVENC.** Current
`upload_reference()` still starts with CPU NV12 bytes. Next work must
resolve Vulkan vs Direct3D adapter identity and design an in-process
hardware-encoder device/texture ownership model; passing a raw pointer
to a separate FFmpeg process is not a correct interoperability mechanism.

Production PySide6/Qt rendering, audio, encoder arguments and the
`music-motion-native 0.3.0` Python extension remain unchanged.


## Milestone 6: bounded native NV12 GPU frame ring and adapter identity

The user's RTX 5060 native D3D11 test confirmed:
- `status: measured` on **NVIDIA GeForce RTX 5060**, DXGI vendor
  `0x10DE`, device `0x2D05`.
- 20 consecutive **1920×1080** native `DXGI_FORMAT_NV12` textures,
  all Y/UV bytes unchanged after CPU staging → GPU copy → diagnostic readback.
- **1.46915ms** median per frame including CPU NV12 upload and mandatory
  verification readback. This is not an NVENC zero-copy measurement.

A new Windows-only Rust module, `d3d11_frame_pool`, builds a bounded ring
of **2–8 persistent NV12 textures on one D3D11 device**. Each enqueued frame
uses GPU `CopyResource` to a free texture; there is **no CPU readback
between the staging upload and GPU texture copy**. Frames carry strictly
increasing presentation timestamps; the oldest ready frame is reserved
first and an in-flight texture cannot be reused until explicitly marked
completed. A full ring reports backpressure instead of overwriting data.

Resource lifecycle is explicit but **hardware encoder integration and
completion fences remain unimplemented**. `complete_encode()` is invoked
only by the diagnostic test, after validation. In a real encoder, calling
it without a confirmed GPU completion event could corrupt subsequent
frames. Never promote these primitives to production without a verified
encode-completion fence and resource-ownership contract.

The diagnostic compares D3D11's DXGI vendor/device IDs to those returned
by the separate Rust `wgpu` GPU adapter. **Matching PCI IDs are only a
guard**, not evidence that a Vulkan storage buffer can be shared with
a D3D11 texture. Exact adapter LUID and interop synchronization will
be required before cross-API memory sharing.

Run on the RTX 5060:

```powershell
git pull
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nv12_frame_pool_probe -- --width 1920 --height 1080 --frames 30 --slots 3 --output rust-d3d11-frame-pool-local.json
```

The report shows adapter identity, whether vendor/device IDs match,
completed frames, backpressure safety, per-frame Y/UV byte parity, and
`median_cpu_staging_upload_plus_gpu_copy_enqueue_seconds`. The timing
is **CPU submission only** and cannot be interpreted as GPU completion
or encoding duration. Each frame is separately verified by a diagnostic
readback outside the timing.

Remaining major milestones:
- Connect the GPU composition output to a native D3D11 texture on the
  same device, rather than uploading CPU NV12 bytes.
- Implement an **in-process encoder** with real D3D11 texture ownership
  through FFmpeg AVHWFramesContext or NVIDIA Video Codec SDK.
- Add GPU completion fences and bounded asynchronous presentation-time
  scheduling, test real H.264 frames and exact timestamp/visual parity.
- Integrate gradually with the existing Qt Canvas+lyrics exports only
  after a same-project performance and fidelity A/B comparison.


## Milestone 7: same-device GPU BGRA to NV12 video conversion

The RTX 5060 D3D11 frame-pool report confirmed **30/30** completed
1920x1080 frames, all NV12 bytes preserved, 28 correct full-pool
backpressure rejections, and matching PCI IDs from Vulkan wgpu and
D3D11 (NVIDIA `0x10DE:0x2D05`). Median CPU staging upload plus
GPU-copy *enqueue* was **0.2641ms**, not a GPU-completion measurement.

We have now implemented an additional Windows-only
`NativeBgraNv12VideoProcessor` using the native D3D11 VideoProcessor:

- Constructs the processor using the **exact D3D11 device/context
  already owned by the NV12 frame pool**, not a second GPU device.
- Checks BGRA8 input and NV12 output capabilities.
- Accepts an opaque `DXGI_FORMAT_B8G8R8A8_UNORM` GPU texture,
  which a future D3D11 Canvas renderer can provide directly.
- Runs `VideoProcessorBlt` entirely on GPU, producing persistent
  `DXGI_FORMAT_NV12` GPU video texture without intermediate CPU bytes.
- Uses same-device `CopyResource` into an available persistent NV12
  frame slot, preserving PTS order and backpressure safeguards.
- Rejects mismatched input GPU devices, dimensions, formats and
  unsupported video processors. Failures never advance the queue's PTS
  or publish an incomplete frame as ready.

The new diagnostic creates simple *CPU-generated* opaque BGRA test frames
and uploads them to a same-device GPU input texture, runs GPU processing,
then downloads the final NV12 frame **only for test validation**. It checks
that white is substantially brighter than black and verifies grayscale
chroma is approximately neutral. These are elementary sanity checks,
**not** proof of precise FFmpeg BT.709 color parity.

```powershell
git pull
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_bgra_nv12_video_probe -- --width 1920 --height 1080 --frames 20 --output d3d11-bgra-nv12-local.json
```

An independent Windows CI build compiles the processor and its CLI, but
a hosted Windows runner typically has only Microsoft Basic Render Driver:
it reports `d3d11_hardware_unavailable`, not a performance measurement.
This RTX 5060 diagnostic is required to prove the NVIDIA driver supports
this conversion path and that output video planes have sane values.

### Remaining production blockers

1. **True native D3D11 Canvas rendering**: the existing wgpu Vulkan
   compositor produces storage buffers, not BGRA D3D11 textures. A
   D3D11-based scene renderer (or verified cross-API external-memory
   transfer and synchronization) is required to provide the above GPU
   input without CPU upload.
2. **In-process NVENC consumer**: native D3D11 video frames must be
   passed to an actual FFmpeg `AVHWFramesContext` or NVIDIA SDK encoder.
   The existing FFmpeg CLI rawvideo pipe cannot consume these COM
   textures directly. No zero-copy video encoder has been implemented.
3. **GPU fences and color fidelity**: queue 'ready' currently means the
   D3D11 command has been enqueued on an immediate context, not that
   NVENC has completed reading it. GPU synchronization and full project
   image/lyrics/audio tests are mandatory before default switching.

The existing Python, Qt and NVENC export pipeline remains unchanged.
