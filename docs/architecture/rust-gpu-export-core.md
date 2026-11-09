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


### RTX 5060 D3D11 BGRA input-view regression (2026-11-09)

The first live RTX 5060 run of `d3d11_bgra_nv12_video_probe`
successfully allocated the D3D11 device, the native video processor,
the BGRA input resource and the NV12 output, but failed while creating
`ID3D11VideoProcessorInputView` with `E_INVALIDARG (0x80070057)`.

**Root cause:** the initial BGRA input resource had
`BindFlags = D3D11_BIND_SHADER_RESOURCE` *only*. Per Microsoft's
`ID3D11VideoDevice::CreateVideoProcessorInputView` requirements,
nonzero bind flags must include an eligible video input usage such as
`D3D11_BIND_RENDER_TARGET`; a shader-resource-only texture is rejected.
The repair creates BGRA inputs with
`D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET`, and
adds a Rust regression test for this required bind combination.
See: https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11videodevice-createvideoprocessorinputview

When the NVIDIA driver rejects a video-processor enqueue, the probe
now writes `status: gpu_video_conversion_failed`, the failing frame,
GPU identity and the original Windows error to its output JSON
**before exiting unsuccessfully**, rather than panicking without a file.

Re-run on the real hardware:

```powershell
git pull
cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_bgra_nv12_video_probe -- --width 1920 --height 1080 --frames 20 --output d3d11-bgra-nv12-local.json
```

A Windows CI pass checks compilation and bind flags but does not
prove `CreateVideoProcessorInputView` succeeds on RTX hardware:
hosted Windows runs on the Microsoft Basic Render Driver. Actual
NVIDIA BGRA→NV12 conversion and color sanity remain the local gate.


## Milestone 8: RTX native BGRA/NV12 color-profile diagnosis

The real RTX 5060 run after PR #63 confirmed that the native D3D11
BGRA -> NV12 video processor executes on 20 consecutive 1920x1080
frames and keeps output in the same-device GPU frame pool. The median
0.0199ms is **CPU command enqueue time** only. The initial report's
grayscale probe gave black Y=0, white Y=255 and neutral chroma=128,
indicating a full-range-looking result; this is **not a guarantee**
that saturated colors, BT.709 matrix or limited-range video encoding
match FFmpeg. NVENC, Vulkan/D3D11 interop and production export are
still not connected.

This diagnostic adds a separate one-frame **12-patch opaque BGRA chart**
(black, white, primary/secondary colors, grays and mixed colors). It
converts that native D3D11 texture to NV12 through the existing GPU-only
same-device video processor. Only the diagnostic downloads NV12 for
pixel samples from the **interior** of each patch, avoiding 4:2:0
boundary-mixing in the comparison. Optionally, the exact original BGRA
bytes are also sent to FFmpeg's software swscale to compare four
matrix/range profiles: BT.601 and BT.709, full (pc) and limited (tv).
Each profile records per-channel Y/U/V deviations and identifies
the closest match, **without auto-enabling a profile**.

On the RTX workstation with FFmpeg installed:

    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_bgra_nv12_color_probe -- --width 1920 --height 1080 --ffmpeg ffmpeg --output d3d11-bgra-color-local.json

Read the closest_ffmpeg_profile, per-patch YUV values and
patch_centers_within_3_codes_of_closest_ffmpeg_profile.
A successful FFmpeg patch-center comparison **does not prove
full-frame FFmpeg parity**, correct color primaries/transfer
metadata, GPU completion or actual H.264 color fidelity. Those must
be checked before a native encoder's D3D11/NVENC integration can
replace the current export pipeline. The command without --ffmpeg
still validates native D3D11 RGB patch conversion on Windows CI.


## Milestone 9: D3D11 per-frame GPU copy-completion query

After the RTX 5060 color-profile diagnostic, 12 patch centers matched
FFmpeg BT.709 full (pc) range within 1 code value; the mean absolute
Y/U/V channel delta was approximately 0.167. BT.709 limited range
and both BT.601 profiles differed substantially. This establishes
a native GPU patch-center color matching gate, not full-frame color
parity or encoded H.264 stream metadata.

The D3D11 frame ring now allocates an ID3D11Query with
D3D11_QUERY_EVENT for each bounded NV12 slot. Its immediate
device context issues End(query) strictly after the conversion or
upload CopyResource for that slot. gpu_copy_complete(slot) polls
GetData with a correctly initialized Win32 BOOL, ensuring pending
S_FALSE never masquerades as completed; errors are propagated, and
the poll allows the runtime to flush queued GPU commands.
wait_for_gpu_copy(slot, timeout) provides a bounded poll for diagnostics
instead of an unbounded busy-loop. acquire_gpu_complete_ready() reserves
the lowest presentation timestamp only if its copy has completed.

Most importantly, complete_encode() now refuses recycling a slot
until that native D3D11 GPU copy query has completed, even when a caller
signals the simulated encoder too early. The fixed ring never overwrites
frames still queued or in flight, and it resets the issued-query marker
only after a safe free transition. Existing acquire_ready() remains
for backward compatibility but is intentionally only a queued-work
handoff: new hardware clients must use acquire_gpu_complete_ready().

On RTX 5060 run:

    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_gpu_completion_probe -- --width 1920 --height 1080 --frames 30 --slots 3 --output d3d11-gpu-completion-local.json

This runs changing full-frame gray BGRA GPU inputs, records GPU copy
completion per frame, checks FIFO PTS and ring backpressure, and
performs diagnostic-only NV12 readbacks after the GPU copy is
confirmed. It reports both enqueue and query wait CPU wall times:
neither is an actual GPU kernel timestamp.

### Important remaining safety boundary

A D3D11 EVENT query confirms that the texture producer copy has
finished. It does NOT confirm that NVENC, FFmpeg, or another consumer
has finished using the texture. The diagnostic calls complete_encode()
only as an explicit simulated consumer acknowledgement after reading
the frame. Real production integration must issue complete_encode()
only after a genuine in-process encoder completion event and must
retain texture ownership until then. NVIDIA Video Codec SDK or native
FFmpeg AVHWFramesContext integration, D3D11 scene rendering,
full-frame color validation, encoded media and audio correctness, and
a real benchmark are still required. No production export setting
or default pipeline has been changed.


## Milestone 10: same-process DirectX11 NV12 to NVENC resource registration

The RTX 5060 result from milestone 9 confirmed 30/30 D3D11_QUERY_EVENT
producer-copy completions, 30/30 validated NV12 slot contents, and
27 bounded-ring backpressure rejections. Enqueue and wait values
reflect CPU wall time and driver polling, not actual GPU encoding.

A new Windows-only experimental d3d11_nvenc_registration module
establishes the next interface boundary. It uses the runtime-loaded
NVENC Video Codec SDK C ABI (via mini-nvcodec 0.1.0)
without a mandatory SDK install or DLL import link at build time:

1. Obtain the same ID3D11Device already owned by NativeNv12FramePool.
2. Dynamically resolve nvEncodeAPI64.dll and negotiate an API version.
3. Open a DirectX NVENC encoder session on that device.
4. Verify H.264 and NV12 format support, query a driver preset and
   initialize the encoder session at the requested resolution and 60 fps.
5. Convert changing opaque BGRA textures to same-device GPU NV12 slots,
   wait for D3D11 EVENT copy completion, and acquire frames in PTS order.
6. Register each distinct D3D11 NV12 texture once per pool slot with
   nvEncRegisterResource, map and unmap it for each test frame,
   and unregister all resources before encoder-session teardown.
7. Retain NV12 COM texture references throughout registration. Do
   not perform NV12 GPU-to-CPU readback in this test.

Example RTX 5060 run:

    git switch main
    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nvenc_registration_probe -- --width 1920 --height 1080 --frames 12 --slots 3 --output d3d11-nvenc-register-local.json

Status measured means NVENC accepted GPU-resident D3D11 NV12 texture
registration and mapping on the same device. CI machines without
NVIDIA encode support return an explicit nvenc_library_unavailable or
d3d11_hardware_unavailable and cannot prove RTX compatibility.

**Remaining blocker:** This does NOT call NvEncEncodePicture,
create/lock a bitstream buffer, produce H.264, or signal an encoder
completion event. The test acknowledges an unencoded frame only
after map/unmap; that is safe for this registration probe but not
for production. Next is real H.264 bitstream output, decoded frame
count, PTS and color metadata, then the Qt scene renderer with no
intermediate CPU frames. Existing Python/FFmpeg exporter, audio
and default settings are unchanged.


## Milestone 11: H.264 Annex-B bitstream from native D3D11 GPU NV12

The user's RTX 5060 probe confirmed 12/12 successful NVENC input maps
through a real SDK 13.1 hardware session on 3 persistent NV12 GPU
textures, all allocated on the original D3D11 device. No NV12 download
was needed, but the previous test did not call NvEncEncodePicture.

The new d3d11_nvenc_h264_probe actually submits NV12 GPU textures to
the synchronous NVENC H.264 encoder, reads only the finished compressed
Annex-B bitstream packets, and writes a new .h264 elementary stream.
For this deliberately simple proof, the driver preset is configured
with frameIntervalP = 1 (no B frames) and lookahead disabled. This
permits one output-bitstream buffer and one input frame in flight at
the encoder level. Output NVENC lock polling has a deadline; if no
complete output becomes available, it fails rather than recycling
the D3D11 input frame. A successful bitstream lock/unlock is the
actual encoder consumer completion event for this synchronous path.

Every H.264 frame is checked for nonempty Annex-B data, strictly
increasing input and output PTS, and GPU slot reuse only after the
compressed output is collected. The generated .h264 output is never
silently overwritten. The encode output is NOT a finished Music Motion
Studio export: it contains no audio, no MP4 container and no Qt Canvas
or lyric rendering.

The RTX 5060's previous 12-patch native BGRA->NV12 color comparison
matched FFmpeg BT.709 *full-range*. The H.264 encoder's SPS VUI is now
explicitly configured for full range, BT.709 primaries, transfer and
matrix. Optional ffprobe verification requires exactly the requested
frame count, 1920x1080 resolution and these VUI fields.

Run on the verified RTX 5060 workstation:

    git switch main
    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nvenc_h264_probe -- --width 1920 --height 1080 --frames 30 --slots 3 --video-output d3d11-nvenc-h264-local.h264 --ffprobe ffprobe --output d3d11-nvenc-h264-local.json

Do not compare this synthetic synchronous pipeline's wall time against
a production playlist render. The synthetic BGRA texture still starts
on the CPU. The next promotions require output decode/color inspection,
GPU-native Canvas scene generation, ring-buffered asynchronous NVENC
delivery with real per-frame consumer completion, MP4/audio muxing and
same-project A/B speed/fidelity checks. The default Python exporter
remains unchanged.

Windows CI compiles and smoke tests the command, but a hosted Windows
runner generally cannot exercise NVIDIA hardware. Only the user's
RTX 5060 report plus a decoded H.264 file can confirm live encoding.


### Failure-forensics note

When ffprobe detects an H.264 decode, frame-count, dimension or VUI
metadata mismatch after a complete bitstream has been written, the
diagnostic now preserves the generated .h264 file and includes its
location under diagnostic_h264_file in the failed JSON report.
Partial file writes still remove incomplete output. Re-running a test
never silently overwrites an existing diagnostic video.


### RTX 5060 first-frame NVENC lock failure (2026-11-09)

Actual 1920x1080 RTX 5060 run opened a native NVENC API V13_1 session
but returned NvEncLockBitstream status 10 on the first submitted frame.
Per NVIDIA's NVENC status enum, code 10 is
NV_ENC_ERR_OUT_OF_MEMORY, NOT the transient
NV_ENC_ERR_LOCK_BUSY (13). The failure occurs after encoder session
initialization, resource mapping and picture submission, but the output
packet cannot be retrieved. This does not alone prove physical VRAM
exhaustion: driver-level resource allocation or an API configuration
difference could surface the same code.

This repair uses NVIDIA's documented synchronous-lock mode:
NV_ENC_INITIALIZE_PARAMS::enableEncodeAsync = 0 together with
NV_ENC_LOCK_BITSTREAM::doNotWait = 0. Polling mode remains available
explicitly with --lock-mode polling, to compare driver behavior only.
Fatal codes such as 10 are NOT retried as lock-busy.

Because the blocking lock can wait indefinitely in a faulty driver,
the diagnostic CLI now launches a dedicated GPU worker process and
the parent terminates it after a 180-second watchdog deadline.
Successful output still follows the original JSON/Annex-B checks.
On failure, NvEncGetLastErrorString is captured immediately and included
with the named code and stage in the JSON report. This provides useful
hardware evidence without guessing about the underlying allocation.

Run again after merging:

    git switch main
    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nvenc_h264_probe -- --width 1920 --height 1080 --frames 30 --slots 3 --lock-mode blocking --video-output d3d11-nvenc-h264-sync-local.h264 --ffprobe ffprobe --output d3d11-nvenc-h264-sync-local.json

If this still returns NV_ENC_ERR_OUT_OF_MEMORY, preserve the new JSON
containing the driver's detailed message. A 320x180 repeat with a
different filename can help distinguish a resolution-driven allocation
limit from a configuration/driver-level error. No production exporter
feature flag or default encoding behavior was changed.


## Milestone 12: verified H.264 -> 60 FPS MP4 container, optional AAC

On the RTX 5060, the blocking NVENC API V13_1 H.264 probe has now
successfully encoded **30/30 frames** at **1920x1080** with zero
uncompressed GPU NV12 readbacks. The submitted 30-frame Annex-B file
was independently decoded with FFmpeg: all frames were intact, center
YUV samples across five six-color cycles matched the native BT.709
full-range reference within one 8-bit channel code, and H.264 SPS
reported color_range=pc with bt709 matrix/transfer/primaries.
The synthetic test reported 1.81905 ms median synchronous encode/output
CPU wall time and 146.0615 ms total wall time. These are NOT a
full real-world Canvas export performance benchmark.

A raw H.264 elementary stream is not a complete video container. FFprobe
may infer different average and nominal frame rates when timestamps
are absent. The same uploaded 30-frame stream remuxed as MP4 with
a 60 FPS input rate successfully reported 30 frames, 60/1 FPS and
0.500000 seconds, retaining the BT.709 full-range VUI.

The separate CPU-side tool tools/nvenc_probe_mux.py implements this
conversion without changing the default application exporter. The
diagnostic tool:

- Checks the input H.264 frame count, resolution, and BT.709 full-range
  metadata before muxing.
- Uses input-side -r 60 for constant-frame-rate timestamps, copy-only
  H.264 video, and a fast-start MP4 container; there is **no video
  re-encoding** and no GPU frame download.
- Optionally takes an audio track and encodes it as AAC, padding/trimming
  the soundtrack to the video's exact frame-count duration.
- Validates output decoded frame count, resolution, exact rational FPS,
  monotonic CFR presentation timestamps, duration, and color metadata.
- Optionally checks the synthetic six-color diagnostic center YUV
  samples (tolerance <=4 codes). This is **not full-frame color parity**.
- Uses a same-directory temporary MP4, then an atomic exclusive file
  creation operation. Existing destination files are never overwritten;
  incomplete/invalid MP4 output is not exposed as a finished result.
- Produces SHA-256 fingerprints and an inspectable validation JSON;
  leaves normal project playback, audio processing and export untouched.

Example after native H.264 generation:

    python -m tools.nvenc_probe_mux --input d3d11-nvenc-h264-sync-local.h264 --output nvenc-sample-60fps.mp4 --fps 60 --expected-frames 30 --validate-color-cycle --report nvenc-mp4-validation.json

Optional soundtrack:

    python -m tools.nvenc_probe_mux --input d3d11-nvenc-h264-sync-local.h264 --output nvenc-with-audio.mp4 --fps 60 --expected-frames 30 --audio path/to/soundtrack.mp3 --report nvenc-mp4-audio.json

An independent Windows CI test creates a synthetic H.264 stream via
software FFmpeg (no NVIDIA GPU), verifies MP4 video stream-copy, exact
CFR, metadata, non-overwrite behavior and optional AAC audio. The
previous real RTX H.264 file remains the hardware validation evidence.

Remaining production blockers: real Qt Canvas/lyrics GPU-native scene
rendering, frame-consumer ownership and async scheduling, PCM/audio
PTS accuracy on full-length projects, arbitrary color range/matrix
handling, MP4 A/V drift, cancellation/recovery, and sustained
hardware-backed project A/B performance and visual-fidelity testing.


## Milestone 13: same-device D3D11 Canvas layer composition

The previously user-verified RTX 5060 pipeline proved that native
GPU NV12 inputs can be encoded into a decodable 30-frame H.264 stream.
The first step toward real Canvas/lyrics compositing adds a *GPU
layer mixer* on the exact D3D11 device and immediate context already
used for NVENC:

- Accept 1..=8 full-resolution BGRA textures in Canvas bottom-to-top
  Z-order. Layer zero must be an opaque full-frame background.
- For each additional layer, use **straight per-pixel BGRA alpha** with
  optional global opacity in [0,1]. Qt's ARGB32_Premultiplied must first
  be converted to straight Format_ARGB32, or text edges would be blended
  twice by the GPU. This is deliberately separate from Qt's QPainter.
- Query native VideoProcessorCaps for MaxInputStreams, MaxStreamStates
  and FEATURE_CAPS_ALPHA_STREAM. Refuse unsupported hardware instead of
  silently flattening transparent text on the CPU or reporting a false
  success. D3D11 video processing applies each stream's pixel and planar
  alpha directly on the GPU.
- Call one multi-stream VideoProcessorBlt into a reusable *native
  NV12 GPU texture*, then CopyResource on the same immediate context
  into a bounded GPU NV12 ring. A D3D11 EVENT query proves that copy
  finished before the frame is handed to the working in-process NVENC
  encoder. Reuse occurs only after completed H.264 output.
- Make a separate synthetic diagnostic that uploads a dark background
  and a red, straight-alpha lyric-*like* overlay. Its center opacity
  cycles 0, 128, 255; a diagnostic-only NV12 readback can check that
  the center luma increases strictly while actual NVENC consumes GPU
  NV12 textures without any CPU NV12 readback.
- CI tests validation, compiles the Windows API implementation,
  executes it only when hardware supports the required capabilities,
  and reports unsupported drivers explicitly.

On RTX 5060, run the *next* optional hardware probe:

    git switch main
    git pull
    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_canvas_layers_probe -- --width 1920 --height 1080 --frames 6 --slots 2 --validate-alpha --video-output d3d11-canvas-layer-local.h264 --output d3d11-canvas-layer-local.json

**Limits of this step:** The alpha mask above is synthetic. The Qt
Canvas's scene graph, font glyph rendering, dynamically positioned
lyrics and source animations are still rasterized in the existing
QPainter path; they have not yet been moved onto the native D3D11
device. The synthetic BGRA layer inputs are initially CPU-uploaded
for reproducibility; only GPU blending / NV12 conversion / NVENC
compression are resident. A real Canvas capture-to-layer bridge, plus
full-frame visual and timing parity against QPainter, are required
before any production renderer switch. The default export remains
unchanged.
