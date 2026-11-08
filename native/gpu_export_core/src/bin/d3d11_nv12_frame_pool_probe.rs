//! Validate a bounded ring of NV12 D3D11 hardware textures on the same GPU.
//!
//! This is a resource scheduling proof for a future same-process NVENC API,
//! NOT an encoder or wgpu→D3D11 shared-texture implementation.
//!
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//! --bin d3d11_nv12_frame_pool_probe -- --width 1920 --height 1080
//! --frames 30 --output d3d11-frame-pool-local.json

#[cfg(not(windows))]
fn main() {
    println!("{{\"schema\":1,\"status\":\"requires_windows\",\"production_export_changed\":false}}");
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::{
        d3d11_frame_pool::NativeNv12FramePool,
        GpuCompositor,
    };
    use serde_json::json;
    use std::env;
    use std::fs;
    use std::path::PathBuf;
    use std::time::Instant;

    fn median(input: &[f64]) -> f64 {
        let mut sorted = input.to_vec();
        sorted.sort_by(|a,b| a.total_cmp(b));
        let n = sorted.len();
        if n % 2 == 0 { (sorted[n/2-1] + sorted[n/2]) / 2.0 }
        else { sorted[n/2] }
    }

    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 30usize;
    let mut slots = 3usize;
    let mut output: Option<PathBuf> = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        let value = args.next().unwrap_or_else(|| {
            eprintln!("Missing value after {arg}");
            std::process::exit(2);
        });
        match arg.as_str() {
            "--width" => width = value.parse().expect("width integer"),
            "--height" => height = value.parse().expect("height integer"),
            "--frames" => frames = value.parse().expect("frames integer"),
            "--slots" => slots = value.parse().expect("slots integer"),
            "--output" => output = Some(value.into()),
            _ => { eprintln!("Unexpected argument: {arg}"); std::process::exit(2); }
        }
    }
    if frames < 3 || frames > 120 || slots < 2 || slots > 8 {
        eprintln!("frames must be 3..120, slots must be 2..8");
        std::process::exit(2);
    }

    let report = match NativeNv12FramePool::new(width, height, slots) {
        Err(reason) => json!({
            "schema": 1,
            "status": "d3d11_hardware_frame_pool_unavailable",
            "reason": reason,
            "production_export_changed": false,
            "zero_copy_nvenc_implemented": false,
        }),
        Ok(mut pool) => {
            let gpu_identity = GpuCompositor::new().ok().map(|gpu| {
                json!({
                    "adapter_name": gpu.adapter_name,
                    "adapter_backend": gpu.adapter_backend,
                    "adapter_vendor_id": gpu.adapter_vendor_id,
                    "adapter_device_id": gpu.adapter_device_id,
                })
            });
            let wgpu_matches_d3d11 = gpu_identity.as_ref().is_some_and(|gpu| {
                gpu["adapter_vendor_id"].as_u64()
                    == Some(pool.adapter_vendor_id() as u64)
                && gpu["adapter_device_id"].as_u64()
                    == Some(pool.adapter_device_id() as u64)
            });

            // Mismatched adapters are an important security/performance gate:
            // do not try cross-device memory copies or shared NVENC textures.
            let mut samples = Vec::with_capacity(frames);
            let mut outstanding = Vec::with_capacity(slots);
            let bytes = pool.frame_bytes().expect("valid NV12 dimensions");
            let mut checks = 0usize;
            let mut full_queue_rejections = 0usize;
            let mut all_slots_identical = true;

            for frame in 0..frames {
                let pixels: Vec<u8> = (0..bytes)
                    .map(|i| ((i * 17 + frame * 29 + 23) & 255) as u8)
                    .collect();

                // Fill the ring to capacity and ensure backpressure prevents
                // writing to an in-flight texture. Do not time frame generation.
                if outstanding.len() == slots {
                    let frame_to_finish = outstanding.remove(0);
                    let reserved = pool.acquire_ready().expect("oldest queued frame");
                    if reserved.pts != frame_to_finish.0 {
                        eprintln!("GPU frame PTS ordering changed");
                        std::process::exit(1);
                    }
                    pool.verify_slot(reserved.slot_index, &frame_to_finish.1)
                        .expect("Read back NV12 GPU slot for byte parity");
                    checks += 1;
                    drop(reserved.texture);
                    pool.complete_encode(reserved.slot_index, reserved.pts)
                        .expect("Reclaim completed diagnostic frame");
                }
                let started = Instant::now();
                let slot_id = pool.queue_cpu_nv12(&pixels, frame as i64)
                    .expect("Upload same-device NV12 frame");
                samples.push(started.elapsed().as_secs_f64());
                pool.verify_slot(slot_id, &pixels)
                    .expect("Immediate D3D11 slot exact Y/UV byte parity");
                checks += 1;
                outstanding.push((frame as i64, pixels));

                if outstanding.len() == slots {
                    let not_enqueued = pool.queue_cpu_nv12(
                        &outstanding.last().unwrap().1, (frame + 1) as i64,
                    );
                    if not_enqueued.is_ok() {
                        eprintln!("GPU ring overwrote a queued frame");
                        std::process::exit(1);
                    }
                    full_queue_rejections += 1;
                }
            }
            while !outstanding.is_empty() {
                let expected = outstanding.remove(0);
                let reserved = pool.acquire_ready().expect("queued GPU frame");
                if reserved.pts != expected.0 {
                    eprintln!("GPU final drain PTS ordering changed");
                    std::process::exit(1);
                }
                pool.verify_slot(reserved.slot_index, &expected.1)
                    .expect("Final D3D11 NV12 frame byte parity");
                checks += 1;
                drop(reserved.texture);
                pool.complete_encode(reserved.slot_index, reserved.pts)
                    .expect("Free final D3D11 GPU frame");
            }
            if pool.completed_count() != frames as u64 || pool.submitted_count() != frames as u64 {
                all_slots_identical = false;
            }
            if !all_slots_identical {
                eprintln!("Frame pool lost an NV12 frame");
                std::process::exit(1);
            }
            json!({
                "schema": 1,
                "status": "measured",
                "resolution": [width, height],
                "frames": frames,
                "pool_capacity": slots,
                "frame_bytes": bytes,
                "adapter_name": pool.adapter_name(),
                "adapter_vendor_id": pool.adapter_vendor_id(),
                "adapter_device_id": pool.adapter_device_id(),
                "wgpu_adapter": gpu_identity,
                "wgpu_vendor_device_match": wgpu_matches_d3d11,
                "wgpu_device_match_limitation": "PCI vendor/device match is not same-process device/texture sharing and does not prove exact GPU LUID",
                "native_d3d11_textures_on_same_device": true,
                "gpu_to_gpu_copy_without_intermediate_readback": true,
                "all_nv12_bytes_match_cpu_reference": all_slots_identical,
                "diagnostic_readback_count": checks,
                "frame_pool_completed": pool.completed_count(),
                "backpressure_rejections": full_queue_rejections,
                "median_cpu_staging_upload_plus_gpu_copy_enqueue_seconds": median(&samples),
                "timing_warning": "Queue submission wall time only: not full GPU-completion time or NVENC performance",
                "nvenc_encoding_performed": false,
                "nvenc_hardware_frame_context_implemented": false,
                "zero_copy_nvenc_implemented": false,
                "production_export_changed": false,
            })
        }
    };
    let content = serde_json::to_string_pretty(&report).expect("JSON output");
    println!("{content}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("Create output directory");
        }
        fs::write(path, content + "\n").expect("Write GPU frame pool report");
    }
}
