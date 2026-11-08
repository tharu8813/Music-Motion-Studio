//! Windows-only Rust D3D11 NV12 GPU texture hardware feasibility test.
//! It is NOT directly wired to the native NVENC API nor to wgpu Vulkan.
//!
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_nv12_surface_probe -- --width 1920 --height 1080 --frames 20 --output d3d11-native-nv12-local.json

#[cfg(not(windows))]
fn main() {
    println!("{{\"schema\":1,\"status\":\"requires_windows\"}}");
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_nv12::{
        nv12_size, NativeD3d11Nv12Surface,
    };
    use serde_json::json;
    use std::{env, fs};
    use std::path::PathBuf;
    use std::time::Instant;

    fn median(data: &[f64]) -> f64 {
        let mut sorted = data.to_vec();
        sorted.sort_by(|a, b| a.total_cmp(b));
        let n = sorted.len();
        if n % 2 == 0 { (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0 }
        else { sorted[n / 2] }
    }
    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 20usize;
    let mut output: Option<PathBuf> = None;
    let mut it = env::args().skip(1);
    while let Some(k) = it.next() {
        let value = it.next().unwrap_or_else(|| {
            eprintln!("Expected a value after {k}");
            std::process::exit(2);
        });
        match k.as_str() {
            "--width" => width = value.parse().expect("width integer"),
            "--height" => height = value.parse().expect("height integer"),
            "--frames" => frames = value.parse().expect("frames integer"),
            "--output" => output = Some(value.into()),
            _ => { eprintln!("Unknown argument: {k}"); std::process::exit(2); }
        }
    }
    let bytes = nv12_size(width, height).unwrap_or_else(|error| {
        eprintln!("{error}");
        std::process::exit(2);
    });
    if frames < 2 || frames > 120 {
        eprintln!("Frames must be between 2 and 120");
        std::process::exit(2);
    }
    let mut samples = Vec::new();
    let report = match NativeD3d11Nv12Surface::new(width, height) {
        Err(reason) => json!({
            "schema": 1,
            "status": "d3d11_nv12_hardware_unavailable",
            "reason": reason,
            "resolution": [width, height],
            "frames": frames,
            "production_export_changed": false,
            "nvenc_interop_implemented": false,
        }),
        Ok(surface) => {
            let mut buffer = vec![0u8; bytes];
            for frame in 0..frames {
                // Every frame changes in BOTH Y and UV planes. Input
                // generation is excluded from the timed GPU round trip.
                for (index, byte) in buffer.iter_mut().enumerate() {
                    *byte = ((index * 19 + frame * 31 + 17) & 255) as u8;
                }
                let started = Instant::now();
                surface.verify_frame(&buffer).unwrap_or_else(|error| {
                    eprintln!("D3D11 frame {frame} verification failed: {error}");
                    std::process::exit(1);
                });
                samples.push(started.elapsed().as_secs_f64());
            }
            json!({
                "schema": 1,
                "status": "measured",
                "scope": "Rust native D3D11 NV12 Texture2D: CPU upload staging, GPU texture copy, CPU validation readback",
                "resolution": [width, height],
                "frames": frames,
                "native_d3d11_nv12_texture_created": true,
                "native_d3d11_hardware_device": true,
                "texture_format": "DXGI_FORMAT_NV12",
                "frame_bytes": bytes,
                "all_nv12_luma_and_chroma_bytes_identical": true,
                "median_upload_copy_readback_seconds": median(&samples),
                "samples_seconds": samples,
                "adapter_identity_verified_against_wgpu": false,
                "zero_copy_nvenc_implemented": false,
                "wgpu_vulkan_to_d3d11_gpu_interop_implemented": false,
                "production_export_changed": false,
            })
        }
    };
    let rendered = serde_json::to_string_pretty(&report).unwrap();
    println!("{rendered}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create output directory");
        }
        fs::write(path, rendered + "\n").expect("save native D3D11 diagnostics");
    }
}
