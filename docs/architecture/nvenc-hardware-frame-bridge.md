# GPU-native NVENC export: D3D11 hardware-frame compatibility gate

## Evidence from the RTX 5060 (November 2026)

The user's 1920×1080, 30-frame Rust/wgpu Vulkan experiments returned:

| Measured workload | Median wall time |
| --- | ---: |
| Rust CPU RGBA composition plus NV12 conversion | 6.38865ms |
| GPU dynamic upload, RGBA layers, NV12, GPU-resident output | 2.34800ms |
| GPU dynamic upload, layers, NV12, final CPU readback | 3.15155ms |
| Full 960×540 overlay upload + GPU NV12 | 0.59060ms |
| Partial 128×64 overlay update + GPU NV12 | 0.39125ms |

The changing-frame GPU path was ~2.72× faster than the comparable Rust CPU
reference for that **synthetic** workload. The partial-update path was ~1.51×
faster for the tested rectangle, but narrower patches require one queue write
per row and may not always win. These are not full 223-second export times.

The user's optional NVENC probe reported a successful synthetic MP4 encode
using GPU NV12 **after downloading the bytes to system memory**. The
standalone JSON does not establish codec stream metadata or confirm that an
existing GPU buffer was shared with NVENC.

## Real missing boundary: wgpu storage memory vs encoder frame resources

The Rust compositor currently creates *wgpu storage buffers* in a Vulkan
backend. NVIDIA NVENC and FFmpeg can consume hardware video surfaces, but
**a wgpu Vulkan storage buffer is not automatically a D3D11 NV12 texture,
a CUDA array or an AVHWFramesContext**.

For a truly GPU-native export on Windows the renderer and encoder must agree
on adapter identity, pixel format, texture layout, resource ownership,
synchronization/fences and lifetime. Passing raw NV12 bytes through standard
FFmpeg stdin always begins with CPU memory and is **not zero-copy**, even if
`h264_nvenc` eventually encodes on the GPU.

FFmpeg documentation confirms that compatible NVIDIA NVENC builds accept
D3D11 hardware frames (`AV_PIX_FMT_D3D11`). The Windows D3D11 hardware
frame context supports `DXGI_FORMAT_NV12`. Relevant upstream references:

- https://www.ffmpeg.org/ffmpeg-codecs.html#NVENC-encoders
- https://www.ffmpeg.org/ffmpeg.html
- https://github.com/FFmpeg/FFmpeg/blob/master/libavutil/hwcontext_d3d11va.c

## New no-project-change compatibility gate

`tools/benchmark_nvenc_d3d11_hwframes.py` runs two isolated FFmpeg chains
with the same synthetic FFmpeg-generated frames and `h264_nvenc` encoder.

- Reference: CPU `testsrc2 -> format=nv12 -> h264_nvenc`.
- Hardware frame candidate: CPU `testsrc2 -> format=nv12,hwupload`
  on `d3d11va=mms:0`, then D3D11 hardware frames -> `h264_nvenc`.
- The D3D11 chain does not include a `hwdownload` filter between GPU upload
  and encoder. This tests *hardware-frame consumption by NVENC*, not direct
  Rust/wgpu GPU-to-GPU interop.
- Every MP4 is generated under a temporary directory and inspected with
  `ffprobe -count_frames`. A successful result requires H.264, matching
  frame count, resolution and 60 FPS (or the requested FPS).
- The script does not change any project, export defaults, video artifacts
  or the existing PyO3 audio wheel.
- If hardware encoding is unavailable, returns a distinct capability status,
  including the FFmpeg error; the user should not mistake it for a speed result.

### RTX 5060 instructions

```powershell
git pull
.\.venv\Scripts\python.exe tools/benchmark_nvenc_d3d11_hwframes.py --ffmpeg ffmpeg --ffprobe ffprobe --width 1920 --height 1080 --fps 60 --frames 120 --repeats 3 --output nvenc-d3d11-hwframes-local.json
```

A `"status": "measured"` result establishes that this FFmpeg+RTX
configuration can pass D3D11 hardware frames to NVENC and decode the encoded
videos. It does **not** establish that Rust can hand its Vulkan storage
buffers to that D3D11 hardware-frame pool. If the hardware input fails,
record `d3d11_hardware_frames_unavailable` and do not enable a D3D11
encoder backend.

### Next engineering gates if the RTX reports success

1. Choose an interoperability model. The most direct likely path is a
   persistent native D3D11 device/texture and a same-process encoder using
   FFmpeg libavcodec `AV_PIX_FMT_D3D11` or the NVIDIA Video Codec SDK.
   A generic FFmpeg CLI stdin pipe is insufficient for sharing GPU texture
   handles across processes.
2. Explicitly allocate an NV12-capable `DXGI_FORMAT_NV12` resource on the
   correct adapter, matching the device passed to the encoder. The existing
   Vulkan wgpu storage buffer needs an interop transfer or a D3D11-specific
   rendering/conversion backend. Merely copying the Rust buffer pointer is
   not valid.
3. Add robust synchronization, simultaneous encoding/rendering lifetime
   tracking, bounded GPU frame queues, cancellation and device-loss recovery.
4. Test a short silent MP4 with proper PTS, CFR frame count and pixel/color
   comparison before passing real music project audio or lyric frames.
5. Keep the old CPU path as fallback; promote based on identical project
   output and measured whole-export wall time, never on overall GPU usage.

## CI limitation

GitHub-hosted Windows runners may lack an NVIDIA GPU and/or FFmpeg on PATH.
The unit tests still validate the exact command construction and decoded
frame contract. A capability report indicating no NVENC is **not evidence**
of an RTX 5060 implementation failure. The real-user Windows test is the
hardware gate.
