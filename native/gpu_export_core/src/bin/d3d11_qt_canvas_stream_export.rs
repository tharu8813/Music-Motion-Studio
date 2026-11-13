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
    use super::{read_manifest_line, FrameClock, MAX_EXPORT_FPS};
    use music_motion_gpu_core::d3d11_bgra_video::{
        NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer,
    };
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_nvenc_registration::NativeNvencRegistrationSession;
    use music_motion_gpu_core::qt_canvas_packet::{load_qt_canvas_packet, CanvasPacketFrame, VideoRequest, CANVAS_PACKET_SCHEMAS};
    use music_motion_gpu_core::canvas_stream::{read_frame, read_frame_reuse, read_frame_adaptive, read_frame_parallel};
    use music_motion_gpu_core::resident_layer_cache::ResidentLayerCache;
    use music_motion_gpu_core::d3d11_affine::AffineLayerRenderer;
    use music_motion_gpu_core::d3d11_video_source::{GpuVideoSource, VideoDecodeRuntime};
    use std::collections::HashMap;
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};
    use std::collections::VecDeque;
    use std::env;
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, BufWriter, Read, Write};
    use std::path::{Path, PathBuf};
    use std::time::{Duration, Instant};
    use windows::core::{HSTRING, Interface};
    use windows::Win32::Foundation::CloseHandle;
    use windows::Win32::Graphics::Direct3D11::{ID3D11Texture2D, ID3D11RenderTargetView};
    use windows::Win32::Graphics::Dxgi::{
        CreateDXGIFactory1, IDXGIFactory1, DXGI_ADAPTER_FLAG_SOFTWARE, DXGI_ERROR_NOT_FOUND,
    };
    use windows::Win32::System::Memory::{
        MapViewOfFile, OpenFileMappingW, UnmapViewOfFile, FILE_MAP_READ,
        MEMORY_MAPPED_VIEW_ADDRESS,
    };

    const SHARED_PACKET_CAPACITY: usize = 32 * 1024 * 1024;

    struct SharedPacketMap(MEMORY_MAPPED_VIEW_ADDRESS);

    impl SharedPacketMap {
        fn open(name: &str) -> Result<Self, String> {
            let handle = unsafe { OpenFileMappingW(FILE_MAP_READ.0, false, &HSTRING::from(name)) }
                .map_err(|e| format!("Cannot open shared Canvas packet mapping: {e}"))?;
            let view = unsafe { MapViewOfFile(handle, FILE_MAP_READ, 0, 0, SHARED_PACKET_CAPACITY) };
            let map_error = view.Value.is_null().then(io::Error::last_os_error);
            let _ = unsafe { CloseHandle(handle) };
            if let Some(error) = map_error {
                return Err(format!("Cannot map shared Canvas packet: {error}"));
            }
            Ok(Self(view))
        }
    }

    impl Drop for SharedPacketMap {
        fn drop(&mut self) {
            let _ = unsafe { UnmapViewOfFile(self.0) };
        }
    }

    #[derive(Clone, Copy)]
    struct EncoderOptions {
        width: u32,
        height: u32,
        fps: u32,
        slots: usize,
        nvenc_preset: u8,
        nvenc_cq: u8,
        adapter_luid: Option<u64>,
    }

    struct Options {
        encoding: EncoderOptions,
        frames: u64,
        seconds: f64,
        video: PathBuf,
        report: PathBuf,
        binary: bool,
        binary_v2: bool,
        binary_v3: bool,
        shared_packet_map: Option<String>,
        parallel_packet_validation: bool,
        video_sources: Vec<PathBuf>,
    }

    enum Command {
        Probe(EncoderOptions, Vec<PathBuf>),
        Export(Options),
    }

    fn parse_options(mut arguments: impl Iterator<Item = String>) -> Result<Command, String> {
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
        let mut binary_v3 = false;
        let mut shared_packet_map = None;
        let mut parallel_packet_validation = false;
        let mut nvenc_preset = 3;
        let mut nvenc_cq = 0;
        let mut adapter_luid = None;
        let mut probe = false;
        let mut video_sources = Vec::new();
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
                "--probe" if value == "1" => probe = true,
                "--video-source" if video_sources.len() < 7 => video_sources.push(PathBuf::from(value)),
                "--adapter-luid" => adapter_luid = Some(value.parse::<u64>()
                    .map_err(|_| "Invalid GPU adapter LUID")?),
                "--expected-frames" => frames = Some(value.parse::<u64>()
                    .map_err(|_| "Invalid expected frame count")?),
                "--duration-seconds" => seconds = Some(value.parse::<f64>()
                    .map_err(|_| "Invalid duration")?),
                "--slots" => slots = value.parse::<usize>()
                    .map_err(|_| "Invalid NV12 frame pool slot count")?,
                "--video-output" => video = Some(PathBuf::from(value)),
                "--output" => report = Some(PathBuf::from(value)),
                "--shared-packet-map" => shared_packet_map = Some(value),
                "--parallel-packet-validation" if value == "1" => parallel_packet_validation = true,
                "--nvenc-preset" => nvenc_preset = match value.as_str() {
                    "p1" => 1,
                    "p3" => 3,
                    "p5" => 5,
                    _ => return Err("NVENC preset must be p1, p3 or p5".into()),
                },
                "--nvenc-cq" => nvenc_cq = value.parse::<u8>().ok()
                    .filter(|cq| *cq <= 51).ok_or("NVENC quality must be between 0 and 51")?,
                "--input-format" => match value.as_str() {
                    "binary-v1" => binary = true,
                    "binary-v2" => { binary = true; binary_v2 = true; },
                    "binary-v3" => { binary = true; binary_v3 = true; },
                    "manifest" => binary = false,
                    _ => return Err("Unsupported frame input format".into()),
                },
                _ => return Err(format!("Unknown native export option: {option}")),
            }
        }
        let encoding = EncoderOptions {
            width: width.ok_or("Missing --width")?,
            height: height.ok_or("Missing --height")?,
            fps: fps.ok_or("Missing --fps")?,
            slots,
            nvenc_preset,
            nvenc_cq,
            adapter_luid,
        };
        if encoding.width < 64 || encoding.width > 3840
            || encoding.height < 64 || encoding.height > 2160
            || encoding.width % 2 != 0 || encoding.height % 2 != 0
            || !(2..=8).contains(&encoding.slots)
        {
            return Err("Production D3D11 needs even 64..3840x64..2160 dimensions and 2..8 slots".into());
        }
        if !(1..=MAX_EXPORT_FPS).contains(&encoding.fps) {
            return Err("Invalid native export FPS".into());
        }
        if probe {
            return Ok(Command::Probe(encoding, video_sources));
        }
        let options = Options {
            encoding,
            frames: frames.ok_or("Missing --expected-frames")?,
            seconds: seconds.ok_or("Missing --duration-seconds")?,
            video: video.ok_or("Missing --video-output")?,
            report: report.ok_or("Missing --output")?,
            binary,
            binary_v2,
            binary_v3,
            shared_packet_map,
            parallel_packet_validation,
            video_sources,
        };
        FrameClock::new(options.frames, encoding.fps, options.seconds)?;
        if options.shared_packet_map.is_some() && !options.binary {
            return Err("Shared Canvas packets require a binary input format".into());
        }
        if options.parallel_packet_validation && !(options.binary_v2 || options.binary_v3) {
            return Err("Parallel Canvas validation requires binary-v2 or binary-v3 input".into());
        }
        if options.video.extension().and_then(|x| x.to_str()) != Some("h264")
            || options.report.extension().and_then(|x| x.to_str()) != Some("json")
            || options.video == options.report
        {
            return Err("Native export needs distinct .h264 video and .json report paths".into());
        }
        if options.video.exists() || options.report.exists() {
            return Err("Native export refuses to overwrite an existing video or report".into());
        }
        Ok(Command::Export(options))
    }

    struct EncoderResources {
        encoder: NativeNvencRegistrationSession,
        affine: AffineLayerRenderer,
        processor: NativeBgraNv12VideoProcessor,
        pool: NativeNv12FramePool,
        adapter_luid: u64,
    }

    struct VideoLayer<'runtime> {
        decoder: GpuVideoSource<'runtime>,
        texture: ID3D11Texture2D,
        view: ID3D11RenderTargetView,
        clip: [u32; 4],
        placement: VideoRequest,
        last_pts: Option<i64>,
    }

    fn open_encoder(options: EncoderOptions) -> Result<EncoderResources, String> {
        let factory: IDXGIFactory1 = unsafe { CreateDXGIFactory1() }
            .map_err(|e| format!("Cannot enumerate DXGI adapters: {e}"))?;
        let mut last_error = if options.adapter_luid.is_some() {
            "The selected NVIDIA GPU is no longer available".to_owned()
        } else {
            "No hardware NVIDIA GPU was found".to_owned()
        };
        for index in 0.. {
            let adapter = match unsafe { factory.EnumAdapters1(index) } {
                Ok(adapter) => adapter,
                Err(error) if error.code() == DXGI_ERROR_NOT_FOUND => break,
                Err(error) => return Err(format!("DXGI adapter enumeration failed: {error}")),
            };
            let desc = unsafe { adapter.GetDesc1() }
                .map_err(|e| format!("Cannot identify DXGI adapter {index}: {e}"))?;
            let luid = (u64::from(desc.AdapterLuid.HighPart as u32) << 32)
                | u64::from(desc.AdapterLuid.LowPart);
            if desc.VendorId != 0x10de || desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32 != 0
                || options.adapter_luid.is_some_and(|selected| selected != luid)
            {
                continue;
            }
            let opened: Result<EncoderResources, String> = (|| {
                let adapter = adapter.cast().map_err(|e| format!("Cannot open DXGI adapter: {e}"))?;
                let pool = NativeNv12FramePool::new_for_adapter(
                    options.width, options.height, options.slots, Some(&adapter),
                )?;
                let encoder = NativeNvencRegistrationSession::new_for_pool_with_encoding(
                    &pool, options.fps, options.nvenc_preset, options.nvenc_cq,
                )?;
                let processor = NativeBgraNv12VideoProcessor::new_for_pool(&pool)?;
                let affine = AffineLayerRenderer::new(&pool)?;
                Ok(EncoderResources { encoder, affine, processor, pool, adapter_luid: luid })
            })();
            match opened {
                Ok(resources) => return Ok(resources),
                Err(error) => last_error = format!("NVIDIA adapter {luid} could not start: {error}"),
            }
        }
        Err(last_error)
    }

    /// Retains only a small buffered writer, never all NVENC packets in RAM.
    /// Rust Drop removes the staging file on every ordinary error path.
    struct StagedVideo {
        partial: PathBuf,
        writer: Option<BufWriter<File>>,
        bytes_written: u64,
        digest: Sha256,
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
                digest: Sha256::new(),
            })
        }
        fn write_packet(&mut self, packet: &[u8]) -> Result<(), String> {
            if packet.is_empty() { return Err("NVENC returned an empty H264 packet".into()); }
            self.bytes_written = self.bytes_written.checked_add(packet.len() as u64)
                .ok_or("Native H264 output size overflow")?;
            self.writer.as_mut().ok_or("Native H264 writer closed")?
                .write_all(packet).map_err(|e| format!("Cannot write H264: {e}"))?;
            self.digest.update(packet);
            Ok(())
        }
        fn publish(&mut self, video: &Path) -> Result<String, String> {
            let mut writer = self.writer.take().ok_or("Native H264 writer missing")?;
            writer.flush().map_err(|e| format!("H264 flush failed: {e}"))?;
            writer.get_ref().sync_all().map_err(|e| format!("H264 sync failed: {e}"))?;
            drop(writer);
            if video.exists() {
                return Err("Refusing to overwrite newly created target H264".into());
            }
            fs::rename(&self.partial, video)
                .map_err(|e| format!("Cannot publish completed H264 video: {e}"))?;
            Ok(format!("{:x}", std::mem::take(&mut self.digest).finalize()))
        }
    }
    impl Drop for StagedVideo {
        fn drop(&mut self) {
            self.writer.take();
            let _ = fs::remove_file(&self.partial);
        }
    }

    #[derive(Default)]
    struct StageTimings {
        gpu_submit: f64,
        gpu_wait: f64,
        nvenc: f64,
        shared_packet_decode: f64,
    }

    fn consume(
        pool: &mut NativeNv12FramePool,
        encoder: &mut NativeNvencRegistrationSession,
        pending: &mut VecDeque<(usize, i64)>,
        writer: &mut StagedVideo,
        clock: &mut FrameClock,
        timings: &mut StageTimings,
    ) -> Result<(), String> {
        let &(slot, expected_pts) = pending.front().ok_or("No queued native video frame")?;
        let waiting = Instant::now();
        pool.wait_for_gpu_copy(slot, Duration::from_secs(15))?;
        timings.gpu_wait += waiting.elapsed().as_secs_f64();
        let ready = pool.acquire_gpu_complete_ready()?
            .ok_or("Native D3D11 frame was not ready for NVENC")?;
        if ready.slot_index != slot || ready.pts != expected_pts {
            return Err("Native D3D11 completion order or slot mismatch".into());
        }
        let encoding = Instant::now();
        let (packet, actual_pts) = encoder.encode_gpu_complete_frame(&ready)?;
        timings.nvenc += encoding.elapsed().as_secs_f64();
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

    // Both v2 and adaptive-LZ4 v3 can reference the immediately preceding
    // decoded frame; keep it alive after GPU submission until the next read.
    fn needs_previous_packet(binary_v2: bool, binary_v3: bool) -> bool {
        binary_v2 || binary_v3
    }

    fn read_binary_frame(
        input: &mut impl Read, index: u64,
        previous: Option<&mut CanvasPacketFrame>, options: &Options,
    ) -> Result<Option<CanvasPacketFrame>, String> {
        if options.parallel_packet_validation { read_frame_parallel(input, index, previous, options.binary_v3) }
        else if options.binary_v3 { read_frame_adaptive(input, index, previous) }
        else if options.binary_v2 { read_frame_reuse(input, index, previous) }
        else { read_frame(input, index) }
    }

    fn read_shared_length(input: &mut impl Read) -> Result<Option<usize>, String> {
        let mut length = [0u8; 4];
        loop {
            match input.read(&mut length[..1]) {
                Ok(0) => return Ok(None),
                Ok(_) => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(format!("Shared packet length: {e}")),
            }
        }
        input.read_exact(&mut length[1..]).map_err(|e| format!("Truncated shared packet length: {e}"))?;
        let length = u32::from_le_bytes(length) as usize;
        if length > SHARED_PACKET_CAPACITY {
            return Err("Shared Canvas packet exceeds mapping capacity".into());
        }
        Ok(Some(length))
    }

    fn next_input_frame(
        input: &mut std::io::StdinLock<'_>, index: u64,
        previous: Option<&mut CanvasPacketFrame>, options: &Options,
        mapping: Option<&SharedPacketMap>,
        timings: &mut StageTimings,
    ) -> Result<Option<CanvasPacketFrame>, String> {
        if let Some(mapping) = mapping {
            let Some(length) = read_shared_length(input)? else { return Ok(None); };
            if length == 0 {
                return read_binary_frame(input, index, previous, options)?
                    .map(Some).ok_or_else(|| "Missing inline Canvas packet".into());
            }
            // The sender publishes the length after writing the view and waits for
            // this frame's ACK. The parser returns only owned planes before ACK.
            let mut packet = unsafe { std::slice::from_raw_parts(mapping.0.Value.cast::<u8>(), length) };
            let started = Instant::now();
            let decoded = read_binary_frame(&mut packet, index, previous, options)?;
            timings.shared_packet_decode += started.elapsed().as_secs_f64();
            if !packet.is_empty() || decoded.is_none() {
                return Err("Shared Canvas packet length does not match the frame".into());
            }
            return Ok(decoded);
        }
        if options.binary {
            return read_binary_frame(input, index, previous, options);
        }
        read_manifest_line(input)?
            .map(|name| load_qt_canvas_packet(Path::new(&name))).transpose()
    }

    fn export(options: Options) -> Result<Value, String> {
        let started = Instant::now();
        let EncoderOptions { width, height, fps, slots, nvenc_preset, nvenc_cq, .. } = options.encoding;
        let mut clock = FrameClock::new(options.frames, fps, options.seconds)?;
        let EncoderResources { mut pool, processor, mut affine, mut encoder, adapter_luid } =
            open_encoder(options.encoding)?;
        let stdin = io::stdin();
        let mut input = stdin.lock();
        let mut writer = StagedVideo::open(&options.video)?;
        let mapping = options.shared_packet_map.as_deref().map(SharedPacketMap::open).transpose()?;
        let mapping = mapping.as_ref();

        // The named helper below returns an owned packet, with no lifetime
        // tied to its previous-frame input borrow. v1 never retains frames.
        let mut transfer_seconds = 0.0;
        let mut timings = StageTimings::default();
        let transfer_started = Instant::now();
        let frame = next_input_frame(&mut input, 0, None, &options, mapping, &mut timings)?
            .ok_or("Native producer closed without any Canvas frames")?;
        transfer_seconds += transfer_started.elapsed().as_secs_f64();
        if (frame.width, frame.height) != (width, height) {
            return Err("First Canvas packet dimensions differ from export settings".into());
        }
        let mut resident = ResidentLayerCache::new(width, height);
        let runtime = if options.video_sources.is_empty() { None } else { Some(VideoDecodeRuntime::new(&pool)?) };
        let mut videos: HashMap<usize, VideoLayer> = HashMap::new();
        let mut video_frames = 0u64;
        let mut video_decode_seconds = 0.0;
        let adapter = pool.adapter_name().to_string();
        let mut pending: VecDeque<(usize, i64)> = VecDeque::with_capacity(slots);
        let mut current = frame;
        loop {
            if pending.len() == slots {
                consume(&mut pool, &mut encoder, &mut pending, &mut writer, &mut clock, &mut timings)?;
            }
            if current.width != width || current.height != height {
                return Err("Canvas packet dimensions changed mid-export".into());
            }
            let pts = clock.next_submission()?;
            let submit_started = Instant::now();
            resident.update_sized(&current,
                |w,h| affine.create_input(w,h),
                |texture,pixels,width| affine.upload(texture,pixels,width),
            )?;
            affine.prepare(&current, &resident)?;
            let mut active_video = [false; 8];
            let mut seen_video = [false; 8];
            let video_started = Instant::now();
            for layer in &current.layers {
                let Some(request) = layer.video else { continue; };
                let path = options.video_sources.get(request.asset).ok_or("Unknown native video asset")?;
                if seen_video[request.asset] {
                    return Err("A native video asset was used more than once in one frame".into());
                }
                seen_video[request.asset] = true;
                let Some(time) = request.media_time else { continue; };
                let [x, y, r, b] = layer.geometry.clip;
                let entry = match videos.entry(request.asset) {
                    std::collections::hash_map::Entry::Occupied(entry) => entry.into_mut(),
                    std::collections::hash_map::Entry::Vacant(entry) => {
                        let runtime = runtime.as_ref().ok_or("Missing native video runtime")?;
                        let decoder = GpuVideoSource::open(runtime, &pool, path, (r - x, b - y), request.crop)?;
                        let (texture, view) = affine.create_output()?;
                        affine.clear_output(&view);
                        entry.insert(VideoLayer { decoder, texture, view, clip: layer.geometry.clip,
                            placement: request, last_pts: None })
                    }
                };
                if entry.clip != layer.geometry.clip || entry.placement.crop != request.crop
                    || entry.placement.flip_horizontal != request.flip_horizontal
                    || entry.placement.flip_vertical != request.flip_vertical
                    || entry.placement.border_radius != request.border_radius
                    || entry.placement.saturation != request.saturation {
                    return Err("Native video layer geometry changed mid-export".into());
                }
                let luma = entry.decoder.luma;
                let Some((decoded, actual)) = entry.decoder.frame_with_limit(time, request.media_limit)?
                    else { continue; };
                if entry.last_pts != Some(actual) {
                    affine.place_video(&entry.texture, &entry.view, decoded, entry.clip, request, luma)?;
                    entry.last_pts = Some(actual);
                }
                active_video[request.asset] = true;
                video_frames += 1;
            }
            if !options.video_sources.is_empty() {
                video_decode_seconds += video_started.elapsed().as_secs_f64();
            }
            let mut layers = Vec::with_capacity(current.layers.len());
            for source in &current.layers {
                let texture = if let Some(request) = source.video {
                    if !active_video[request.asset] { continue; }
                    &videos.get(&request.asset).ok_or("Native video output was not prepared")?.texture
                } else if source.uses_resident_texture(width, height) {
                    resident.resource(&source.stream_key)?
                } else {
                    affine.output(&source.stream_key)?
                };
                layers.push(NativeCanvasBgraLayer { texture, opacity: source.composition_opacity() });
            }
            let slot = pool.queue_gpu_canvas_layers(&processor, &layers, pts)?;
            timings.gpu_submit += submit_started.elapsed().as_secs_f64();
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
                let unexpected = if needs_previous_packet(options.binary_v2, options.binary_v3) {
                    next_input_frame(&mut input, clock.submitted, Some(&mut current), &options, mapping, &mut timings)?
                } else {
                    drop(current);
                    next_input_frame(&mut input, clock.submitted, None, &options, mapping, &mut timings)?
                };
                if unexpected.is_some() {
                    return Err("More Canvas packets supplied than compiled playlist frames".into());
                }
                break;
            }
            let transfer_started = Instant::now();
            let next = if needs_previous_packet(options.binary_v2, options.binary_v3) {
                next_input_frame(&mut input, clock.submitted, Some(&mut current), &options, mapping, &mut timings)?
            } else {
                drop(current);
                next_input_frame(&mut input, clock.submitted, None, &options, mapping, &mut timings)?
            };
            current = next.ok_or("Native Canvas source ended before the full playlist")?;
            transfer_seconds += transfer_started.elapsed().as_secs_f64();
        }
        while !pending.is_empty() {
            consume(&mut pool, &mut encoder, &mut pending, &mut writer, &mut clock, &mut timings)?;
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
        let h264_sha256 = writer.publish(&options.video)?;
        let stage_seconds = json!({
            "d3d11_upload_and_submit": timings.gpu_submit,
            "d3d11_completion_wait": timings.gpu_wait,
            "nvenc_encode": timings.nvenc,
            "shared_packet_decode": timings.shared_packet_decode,
        });
        let mut result = json!({
            "schema": 1,
            "status": "measured",
            "production_export_scope": "production_complete_playlist",
            "production_export_changed": true,
            "source": "real_qt_canvas_snapshot_cpu_raster_packets",
            "adapter_name": adapter,
            "nvenc_api_version": encoder.negotiated_api_version(),
            "resolution": [width, height],
            "fps": fps,
            "adapter_luid": adapter_luid,
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
            "canvas_packet_schemas": CANVAS_PACKET_SCHEMAS,
            "gpu_nv12_to_cpu_readbacks": 0,
            "nvenc_output_synchronously_completed": true,
            "qt_canvas_rasterized_on_cpu": true,
            "native_gpu_text_painting_implemented": affine.text_layers_drawn > 0,
            "max_pending_gpu_slots": slots,
            "input": if options.binary_v3 { "binary-v3" } else if options.binary_v2 { "binary-v2" } else if options.binary { "binary-v1" } else { "newline_separated_manifest_paths_stdin" },
            "frame_receive_seconds": transfer_seconds,
            "native_stage_seconds": stage_seconds,
            "parallel_packet_validation": options.parallel_packet_validation,
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
        result["h264_sha256"] = json!(h264_sha256);
        result["d3d11_particle_layers_drawn"] = json!(affine.particle_layers_drawn);
        result["d3d11_particle_sprites_drawn"] = json!(affine.particle_sprites_drawn);
        result["d3d11_radial_layers_drawn"] = json!(affine.radial_layers_drawn);
        result["d3d11_text_layers_drawn"] = json!(affine.text_layers_drawn);
        result["d3d11_text_quads_drawn"] = json!(affine.text_quads_drawn);
        result["d3d11_video_sources"] = json!({
            "composited_frames": video_frames,
            "decoded_samples": videos.values().map(|v| v.decoder.decoded_frames).sum::<u64>(),
            "converted_samples": videos.values().map(|v| v.decoder.converted_frames).sum::<u64>(),
            "seeks": videos.values().map(|v| v.decoder.seeks).sum::<u64>(),
            "cpu_pixel_bytes": 0,
            "cpu_readbacks": 0,
            "decode_and_submit_seconds": video_decode_seconds,
        });
        result["nvenc_encoding"] = json!({
            "preset": format!("p{nvenc_preset}"),
            "cq": nvenc_cq,
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

    #[cfg(test)]
    mod reference_lifetime_tests {
        use super::{needs_previous_packet, parse_options, read_shared_length, Command, SHARED_PACKET_CAPACITY};

        #[test]
        fn probe_requires_encoding_settings_but_no_timeline_or_output() {
            let mut args = vec![
                "--probe", "1", "--width", "1920", "--height", "1080", "--fps", "60",
                "--nvenc-preset", "p5", "--nvenc-cq", "19",
                "--adapter-luid", "4294967297",
            ];
            let Command::Probe(encoding, paths) = parse_options(args.iter().map(|arg| arg.to_string())).unwrap()
                else { panic!("expected probe mode") };
            assert_eq!((encoding.width, encoding.height, encoding.fps), (1920, 1080, 60));
            assert_eq!((encoding.nvenc_preset, encoding.nvenc_cq), (5, 19));
            assert_eq!(encoding.adapter_luid, Some(4294967297));
            assert!(paths.is_empty());
            args[3] = "1921";
            assert!(parse_options(args.iter().map(|arg| arg.to_string())).is_err());
        }

        #[test]
        fn v3_keeps_previous_packet_for_subsequent_frames() {
            assert!(!needs_previous_packet(false, false)); // v1 and manifest
            assert!(needs_previous_packet(true, false));   // v2
            assert!(needs_previous_packet(false, true));   // LZ4 v3
        }

        #[test]
        fn shared_lengths_distinguish_inline_eof_and_bad_headers() {
            assert_eq!(read_shared_length(&mut &[][..]).unwrap(), None);
            assert_eq!(read_shared_length(&mut &0u32.to_le_bytes()[..]).unwrap(), Some(0));
            let max = SHARED_PACKET_CAPACITY as u32;
            assert_eq!(read_shared_length(&mut &max.to_le_bytes()[..]).unwrap(), Some(max as usize));
            assert!(read_shared_length(&mut &(max + 1).to_le_bytes()[..]).is_err());
            assert!(read_shared_length(&mut &[1u8, 0][..]).is_err());
        }
    }

    pub fn run() -> Result<(), String> {
        let report = match parse_options(env::args().skip(1))? {
            Command::Probe(options, paths) => {
                let resources = open_encoder(options)?;
                if !paths.is_empty() {
                    let runtime = VideoDecodeRuntime::new(&resources.pool)?;
                    for path in &paths {
                        GpuVideoSource::open(&runtime, &resources.pool, path, (64, 64), false)?;
                    }
                }
                json!({
                    "status": "ready",
                    "adapter_luid": resources.adapter_luid,
                    "adapter_name": resources.pool.adapter_name(),
                    "canvas_packet_schemas": CANVAS_PACKET_SCHEMAS,
                    "canvas_layer_limit": resources.processor.canvas_layer_limit()?,
                })
            }
            Command::Export(options) => export(options)?,
        };
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
