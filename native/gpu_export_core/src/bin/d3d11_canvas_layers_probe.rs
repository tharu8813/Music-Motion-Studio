//! Synthetic GPU Canvas/lyric alpha compositor -> NV12 -> real NVENC.
//!
//! This deliberately validates ONLY two GPU-resident image layers;
//! the existing Qt/QPainter Canvas source is not yet a native GPU renderer.
//! Synthetic BGRA is CPU-generated/uploaded, while *layer blending*,
//! BGRA->NV12 and NVENC H.264 execute entirely on one D3D11 device.
//!
//! Run RTX test:
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin d3d11_canvas_layers_probe -- --frames 6 --slots 2
//!   --width 1920 --height 1080 --validate-alpha
//!   --video-output d3d11-canvas-layer-test.h264
//!   --output d3d11-canvas-layer-test.json

#[cfg(not(windows))]
fn main() {
    println!(r#"{"schema":1,"status":"requires_windows","production_export_changed":false}"#);
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::{
        NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer,
    };
    use music_motion_gpu_core::canvas_layer_manifest::load_canvas_frame_packet;
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_nvenc_registration::NativeNvencRegistrationSession;
    use serde_json::{json, Value};
    use std::collections::VecDeque;
    use std::env;
    use std::fs::{self, OpenOptions};
    use std::io::Write;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    // Blocking NVENC lock must never freeze the calling shell. Reuse the
    // same child-process timeout policy as the proven H.264 diagnostic.
    let mut argv: Vec<String> = env::args().skip(1).collect();
    if argv.first().map(String::as_str) != Some("--internal-worker") {
        let exe = env::current_exe().expect("Find native Canvas layer probe executable");
        let mut worker = Command::new(exe).arg("--internal-worker")
            .args(&argv).stdin(Stdio::null())
            .spawn().expect("Start Canvas layer GPU diagnostic worker");
        let started = Instant::now();
        loop {
            match worker.try_wait() {
                Ok(Some(status)) => std::process::exit(status.code().unwrap_or(1)),
                Ok(None) if started.elapsed() < Duration::from_secs(180) => {
                    std::thread::sleep(Duration::from_millis(50));
                }
                Ok(None) => {
                    let _ = worker.kill();
                    let _ = worker.wait();
                    let report = json!({
                        "schema": 1, "status": "nvenc_watchdog_timeout",
                        "reason": "Canvas GPU/NVENC worker exceeded 180 seconds",
                        "production_export_changed": false,
                    });
                    eprintln!("{report}");
                    if let Some(i) = argv.iter().position(|arg| arg == "--output") {
                        if let Some(p) = argv.get(i+1) {
                            let _ = fs::write(p, report.to_string());
                        }
                    }
                    std::process::exit(1);
                }
                Err(error) => {
                    eprintln!("Cannot monitor native Canvas GPU worker: {error}");
                    let _ = worker.kill();
                    let _ = worker.wait();
                    std::process::exit(1);
                }
            }
        }
    }
    argv.remove(0);

    fn center_y(nv12: &[u8], width: usize, height: usize) -> Result<u8, String> {
        let luma_len = width.checked_mul(height)
            .ok_or("NV12 dimensions overflow")?;
        if nv12.len() < luma_len {
            return Err("NV12 GPU diagnostic readback was truncated".into());
        }
        Ok(nv12[(height / 2) * width + width / 2])
    }

    fn drain(
        pool: &mut NativeNv12FramePool,
        encoder: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize, i64)>,
        bytes: &mut Vec<u8>,
        center_samples: &mut Vec<u8>,
        validate_alpha: bool,
        dimensions: (usize, usize),
    ) -> Result<(), String> {
        let &(slot, expected_pts) = pending.front()
            .ok_or("No Canvas GPU frame pending")?;
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        if validate_alpha {
            let nv12 = pool.readback_slot_for_validation(slot)?;
            center_samples.push(center_y(&nv12, dimensions.0, dimensions.1)?);
        }
        let frame = pool.acquire_gpu_complete_ready()?
            .ok_or("No completed native Canvas NV12 frame")?;
        if frame.slot_index != slot || frame.pts != expected_pts {
            return Err("Canvas GPU encoder changed frame slot or presentation order".into());
        }
        let (packet, pts) = encoder.encode_gpu_complete_frame(&frame)?;
        if pts != expected_pts as u64 || packet.is_empty() {
            return Err("Native Canvas NVENC packet PTS mismatch or empty packet".into());
        }
        if bytes.len().saturating_add(packet.len()) > 128 * 1024 * 1024 {
            return Err("Native Canvas probe exceeded 128 MiB bitstream limit".into());
        }
        bytes.extend_from_slice(&packet);
        drop(frame);
        pool.complete_encode(slot, expected_pts)?;
        pending.pop_front();
        Ok(())
    }

    let mut width = 320u32;
    let mut height = 180u32;
    let mut frames = 6usize;
    let mut slots = 2usize;
    let mut report_path: Option<PathBuf> = None;
    let mut video_path: Option<PathBuf> = None;
    let mut validate_alpha = false;
    let mut layer_manifest: Option<PathBuf> = None;
    let mut iter = argv.into_iter();
    while let Some(flag) = iter.next() {
        if flag == "--validate-alpha" {
            validate_alpha = true;
            continue;
        }
        let value = iter.next().unwrap_or_else(|| {
            eprintln!("Missing value for {flag}");
            std::process::exit(2);
        });
        match flag.as_str() {
            "--width" => width = value.parse().expect("integer width"),
            "--height" => height = value.parse().expect("integer height"),
            "--frames" => frames = value.parse().expect("integer frames"),
            "--slots" => slots = value.parse().expect("integer slots"),
            "--output" => report_path = Some(value.into()),
            "--video-output" => video_path = Some(value.into()),
            "--layer-manifest" => layer_manifest = Some(value.into()),
            _ => { eprintln!("Unknown option: {flag}"); std::process::exit(2); }
        }
    }
    if width < 64 || height < 64 || width > 3840 || height > 2160 ||
        width % 2 != 0 || height % 2 != 0 ||
        frames < 3 || frames > 120 || slots < 2 || slots > 8 || slots > frames
    {
        eprintln!("Invalid resolution, frames (3..120) or pool slots (2..8)");
        std::process::exit(2);
    }
    if video_path.as_ref().is_some_and(|p| p.exists()) {
        eprintln!("Refusing to overwrite existing Canvas H.264 file");
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
            Ok(bridge) => match NativeNvencRegistrationSession::new_for_pool(&pool) {
                Err(reason) if reason.contains("NVENC driver library or supported API unavailable") =>
                    json!({
                        "schema": 1, "status": "nvenc_library_unavailable",
                        "reason": reason, "production_export_changed": false,
                    }),
                Err(reason) => json!({
                    "schema": 1, "status": "nvenc_session_failed",
                    "reason": reason, "production_export_changed": false,
                }),
                Ok(mut encoder) => {
                    let attempted: Result<(Vec<u8>, Vec<u8>, usize), String> = (|| {
                        let packet = layer_manifest.as_deref()
                            .map(load_canvas_frame_packet)
                            .transpose()?;
                        if packet.as_ref().is_some_and(|p| p.width != width || p.height != height) {
                            return Err("Qt Canvas packet dimensions do not match requested GPU dimensions".into());
                        }
                        if packet.is_some() && validate_alpha {
                            return Err("--validate-alpha is only for the synthetic mask test, not Qt Canvas content".into());
                        }
                        let layer_count = packet.as_ref().map_or(2, |p| p.layers.len());
                        let textures: Vec<_> = (0..layer_count)
                            .map(|_| bridge.create_bgra_input_texture())
                            .collect::<Result<Vec<_>, String>>()?;
                        let opacities: Vec<f32> = match &packet {
                            Some(p) => {
                                for (layer, texture) in p.layers.iter().zip(&textures) {
                                    bridge.upload_canvas_layer_for_validation(texture, &layer.bgra)?;
                                }
                                p.layers.iter().map(|l| l.opacity).collect()
                            }
                            None => {
                                let base = [32, 32, 32, 255]
                                    .repeat(width as usize * height as usize);
                                bridge.upload_canvas_layer_for_validation(&textures[0], &base)?;
                                vec![1.0, 1.0]
                            }
                        };
                        let mut pending = VecDeque::new();
                        let mut compressed = Vec::new();
                        let mut center_samples = Vec::new();
                        for i in 0..frames {
                            if pending.len() == slots {
                                drain(&mut pool, &mut encoder, &mut pending,
                                    &mut compressed, &mut center_samples,
                                    validate_alpha, (width as usize, height as usize))?;
                            }
                            if packet.is_none() {
                                // In synthetic mode, vary only the lyric mask.
                                let ink_alpha = match i % 3 { 0 => 0, 1 => 128, _ => 255 };
                                let mut lyric = vec![0_u8; width as usize * height as usize * 4];
                                for y in height / 3..height * 2 / 3 {
                                    for x in width / 3..width * 2 / 3 {
                                        let off = (y as usize * width as usize + x as usize) * 4;
                                        lyric[off..off + 4].copy_from_slice(&[0, 0, 255, ink_alpha]);
                                    }
                                }
                                bridge.upload_canvas_layer_for_validation(&textures[1], &lyric)?;
                            }
                            let composed: Vec<NativeCanvasBgraLayer<'_>> = textures.iter()
                                .zip(&opacities)
                                .map(|(texture, &opacity)| NativeCanvasBgraLayer {
                                    texture, opacity,
                                })
                                .collect();
                            let slot = pool.queue_gpu_canvas_layers(
                                &bridge, &composed, i as i64,
                            )?;
                            pending.push_back((slot, i as i64));
                        }
                        while !pending.is_empty() {
                            drain(&mut pool, &mut encoder, &mut pending,
                                &mut compressed, &mut center_samples,
                                validate_alpha, (width as usize, height as usize))?;
                        }
                        if pool.submitted_count() != frames as u64 ||
                            pool.completed_count() != frames as u64 ||
                            encoder.successful_map_count() != frames
                        {
                            return Err("Canvas GPU encoder frame/slot completion mismatch".into());
                        }
                        if validate_alpha {
                            // Require repeatable visibly distinct alpha states.
                            // The exact YUV coefficients are driver-dependent.
                            for triple in center_samples.chunks_exact(3) {
                                if !(triple[0] + 3 < triple[1] &&
                                     triple[1] + 3 < triple[2]) {
                                    return Err(format!(
                                        "D3D11 alpha composition failed: center Y values {triple:?}"
                                    ));
                                }
                            }
                        }
                        Ok((compressed, center_samples, layer_count))
                    })();
                    match attempted {
                        Err(reason) if reason.contains("alpha blending unsupported") ||
                            reason.contains("input streams") || reason.contains("stream states") =>
                            json!({
                                "schema": 1, "status": "canvas_alpha_unsupported",
                                "reason": reason, "adapter_name": pool.adapter_name(),
                                "production_export_changed": false,
                            }),
                        Err(reason) => json!({
                            "schema": 1, "status": "canvas_gpu_validation_failed",
                            "reason": reason, "adapter_name": pool.adapter_name(),
                            "production_export_changed": false,
                        }),
                        Ok((bytes, samples, layer_count)) => {
                            let file_result = video_path.as_ref().map(|path| -> std::io::Result<()> {
                                let mut file = OpenOptions::new().write(true)
                                    .create_new(true).open(path)?;
                                if let Err(error) = file.write_all(&bytes).and_then(|_| file.flush()) {
                                    drop(file);
                                    let _ = fs::remove_file(path);
                                    return Err(error);
                                }
                                Ok(())
                            });
                            match file_result {
                                Some(Err(error)) => json!({
                                    "schema": 1, "status": "canvas_gpu_validation_failed",
                                    "reason": format!("Cannot write H264 bitstream: {error}"),
                                    "production_export_changed": false,
                                }),
                                _ => json!({
                                    "schema": 1, "status": "measured",
                                    "adapter_name": pool.adapter_name(),
                                    "nvenc_api_version": encoder.negotiated_api_version(),
                                    "resolution": [width, height],
                                    "frames_encoded": frames,
                                    "gpu_composited_layers": layer_count,
                                    "input_mode": if layer_manifest.is_some() { "qt_canvas_manifest" } else { "synthetic_mask" },
                                    "qt_canvas_packet_sha256_checked": layer_manifest.is_some(),
                                    "qt_canvas_cpu_rasterized": layer_manifest.is_some(),
                                    "per_pixel_straight_alpha": true,
                                    "alpha_validation_requested": validate_alpha,
                                    "alpha_validation_passed": validate_alpha,
                                    "alpha_center_y_samples": samples,
                                    "diagnostic_gpu_nv12_readbacks": if validate_alpha { frames } else { 0 },
                                    "gpu_to_cpu_nv12_readbacks_on_encode_path": 0,
                                    "encoded_h264_bytes": bytes.len(),
                                    "h264_file": video_path.as_ref().map(|p| p.display().to_string()),
                                    "qt_canvas_gpu_painting_implemented": false,
                                    "production_export_changed": false,
                                }),
                            }
                        }
                    }
                }
            },
        },
    };
    let result = serde_json::to_string_pretty(&report).expect("Serialize GPU Canvas report");
    println!("{result}");
    if let Some(path) = report_path {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).expect("Create report folder");
        }
        fs::write(path, result + "\n").expect("Write Canvas GPU report");
    }
    if matches!(
        report["status"].as_str(),
        Some("nvenc_session_failed" | "canvas_gpu_validation_failed"),
    ) {
        std::process::exit(1);
    }
}
