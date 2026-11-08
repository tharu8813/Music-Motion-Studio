# Optional Rust acceleration for export visualizer analysis

This is an **opt-in experiment**. The app still uses its existing Qt Canvas,
FFmpeg encoding, audio processing, project format, and export controls.

Only the reduction of FFT magnitude spectra into logarithmic visualizer bands
moves to the `music_motion_native` PyO3 extension. NumPy still performs
the FFT. This can accelerate analysis for many bars and long mixes, but the
overall export speedup depends on the bottleneck. It does not accelerate
Qt scene capture or FFmpeg compression.

## Windows build in the Python 3.14 environment

Install Rust MSVC and the stable Rust toolchain, then in the repository:

```powershell
.\.venv\Scripts\python.exe -m pip install "maturin>=1.9.4,<2"
.\.venv\Scripts\maturin.exe build --release --manifest-path native/export_analysis/Cargo.toml --interpreter .\.venv\Scripts\python.exe
$wheel = Get-ChildItem native/export_analysis/target/wheels/*.whl | Select-Object -First 1
.\.venv\Scripts\python.exe -m pip install $wheel.FullName
$env:PLAYLIST_CANVAS_ENABLE_RUST_EXPORT_ANALYSIS = "1"
.\.venv\Scripts\python.exe -m unittest -v tests.test_rust_export_analysis
.\.venv\Scripts\python.exe tools/benchmark_rust_export_analysis.py
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
