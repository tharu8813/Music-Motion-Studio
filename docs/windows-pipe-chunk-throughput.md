# Windows named-pipe raw-video throughput benchmark

Windows FFmpeg receives 32-bit raw Canvas frames through a bounded single-instance
named pipe. The writer uses overlapped WinAPI writes and is backpressured by
the FFmpeg consumer; a faster producer or fewer WinAPI calls is not necessarily
a faster total export.

A previous user 1080p60 export profile showed approximately 111 GB of *base*
rawvideo and 26,790 WinAPI writes. The default writer chunks bytes into 4MiB
pieces, with a 1MiB named-pipe kernel buffer.

## Reproducible Windows experiment

The test exercises the actual `PipedCanvasStream` and FFmpeg H.264
reader. The same QImage RGB32 frames are supplied to each 2/4/8MiB mode.
Each mode must produce the same number of frames and byte-identical
FFmpeg `framemd5` output before performance is compared.

```powershell
.\.venv\Scripts\python.exe tools/benchmark_windows_pipe_chunks.py --ffmpeg ffmpeg --width 1920 --height 1080 --fps 60 --frames 36 --repeats 3 --chunks 2,4,8
```

A standalone experiment can specify
`PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB=2`, `4`, or `8` before launching
Python; existing releases still default to 4.

## CI evidence

[Windows 1080p and 4K benchmark](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/42521437538)

| Windows FFmpeg producer+encoder workload | 2MiB | 4MiB default | 8MiB |
| --- | ---: | ---: | ---: |
| 1080p60, 36 frames (median of 3) | 0.74684s | 0.74688s | 0.76092s |
| 4K30, 12 frames (median of 2) | n/a | 1.03738s | 1.03895s |

- Byte-for-byte frame parity passed in both configurations.
- Existing named-pipe regression tests passed after correcting a test fixture
  that omitted the required `target_frame_count` field.
- At 1080p, write API calls dropped from 72 calls at 4MiB to 36 calls at
  8MiB for 36 frames, **without** a corresponding speedup.

**Decision:** Preserve the 4MiB production default. The 8MiB setting
was not faster in these synthetic tests; 2MiB and 4MiB were effectively tied.
No user-visible encoder behavior, Rust wheel version or export format changed.

Timing caveat: this test includes a real FFmpeg H.264 encoding stage but
uses short, simple synthetic images, and does not capture full Canvas scene
painting, audio mixing, the user's particular GPU encoder, or whole-project
end-to-end timing. Repeat the comparison on a representative real project
before claiming universal performance results.
