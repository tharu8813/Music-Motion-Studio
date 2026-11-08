# Optional Rust acceleration for export visualizer analysis

This is an **opt-in experiment**. The app still uses its existing Qt Canvas,
FFmpeg encoding, audio processing, project format, and export controls.

The optional `music_motion_native` PyO3 extension accelerates:
- FFT magnitude spectrum reduction into logarithmic visualizer bands (NumPy still does FFT).
- Per-frame mono/stereo RMS envelope analysis used by the level meter.
- Signed PCM waveform sampling and interpolation used by waveform overlays.

The existing NumPy algorithms remain the default and the exact behavioral
reference. These kernels can help long mixes containing audio-reactive layers,
but **do not accelerate Qt scene capture, FFmpeg compression or general export
without those overlays**. The overall export speedup must be measured separately.

## Windows build in the Python 3.14 environment

Install Rust MSVC and the stable Rust toolchain, then in the repository:

```powershell
.\.venv\Scripts\python.exe -m pip install "maturin>=1.9.4,<2"
.\.venv\Scripts\maturin.exe build --release --manifest-path native/export_analysis/Cargo.toml --interpreter .\.venv\Scripts\python.exe
$wheel = Get-ChildItem native/export_analysis/target/wheels/*.whl | Sort-Object LastWriteTime -Descending | Select-Object -First 1
.\.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall --no-deps $wheel.FullName
$env:PLAYLIST_CANVAS_ENABLE_RUST_EXPORT_ANALYSIS = "1"
.\.venv\Scripts\python.exe -m unittest -v tests.test_rust_export_analysis tests.test_rust_export_pcm
.\.venv\Scripts\python.exe tools/benchmark_rust_export_analysis.py
.\.venv\Scripts\python.exe tools/benchmark_rust_export_pcm.py
```

Compare with Python-only behavior by setting
`PLAYLIST_CANVAS_ENABLE_RUST_EXPORT_ANALYSIS=0`, or removing the variable.

The normal `requirements.txt` and `requirements-lock.txt` intentionally
do not require Rust or maturin. Missing or failing native acceleration safely
falls back to NumPy, and the application works without the native wheel.
The build specification includes `music_motion_native` **only if installed**
on the packaging host.

## Release safety and verification

The Windows deployment workflow builds the Rust extension, runs A/B parity
tests, and includes it in the packaged build. Runtime use remains **off by
default**, even if the wheel is present. Do not enable this by default until
equivalent 1080p and 4K export projects have been tested on Windows.

For a real performance decision, measure identical source projects with the
existing profiling metrics, compare total render wall time, and verify the
final video output (duration, frame count, alpha, lyrics, audio, and cancellations).
Do not infer an end-to-end speedup from a microbenchmark alone.

The switch is for testing only. It doesn't alter the FFmpeg encoder, the
Canvas pipe protocol, or the fragile low-FPS overlay EOF scheduling.

## Verified baseline (2026-11-09)

The first Windows GitHub Actions run compiled the Rust wheel, passed the FFT
parity tests, and measured 44.9 ms NumPy versus 30.1 ms Rust on a 20-second,
60 FPS, 96-band synthetic sample (1.49x faster for that analysis operation).
The numeric difference reported by that benchmark was zero. This is **not a
whole-export speedup**; other legacy suite failures were present in the same
workflow. The second-stage RMS/waveform kernels passed the focused Windows workflow:
on 20 seconds of synthetic audio at 60 FPS, RMS took 5.27 ms in NumPy
versus 0.13 ms in Rust (40.73x); 96-point waveform took 9.51 ms versus
0.67 ms (14.26x). Max absolute differences were below 3e-8. These
are short, synthetic **kernel** benchmarks, not a measurement of total
video export. Windows FFmpeg integration testing is tracked separately.

Native module v0.2.0 contains the new PCM APIs. When upgrading an installed
0.1.x native wheel, rebuild and reinstall it using the commands above. Older
wheels fall back to the NumPy implementation instead of changing export output.
