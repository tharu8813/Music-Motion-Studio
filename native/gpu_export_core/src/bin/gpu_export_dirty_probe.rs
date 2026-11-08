//! Measure full overlay uploads versus dirty-rectangle patches during
//! real wgpu composition and GPU-resident NV12 conversion.
//! Both paths render exactly the same changing frame. CPU/Qt/FFmpeg/NVENC
//! are not included in the timing. This does not alter production exports.

use music_motion_gpu_core::{
    composite_reference, rgba_to_nv12_reference, GpuCompositor, Overlay, PixelRect,
};
use serde_json::json;
use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Instant;

fn median(samples: &[f64]) -> f64 {
    let mut samples = samples.to_vec();
    samples.sort_by(|a, b| a.total_cmp(b));
    if samples.len() % 2 == 0 {
        (samples[samples.len()/2 - 1] + samples[samples.len()/2]) / 2.0
    } else {
        samples[samples.len()/2]
    }
}

fn rgba(width: u32, height: u32, color: [u8; 4]) -> Vec<u8> {
    vec![color].into_iter().cycle().take(
        width as usize * height as usize
    ).flatten().collect()
}

fn cpu_patch(image: &mut [u8], width: u32, rect: PixelRect, pixels: &[u8]) {
    let stride = rect.width as usize * 4;
    for row in 0..rect.height as usize {
        let start = ((rect.y as usize + row) * width as usize + rect.x as usize) * 4;
        image[start..start + stride]
            .copy_from_slice(&pixels[row * stride..(row + 1) * stride]);
    }
}

fn main() {
    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 30usize;
    let mut output: Option<PathBuf> = None;
    let mut args = env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().unwrap_or_else(|| {
            eprintln!("Missing argument value for {flag}");
            std::process::exit(2);
        });
        match flag.as_str() {
            "--width" => width = value.parse().expect("positive integer width"),
            "--height" => height = value.parse().expect("positive integer height"),
            "--frames" => frames = value.parse().expect("positive integer frame count"),
            "--output" => output = Some(PathBuf::from(value)),
            _ => { eprintln!("Unknown option: {flag}"); std::process::exit(2); }
        }
    }
    if !(64..=3840).contains(&width) || width % 4 != 0
        || !(64..=2160).contains(&height) || height % 4 != 0
        || !(2..=180).contains(&frames)
    {
        eprintln!("Width/height must be multiples of four, 64..3840x64..2160; frames 2..180");
        std::process::exit(2);
    }

    let width_overlay = width / 2;
    let height_overlay = height / 2;
    let base = rgba(width, height, [29, 47, 84, 255]);
    let mut overlay = rgba(width_overlay, height_overlay, [140, 25, 240, 140]);
    let report = match GpuCompositor::new() {
        Err(reason) => json!({
            "schema": 1, "status": "gpu_unavailable", "reason": reason,
            "hardware_gpu_measured": false, "production_export_changed": false,
        }),
        Ok(gpu) => {
            let initial = [Overlay {
                pixels: &overlay, width: width_overlay, height: height_overlay,
                x: 48, y: 32,
            }];
            let mut session = gpu.prepare_session(&base, width, height, &initial)
                .expect("Create persistent GPU overlay session");
            session.prepare_nv12().expect("Prepare reusable NV12 output");
            let rect = PixelRect {
                x: width_overlay.saturating_sub(128) / 2,
                y: height_overlay.saturating_sub(64) / 2,
                width: width_overlay.min(128),
                height: height_overlay.min(64),
            };
            let mut full_upload = Vec::with_capacity(frames);
            let mut dirty_upload = Vec::with_capacity(frames);
            for frame in 0..frames {
                let color = [
                    ((frame * 13 + 43) & 255) as u8,
                    ((frame * 29 + 59) & 255) as u8,
                    ((frame * 7 + 173) & 255) as u8,
                    ((frame * 17 + 80) & 255) as u8,
                ];
                let patch = rgba(rect.width, rect.height, color);
                cpu_patch(&mut overlay, width_overlay, rect, &patch);
                let layers = [Overlay {
                    pixels: &overlay, width: width_overlay,
                    height: height_overlay, x: 48, y: 32,
                }];

                // Alternate ordering: for every frame both paths write the
                // SAME overlay contents before the GPU kernel starts.
                for partial in if frame % 2 == 0 { [false, true] } else { [true, false] } {
                    let started = Instant::now();
                    if partial {
                        session.update_overlay_rect(0, rect, &patch)
                            .expect("Validated dirty rectangle upload");
                    } else {
                        session.update_overlay(0, &overlay)
                            .expect("Full overlay upload");
                    }
                    session.compose_nv12_gpu_only().expect("GPU output completion");
                    let elapsed = started.elapsed().as_secs_f64();
                    if partial { dirty_upload.push(elapsed); }
                    else { full_upload.push(elapsed); }
                }

                // This is outside timed measurements. It ensures neither the
                // per-row partial upload nor full-frame upload changes pixels.
                let composed = composite_reference(&base, width, height, &layers)
                    .expect("CPU reference composition");
                let expected = rgba_to_nv12_reference(&composed, width, height)
                    .expect("CPU NV12 reference");
                let actual = session.compose_nv12_and_readback()
                    .expect("Readback of latest dirty overlay frame");
                if actual != expected {
                    eprintln!("GPU dirty/full NV12 parity failed at frame {frame}");
                    std::process::exit(1);
                }
            }
            let full = median(&full_upload);
            let dirty = median(&dirty_upload);
            json!({
                "schema": 1,
                "status": "measured",
                "adapter": gpu.adapter_name,
                "backend": gpu.adapter_backend,
                "resolution": [width, height],
                "frames": frames,
                "overlay_size": [width_overlay, height_overlay],
                "dirty_rect_size": [rect.width, rect.height],
                "full_overlay_bytes_per_frame": overlay.len(),
                "dirty_bytes_per_frame": rect.width as usize * rect.height as usize * 4,
                "full_update_plus_gpu_nv12_median_seconds": full,
                "dirty_update_plus_gpu_nv12_median_seconds": dirty,
                "dirty_speedup_vs_full": full / dirty,
                "byte_identical_nv12_for_every_frame": true,
                "gpu_only_measured": true,
                "hardware_gpu_measured": true,
                "NVENC_interop_implemented": false,
                "production_export_changed": false,
                "timing_includes": "GPU upload + NV12 composition + wait for GPU completion",
                "timing_excludes": "frame generation, CPU reference, NV12 readback, FFmpeg/NVENC",
                "caveat": "Partial updates reduce bytes but issue one queue write per dirty row; might be slower."
            })
        }
    };
    let output_text = serde_json::to_string_pretty(&report).expect("Serialize GPU patch report");
    println!("{output_text}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("Create diagnostics directory");
        }
        fs::write(path, output_text + "\n").expect("Write GPU patch benchmark");
    }
}
