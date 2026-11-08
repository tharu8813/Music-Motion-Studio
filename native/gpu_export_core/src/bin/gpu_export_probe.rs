//! Standalone GPU compositor experiment, without altering production export.
//!
//! cargo run --manifest-path native/gpu_export_core/Cargo.toml --release
//!     --bin gpu_export_probe -- --width 1920 --height 1080 --frames 30
//!     --output gpu-compositor-probe.json

use music_motion_gpu_core::{composite_reference, GpuCompositor, Overlay};
use serde_json::json;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Instant;

struct Options {
    width: u32,
    height: u32,
    frames: usize,
    output: Option<PathBuf>,
}

fn parse_args() -> Result<Options, String> {
    let mut result = Options { width: 1920, height: 1080, frames: 20, output: None };
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().ok_or_else(|| format!("Missing value after {flag}"))?;
        match flag.as_str() {
            "--width" => result.width = value.parse().map_err(|_| "Invalid width")?,
            "--height" => result.height = value.parse().map_err(|_| "Invalid height")?,
            "--frames" => result.frames = value.parse().map_err(|_| "Invalid frame count")?,
            "--output" => result.output = Some(PathBuf::from(value)),
            _ => return Err(format!("Unknown argument: {flag}")),
        }
    }
    if result.width < 32 || result.width > 3840 || result.height < 32
        || result.height > 2160 || result.frames < 1 || result.frames > 240
    {
        return Err("Unsupported benchmark dimensions or frame count".to_owned());
    }
    Ok(result)
}

fn median(values: &[f64]) -> f64 {
    let mut ordered = values.to_vec();
    ordered.sort_by(|a, b| a.total_cmp(b));
    if ordered.len() % 2 == 0 {
        (ordered[ordered.len() / 2 - 1] + ordered[ordered.len() / 2]) / 2.0
    } else {
        ordered[ordered.len() / 2]
    }
}

fn build_pixels(width: u32, height: u32, alpha: bool, bias: u32) -> Vec<u8> {
    let mut pixels = vec![0u8; width as usize * height as usize * 4];
    for (idx, px) in pixels.chunks_exact_mut(4).enumerate() {
        let x = idx as u32 % width;
        let y = idx as u32 / width;
        px[0] = ((17 * x + 7 * y + bias) & 255) as u8;
        px[1] = ((3 * x + 19 * y + bias) & 255) as u8;
        px[2] = ((13 * x + 11 * y + bias) & 255) as u8;
        px[3] = if alpha { ((x + y + bias) & 255) as u8 } else { 255 };
    }
    pixels
}

fn main() {
    let options = match parse_args() {
        Ok(x) => x,
        Err(x) => {
            eprintln!("{x}");
            std::process::exit(2);
        }
    };
    let base = build_pixels(options.width, options.height, false, 19);
    let overlay1 = build_pixels(options.width / 2, options.height / 2, true, 37);
    let overlay2 = build_pixels(options.width / 3, options.height / 3, true, 193);
    let layers = [
        Overlay { pixels: &overlay1, width: options.width / 2,
                  height: options.height / 2, x: 77, y: 41 },
        Overlay { pixels: &overlay2, width: options.width / 3,
                  height: options.height / 3, x: -19,
                  y: (options.height / 3) as i32 },
    ];

    let cpu_start = Instant::now();
    let reference = match composite_reference(&base, options.width, options.height, &layers) {
        Ok(x) => x,
        Err(e) => {
            eprintln!("Invalid GPU experiment inputs: {e}");
            std::process::exit(2);
        }
    };
    let cpu_first = cpu_start.elapsed().as_secs_f64();

    let report = match GpuCompositor::new() {
        Err(error) => json!({
            "schema": 1,
            "status": "gpu_unavailable",
            "reason": error,
            "resolution": [options.width, options.height],
            "frames": options.frames,
            "cpu_first_seconds": cpu_first,
            "hardware_gpu_measured": false,
            "production_export_changed": false,
        }),
        Ok(gpu) => {
            let warmup = gpu.composite(&base, options.width, options.height, &layers);
            match warmup {
                Err(error) => {
                    eprintln!("Hardware GPU compute failed: {error}");
                    std::process::exit(1);
                }
                Ok(result) if result != reference => {
                    let differences = result.iter().zip(reference.iter())
                        .filter(|(a, b)| a != b).count();
                    eprintln!("GPU alpha compositor pixel parity failed: {differences} bytes differ");
                    std::process::exit(1);
                }
                _ => {}
            }

            let mut cpu_samples = Vec::with_capacity(options.frames);
            let mut gpu_samples = Vec::with_capacity(options.frames);
            for frame in 0..options.frames {
                let modes = if frame % 2 == 0 { [false, true] } else { [true, false] };
                for accelerated in modes {
                    let start = Instant::now();
                    if accelerated {
                        let gpu_frame = gpu.composite(
                            &base, options.width, options.height, &layers,
                        ).expect("GPU frame allocation, compute and readback");
                        if gpu_frame != reference {
                            eprintln!("GPU parity diverged after warming up");
                            std::process::exit(1);
                        }
                        gpu_samples.push(start.elapsed().as_secs_f64());
                    } else {
                        let cpu_frame = composite_reference(
                            &base, options.width, options.height, &layers,
                        ).expect("CPU frame reference");
                        if cpu_frame != reference {
                            eprintln!("CPU reference changed unexpectedly");
                            std::process::exit(1);
                        }
                        cpu_samples.push(start.elapsed().as_secs_f64());
                    }
                }
            }
            let cpu_median = median(&cpu_samples);
            let gpu_median = median(&gpu_samples);

            // A persistent session uploads the static base/layers once,
            // reuses bind groups, scratch buffers and staging storage.
            // Compare the GPU-resident path (NO readback) to a diagnostic
            // path with a final GPU->CPU readback.
            let session_start = Instant::now();
            let prepared = gpu.prepare_session(
                &base, options.width, options.height, &layers,
            ).expect("Prepare reusable GPU compositor session");
            let session_prepare_seconds = session_start.elapsed().as_secs_f64();
            let first = prepared.compose_and_readback()
                .expect("Persistent GPU first frame");
            if first != reference {
                eprintln!("GPU session first frame differs from Rust CPU reference");
                std::process::exit(1);
            }
            let mut resident_samples = Vec::with_capacity(options.frames);
            let mut readback_samples = Vec::with_capacity(options.frames);
            for index in 0..options.frames {
                // Alternate order across frames to reduce clock/cache bias.
                let order = if index % 2 == 0 {
                    [false, true]
                } else {
                    [true, false]
                };
                for download in order {
                    let start = Instant::now();
                    if download {
                        let result = prepared.compose_and_readback()
                            .expect("Persistent GPU readback failed");
                        if result != reference {
                            eprintln!("Persistent GPU readback pixels diverged");
                            std::process::exit(1);
                        }
                        readback_samples.push(start.elapsed().as_secs_f64());
                    } else {
                        prepared.compose_gpu_only()
                            .expect("Persistent GPU-resident composition failed");
                        resident_samples.push(start.elapsed().as_secs_f64());
                    }
                }
            }
            let resident_median = median(&resident_samples);
            let readback_median = median(&readback_samples);
            json!({
                "schema": 1,
                "status": "measured",
                "scope": "Rust opaque-base RGBA overlays, GPU upload + compute + one readback",
                "resolution": [options.width, options.height],
                "frames": options.frames,
                "layers": layers.len(),
                "adapter": gpu.adapter_name,
                "backend": gpu.adapter_backend,
                "cpu_first_seconds": cpu_first,
                "cpu_median_seconds": cpu_median,
                "gpu_median_seconds": gpu_median,
                "gpu_vs_cpu_speedup": cpu_median / gpu_median,
                "prepared_session_upload_seconds": session_prepare_seconds,
                "persistent_gpu_only_median_seconds": resident_median,
                "persistent_readback_median_seconds": readback_median,
                "persistent_vs_legacy_speedup_with_readback": gpu_median / readback_median,
                "persistent_gpu_only_vs_cpu_reference_speedup": cpu_median / resident_median,
                "persistent_session_frame_reuse": true,
                "persistent_gpu_only_includes": [
                    "GPU command encoding",
                    "GPU queue submit and completion wait"
                ],
                "persistent_gpu_only_excludes": [
                    "per-frame changed-input uploads",
                    "GPU-to-CPU readback",
                    "FFmpeg/NVENC encoding"
                ],
                "pixel_byte_identical": true,
                "hardware_gpu_measured": true,
                "production_export_changed": false,
                "limitations": [
                    "GPU->CPU readback is still required",
                    "No GPU->NVENC native hardware-frame sharing yet",
                    "Only straight-alpha rectangular overlays over an opaque base",
                    "Does not represent complete Qt Canvas or MP4 export performance",
                    "Persistent benchmark uses unchanged pre-uploaded test layers",
                    "Native NVENC hardware-frame interop is not yet implemented"
                ],
            })
        }
    };
    let json_text = serde_json::to_string_pretty(&report).expect("Serialize GPU benchmark");
    println!("{json_text}");
    if let Some(path) = options.output {
        if let Some(parent) = path.parent() {
            if let Err(e) = fs::create_dir_all(parent) {
                eprintln!("Cannot create output folder: {e}");
                std::process::exit(1);
            }
        }
        if let Err(error) = fs::write(path, json_text + "\n") {
            eprintln!("Unable to save GPU diagnostics: {error}");
            std::process::exit(1);
        }
    }
}
