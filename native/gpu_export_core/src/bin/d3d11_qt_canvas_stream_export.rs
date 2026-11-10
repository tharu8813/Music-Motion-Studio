//! Persistent, bounded-memory Qt Canvas -> D3D11 -> NVENC H.264 export.
//!
//! Production uses --input-format binary-v1 (bounded MMSB frames on stdin).
//! Legacy CLI defaults to one UTF-8 MANIFEST PATH per line. Caller produces the
//! *complete* playlist's native BGRA manifests in sequential CFR order.
//! The process owns ONE D3D11/NVENC session and streams packets to disk.
//! It does not hold an entire video, all source packets or PTS arrays in RAM.
//! Cancellation: close/terminate the child; an incomplete .h264.part file is
//! removed on clean errors. A killed process may leave the .part file.
//!
//! Qt still rasterizes text and effect layers on CPU. This is the GPU
//! composition/encode backend, not a native replacement for Qt painting.

use std::io::BufRead;

const MAX_MANIFEST_LINE_BYTES: usize = 4096;
const MAX_EXPORT_SECONDS: f64 = 24.0 * 3600.0;
const MAX_EXPORT_FPS: u32 = 240;

fn read_manifest_line<R: BufRead>(reader: &mut R) -> Result<Option<String>, String> {
    let mut bytes = Vec::with_capacity(256);
    loop {
        let available = reader.fill_buf()
            .map_err(|e| format!("Cannot read native frame stream: {e}"))?;
        if available.is_empty() {
            return if bytes.is_empty() { Ok(None) }
                else { Err("Native frame path is not newline terminated".into()) };
        }
        let newline_at = available.iter().position(|&item| item == b'\n');
        let consumed = newline_at.map_or(available.len(), |index| index + 1);
        if bytes.len().saturating_add(consumed) > MAX_MANIFEST_LINE_BYTES {
            return Err("Native frame manifest path is too long".into());
        }
        bytes.extend_from_slice(&available[..consumed]);
        reader.consume(consumed);
        if newline_at.is_some() {
            bytes.pop(); // LF
            if bytes.last() == Some(&b'\r') { bytes.pop(); }
            let line = String::from_utf8(bytes)
                .map_err(|_| "Native frame path is not valid UTF-8")?;
            if line.trim().is_empty() || line.chars().any(|c| c == '\0') {
                return Err("Native frame path cannot be empty or contain NUL".into());
            }
            return Ok(Some(line));
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct FrameClock {
    expected: u64,
    submitted: u64,
    completed: u64,
}

impl FrameClock {
    fn new(expected: u64, fps: u32, seconds: f64) -> Result<Self, String> {
        if !(1..=MAX_EXPORT_FPS).contains(&fps)
            || !seconds.is_finite() || !(0.0..=MAX_EXPORT_SECONDS).contains(&seconds)
            || seconds <= 0.0
        {
            return Err("Native export needs 1..240 FPS and 0..24 hours".into());
        }
        let calculated = (seconds * f64::from(fps) - 1e-9).ceil() as u64;
        if calculated == 0 || expected != calculated {
            return Err(format!(
                "Expected CFR frames {expected} differ from duration/FPS {calculated}"
            ));
        }
        Ok(Self { expected, submitted: 0, completed: 0 })
    }

    fn next_submission(&mut self) -> Result<i64, String> {
        if self.submitted >= self.expected {
            return Err("More Canvas source frames than the compiled timeline".into());
        }
        let pts = i64::try_from(self.submitted)
            .map_err(|_| "Native export PTS overflow")?;
        self.submitted += 1;
        Ok(pts)
    }

    fn confirm_encoded(&mut self, pts: u64) -> Result<(), String> {
        if pts != self.completed || self.completed >= self.submitted {
            return Err("Native export lost, reordered or duplicated an NVENC frame".into());
        }
        self.completed += 1;
        Ok(())
    }

    fn finish(self) -> Result<(), String> {
        if self.submitted != self.expected || self.completed != self.expected {
            return Err(format!(
                "Incomplete native export: {} submitted, {} encoded, {} required",
                self.submitted, self.completed, self.expected
            ));
        }
        Ok(())
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("Native streaming D3D11/NVENC export requires Windows");
    std::process::exit(2);
}

#[cfg(windows)]
mod windows_export {
    use super::{read_manifest_line, FrameClock};
    use music_motion_gpu_core::d3d11_bgra_video::{
        NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer,
    };
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_nvenc_registration::NativeNvencRegistrationSession;
    use music_motion_gpu_core::qt_canvas_packet::{load_qt_canvas_packet, CanvasPacketFrame};
    use music_motion_gpu_core::canvas_stream::{read_frame, read_frame_reuse};
    use music_motion_gpu_core::resident_layer_cache::ResidentLayerCache;
    use music_motion_gpu_core::d3d11_affine::AffineLayerRenderer;
    use serde_json::{json, Value};
    use std::collections::VecDeque;
    use std::env;
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, BufWriter, Write};
    use std::path::{Path, PathBuf};
    use std::time::{Duration, Instant};

    struct Options {
        width: u32,
        height: u32,
        fps: u32,
        frames: u64,
        seconds: f64,
        slots: usize,
        video: PathBuf,
        report: PathBuf,
        binary: bool,
        binary_v2: bool,
    }

    fn parse_options() -> Result<Options, String> {
        let mut width = None;
        let mut height = None;
        let mut fps = None;
        let mut frames = None;
        let mut seconds = None;
        let mut slots = 2usize;
        let mut video = None;
        let mut report = None;
        let mut binary = false;
        let mut binary_v2 = false;
        let mut arguments = env::args().skip(1);
        while let Some(option) = arguments.next() {
            let value = arguments.next()
                .ok_or_else(|| format!("Missing value for {option}"))?;
            match option.as_str() {
                "--width" => width = Some(value.parse::<u32>()
                    .map_err(|_| "Invalid width")?),
                "--height" => height = Some(value.parse::<u32>()
                    .map_err(|_| "Invalid height")?),
                "--fps" => fps = Some(value.parse::<u32>()
                    .map_err(|_| "Invalid FPS")?),
                "--expected-frames" => frames = Some(value.parse::<u64>()
                    .map_err(|_| "Invalid expected frame count")?),
                "--duration-seconds" => seconds = Some(value.parse::<f64>()
                    .map_err(|_| "Invalid duration")?),
                "--slots" => slots = value.parse::<usize>()
                    .map_err(|_| "Invalid NV12 frame pool slot count")?,
                "--video-output" => video = Some(PathBuf::from(value)),
                "--output" => report = Some(PathBuf::from(value)),
                "--input-format" => match value.as_str() {
                    "binary-v1" => binary = true,
                    "binary-v2" => { binary = true; binary_v2 = true; },
                    "manifest" => binary = false,
                    _ => return Err("Unsupported frame input format".into()),
                },
                _ => return Err(format!("Unknown native export option: {option}")),
            }
        }
        let options = Options {
            width: width.ok_or("Missing --width")?,
            height: height.ok_or("Missing --height")?,
            fps: fps.ok_or("Missing --fps")?,
            frames: frames.ok_or("Missing --expected-frames")?,
            seconds: seconds.ok_or("Missing --duration-seconds")?,
            slots,
            video: video.ok_or("Missing --video-output")?,
            report: report.ok_or("Missing --output")?,
            binary,
            binary_v2,
        };
        if options.width < 64 || options.width > 3840
            || options.height < 64 || options.height > 2160
            || options.width % 2 != 0 || options.height % 2 != 0
            || !(2..=8).contains(&options.slots)
        {
            return Err("Production D3D11 needs even 64..3840x64..2160 dimensions and 2..8 slots".into());
        }
        FrameClock::new(options.frames, options.fps, options.seconds)?;
        if options.video.extension().and_then(|x| x.to_str()) != Some("h264")
            || options.report.extension().and_then(|x| x.to_str()) != Some("json")
            || options.video == options.report
        {
            return Err("Native export needs distinct .h264 video and .json report paths".into());
        }
        if options.video.exists() || options.report.exists() {
            return Err("Native export refuses to overwrite an existing video or report".into());
        }
        Ok(options)
    }

    /// Retains only a small buffered writer, never all NVENC packets in RAM.
    /// Rust Drop removes the staging file on every ordinary error path.
    struct StagedVideo {
        partial: PathBuf,
        writer: Option<BufWriter<File>>,
        bytes_written: u64,
    }
    impl StagedVideo {
        fn open(video: &Path) -> Result<Self, String> {
            if let Some(parent) = video.parent().filter(|p| !p.as_os_str().is_empty()) {
                fs::create_dir_all(parent)
                    .map_err(|e| format!("Cannot prepare native video folder: {e}"))?;
            }
            let partial = video.with_extension("h264.part");
            let file = OpenOptions::new().write(true).create_new(true)
                .open(&partial)
                .map_err(|e| format!("Cannot create native H264 staging file: {e}"))?;
            Ok(Self {
                partial, writer: Some(BufWriter::with_capacity(256 * 1024, file)),
                bytes_written: 0,
            })
        }
        fn write_packet(&mut self, packet: &[u8]) -> Result<(), String> {
            if packet.is_empty() { return Err("NVENC returned an empty H264 packet".into()); }
            self.bytes_written = self.bytes_written.checked_add(packet.len() as u64)
                .ok_or("Native H264 output size overflow")?;
            self.writer.as_mut().ok_or("Native H264 writer closed")?
                .write_all(packet).map_err(|e| format!("Cannot write H264: {e}"))
        }
        fn publish(&mut self, video: &Path) -> Result<(), String> {
            let mut writer = self.writer.take().ok_or("Native H264 writer missing")?;
            writer.flush().map_err(|e| format!("H264 flush failed: {e}"))?;
            writer.get_ref().sync_all().map_err(|e| format!("H264 sync failed: {e}"))?;
            drop(writer);
            if video.exists() {
                return Err("Refusing to overwrite newly created target H264".into());
            }
            fs::rename(&self.partial, video)
                .map_err(|e| format!("Cannot publish completed H264 video: {e}"))?;
            Ok(())
        }
    }
    impl Drop for StagedVideo {
        fn drop(&mut self) {
            self.writer.take();
            let _ = fs::remove_file(&self.partial);
        }
    }

    fn consume(
        pool: &mut NativeNv12FramePool,
        encoder: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize, i64)>,
        writer: &mut StagedVideo,
        clock: &mut FrameClock,
    ) -> Result<(), String> {
        let &(slot, expected_pts) = pending.front().ok_or("No queued native video frame")?;
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        let ready = pool.acquire_gpu_complete_ready()?
            .ok_or("Native D3D11 frame was not ready for NVENC")?;
        if ready.slot_index != slot || ready.pts != expected_pts {
            return Err("Native D3D11 completion order or slot mismatch".into());
        }
        let (packet, actual_pts) = encoder.encode_gpu_complete_frame(&ready)?;
        if actual_pts != expected_pts as u64 {
            return Err("NVENC picture presentation timestamp mismatch".into());
        }
        writer.write_packet(&packet)?;
        clock.confirm_encoded(actual_pts)?;
        drop(ready);
        pool.complete_encode(slot, expected_pts)?;
        pending.pop_front();
        Ok(())
    }

    fn next_input_frame(
        input: &mut std::io::StdinLock<'_>, index: u64,
        previous: Option<&mut CanvasPacketFrame>, options: &Options,
    ) -> Result<Option<CanvasPacketFrame>, String> {
        if options.binary_v2 { read_frame_reuse(input, index, previous) }
        else if options.binary { read_frame(input, index) }
        else {
            read_manifest_line(input)?
                .map(|name| load_qt_canvas_packet(Path::new(&name))).transpose()
        }
    }

    fn export(options: Options) -> Result<Value, String> {
        let started = Instant::now();
        let mut clock = FrameClock::new(options.frames, options.fps, options.seconds)?;
        let stdin = io::stdin();
        let mut input = stdin.lock();
        let mut writer = StagedVideo::open(&options.video)?;

        // The named helper below returns an owned packet, with no lifetime
        // tied to its previous-frame input borrow. v1 never retains frames.
        let mut transfer_seconds = 0.0;
        let transfer_started = Instant::now();
        let frame = next_input_frame(&mut input, 0, None, &options)?
            .ok_or("Native producer closed without any Canvas frames")?;
        transfer_seconds += transfer_started.elapsed().as_secs_f64();
        if (frame.width, frame.height) != (options.width, options.height) {
            return Err("First Canvas packet dimensions differ from export settings".into());
        }
        let mut pool = NativeNv12FramePool::new(options.width, options.height, options.slots)?;
        let processor = NativeBgraNv12VideoProcessor::new_for_pool(&pool)?;
        let mut resident = ResidentLayerCache::new(options.width, options.height);
        let mut affine = AffineLayerRenderer::new(&pool)?;
        let mut encoder = NativeNvencRegistrationSession::new_for_pool_with_fps(
            &pool, options.fps,
        )?;
        let adapter = pool.adapter_name().to_string();
        let mut pending: VecDeque<(usize, i64)> = VecDeque::with_capacity(options.slots);
        let mut current = frame;
        loop {
            if pending.len() == options.slots {
                consume(&mut pool, &mut encoder, &mut pending, &mut writer, &mut clock)?;
            }
            if current.width != options.width || current.height != options.height {
                return Err("Canvas packet dimensions changed mid-export".into());
            }
            let pts = clock.next_submission()?;
            resident.update_sized(&current,
                |w,h| affine.create_input(w,h),
                |texture,pixels,width| affine.upload(texture,pixels,width),
            )?;
            affine.prepare(&current, &resident)?;
            let layers = current.layers.iter().map(|source| {
                Ok(NativeCanvasBgraLayer {
                    texture: if source.geometry.is_identity(options.width, options.height) {
                        resident.resource(&source.stream_key)?
                    } else { affine.output(&source.stream_key)? }, opacity: source.opacity,
                })
            }).collect::<Result<Vec<_>, String>>()?;
            let slot = pool.queue_gpu_canvas_layers(&processor, &layers, pts)?;
            pending.push_back((slot, pts));
            // Keep an actual maximum of ONE decoded CPU packet in memory.
            // Resident input textures are reused on the same immediate context;
            // prior composition commands precede the next dirty upload.
            drop(layers);
            // The previous packet remains owned until the next v2 read has
            // validated its reference metadata and moved matching planes.
            // No old frame pixels are accessed after GPU submission.
            // Bounded producer/consumer handshake: the caller may release
            // THIS frame's bytes (or legacy files) only after
            // native loading, texture upload and GPU submission completed.
            // A parent waiting for this line will not stage a whole video.
            {
                let mut output = io::stdout().lock();
                writeln!(output, "MMS_FRAME_ACCEPTED {}", pts)
                    .and_then(|_| output.flush())
                    .map_err(|e| format!("Native frame acceptance channel closed: {e}"))?;
            }
            if clock.submitted == clock.expected {
                let unexpected = if options.binary_v2 {
                    next_input_frame(&mut input, clock.submitted, Some(&mut current), &options)?
                } else {
                    drop(current);
                    next_input_frame(&mut input, clock.submitted, None, &options)?
                };
                if unexpected.is_some() {
                    return Err("More Canvas packets supplied than compiled playlist frames".into());
                }
                break;
            }
            let transfer_started = Instant::now();
            let next = if options.binary_v2 {
                next_input_frame(&mut input, clock.submitted, Some(&mut current), &options)?
            } else {
                drop(current);
                next_input_frame(&mut input, clock.submitted, None, &options)?
            };
            current = next.ok_or("Native Canvas source ended before the full playlist")?;
            transfer_seconds += transfer_started.elapsed().as_secs_f64();
        }
        while !pending.is_empty() {
            consume(&mut pool, &mut encoder, &mut pending, &mut writer, &mut clock)?;
        }
        clock.finish()?;
        resident.clear();
        affine.clear();
        if pool.completed_count() != options.frames
            || encoder.successful_map_count() != options.frames as usize
        {
            return Err("Native D3D11/NVENC completion counters did not match".into());
        }
        let bytes = writer.bytes_written;
        writer.publish(&options.video)?;
        let result = json!({
            "schema": 1,
            "status": "measured",
            "production_export_scope": "production_complete_playlist",
            "production_export_changed": true,
            "source": "real_qt_canvas_snapshot_cpu_raster_packets",
            "adapter_name": adapter,
            "nvenc_api_version": encoder.negotiated_api_version(),
            "resolution": [options.width, options.height],
            "fps": options.fps,
            "duration_seconds": options.seconds,
            "expected_frames": options.frames,
            "frames_encoded": options.frames,
            "pts_monotonic": true,
            "compressed_h264_bytes": bytes,
            "elapsed_seconds": started.elapsed().as_secs_f64(),
            "h264_file": options.video.display().to_string(),
            "d3d11_alpha_composition_on_gpu": true,
            "d3d11_affine_shader_draws": affine.draws,
            "d3d11_exact_roi_gpu_copies": affine.exact_roi_copies,
            "affine_output_textures": {"allocations":affine.allocations,"releases":affine.releases,"peak_bytes":affine.peak_output_bytes},
            "canvas_packet_schemas": [1,2],
            "gpu_nv12_to_cpu_readbacks": 0,
            "nvenc_output_synchronously_completed": true,
            "qt_canvas_rasterized_on_cpu": true,
            "native_gpu_text_painting_implemented": false,
            "max_pending_gpu_slots": options.slots,
            "input": if options.binary_v2 { "binary-v2" } else if options.binary { "binary-v1" } else { "newline_separated_manifest_paths_stdin" },
            "frame_receive_seconds": transfer_seconds,
            "bgra_disk_bytes": if options.binary { Some(0u64) } else { None },
            "resident_layer_cache": {
                "allocations": resident.counters.allocations,
                "uploads": resident.counters.uploads,
                "upload_bytes": resident.counters.upload_bytes,
                "digest_reuse_bytes": resident.counters.digest_reuse_bytes,
                "reuse_hits": resident.counters.reuse_hits,
                "releases": resident.counters.releases,
                "peak_resident_bytes": resident.counters.peak_resident_bytes,
                "pixel_identity": "sha256_cpu",
            },
        });
        // A failed report means no completed deliverable. If the runner
        // closes/crashes mid-export, no report is emitted at all.
        let result_text = serde_json::to_string_pretty(&result)
            .map_err(|e| format!("Cannot serialize GPU export report: {e}"))?;
        let mut report_created = false;
        let report_write = (|| -> Result<(), String> {
            if let Some(parent) = options.report.parent()
                .filter(|p| !p.as_os_str().is_empty()) {
                fs::create_dir_all(parent)
                    .map_err(|e| format!("Cannot create report directory: {e}"))?;
            }
            let mut file = OpenOptions::new().write(true).create_new(true)
                .open(&options.report)
                .map_err(|e| format!("Cannot create GPU export report: {e}"))?;
            report_created = true;
            file.write_all(result_text.as_bytes()).and_then(|_| file.write_all(b"\n"))
                .and_then(|_| file.sync_all())
                .map_err(|e| format!("Cannot write GPU export report: {e}"))
        })();
        if let Err(reason) = report_write {
            if report_created {
                let _ = fs::remove_file(&options.report);
            }
            let _ = fs::remove_file(&options.video);
            return Err(reason);
        }
        Ok(result)
    }

    pub fn run() -> Result<(), String> {
        let options = parse_options()?;
        let report = export(options)?;
        println!("{}", serde_json::to_string_pretty(&report)
            .map_err(|e| e.to_string())?);
        Ok(())
    }
}

#[cfg(windows)]
fn main() {
    if let Err(problem) = windows_export::run() {
        eprintln!("Native production NVENC export failed: {problem}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::{read_manifest_line, FrameClock, MAX_MANIFEST_LINE_BYTES};
    use std::io::Cursor;

    #[test]
    fn native_manifest_paths_are_bounded_and_newline_framed() {
        let mut input = Cursor::new(b"C:\\media\\first.json\r\nnext.json\n".as_slice());
        assert_eq!(read_manifest_line(&mut input).unwrap(),
                   Some("C:\\media\\first.json".into()));
        assert_eq!(read_manifest_line(&mut input).unwrap(),
                   Some("next.json".into()));
        assert_eq!(read_manifest_line(&mut input).unwrap(), None);
        assert!(read_manifest_line(&mut Cursor::new(b"unterminated".as_slice())).is_err());
        assert!(read_manifest_line(&mut Cursor::new(b"\n".as_slice())).is_err());
        let long = format!("{}\n", "A".repeat(MAX_MANIFEST_LINE_BYTES));
        assert!(read_manifest_line(&mut Cursor::new(long.as_bytes())).is_err());
    }

    #[test]
    fn production_clock_accepts_long_videos_without_pts_buffer() {
        let mut clock = FrameClock::new(216_000, 60, 3600.0).unwrap();
        for pt in 0..216_000u64 {
            assert_eq!(clock.next_submission().unwrap(), pt as i64);
            clock.confirm_encoded(pt).unwrap();
        }
        assert!(clock.next_submission().is_err());
        clock.finish().unwrap();
    }

    #[test]
    fn clock_rejects_missing_duplicates_reorder_and_bad_duration() {
        let mut clock = FrameClock::new(150, 30, 5.0).unwrap();
        let first = clock.next_submission().unwrap();
        assert_eq!(first, 0);
        assert!(clock.confirm_encoded(1).is_err());
        clock.confirm_encoded(0).unwrap();
        assert!(clock.confirm_encoded(0).is_err());
        assert!(clock.finish().is_err());
        assert!(FrameClock::new(30, 60, 1.0).is_err());
        assert!(FrameClock::new(60, 60, f64::NAN).is_err());
        assert!(FrameClock::new(60, 0, 1.0).is_err());
        assert!(FrameClock::new(60, 241, 1.0).is_err());
    }
}
