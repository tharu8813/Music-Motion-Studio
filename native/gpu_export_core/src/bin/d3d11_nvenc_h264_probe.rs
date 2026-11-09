//! Actual in-process NVENC H.264 elementary-stream diagnostic.
//!
//! CPU creates synthetic BGRA input only. The device-local D3D11 video
//! processor performs BGRA->NV12, whose texture is registered with the
//! same-device NVENC session. NVENC consumes the GPU texture, and ONLY
//! the compressed H.264 bitstream is read into host memory.
//!
//! Usage:
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin d3d11_nvenc_h264_probe -- --width 1920 --height 1080 --frames 30
//!   --slots 3 --video-output d3d11-nvenc-h264-local.h264 --ffprobe ffprobe
//!   --output d3d11-nvenc-h264-local.json
//!
//! This is NOT connected to Music Motion Studio production exports:
//! no real Canvas rendering, no audio/muxing or async streaming yet.

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
    use std::fs::{self, OpenOptions};
    use std::io::Write;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::time::{Duration, Instant};

    fn median(input: &[f64]) -> f64 {
        let mut vals = input.to_vec();
        vals.sort_by(|a, b| a.total_cmp(b));
        if vals.len() % 2 == 0 {
            (vals[vals.len()/2-1] + vals[vals.len()/2]) / 2.0
        } else {
            vals[vals.len()/2]
        }
    }

    fn verify_ffprobe(command: &str, path: &Path, width: u32, height: u32, frames: usize)
        -> Result<Value, String>
    {
        let out = Command::new(command)
            .args([
                "-v", "error", "-f", "h264", "-count_frames",
                "-show_entries",
                "stream=codec_name,width,height,nb_read_frames,color_range,color_space,color_transfer,color_primaries",
                "-of", "json",
            ])
            .arg(path)
            .output()
            .map_err(|e| format!("Could not launch ffprobe: {e}"))?;
        if !out.status.success() {
            return Err(format!(
                "ffprobe rejected H.264: {}",
                String::from_utf8_lossy(&out.stderr),
            ));
        }
        let value: Value = serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("Invalid ffprobe JSON: {e}"))?;
        let streams = value["streams"].as_array().ok_or("ffprobe found no streams")?;
        if streams.len() != 1 { return Err("H.264 probe expected exactly one video stream".into()); }
        let video = &streams[0];
        if video["codec_name"] != "h264"
            || video["width"].as_u64() != Some(width as u64)
            || video["height"].as_u64() != Some(height as u64)
            || video["nb_read_frames"].as_str().and_then(|n| n.parse::<usize>().ok()) != Some(frames)
        {
            return Err(format!(
                "H.264 decoded frame/dimension mismatch: expected {frames} frames {width}x{height}, got {video}"
            ));
        }
        // The D3D11 VideoProcessor output matched FFmpeg bt709 full range
        // on the user's RTX 5060. Signal the same values in H.264 SPS VUI.
        if video["color_range"] != "pc"
            || video["color_space"] != "bt709"
            || video["color_transfer"] != "bt709"
            || video["color_primaries"] != "bt709"
        {
            return Err(format!("H.264 color VUI mismatch (expect full bt709): {video}"));
        }
        Ok(video.clone())
    }

    fn consume(
        pool: &mut NativeNv12FramePool,
        session: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize,i64)>,
        packets: &mut Vec<u8>,
        pts: &mut Vec<u64>,
        encode_latencies: &mut Vec<f64>,
    ) -> Result<(), String> {
        let &(slot, expected_pts) = pending.front().ok_or("No GPU NV12 input")?;
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        let frame = pool.acquire_gpu_complete_ready()?
            .ok_or("GPU-complete native NV12 frame was not ready")?;
        if frame.slot_index != slot || frame.pts != expected_pts {
            return Err("GPU NV12 frame slot/PTS order corrupted".into());
        }
        let start = Instant::now();
        let (packet, encoded_pts) = session.encode_gpu_complete_frame(&frame)?;
        encode_latencies.push(start.elapsed().as_secs_f64());
        if encoded_pts != expected_pts as u64 {
            return Err("NVENC emitted out-of-order H.264 PTS".into());
        }
        if packets.len().saturating_add(packet.len()) > 256 * 1024 * 1024 {
            return Err("H.264 synthetic probe exceeded 256 MiB output limit".into());
        }
        if !packet.windows(4).any(|slice| slice == [0,0,0,1]) &&
            !packet.windows(3).any(|slice| slice == [0,0,1])
        {
            return Err("NVENC output does not contain Annex-B NAL start codes".into());
        }
        packets.extend_from_slice(&packet);
        pts.push(encoded_pts);
        drop(frame);
        // Synchronous NvEncLockBitstream completed successfully, the
        // encoded packet was copied and unlocked, and the GPU input unmapped:
        // only NOW is this input slot safe to recycle.
        pool.complete_encode(slot, expected_pts)?;
        pending.pop_front();
        Ok(())
    }

    let mut width = 1920u32;
    let mut height = 1080u32;
    let mut frames = 30usize;
    let mut slots = 3usize;
    let mut output: Option<PathBuf> = None;
    let mut video: Option<PathBuf> = None;
    let mut ffprobe: Option<String> = None;
    let mut iter = env::args().skip(1);
    while let Some(option) = iter.next() {
        let value = iter.next().unwrap_or_else(|| {
            eprintln!("Missing argument for {option}"); std::process::exit(2);
        });
        match option.as_str() {
            "--width" => width = value.parse().expect("integer width"),
            "--height" => height = value.parse().expect("integer height"),
            "--frames" => frames = value.parse().expect("integer frames"),
            "--slots" => slots = value.parse().expect("integer slots"),
            "--output" => output = Some(value.into()),
            "--video-output" => video = Some(value.into()),
            "--ffprobe" => ffprobe = Some(value),
            _ => { eprintln!("Unknown argument {option}"); std::process::exit(2); }
        }
    }
    if width < 64 || width > 3840 || height < 64 || height > 2160
        || width % 2 != 0 || height % 2 != 0
        || !(4..=120).contains(&frames) || !(2..=8).contains(&slots)
        || frames < slots || video.is_none()
    {
        eprintln!("Use even dimensions 64..3840x64..2160, frames 4..120, slots 2..8, --video-output PATH");
        std::process::exit(2);
    }
    let video = video.expect("Validated video output");
    if video.exists() {
        eprintln!("Refusing to overwrite existing H.264 file: {}", video.display());
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
                        "reason": reason, "adapter_name": pool.adapter_name(),
                        "production_export_changed": false,
                    }),
                Err(reason) => json!({
                    "schema": 1, "status": "nvenc_session_failed",
                    "reason": reason, "adapter_name": pool.adapter_name(),
                    "production_export_changed": false,
                }),
                Ok(mut session) => {
                    let run: Result<Value, String> = (|| {
                        let texture = bridge.create_bgra_input_texture()?;
                        let mut pending = VecDeque::new();
                        let mut bitstream = Vec::new();
                        let mut pts = Vec::with_capacity(frames);
                        let mut timings = Vec::with_capacity(frames);
                        let start = Instant::now();
                        for i in 0..frames {
                            if pending.len() == slots {
                                consume(&mut pool, &mut session, &mut pending,
                                    &mut bitstream, &mut pts, &mut timings)?;
                            }
                            let bgra = match i % 6 {
                                0 => [0, 0, 0, 255],
                                1 => [255, 255, 255, 255],
                                2 => [0, 0, 255, 255],
                                3 => [0, 255, 0, 255],
                                4 => [255, 0, 0, 255],
                                _ => [0, 255, 255, 255],
                            };
                            let pixels = bgra.repeat(width as usize * height as usize);
                            bridge.upload_bgra_for_validation(&texture, &pixels)?;
                            let slot = pool.queue_gpu_bgra(&bridge, &texture, i as i64)?;
                            pending.push_back((slot, i as i64));
                        }
                        while !pending.is_empty() {
                            consume(&mut pool, &mut session, &mut pending,
                                &mut bitstream, &mut pts, &mut timings)?;
                        }
                        if pts != (0..frames as u64).collect::<Vec<_>>() ||
                            pool.completed_count() != frames as u64 ||
                            session.successful_map_count() != frames
                        {
                            return Err("H.264 PTS, output count or GPU slot release mismatch".into());
                        }
                        let elapsed = start.elapsed().as_secs_f64();
                        if let Some(parent) = video.parent().filter(|p| !p.as_os_str().is_empty()) {
                            fs::create_dir_all(parent)
                                .map_err(|e| format!("Cannot create video directory: {e}"))?;
                        }
                        let mut handle = OpenOptions::new().write(true).create_new(true)
                            .open(&video).map_err(|e| format!("Cannot create new H.264 output: {e}"))?;
                        if let Err(e) = handle.write_all(&bitstream).and_then(|_| handle.flush()) {
                            drop(handle);
                            let _ = fs::remove_file(&video);
                            return Err(format!("H.264 file write failed: {e}"));
                        }
                        drop(handle);
                        let ffprobe_report = match ffprobe.as_deref() {
                            Some(cmd) => match verify_ffprobe(cmd, &video, width, height, frames) {
                                Ok(report) => Some(report),
                                Err(error) => {
                                    let _ = fs::remove_file(&video);
                                    return Err(format!("Bitstream decoding or metadata verification failed: {error}"));
                                }
                            },
                            None => None,
                        };
                        Ok(json!({
                            "schema": 1, "status": "measured",
                            "adapter_name": pool.adapter_name(),
                            "resolution": [width, height],
                            "frames_encoded": frames,
                            "pts": pts,
                            "nvenc_api_version": session.negotiated_api_version(),
                            "registered_nv12_gpu_slots": session.registered_slot_count(),
                            "nvenc_h264_bitstream_encoded": true,
                            "h264_annexb_bytes": bitstream.len(),
                            "h264_file": video.display().to_string(),
                            "nv12_gpu_to_cpu_readbacks": 0,
                            "compressed_bitstream_read_to_cpu": true,
                            "median_synchronous_encode_and_output_seconds": median(&timings),
                            "total_gpu_path_and_encode_seconds": elapsed,
                            "timing_warning": "Synthetic input upload, native GPU processing and blocking NVENC output; not a real project export benchmark",
                            "ffprobe_video": ffprobe_report,
                            "ffprobe_requested": ffprobe.is_some(),
                            "full_frame_color_parity_verified": false,
                            "production_export_changed": false,
                        }))
                    })();
                    match run {
                        Ok(result) => result,
                        Err(reason) => json!({
                            "schema": 1, "status": "nvenc_bitstream_validation_failed",
                            "reason": reason, "adapter_name": pool.adapter_name(),
                            "nvenc_api_version": session.negotiated_api_version(),
                            "frames_mapped": session.successful_map_count(),
                            "production_export_changed": false,
                        }),
                    }
                },
            },
        },
    };
    let rendered = serde_json::to_string_pretty(&report)
        .expect("Serialize NVENC H.264 diagnostic");
    println!("{rendered}");
    if let Some(path) = output {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).expect("Create diagnostic report folder");
        }
        fs::write(path, rendered + "\n").expect("Save H.264 diagnostic JSON");
    }
    if matches!(
        report["status"].as_str(),
        Some("nvenc_session_failed" | "nvenc_bitstream_validation_failed"),
    ) {
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn h264_start_code_sanity() {
        let packet = [0, 0, 0, 1, 0x67, 0, 0, 1, 0x68];
        assert!(packet.windows(4).any(|w| w == [0,0,0,1]));
        assert!(![1,2,3,4,5].windows(4).any(|w| w == [0,0,0,1]));
    }
}
