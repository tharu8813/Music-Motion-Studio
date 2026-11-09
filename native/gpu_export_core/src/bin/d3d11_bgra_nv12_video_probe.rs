//! Validate native GPU BGRA -> D3D11 NV12 conversion without readback
//! between processing and the persistent encoder-frame texture ring.
//!
//! On RTX hardware:
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin d3d11_bgra_nv12_video_probe -- --width 1920 --height 1080
//!   --frames 20 --output d3d11-bgra-nv12-local.json
//!
//! The synthetic BGRA input starts in CPU RAM for this diagnostic. Its
//! upload and *validation-only* NV12 download occur OUTSIDE the timed
//! native GPU conversion+CopyResource section. This does NOT encode an MP4
//! and is not a wgpu Vulkan->D3D11 external-memory interop test.

#[cfg(not(windows))]
fn main() {
    println!("{{\"schema\":1,\"status\":\"requires_windows\",\"production_export_changed\":false}}");
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::NativeBgraNv12VideoProcessor;
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use serde_json::json;
    use std::env;
    use std::fs;
    use std::path::PathBuf;
    use std::time::Instant;

    fn median(samples: &[f64]) -> f64 {
        let mut vals = samples.to_vec();
        vals.sort_by(|a, b| a.total_cmp(b));
        if vals.len() % 2 == 0 {
            (vals[vals.len()/2 - 1] + vals[vals.len()/2]) / 2.0
        } else {
            vals[vals.len()/2]
        }
    }

    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 20usize;
    let mut output: Option<PathBuf> = None;
    let mut it = env::args().skip(1);
    while let Some(name) = it.next() {
        let value = it.next().unwrap_or_else(|| {
            eprintln!("Missing value for {name}");
            std::process::exit(2);
        });
        match name.as_str() {
            "--width" => width = value.parse().expect("width must be an integer"),
            "--height" => height = value.parse().expect("height must be an integer"),
            "--frames" => frames = value.parse().expect("frames must be an integer"),
            "--output" => output = Some(value.into()),
            _ => { eprintln!("Unknown option {name}"); std::process::exit(2); }
        }
    }
    if !(4..=120).contains(&frames) || width < 64 || height < 64
        || width > 3840 || height > 2160 || width % 2 != 0 || height % 2 != 0
    {
        eprintln!("Invalid probe dimensions or frame count");
        std::process::exit(2);
    }

    let report = match NativeNv12FramePool::new(width, height, 2) {
        Err(reason) => json!({
            "schema": 1, "status": "d3d11_hardware_unavailable",
            "reason": reason, "production_export_changed": false,
            "nvenc_interop_implemented": false,
        }),
        Ok(mut pool) => match NativeBgraNv12VideoProcessor::new_for_pool(&pool) {
            Err(reason) => json!({
                "schema": 1, "status": "d3d11_video_processor_unavailable",
                "reason": reason, "adapter_name": pool.adapter_name(),
                "production_export_changed": false,
                "nvenc_interop_implemented": false,
            }),
            Ok(bridge) => {
                let texture = bridge.create_bgra_input_texture()
                    .expect("Create same-device BGRA input GPU texture");
                let mut samples = Vec::with_capacity(frames);
                let mut colors = Vec::with_capacity(frames);
                let mut checked = 0usize;
                let black_luma;
                let white_luma;
                let mut first = 0.0;
                let mut second = 0.0;

                for index in 0..frames {
                    let gray: u8 = match index % 4 {
                        0 => 0, 1 => 255, 2 => 64, _ => 192,
                    };
                    let rgba = vec![gray, gray, gray, 255].repeat(
                        width as usize * height as usize,
                    );
                    bridge.upload_bgra_for_validation(&texture, &rgba)
                        .expect("Upload test BGRA frame");
                    let started = Instant::now();
                    let slot = match pool.queue_gpu_bgra(
                        &bridge, &texture, index as i64,
                    ) {
                        Ok(slot) => slot,
                        Err(reason) => {
                            // On real RTX hardware the CI runner cannot
                            // reproduce driver-specific D3D11 failures.
                            // Preserve an inspectable JSON diagnostic rather
                            // than losing the reason in a Rust panic.
                            let failure = json!({
                                "schema": 1,
                                "status": "gpu_video_conversion_failed",
                                "stage": "queue_gpu_bgra",
                                "failed_frame": index,
                                "reason": reason,
                                "adapter_name": pool.adapter_name(),
                                "adapter_vendor_id": pool.adapter_vendor_id(),
                                "adapter_device_id": pool.adapter_device_id(),
                                "resolution": [width, height],
                                "production_export_changed": false,
                                "nvenc_interop_implemented": false,
                            });
                            let text = serde_json::to_string_pretty(&failure)
                                .expect("Serialize driver error report");
                            eprintln!("{text}");
                            if let Some(path) = output.as_ref() {
                                if let Some(parent) = path.parent() {
                                    fs::create_dir_all(parent)
                                        .expect("Create diagnostics folder");
                                }
                                fs::write(path, text + "\n")
                                    .expect("Save D3D11 driver error JSON");
                            }
                            std::process::exit(1);
                        }
                    };
                    samples.push(started.elapsed().as_secs_f64());
                    // The only readback is *outside* the measured GPU path
                    // and is not used by any future native encoder.
                    let output_bytes = pool.readback_slot_for_validation(slot)
                        .expect("Read back NV12 for visual sanity check");
                    assert_eq!(output_bytes.len(), pool.frame_bytes().unwrap());
                    let plane_bytes = width as usize * height as usize;
                    let y_mean = output_bytes[..plane_bytes].iter()
                        .map(|&x| x as u64).sum::<u64>() as f64 / plane_bytes as f64;
                    let uv_mean = output_bytes[plane_bytes..].iter()
                        .map(|&x| x as u64).sum::<u64>() as f64
                        / (output_bytes.len() - plane_bytes) as f64;
                    if !(y_mean.is_finite() && uv_mean.is_finite())
                        || !(90.0..=166.0).contains(&uv_mean)
                    {
                        eprintln!("Unexpected grayscale NV12 output at frame {index}: Y={y_mean}, UV={uv_mean}");
                        std::process::exit(1);
                    }
                    if index == 0 { first = y_mean; }
                    if index == 1 { second = y_mean; }
                    colors.push(json!({
                        "input_gray": gray,
                        "nv12_y_mean": y_mean,
                        "nv12_uv_mean": uv_mean,
                    }));
                    checked += 1;
                    let reserved = pool.acquire_ready().expect("Frame must be ready");
                    assert_eq!(reserved.pts, index as i64);
                    assert_eq!(reserved.slot_index, slot);
                    drop(reserved);
                    pool.complete_encode(slot, index as i64)
                        .expect("Reclaim diagnostic frame after readback");
                }
                black_luma = first;
                white_luma = second;
                if white_luma - black_luma < 100.0 {
                    eprintln!("D3D11 NV12 luma did not distinguish white from black: {white_luma} vs {black_luma}");
                    std::process::exit(1);
                }
                json!({
                    "schema": 1,
                    "status": "measured",
                    "adapter_name": pool.adapter_name(),
                    "adapter_vendor_id": pool.adapter_vendor_id(),
                    "adapter_device_id": pool.adapter_device_id(),
                    "resolution": [width, height],
                    "frames": frames,
                    "bgra_texture_and_nv12_pool_same_d3d11_device": true,
                    "d3d11_hardware_video_processor_executed": true,
                    "nv12_output_gpu_resident": true,
                    "no_gpu_to_cpu_readback_between_bgra_and_nv12": true,
                    "nv12_diagnostic_readbacks": checked,
                    "white_luma_exceeds_black_by": white_luma - black_luma,
                    "nv12_output_sanity_checks_passed": true,
                    "median_gpu_video_processor_plus_nv12_copy_enqueue_seconds": median(&samples),
                    "timing_excludes": ["test frame generation", "CPU BGRA input upload", "GPU diagnostic NV12 readback", "NVENC encoding"],
                    "timing_warning": "CPU enqueue time, not GPU completion or true NVENC latency",
                    "color_accuracy_vs_ffmpeg_verified": false,
                    "vulkan_wgpu_to_d3d11_interop_implemented": false,
                    "nvenc_interop_implemented": false,
                    "production_export_changed": false,
                    "color_samples": colors,
                })
            }
        },
    };
    let rendered = serde_json::to_string_pretty(&report).expect("Serialize D3D11 video processor diagnostic");
    println!("{rendered}");
    if let Some(output) = output {
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent).expect("Create report directory");
        }
        fs::write(output, rendered + "\n").expect("Save GPU video processor report");
    }
}
