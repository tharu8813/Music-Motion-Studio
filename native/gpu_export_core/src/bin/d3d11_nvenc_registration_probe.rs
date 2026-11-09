//! Probe NVENC D3D11 H.264 session and native NV12 resource registration.
//!
//! Only the synthetic BGRA input originates in CPU RAM. The BGRA->NV12
//! conversion, NV12 ring copies, and NVENC register/map/unmap are all in one
//! process and on the SAME D3D11 device. No GPU->CPU NV12 readback.
//! No NvEncEncodePicture call yet. This is a registration/mapping gate,
//! NOT proof of an actual zero-copy H.264 encode.
//!
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin d3d11_nvenc_registration_probe -- --width 1920 --height 1080
//!   --frames 12 --slots 3 --output d3d11-nvenc-register-local.json

#[cfg(not(windows))]
fn main() {
    println!(r#"{"schema":1,"status":"requires_windows","production_export_changed":false}"#);
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::NativeBgraNv12VideoProcessor;
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_nvenc_registration::NativeNvencRegistrationSession;
    use serde_json::{json, Value};
    use std::collections::VecDeque;
    use std::env;
    use std::fs;
    use std::path::PathBuf;
    use std::time::Duration;

    fn consume_registered(
        pool: &mut NativeNv12FramePool,
        session: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize, i64)>,
    ) -> Result<(), String> {
        let (slot, pts) = *pending.front().ok_or("No frame pending NVENC registration")?;
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        let reserved = pool.acquire_gpu_complete_ready()?
            .ok_or("GPU-completed NV12 texture was not ready")?;
        if reserved.slot_index != slot || reserved.pts != pts {
            return Err("NVENC resource handoff changed PTS or slot order".into());
        }
        session.probe_gpu_complete_frame(&reserved)?;
        drop(reserved);
        // The diagnostic never calls NvEncEncodePicture; after successful
        // unmap, there is no encoder consumer. A future encoder must wait
        // for its real bitstream completion before completing this slot.
        pool.complete_encode(slot, pts)?;
        pending.pop_front();
        Ok(())
    }

    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 12usize;
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
    if width < 64 || width > 3840 || height < 64 || height > 2160 ||
        width % 2 != 0 || height % 2 != 0 ||
        frames < 4 || frames > 120 || slots < 2 || slots > 8 || frames < slots
    {
        eprintln!("Invalid resolution or frames (4..120) or slots (2..8; <=frames)");
        std::process::exit(2);
    }

    let report: Value = match NativeNv12FramePool::new(width, height, slots) {
        Err(reason) => json!({
            "schema": 1, "status": "d3d11_hardware_unavailable",
            "reason": reason, "production_export_changed": false,
        }),
        Ok(mut pool) => {
            match NativeBgraNv12VideoProcessor::new_for_pool(&pool) {
                Err(reason) => json!({
                    "schema": 1, "status": "d3d11_video_processor_unavailable",
                    "reason": reason, "production_export_changed": false,
                }),
                Ok(bridge) => match NativeNvencRegistrationSession::new_for_pool(&pool) {
                    Err(reason) if reason.contains("NVENC driver library or supported API unavailable") =>
                        json!({
                            "schema": 1, "status": "nvenc_library_unavailable",
                            "reason": reason, "adapter_name": pool.adapter_name(),
                            "production_export_changed": false,
                        }),
                    Err(reason) => json!({
                        "schema": 1, "status": "nvenc_session_failed",
                        "reason": reason, "adapter_name": pool.adapter_name(),
                        "production_export_changed": false,
                    }),
                    Ok(mut session) => {
                        let result: Result<(), String> = (|| {
                            let texture = bridge.create_bgra_input_texture()?;
                            let mut pending = VecDeque::new();
                            for i in 0..frames {
                                if pending.len() == slots {
                                    consume_registered(&mut pool, &mut session, &mut pending)?;
                                }
                                let gray: u8 = match i % 4 {
                                    0 => 0, 1 => 255, 2 => 64, _ => 192,
                                };
                                let bgra = vec![gray, gray, gray, 255]
                                    .repeat(width as usize * height as usize);
                                bridge.upload_bgra_for_validation(&texture, &bgra)?;
                                let slot = pool.queue_gpu_bgra(&bridge, &texture, i as i64)?;
                                pending.push_back((slot, i as i64));
                            }
                            while !pending.is_empty() {
                                consume_registered(&mut pool, &mut session, &mut pending)?;
                            }
                            if pool.completed_count() != frames as u64 ||
                                session.successful_map_count() != frames ||
                                session.registered_slot_count() != slots
                            {
                                return Err("NVENC frame count or slot registration mismatch".into());
                            }
                            Ok(())
                        })();
                        match result {
                            Err(reason) => json!({
                                "schema": 1, "status": "nvenc_registration_failed",
                                "reason": reason, "adapter_name": pool.adapter_name(),
                                "nvenc_api_version": session.negotiated_api_version(),
                                "registered_slot_count": session.registered_slot_count(),
                                "mapped_frames": session.successful_map_count(),
                                "production_export_changed": false,
                            }),
                            Ok(()) => json!({
                                "schema": 1, "status": "measured",
                                "adapter_name": pool.adapter_name(),
                                "adapter_vendor_id": pool.adapter_vendor_id(),
                                "adapter_device_id": pool.adapter_device_id(),
                                "resolution": [width, height],
                                "frames": frames, "pool_slots": slots,
                                "nvenc_api_version": session.negotiated_api_version(),
                                "nvenc_d3d11_session_opened": true,
                                "native_nv12_slots_registered_with_nvenc": session.registered_slot_count(),
                                "nvenc_nv12_resources_mapped_and_unmapped": session.successful_map_count(),
                                "registered_textures_same_d3d11_device": true,
                                "nv12_gpu_to_cpu_readbacks": 0,
                                "nvenc_h264_bitstream_encoded": false,
                                "encoder_completion_fence_implemented": false,
                                "vulkan_wgpu_to_d3d11_interop_implemented": false,
                                "production_export_changed": false,
                            })
                        }
                    }
                }
            }
        }
    };
    let text = serde_json::to_string_pretty(&report).expect("Serialize NVENC probe");
    println!("{text}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("Create diagnostics folder");
        }
        fs::write(path, text + "\n").expect("Save NVENC registration result");
    }
    if matches!(
        report["status"].as_str(),
        Some("nvenc_session_failed" | "nvenc_registration_failed"),
    ) {
        std::process::exit(1);
    }
}
