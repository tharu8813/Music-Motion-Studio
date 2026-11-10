# Qt Canvas / lyric Z-band -> D3D11 -> NVENC bridge (experimental)

## Current tested baseline

- RTX 5060: standalone same-device D3D11 BGRA -> NV12 -> NVENC H.264
  successful 30/30 at 1920x1080, BT.709 full range.
- Native D3D11 compositor: ordered full-frame BGRA layer streams,
  straight-alpha blending and one GPU NV12 output, gated on native
  MaxInputStreams/MaxStreamStates/ALPHA_STREAM device capabilities.
- Qt source: CanvasSnapshot.capture(scene, z_min, z_max, transparent) is
  already the renderer of SourceItems including text, prepared lyrics,
  and animations at the selected export timeline position.
- The source layer adapter emits full-frame BGRA8_STRAIGHT files with
  SHA-256 hashes, per-layer opacity, Z-order and top-left origin.

This milestone connects **those Qt layers** to the already implemented
D3D11 compositor and NVENC encoder. It is a diagnostic migration, not
the default exporter. The Qt/QPainter glyph rasterizer still uses CPU;
the source BGRA bytes are uploaded to D3D11. GPU layer blending,
NV12 conversion and encode need no additional uncompressed readback.

## Generate actual Qt-rendered layer input

Create a two-layer Qt Canvas fixture with real SourceItem background,
text and overlay geometry:

    python -m tools.native_canvas_fixture_capture --output-dir native-canvas-fixture --scale 0.5

The command writes native-canvas-fixture/qt-canvas-layers.json and two
.raw BGRA image planes, 640x360 pixels at 4 bytes/pixel each.

The same packet interface can be used by a future real-project
ExportCanvasCapturer to capture selected prepared timeline frames;
**this helper does not independently advance the music timeline**.
The fixture text is representative of Qt glyph rendering but is not
a real user's track-synchronized lyrics project.

## Validate the full Qt packet without an NVIDIA GPU

The hardware-independent Rust CLI reads every raw layer byte and verifies
the manifest schema, hashes, opacity, dimensions and source order:

    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin canvas_layer_manifest_probe -- --manifest native-canvas-fixture/qt-canvas-layers.json --output native-qt-packet-validated.json

It returns status=verified only after inspecting the complete real Qt
BGRA files. The Windows CI executes this check even when its GPU has
no compatible NVENC hardware. Oversized or checksum-mismatched packets
are rejected before GPU allocation.

## Compose and H.264-encode the Qt fixture on RTX

    cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_canvas_layers_probe -- --width 640 --height 360 --frames 6 --slots 2 --layer-manifest native-canvas-fixture/qt-canvas-layers.json --video-output native-qt-canvas-local.h264 --output native-qt-canvas-local.json

The native parser refuses wrong schema, premultiplied pixels, missing
or oversized files, out-of-order Z keys, invalid base opacity, directory
escape or SHA-256 checksum mismatch. Only one process owns the GPU
texture ring and NVENC session. The same six-frame static Qt packet is
encoded repeatedly to validate GPU layer blending and consumer
lifetime; this is **not** an animated playlist demonstration.

Remux the H.264 to a valid CFR MP4 with the existing verified tool:

    python -m tools.nvenc_probe_mux --input native-qt-canvas-local.h264 --output native-qt-canvas-local.mp4 --fps 60 --expected-frames 6 --report native-qt-canvas-mp4.json

Do not use --validate-color-cycle for this fixture: that comparison
is only for the earlier six-color synthetic BGRA diagnostic.

## Verification and promotion gates

CPU-only Rust unit tests verify manifest parsing, actual file hashes,
corruption rejection, limits, and path traversal. A Windows CI smoke
test generates a byte-identical manifest using Python's standard
library and invokes the native D3D11/NVENC probe. It reports missing
hardware explicitly without claiming an RTX pass. Separate Qt Windows
CI already validates real SourceItem rasterization, straight-alpha
conversion and manifest serialization.

Before enabling a user-facing native export mode, still required:

1. RTX 5060 alpha/geometry inspection of actual decoded Qt text
   (including antialiasing, overlays, complex images and blur), against
   the reference QPainter/FFmpeg rendered project frames.
2. Source and lyric timeline state handoff for each CFR frame, retaining
   all existing keyframe/word overlap rules.
3. A persistent native GPU scene paint engine so per-frame QImage CPU
   rasterization and BGRA upload can be eliminated.
4. Video/effect interleaving, bounded asynchronous NVENC completion,
   cancellation/device-loss recovery, MP4 audio timestamp drift
   testing, and multi-minute project A/B performance benchmarks.

The default Python + FFmpeg export path remains unmodified.
