//! Dynamic per-frame RGBA updates, GPU-resident NV12 conversion and parity.
//! This is an independent experiment, NOT the running FFmpeg/NVENC pipeline.
//!
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin gpu_export_dynamic_probe -- --width 1920 --height 1080 --frames 30 --output rust-gpu-dynamic-nv12.json

use music_motion_gpu_core::{
    composite_reference, rgba_to_nv12_reference, GpuCompositor, Overlay,
};
use serde_json::json;
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Instant;

struct Args {
    width: u32,
    height: u32,
    frames: usize,
    output: Option<PathBuf>,
    nvenc_output: Option<PathBuf>,
    ffmpeg: String,
}

fn arguments() -> Result<Args, String> {
    let mut result = Args {
        width: 1920, height: 1080, frames: 30, output: None,
        nvenc_output: None, ffmpeg: "ffmpeg".to_owned(),
    };
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().ok_or_else(|| format!("Missing value for {flag}"))?;
        match flag.as_str() {
            "--width" => result.width = value.parse().map_err(|_| "Invalid width")?,
            "--height" => result.height = value.parse().map_err(|_| "Invalid height")?,
            "--frames" => result.frames = value.parse().map_err(|_| "Invalid frames")?,
            "--output" => result.output = Some(PathBuf::from(value)),
            "--nvenc-output" => result.nvenc_output = Some(PathBuf::from(value)),
            "--ffmpeg" => result.ffmpeg = value,
            _ => return Err(format!("Unknown option {flag}")),
        }
    }
    if !(32..=3840).contains(&result.width) || result.width % 4 != 0
        || !(32..=2160).contains(&result.height) || result.height % 2 != 0
        || !(2..=120).contains(&result.frames)
    {
        return Err("Frame dimensions must be <=3840x2160; width multiple of 4, even height, 2..120 frames".into());
    }
    Ok(result)
}

fn median(data: &[f64]) -> f64 {
    let mut sorted = data.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let midpoint = sorted.len() / 2;
    if sorted.len() % 2 == 0 { (sorted[midpoint-1] + sorted[midpoint]) / 2.0 }
    else { sorted[midpoint] }
}

fn rgba_pixels(width: u32, height: u32, frame: usize, alpha: u8, bias: u8) -> Vec<u8> {
    let mut data = vec![0u8; width as usize * height as usize * 4];
    // Real data changes each frame but generation is *outside* timing.
    for (idx, pixel) in data.chunks_exact_mut(4).enumerate() {
        let x = idx as u32 % width;
        let y = idx as u32 / width;
        let frame = frame as u32;
        pixel[0] = (13u32.wrapping_mul(x) + 5u32.wrapping_mul(y)
            + 11u32.wrapping_mul(frame) + u32::from(bias)) as u8;
        pixel[1] = (3u32.wrapping_mul(x) + 17u32.wrapping_mul(y)
            + 7u32.wrapping_mul(frame) + u32::from(bias)) as u8;
        pixel[2] = (19u32.wrapping_mul(x) + 29u32.wrapping_mul(y)
            + 23u32.wrapping_mul(frame) + u32::from(bias)) as u8;
        pixel[3] = alpha;
    }
    data
}

fn nvenc_command(ffmpeg: &str, width: u32, height: u32, output: &Path) -> Command {
    let mut command = Command::new(ffmpeg);
    command.args([
        "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-f", "rawvideo", "-pixel_format", "nv12",
        "-video_size", &format!("{width}x{height}"),
        "-framerate", "60", "-i", "pipe:0",
        "-an", "-c:v", "h264_nvenc", "-preset", "p4",
        "-cq", "18", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709",
    ]);
    command.arg(output);
    command.stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped());
    command
}

fn finish_encode(mut process: Child) -> Result<(), String> {
    process.stdin.take();
    let result = process.wait_with_output().map_err(
        |error| format!("FFmpeg NVENC process terminated unexpectedly: {error}"),
    )?;
    if !result.status.success() {
        return Err(format!(
            "FFmpeg NVENC encoder failed ({}): {}",
            result.status,
            String::from_utf8_lossy(&result.stderr).trim(),
        ));
    }
    Ok(())
}

fn run(args: &Args) -> Result<serde_json::Value, String> {
    let initial_base = rgba_pixels(args.width, args.height, 0, 255, 15);
    let initial_overlay = rgba_pixels(args.width / 2, args.height / 2, 0, 135, 67);
    let static_overlay = rgba_pixels(args.width / 4, args.height / 4, 0, 230, 111);
    let layout = [
        Overlay {
            pixels: &initial_overlay,
            width: args.width / 2, height: args.height / 2,
            x: 35, y: -17,
        },
        Overlay {
            pixels: &static_overlay,
            width: args.width / 4, height: args.height / 4,
            x: (args.width / 3) as i32, y: (args.height / 4) as i32,
        },
    ];
    let gpu = match GpuCompositor::new() {
        Ok(device) => device,
        Err(reason) => return Ok(json!({
            "schema": 1,
            "status": "gpu_unavailable",
            "reason": reason,
            "resolution": [args.width, args.height],
            "frames": args.frames,
            "hardware_gpu_measured": false,
            "production_export_changed": false
        })),
    };
    let mut session = gpu.prepare_session(
        &initial_base, args.width, args.height, &layout,
    )?;
    let prepare_at = Instant::now();
    session.prepare_nv12()?;
    let nv12_prepare_seconds = prepare_at.elapsed().as_secs_f64();
    // Optional short-video integration test, NOT a zero-copy encoder.
    // Refuse to overwrite a pre-existing video. Output is never requested
    // by default and does not affect the application's own export pipeline.
    let mut encoder = if let Some(path) = args.nvenc_output.as_ref() {
        if path.exists() {
            return Err(format!("Refusing to overwrite NVENC test file: {}", path.display()));
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(
                |error| format!("Cannot create NVENC output directory: {error}"),
            )?;
        }
        Some(nvenc_command(&args.ffmpeg, args.width, args.height, path)
            .spawn().map_err(|error| format!("Unable to launch FFmpeg NVENC: {error}"))?)
    } else {
        None
    };
    let mut cpu = Vec::with_capacity(args.frames);
    let mut rgba_gpu = Vec::with_capacity(args.frames);
    let mut nv12_gpu_readback = Vec::with_capacity(args.frames);
    let mut nv12_gpu_only = Vec::with_capacity(args.frames);
    for frame in 0..args.frames {
        let base = rgba_pixels(args.width, args.height, frame, 255, 15);
        let changing_overlay = rgba_pixels(
            args.width / 2, args.height / 2, frame, (90 + frame % 150) as u8, 67,
        );
        let layers = [
            Overlay {
                pixels: &changing_overlay,
                width: args.width / 2, height: args.height / 2,
                x: 35, y: -17,
            },
            Overlay {
                pixels: &static_overlay,
                width: args.width / 4, height: args.height / 4,
                x: (args.width / 3) as i32,
                y: (args.height / 4) as i32,
            },
        ];
        let time = Instant::now();
        let rgba_expected = composite_reference(&base, args.width, args.height, &layers)?;
        let expected = rgba_to_nv12_reference(
            &rgba_expected, args.width, args.height,
        )?;
        cpu.push(time.elapsed().as_secs_f64());

        let time = Instant::now();
        session.update_base(&base)?;
        session.update_overlay(0, &changing_overlay)?;
        let rgba_output = session.compose_and_readback()?;
        rgba_gpu.push(time.elapsed().as_secs_f64());
        if rgba_output != rgba_expected {
            return Err(format!("GPU RGBA pixels differ from Rust CPU reference on frame {frame}"));
        }

        let time = Instant::now();
        session.update_base(&base)?;
        session.update_overlay(0, &changing_overlay)?;
        let nv12_output = session.compose_nv12_and_readback()?;
        nv12_gpu_readback.push(time.elapsed().as_secs_f64());
        if nv12_output != expected {
            let mismatches = nv12_output.iter().zip(&expected).filter(
                |(a, b)| a != b
            ).count();
            return Err(format!(
                "GPU NV12 bytes differ from Rust reference on frame {frame}: {mismatches} mismatches"
            ));
        }
        if let Some(process) = encoder.as_mut() {
            process.stdin.as_mut().ok_or("FFmpeg stdin unavailable")?
                .write_all(&nv12_output)
                .map_err(|error| format!("FFmpeg NVENC frame write failed: {error}"))?;
        }

        let time = Instant::now();
        session.update_base(&base)?;
        session.update_overlay(0, &changing_overlay)?;
        session.compose_nv12_gpu_only()?;
        nv12_gpu_only.push(time.elapsed().as_secs_f64());
    }
    if let Some(process) = encoder.take() {
        finish_encode(process)?;
    }
    Ok(json!({
        "schema": 1,
        "status": "measured",
        "optional_ffmpeg_nvenc_mp4": args.nvenc_output.as_ref()
            .map(|p| p.to_string_lossy().to_string()),
        "nvenc_via_cpu_readback": args.nvenc_output.is_some(),
        "scope": "Dynamic RGBA upload + 2 GPU layers + NV12, no FFmpeg/NVENC",
        "adapter": gpu.adapter_name,
        "backend": gpu.adapter_backend,
        "resolution": [args.width, args.height],
        "frames": args.frames,
        "dynamic_overlay_count": 1,
        "static_overlay_count": 1,
        "byte_identical_rgba": true,
        "byte_identical_nv12_to_rust_reference": true,
        "nv12_color_space": "BT.709 limited range, explicitly defined integer approximation; not verified vs FFmpeg",
        "nv12_output_bytes": session.nv12_byte_count()?,
        "prepared_nv12_pipeline_seconds": nv12_prepare_seconds,
        "cpu_reference_rgba_nv12_median_seconds": median(&cpu),
        "gpu_dynamic_rgba_readback_median_seconds": median(&rgba_gpu),
        "gpu_dynamic_nv12_readback_median_seconds": median(&nv12_gpu_readback),
        "gpu_dynamic_nv12_resident_median_seconds": median(&nv12_gpu_only),
        "resident_frame_median_excludes": [
            "GPU->CPU readback",
            "FFmpeg/NVENC encode",
            "Python/Qt scene painting",
            "per-frame RGBA test data generation"
        ],
        "resident_frame_median_includes": [
            "GPU base upload",
            "GPU changed overlay upload",
            "GPU compositing",
            "GPU RGBA-to-NV12 conversion",
            "GPU submission and completion wait"
        ],
        "production_export_changed": false,
        "NVENC_interop_implemented": false,
        "hardware_gpu_measured": true
    }))
}

fn main() {
    let args = match arguments() {
        Ok(x) => x,
        Err(error) => { eprintln!("{error}"); std::process::exit(2); }
    };
    let report = match run(&args) {
        Ok(value) => value,
        Err(error) => { eprintln!("GPU dynamic NV12 validation failed: {error}"); std::process::exit(1); }
    };
    let output = serde_json::to_string_pretty(&report).expect("serialize NV12 report");
    println!("{output}");
    if let Some(path) = args.output {
        if let Some(parent) = path.parent() {
            if let Err(error) = fs::create_dir_all(parent) {
                eprintln!("Unable to create diagnostic directory: {error}");
                std::process::exit(1);
            }
        }
        if let Err(error) = fs::write(path, output + "\n") {
            eprintln!("Unable to write diagnostic JSON: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nvenc_probe_explicitly_uses_nv12_input_and_color_metadata() {
        let args = nvenc_command("ffmpeg", 1920, 1080, Path::new("test-output.mp4"));
        let parts: Vec<String> = args.get_args()
            .map(|a| a.to_string_lossy().to_string()).collect();
        let rendered = parts.join(" ");
        assert!(rendered.contains("-pixel_format nv12"));
        assert!(rendered.contains("-video_size 1920x1080"));
        assert!(rendered.contains("-c:v h264_nvenc"));
        assert!(rendered.contains("-color_primaries bt709"));
        assert!(rendered.contains("-n -f rawvideo"));
        assert!(!rendered.contains("-hwaccel cuda"));
    }
}
