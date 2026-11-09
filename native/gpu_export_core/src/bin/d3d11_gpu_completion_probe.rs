//! Validate that an RTX D3D11 GPU finishes native BGRA->NV12 copies
//! before frames are handed to an eventual in-process NVENC encoder.
//!
//! This is NOT NVENC interop: diagnostic readbacks occur only after
//! explicit D3D11 EVENT-query completion; complete_encode below
//! simulates an external encoder's completion acknowledgement.
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin d3d11_gpu_completion_probe -- --width 1920 --height 1080
//!   --frames 30 --slots 3 --output d3d11-gpu-completion-local.json

#[cfg(not(windows))]
fn main() {
    println!(r#"{"schema":1,"status":"requires_windows","production_export_changed":false}"#);
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::NativeBgraNv12VideoProcessor;
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use serde_json::{json, Value};
    use std::collections::VecDeque;
    use std::env;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{Duration, Instant};

    fn median(samples: &[f64]) -> f64 {
        let mut sorted = samples.to_vec();
        sorted.sort_by(|a, b| a.total_cmp(b));
        let mid = sorted.len() / 2;
        if sorted.len() % 2 == 0 {
            (sorted[mid-1] + sorted[mid]) / 2.0
        } else {
            sorted[mid]
        }
    }

    fn drain_oldest(
        pool: &mut NativeNv12FramePool,
        outstanding: &mut VecDeque<(usize, i64, u8)>,
        completion_waits: &mut Vec<f64>,
        checked: &mut usize,
    ) -> Result<(), String> {
        let (slot, pts, expected_luma) =
            outstanding.pop_front().ok_or("No queued frame to complete")?;
        let started = Instant::now();
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        completion_waits.push(started.elapsed().as_secs_f64());
        let reserved = pool.acquire_gpu_complete_ready()?
            .ok_or("GPU-complete oldest ready frame was not reservable")?;
        if reserved.slot_index != slot || reserved.pts != pts {
            return Err("GPU completion query caused PTS/slot reordering".into());
        }
        // Test-only readback: the real encoder MUST consume the GPU texture
        // without this CPU download, and must emit its own completion signal.
        let bytes = pool.readback_slot_for_validation(slot)?;
        let (width, height) = pool.dimensions();
        let n = width as usize * height as usize;
        let mean = bytes[..n].iter().map(|&y| y as u64).sum::<u64>() as f64 / n as f64;
        if (mean - f64::from(expected_luma)).abs() > 3.0 {
            return Err(format!("GPU frame {pts}: unexpected Y mean {mean} vs {expected_luma}"));
        }
        drop(reserved);
        pool.complete_encode(slot, pts)?;
        *checked += 1;
        Ok(())
    }

    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 30usize;
    let mut slots = 3usize;
    let mut output: Option<PathBuf> = None;
    let mut args = env::args().skip(1);
    while let Some(name) = args.next() {
        let value = args.next().unwrap_or_else(|| {
            eprintln!("Missing value for {name}"); std::process::exit(2);
        });
        match name.as_str() {
            "--width" => width = value.parse().expect("width integer"),
            "--height" => height = value.parse().expect("height integer"),
            "--frames" => frames = value.parse().expect("frames integer"),
            "--slots" => slots = value.parse().expect("slots integer"),
            "--output" => output = Some(value.into()),
            _ => { eprintln!("Unknown option {name}"); std::process::exit(2); }
        }
    }
    if frames < 4 || frames > 120 || slots < 2 || slots > 8 ||
        width < 64 || width > 3840 || height < 64 || height > 2160 ||
        width % 2 != 0 || height % 2 != 0
    {
        eprintln!("Invalid dimensions, frames (4..120), or pool slots (2..8)");
        std::process::exit(2);
    }

    let report: Value = match NativeNv12FramePool::new(width, height, slots) {
        Err(reason) => json!({
            "schema": 1, "status": "d3d11_hardware_unavailable",
            "reason": reason, "production_export_changed": false,
        }),
        Ok(mut pool) => match NativeBgraNv12VideoProcessor::new_for_pool(&pool) {
            Err(reason) => json!({
                "schema": 1, "status": "d3d11_video_processor_unavailable",
                "reason": reason, "production_export_changed": false,
            }),
            Ok(bridge) => {
                let run: Result<(usize, usize, Vec<f64>, Vec<f64>), String> = (|| {
                    let texture = bridge.create_bgra_input_texture()?;
                    let mut outstanding: VecDeque<(usize, i64, u8)> = VecDeque::new();
                    let mut enqueue_times = Vec::with_capacity(frames);
                    let mut wait_times = Vec::with_capacity(frames);
                    let mut backpressure = 0usize;
                    let mut checked = 0usize;
                    for i in 0..frames {
                        let gray: u8 = match i % 4 { 0 => 0, 1 => 255, 2 => 64, _ => 192 };
                        let pixels = vec![gray, gray, gray, 255]
                            .repeat(width as usize * height as usize);
                        bridge.upload_bgra_for_validation(&texture, &pixels)?;
                        if outstanding.len() == slots {
                            // Reusing any slot before encoder acknowledgement
                            // must fail even when the GPU copy has finished.
                            if pool.queue_gpu_bgra(&bridge, &texture, i as i64).is_ok() {
                                return Err("Backpressure failed: in-flight slot overwritten".into());
                            }
                            backpressure += 1;
                            drain_oldest(
                                &mut pool, &mut outstanding, &mut wait_times, &mut checked,
                            )?;
                        }
                        let started = Instant::now();
                        let slot = pool.queue_gpu_bgra(&bridge, &texture, i as i64)?;
                        enqueue_times.push(started.elapsed().as_secs_f64());
                        outstanding.push_back((slot, i as i64, gray));
                    }
                    while !outstanding.is_empty() {
                        drain_oldest(
                            &mut pool, &mut outstanding, &mut wait_times, &mut checked,
                        )?;
                    }
                    if checked != frames || pool.completed_count() != frames as u64 ||
                        pool.submitted_count() != frames as u64
                    {
                        return Err("GPU completion frame count mismatch".into());
                    }
                    if pool.gpu_copy_complete(0).is_ok() {
                        return Err("Free slot unexpectedly retained active GPU query".into());
                    }
                    Ok((checked, backpressure, enqueue_times, wait_times))
                })();
                match run {
                    Err(reason) => json!({
                        "schema": 1, "status": "gpu_completion_validation_failed",
                        "reason": reason, "adapter_name": pool.adapter_name(),
                        "production_export_changed": false,
                    }),
                    Ok((checked, backpressure, enqueue_times, wait_times)) => json!({
                        "schema": 1, "status": "measured",
                        "adapter_name": pool.adapter_name(),
                        "adapter_vendor_id": pool.adapter_vendor_id(),
                        "adapter_device_id": pool.adapter_device_id(),
                        "resolution": [width, height],
                        "frames": frames, "pool_slots": slots,
                        "gpu_copy_event_queries_confirmed": checked,
                        "frames_verified_after_gpu_copy_completion": checked,
                        "pool_backpressure_rejections": backpressure,
                        "median_copy_command_enqueue_seconds": median(&enqueue_times),
                        "median_wait_for_copy_completion_seconds": median(&wait_times),
                        "timing_warning": "CPU wall time to submit and poll GPU EVENT query; not device timestamp or NVENC latency",
                        "completed_slots_reclaimed_only_after_gpu_copy": true,
                        "diagnostic_nv12_readbacks": checked,
                        "nvenc_encode_completion_fence_implemented": false,
                        "nvenc_encoding_performed": false,
                        "zero_copy_nvenc_implemented": false,
                        "vulkan_wgpu_to_d3d11_interop_implemented": false,
                        "production_export_changed": false,
                    }),
                }
            }
        },
    };
    let rendered = serde_json::to_string_pretty(&report)
        .expect("Serialize GPU completion report");
    println!("{rendered}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("Create report directory");
        }
        fs::write(path, rendered + "\n").expect("Write GPU completion report");
    }
    if matches!(report["status"].as_str(), Some("gpu_completion_validation_failed")) {
        std::process::exit(1);
    }
}
