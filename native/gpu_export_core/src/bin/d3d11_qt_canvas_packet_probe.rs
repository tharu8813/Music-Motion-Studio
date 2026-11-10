//! Replay REAL Qt CanvasSnapshot straight-BGRA bands through native D3D11
//! video composition and synchronous NVENC. Qt has rendered raster layers
//! on the CPU in advance; this does not implement native GPU text drawing.
//!
//! Example:
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml
//! --bin d3d11_qt_canvas_packet_probe -- --manifest samples/scene.json
//! --repeat 6 --slots 2 --video-output qt-scene.h264 --output qt-scene.json

#[cfg(not(windows))]
fn main() {
    println!(r#"{"schema":1,"status":"requires_windows","production_export_changed":false}"#);
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::{
        NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer,
    };
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_nvenc_registration::NativeNvencRegistrationSession;
    use music_motion_gpu_core::qt_canvas_packet::{load_qt_canvas_packet, CanvasPacketFrame};
    use serde_json::{json, Value};
    use std::collections::VecDeque;
    use std::env;
    use std::fs::{self, OpenOptions};
    use std::io::Write;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    fn consume(
        pool: &mut NativeNv12FramePool,
        encoder: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize, i64)>,
        packets: &mut Vec<u8>,
        pts: &mut Vec<u64>,
    ) -> Result<(), String> {
        let &(slot, expected_pts) = pending.front().ok_or("No pending Qt frame")?;
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        let frame = pool.acquire_gpu_complete_ready()?
            .ok_or("Qt frame GPU copy not complete")?;
        if frame.slot_index != slot || frame.pts != expected_pts {
            return Err("Qt Canvas native NVENC slot or PTS mismatch".into());
        }
        let (packet, actual_pts) = encoder.encode_gpu_complete_frame(&frame)?;
        if packet.is_empty() || actual_pts != expected_pts as u64 {
            return Err("Qt Canvas native H264 packet empty or out of PTS order".into());
        }
        if packets.len().saturating_add(packet.len()) > 256 * 1024 * 1024 {
            return Err("Qt Canvas H264 output exceeds diagnostic 256 MiB cap".into());
        }
        packets.extend_from_slice(&packet);
        pts.push(actual_pts);
        drop(frame);
        pool.complete_encode(slot, expected_pts)?;
        pending.pop_front();
        Ok(())
    }

    fn try_encode(
        frames: &[&CanvasPacketFrame], slots: usize, video: &PathBuf,
        adapter: &mut String,
    ) -> Result<Value, String> {
        let (width, height) = (frames[0].width, frames[0].height);
        let mut pool = NativeNv12FramePool::new(width, height, slots)?;
        *adapter = pool.adapter_name().to_string();
        let processor = NativeBgraNv12VideoProcessor::new_for_pool(&pool)?;
        let mut encoder = NativeNvencRegistrationSession::new_for_pool(&pool)?;
        // Each packet is immutable and has its own set of GPU BGRA textures.
        // A later live renderer should use a bounded pool of BGRA textures
        // rather than allocate per frame. For a diagnostic <=120 frames
        // the allocations make source ownership explicit and easy to audit.
        let mut pending = VecDeque::new();
        let mut compressed = Vec::new();
        let mut completed_pts = Vec::new();
        let start = Instant::now();
        for (index, source) in frames.iter().enumerate() {
            if pending.len() == slots {
                consume(&mut pool, &mut encoder, &mut pending,
                        &mut compressed, &mut completed_pts)?;
            }
            let mut textures = Vec::with_capacity(source.layers.len());
            for layer in &source.layers {
                if !layer.geometry.is_identity(width,height) { return Err("Legacy packet probe requires full-frame identity layers; use the stream exporter for v2".into()); }
                let texture = processor.create_bgra_input_texture()?;
                processor.upload_canvas_layer_for_validation(&texture, &layer.pixels)?;
                textures.push(texture);
            }
            let composed: Vec<_> = textures.iter().zip(source.layers.iter())
                .map(|(texture, layer)| NativeCanvasBgraLayer {
                    texture, opacity: layer.opacity,
                }).collect();
            let slot = pool.queue_gpu_canvas_layers(
                &processor, &composed, index as i64,
            )?;
            pending.push_back((slot, index as i64));
        }
        while !pending.is_empty() {
            consume(&mut pool, &mut encoder, &mut pending,
                    &mut compressed, &mut completed_pts)?;
        }
        if completed_pts != (0..frames.len() as u64).collect::<Vec<_>>()
            || encoder.successful_map_count() != frames.len()
            || pool.completed_count() != frames.len() as u64
        {
            return Err("Qt Canvas frame count or native NVENC completion mismatch".into());
        }
        if let Some(parent) = video.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)
                .map_err(|e| format!("Cannot make H264 destination: {e}"))?;
        }
        let mut output = OpenOptions::new().write(true).create_new(true)
            .open(video).map_err(|e| format!("Cannot create H264 without overwriting: {e}"))?;
        if let Err(e) = output.write_all(&compressed).and_then(|_| output.flush()) {
            drop(output);
            let _ = fs::remove_file(video);
            return Err(format!("Incomplete H264 output: {e}"));
        }
        drop(output);
        Ok(json!({
            "schema": 1, "status": "measured",
            "adapter_name": pool.adapter_name(),
            "nvenc_api_version": encoder.negotiated_api_version(),
            "resolution": [width, height],
            "frames_encoded": frames.len(),
            "pts": completed_pts,
            "layers_per_frame": frames.iter().map(|f| f.layers.len()).collect::<Vec<_>>(),
            "compressed_h264_bytes": compressed.len(),
            "elapsed_seconds": start.elapsed().as_secs_f64(),
            "h264_file": video.display().to_string(),
            "source": "real_qt_canvas_snapshot_cpu_raster_packets",
            "qt_canvas_rasterized_on_cpu": true,
            "d3d11_alpha_composition_on_gpu": true,
            "gpu_nv12_to_cpu_readbacks": 0,
            "nvenc_output_synchronously_completed": true,
            "native_gpu_text_painting_implemented": false,
            "production_export_changed": false,
        }))
    }

    let mut argv: Vec<String> = env::args().skip(1).collect();
    if argv.first().map(String::as_str) != Some("--internal-worker") {
        let exe = env::current_exe().expect("Locate Qt Canvas NVENC probe");
        let mut child = Command::new(exe).arg("--internal-worker")
            .args(&argv).stdin(Stdio::null()).spawn()
            .expect("Launch Qt Canvas GPU worker");
        let started = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(status)) => std::process::exit(status.code().unwrap_or(1)),
                Ok(None) if started.elapsed() < Duration::from_secs(180) =>
                    std::thread::sleep(Duration::from_millis(50)),
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    eprintln!("Qt Canvas NVENC worker exceeded 180s watchdog");
                    std::process::exit(1);
                }
                Err(error) => {
                    eprintln!("Qt Canvas NVENC worker monitor failed: {error}");
                    let _ = child.kill();
                    let _ = child.wait();
                    std::process::exit(1);
                }
            }
        }
    }
    argv.remove(0);
    let mut manifests = Vec::<PathBuf>::new();
    let mut repeat = 1usize;
    let mut slots = 2usize;
    let mut output: Option<PathBuf> = None;
    let mut video: Option<PathBuf> = None;
    let mut it = argv.into_iter();
    while let Some(option) = it.next() {
        let val = it.next().unwrap_or_else(|| {
            eprintln!("Missing argument for {option}"); std::process::exit(2);
        });
        match option.as_str() {
            "--manifest" => manifests.push(val.into()),
            "--repeat" => repeat = val.parse().expect("integer repeat"),
            "--slots" => slots = val.parse().expect("integer slots"),
            "--output" => output = Some(val.into()),
            "--video-output" => video = Some(val.into()),
            _ => { eprintln!("Unknown argument {option}"); std::process::exit(2); }
        }
    }
    if manifests.is_empty() || manifests.len() > 120 || repeat == 0
        || repeat > 120 || manifests.len().saturating_mul(repeat) > 120
        || !(2..=8).contains(&slots) || video.is_none()
    {
        eprintln!("Use --manifest FILE (repeatable), --repeat 1..120, --slots 2..8 and --video-output FILE");
        std::process::exit(2);
    }
    let video = video.expect("Video path required");
    if video.exists() {
        eprintln!("Refusing to overwrite existing Qt Canvas H264 file");
        std::process::exit(2);
    }
    let loaded: Result<Vec<_>, String> = manifests.iter()
        .map(|path| load_qt_canvas_packet(path))
        .collect();
    let report = (|| -> Result<Value, String> {
        let originals = loaded?;
        let (width, height) = (originals[0].width, originals[0].height);
        for frame in &originals {
            if frame.width != width || frame.height != height {
                return Err("All Qt Canvas frames must have equal dimensions".into());
            }
        }
        // Repetitions borrow the immutable Qt source packets. Never
        // duplicate multi-megabyte BGRA byte planes just to emit a PTS.
        let frames: Vec<&CanvasPacketFrame> = (0..repeat)
            .flat_map(|_| originals.iter()).collect();
        let mut adapter = String::new();
        match try_encode(&frames, slots, &video, &mut adapter) {
            Ok(result) => Ok(result),
            Err(reason) if adapter.is_empty() && (
                reason.contains("D3D11") || reason.contains("GPU") ||
                reason.contains("hardware") || reason.contains("adapter")
            ) => Ok(json!({
                "schema": 1, "status": "d3d11_hardware_unavailable",
                "reason": reason, "production_export_changed": false,
            })),
            Err(reason) if reason.contains("NVENC driver library or supported API unavailable") =>
                Ok(json!({
                    "schema": 1, "status": "nvenc_library_unavailable",
                    "reason": reason, "adapter_name": adapter,
                    "production_export_changed": false,
                })),
            Err(reason) if reason.contains("video processor unsupported")
                || reason.contains("GPU layer alpha blending unsupported")
                || reason.contains("input streams") || reason.contains("stream states") =>
                Ok(json!({
                    "schema": 1, "status": "canvas_gpu_compositor_unsupported",
                    "reason": reason, "adapter_name": adapter,
                    "production_export_changed": false,
                })),
            Err(reason) => Err(reason),
        }
    })();
    let data = match report {
        Ok(report) => report,
        Err(reason) => json!({
            "schema": 1, "status": "qt_canvas_native_encode_failed",
            "reason": reason, "production_export_changed": false,
        }),
    };
    let rendered = serde_json::to_string_pretty(&data).expect("Serialize Qt GPU probe");
    println!("{rendered}");
    if let Some(path) = output {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).expect("Create report directory");
        }
        fs::write(path, rendered + "\n").expect("Write Qt Canvas NVENC report");
    }
    if data["status"] == "qt_canvas_native_encode_failed" {
        std::process::exit(1);
    }
}
