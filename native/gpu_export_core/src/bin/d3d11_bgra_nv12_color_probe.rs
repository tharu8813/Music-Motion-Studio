//! Diagnostic-only color patch comparison: native D3D11 BGRA -> NV12
//! versus FFmpeg swscale on the same opaque BGRA input.
//!
//! This NEVER feeds the production encoder. Readback happens only after
//! native GPU conversion, to test the color matrix and range chosen by
//! the driver. The optional FFmpeg comparisons sample patch interiors:
//! they are not proof of full-frame color fidelity or NVENC compatibility.
//!
//! cargo run --release --manifest-path native/gpu_export_core/Cargo.toml \
//!   --bin d3d11_bgra_nv12_color_probe -- --width 1920 --height 1080 \
//!   --ffmpeg ffmpeg --output d3d11-bgra-color-local.json

#[cfg(not(windows))]
fn main() {
    println!(r#"{"schema":1,"status":"requires_windows","production_export_changed":false}"#);
}

#[cfg(windows)]
fn main() {
    use music_motion_gpu_core::d3d11_bgra_video::NativeBgraNv12VideoProcessor;
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use serde_json::{json, Value};
    use std::env;
    use std::fs;
    use std::io::Write;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    const COLS: usize = 4;
    const ROWS: usize = 3;
    const PATCHES: [(&str, [u8; 4]); 12] = [
        ("black",   [  0,   0,   0, 255]),
        ("white",   [255, 255, 255, 255]),
        ("red",     [  0,   0, 255, 255]),
        ("green",   [  0, 255,   0, 255]),
        ("blue",    [255,   0,   0, 255]),
        ("cyan",    [255, 255,   0, 255]),
        ("magenta", [255,   0, 255, 255]),
        ("yellow",  [  0, 255, 255, 255]),
        ("gray64",  [ 64,  64,  64, 255]),
        ("gray192", [192, 192, 192, 255]),
        ("skin",    [135, 170, 210, 255]),
        ("orange",  [  0, 128, 255, 255]),
    ];

    fn make_bgra(width: usize, height: usize) -> Vec<u8> {
        let mut pixels = vec![0u8; width * height * 4];
        for y in 0..height {
            for x in 0..width {
                let patch = (y * ROWS / height) * COLS + (x * COLS / width);
                let offset = (y * width + x) * 4;
                pixels[offset..offset + 4].copy_from_slice(&PATCHES[patch].1);
            }
        }
        pixels
    }

    fn sample_patch_centers(nv12: &[u8], width: usize, height: usize) -> Vec<[u8; 3]> {
        let y_plane_len = width * height;
        let mut samples = Vec::with_capacity(PATCHES.len());
        for index in 0..PATCHES.len() {
            let col = index % COLS;
            let row = index / COLS;
            // Use even coordinates, strictly inside each uniformly colored
            // patch, so 4:2:0 sampling does not straddle its boundary.
            let x = ((col * width / COLS + (col + 1) * width / COLS) / 2) & !1;
            let y = ((row * height / ROWS + (row + 1) * height / ROWS) / 2) & !1;
            let uv = y_plane_len + (y / 2) * width + (x / 2) * 2;
            samples.push([nv12[y * width + x], nv12[uv], nv12[uv + 1]]);
        }
        samples
    }

    fn run_ffmpeg(
        command: &str, frame: &[u8], width: usize, height: usize,
        matrix: &str, range: &str,
    ) -> Result<Vec<u8>, String> {
        let scale = format!(
            "scale=in_range=pc:out_range={range}:out_color_matrix={matrix},format=nv12"
        );
        let mut process = Command::new(command)
            .args([
                "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "rawvideo", "-pixel_format", "bgra",
                "-video_size", &format!("{width}x{height}"),
                "-framerate", "1", "-i", "pipe:0",
                "-frames:v", "1", "-vf", &scale,
                "-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1",
            ])
            .stdin(Stdio::piped()).stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn().map_err(|e| format!("Could not launch FFmpeg: {e}"))?;
        process.stdin.take().ok_or("Missing FFmpeg stdin")?
            .write_all(frame)
            .map_err(|e| format!("Cannot send BGRA patch chart to FFmpeg: {e}"))?;
        let output = process.wait_with_output()
            .map_err(|e| format!("Could not collect FFmpeg reference frame: {e}"))?;
        if !output.status.success() {
            return Err(format!(
                "FFmpeg {matrix}/{range} conversion failed: {}",
                String::from_utf8_lossy(&output.stderr),
            ));
        }
        let expected_len = width * height * 3 / 2;
        if output.stdout.len() != expected_len {
            return Err(format!(
                "FFmpeg returned {} NV12 bytes, expected {expected_len}",
                output.stdout.len(),
            ));
        }
        Ok(output.stdout)
    }

    let mut width = 320usize;
    let mut height = 180usize;
    let mut ffmpeg: Option<String> = None;
    let mut output: Option<PathBuf> = None;
    let mut args = env::args().skip(1);
    while let Some(option) = args.next() {
        let value = args.next().unwrap_or_else(|| {
            eprintln!("Missing argument for {option}"); std::process::exit(2);
        });
        match option.as_str() {
            "--width" => width = value.parse().expect("width must be a positive integer"),
            "--height" => height = value.parse().expect("height must be a positive integer"),
            "--ffmpeg" => ffmpeg = Some(value),
            "--output" => output = Some(value.into()),
            _ => { eprintln!("Unknown option: {option}"); std::process::exit(2); }
        }
    }
    if width < 64 || height < 48 || width > 3840 || height > 2160
        || width % (COLS * 2) != 0 || height % (ROWS * 2) != 0
    {
        eprintln!("Width must be a multiple of 8; height a multiple of 6; both within 64x48..3840x2160.");
        std::process::exit(2);
    }
    let report: Value = match NativeNv12FramePool::new(width as u32, height as u32, 2) {
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
                let input = make_bgra(width, height);
                let converted: Result<Vec<u8>, String> = (|| {
                    let texture = bridge.create_bgra_input_texture()?;
                    bridge.upload_bgra_for_validation(&texture, &input)?;
                    let index = pool.queue_gpu_bgra(&bridge, &texture, 0)?;
                    let bytes = pool.readback_slot_for_validation(index)?;
                    let reserved = pool.acquire_ready().ok_or("Missing GPU color frame")?;
                    if reserved.slot_index != index || reserved.pts != 0 {
                        return Err("GPU color frame PTS/slot mismatch".into());
                    }
                    drop(reserved);
                    // This is a diagnostic, with a completed synchronous GPU
                    // readback. NOT an encoder-completion fence.
                    pool.complete_encode(index, 0)?;
                    Ok(bytes)
                })();
                match converted {
                    Err(reason) => json!({
                        "schema": 1, "status": "gpu_video_conversion_failed",
                        "reason": reason, "adapter_name": pool.adapter_name(),
                        "production_export_changed": false,
                    }),
                    Ok(native_bytes) => {
                        let native = sample_patch_centers(&native_bytes, width, height);
                        let patches: Vec<Value> = PATCHES.iter().zip(native.iter())
                            .map(|((name, bgra), yuv)| json!({
                                "patch": name, "input_bgra": bgra,
                                "native_nv12_yuv": yuv,
                            })).collect();
                        let mut profiles = Vec::new();
                        let mut reference_error: Option<String> = None;
                        if let Some(ffmpeg_command) = ffmpeg.as_deref() {
                            for matrix in ["bt709", "bt601"] {
                                for range in ["pc", "tv"] {
                                    match run_ffmpeg(
                                        ffmpeg_command, &input, width, height,
                                        matrix, range,
                                    ) {
                                        Ok(frame) => {
                                            let reference = sample_patch_centers(&frame, width, height);
                                            let diffs: Vec<[u8; 3]> = native.iter()
                                                .zip(reference.iter())
                                                .map(|(a, b)| [
                                                    a[0].abs_diff(b[0]),
                                                    a[1].abs_diff(b[1]),
                                                    a[2].abs_diff(b[2]),
                                                ]).collect();
                                            let max_abs_delta = diffs.iter().flat_map(|d| d.iter())
                                                .copied().max().unwrap_or(0);
                                            let total: u32 = diffs.iter().flat_map(|d| d.iter())
                                                .map(|&d| u32::from(d)).sum();
                                            profiles.push(json!({
                                                "matrix": matrix, "range": range,
                                                "max_patch_center_channel_delta": max_abs_delta,
                                                "mean_patch_center_channel_delta": total as f64 / (PATCHES.len() * 3) as f64,
                                                "patch_center_yuv": reference,
                                                "patch_center_abs_deltas": diffs,
                                            }));
                                        }
                                        Err(e) => {
                                            reference_error = Some(e);
                                            break;
                                        }
                                    }
                                }
                                if reference_error.is_some() { break; }
                            }
                        }
                        let best = profiles.iter()
                            .min_by(|a, b| a["mean_patch_center_channel_delta"]
                                .as_f64().unwrap_or(f64::INFINITY)
                                .total_cmp(&b["mean_patch_center_channel_delta"]
                                    .as_f64().unwrap_or(f64::INFINITY)));
                        json!({
                            "schema": 1, "status": "measured",
                            "adapter_name": pool.adapter_name(),
                            "resolution": [width, height],
                            "gpu_conversion_and_diagnostic_readback_succeeded": true,
                            "patches": patches,
                            "ffmpeg_reference_requested": ffmpeg.is_some(),
                            "ffmpeg_reference_error": reference_error,
                            "ffmpeg_color_profiles": profiles,
                            "closest_ffmpeg_profile": best.map(|p| json!({
                                "matrix": p["matrix"], "range": p["range"],
                                "mean_patch_center_channel_delta": p["mean_patch_center_channel_delta"],
                                "max_patch_center_channel_delta": p["max_patch_center_channel_delta"],
                            })),
                            "patch_centers_within_3_codes_of_closest_ffmpeg_profile":
                                best.is_some_and(|p| p["max_patch_center_channel_delta"].as_u64()
                                    .is_some_and(|difference| difference <= 3)),
                            "full_frame_color_accuracy_verified": false,
                            "nvenc_interop_implemented": false,
                            "vulkan_wgpu_to_d3d11_interop_implemented": false,
                            "production_export_changed": false,
                        })
                    }
                }
            }
        },
    };
    let rendered = serde_json::to_string_pretty(&report)
        .expect("Serialize native GPU color diagnostic");
    println!("{rendered}");
    if let Some(path) = output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("Create report folder");
        }
        fs::write(path, rendered + "\n").expect("Write GPU color diagnostic");
    }
    if report["status"] == "gpu_video_conversion_failed" ||
        report["ffmpeg_reference_error"].as_str().is_some()
    {
        std::process::exit(1);
    }
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    fn patch_chart_is_even_chroma_aligned_at_default_size() {
        let (width, height) = (320usize, 180usize);
        for col in 0..4 {
            let x = ((col * width / 4 + (col + 1) * width / 4) / 2) & !1;
            assert_eq!(x % 2, 0);
            assert_eq!(x * 4 / width, col);
        }
        for row in 0..3 {
            let y = ((row * height / 3 + (row + 1) * height / 3) / 2) & !1;
            assert_eq!(y % 2, 0);
            assert_eq!(y * 3 / height, row);
        }
    }
}
