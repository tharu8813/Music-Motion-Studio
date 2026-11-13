//! Same-device DXVA decode and NV12-to-BGRA conversion for native Canvas video.
use std::{mem::ManuallyDrop, path::Path};
use windows::core::{s, w, Interface, GUID, HRESULT, HSTRING, PCSTR, PCWSTR};
use windows::Win32::Foundation::{FreeLibrary, HMODULE, RECT};
use windows::Win32::Graphics::{Direct3D11::*, Dxgi::Common::*};
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::StructuredStorage::{
    PROPVARIANT, PROPVARIANT_0, PROPVARIANT_0_0, PROPVARIANT_0_0_0,
};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};
use windows::Win32::System::LibraryLoader::{
    GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32,
};
use windows::Win32::System::Variant::VT_I8;

use crate::d3d11_frame_pool::NativeNv12FramePool;

const VIDEO: u32 = MF_SOURCE_READER_FIRST_VIDEO_STREAM.0 as u32;
// Documented by Media Foundation but absent from the Windows SDK headers.
const MF_SOURCE_READER_PASSTHROUGH_MODE: GUID =
    GUID::from_u128(0x043ff126_fe2c_4708_a09b_da2ab435ced9);

pub struct VideoDecodeRuntime {
    manager: Option<IMFDXGIDeviceManager>,
    platform: SystemDll,
    reader: SystemDll,
    shutdown: unsafe extern "system" fn() -> HRESULT,
}

struct SystemDll(HMODULE);

impl SystemDll {
    fn open(name: PCWSTR) -> Result<Self, String> {
        unsafe { LoadLibraryExW(name, None, LOAD_LIBRARY_SEARCH_SYSTEM32) }
            .map(Self)
            .map_err(|e| format!("Windows Media Foundation is unavailable: {e}"))
    }

    fn proc(&self, name: PCSTR) -> Result<unsafe extern "system" fn() -> isize, String> {
        unsafe { GetProcAddress(self.0, name) }
            .ok_or_else(|| format!("Windows Media Foundation function is unavailable: {name:?}"))
    }
}

impl Drop for SystemDll {
    fn drop(&mut self) {
        let _ = unsafe { FreeLibrary(self.0) };
    }
}

type MfStartup = unsafe extern "system" fn(u32, u32) -> HRESULT;
type CreateManager =
    unsafe extern "system" fn(*mut u32, *mut Option<IMFDXGIDeviceManager>) -> HRESULT;
type CreateAttributes = unsafe extern "system" fn(*mut Option<IMFAttributes>, u32) -> HRESULT;
type CreateMediaType = unsafe extern "system" fn(*mut Option<IMFMediaType>) -> HRESULT;
type CreateReader = unsafe extern "system" fn(
    PCWSTR,
    *mut std::ffi::c_void,
    *mut Option<IMFSourceReader>,
) -> HRESULT;

impl VideoDecodeRuntime {
    pub fn new(pool: &NativeNv12FramePool) -> Result<Self, String> {
        let platform = SystemDll::open(w!("mfplat.dll"))?;
        let reader = SystemDll::open(w!("mfreadwrite.dll"))?;
        // The signatures are those of the corresponding Media Foundation C functions.
        let (startup, shutdown): (MfStartup, unsafe extern "system" fn() -> HRESULT) = unsafe {
            (
                std::mem::transmute(platform.proc(s!("MFStartup"))?),
                std::mem::transmute(platform.proc(s!("MFShutdown"))?),
            )
        };
        let create_manager: CreateManager =
            unsafe { std::mem::transmute(platform.proc(s!("MFCreateDXGIDeviceManager"))?) };
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED).ok() }
            .map_err(|e| format!("Cannot initialize video COM: {e}"))?;
        if let Err(error) = unsafe { startup(MF_VERSION, MFSTARTUP_NOSOCKET).ok() } {
            unsafe { CoUninitialize() };
            return Err(format!("Cannot start Windows Media Foundation: {error}"));
        }
        let mut runtime = Self {
            manager: None,
            platform,
            reader,
            shutdown,
        };
        let threading: ID3D11Multithread = pool
            .native_device()
            .cast()
            .map_err(|e| format!("D3D11 video thread protection unavailable: {e}"))?;
        let _ = unsafe { threading.SetMultithreadProtected(true) };
        let mut token = 0;
        unsafe { create_manager(&mut token, &mut runtime.manager).ok() }
            .map_err(|e| format!("Cannot create DXVA device manager: {e}"))?;
        unsafe {
            runtime
                .manager
                .as_ref()
                .ok_or("Missing DXVA manager")?
                .ResetDevice(pool.native_device(), token)
        }
        .map_err(|e| format!("Cannot share the export GPU with DXVA: {e}"))?;
        Ok(runtime)
    }
}

impl Drop for VideoDecodeRuntime {
    fn drop(&mut self) {
        self.manager.take();
        unsafe {
            let _ = (self.shutdown)();
            CoUninitialize();
        }
    }
}

pub struct GpuVideoSource<'runtime> {
    reader: IMFSourceReader,
    device: ID3D11Device,
    video_device: ID3D11VideoDevice,
    video_context: ID3D11VideoContext1,
    enumerator: ID3D11VideoProcessorEnumerator,
    processor: ID3D11VideoProcessor,
    target: ID3D11Texture2D,
    target_view: ID3D11VideoProcessorOutputView,
    current: Option<(i64, IMFSample)>,
    next: Option<(i64, IMFSample)>,
    submitted: Option<IMFSample>,
    end: bool,
    first_pts: i64,
    last_request: Option<i64>,
    media_size: (u32, u32),
    target_size: (u32, u32),
    crop: bool,
    color_space: DXGI_COLOR_SPACE_TYPE,
    pub luma: [f32; 3],
    pub decoded_frames: u64,
    pub converted_frames: u64,
    pub seeks: u64,
    _runtime: &'runtime VideoDecodeRuntime,
}

impl<'runtime> GpuVideoSource<'runtime> {
    pub fn open(
        runtime: &'runtime VideoDecodeRuntime,
        pool: &NativeNv12FramePool,
        path: &Path,
        target_size: (u32, u32),
        crop: bool,
    ) -> Result<Self, String> {
        if !path.is_absolute()
            || !path.is_file()
            || !path
                .extension()
                .and_then(|s| s.to_str())
                .is_some_and(|s| s.eq_ignore_ascii_case("mp4"))
        {
            return Err("Native video requires an existing local MP4 file".into());
        }
        if !(8..=3840).contains(&target_size.0)
            || !(8..=2160).contains(&target_size.1)
            || target_size.0 % 2 != 0
            || target_size.1 % 2 != 0
        {
            return Err(
                "Native video requires even output dimensions between 8 and 3840×2160".into(),
            );
        }
        let mut attributes = None;
        let create_attributes: CreateAttributes =
            unsafe { std::mem::transmute(runtime.platform.proc(s!("MFCreateAttributes"))?) };
        let create_reader: CreateReader =
            unsafe { std::mem::transmute(runtime.reader.proc(s!("MFCreateSourceReaderFromURL"))?) };
        let create_media_type: CreateMediaType =
            unsafe { std::mem::transmute(runtime.platform.proc(s!("MFCreateMediaType"))?) };
        let reader = unsafe {
            create_attributes(&mut attributes, 4)
                .ok()
                .map_err(|e| format!("Cannot create video reader attributes: {e}"))?;
            let attributes = attributes.ok_or("Missing video reader attributes")?;
            let manager = runtime.manager.as_ref().ok_or("Missing DXVA manager")?;
            attributes
                .SetUnknown(&MF_SOURCE_READER_D3D_MANAGER, manager)
                .and_then(|()| attributes.SetUINT32(&MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, 1))
                .and_then(|()| attributes.SetUINT32(&MF_SOURCE_READER_DISABLE_DXVA, 0))
                .and_then(|()| attributes.SetUINT32(&MF_SOURCE_READER_PASSTHROUGH_MODE, 1))
                .map_err(|e| format!("Cannot configure Media Foundation video reader: {e}"))?;
            let url = HSTRING::from(path.as_os_str());
            let mut reader = None;
            create_reader(PCWSTR(url.as_ptr()), attributes.as_raw(), &mut reader)
                .ok()
                .map_err(|e| format!("Cannot open Media Foundation video reader: {e}"))?;
            reader.ok_or("Missing Media Foundation video reader")?
        };
        let media = unsafe {
            reader
                .SetStreamSelection(MF_SOURCE_READER_ALL_STREAMS.0 as u32, false)
                .and_then(|()| reader.SetStreamSelection(VIDEO, true))
                .and_then(|()| reader.GetNativeMediaType(VIDEO, 0))
        }
        .map_err(|e| format!("Cannot select the first video stream: {e}"))?;
        if unsafe { media.GetGUID(&MF_MT_SUBTYPE) }.ok() != Some(MFVideoFormat_H264) {
            return Err("Native video supports H.264 MP4 sources".into());
        }
        let mut wanted = None;
        unsafe { create_media_type(&mut wanted).ok() }
            .map_err(|e| format!("Cannot create video media type: {e}"))?;
        let wanted = wanted.ok_or("Missing video media type")?;
        let media = unsafe {
            (|| {
                wanted.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video)?;
                wanted.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_NV12)?;
                reader.SetCurrentMediaType(VIDEO, None, &wanted)?;
                reader.GetCurrentMediaType(VIDEO)
            })()
        }
        .map_err(|e| format!("Cannot configure DXVA NV12 decoder: {e}"))?;
        let size = unsafe { media.GetUINT64(&MF_MT_FRAME_SIZE) }
            .map_err(|e| format!("Cannot query decoder frame size: {e}"))?;
        let media_size = ((size >> 32) as u32, size as u32);
        if media_size.0 == 0
            || media_size.1 == 0
            || media_size.0 > 3840
            || media_size.1 > 2160
            || media_size.0 % 2 != 0
            || media_size.1 % 2 != 0
        {
            return Err("Native video requires even source dimensions up to 3840×2160".into());
        }
        let (color_space, luma) = video_colors(&media)?;
        let device = pool.native_device().clone();
        let context = pool.native_context().clone();
        let video_device: ID3D11VideoDevice = device.cast().map_err(|e| e.to_string())?;
        let video_context: ID3D11VideoContext1 = context.cast().map_err(|e| e.to_string())?;
        let description = D3D11_VIDEO_PROCESSOR_CONTENT_DESC {
            InputFrameFormat: D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
            InputWidth: media_size.0,
            InputHeight: media_size.1,
            OutputWidth: target_size.0,
            OutputHeight: target_size.1,
            Usage: D3D11_VIDEO_USAGE_PLAYBACK_NORMAL,
            ..Default::default()
        };
        let enumerator = unsafe { video_device.CreateVideoProcessorEnumerator(&description) }
            .map_err(|e| format!("Cannot open GPU video source converter: {e}"))?;
        let processor = unsafe { video_device.CreateVideoProcessor(&enumerator, 0) }
            .map_err(|e| format!("Cannot create GPU video source converter: {e}"))?;
        let mut target = None;
        unsafe {
            device.CreateTexture2D(
                &D3D11_TEXTURE2D_DESC {
                    Width: target_size.0,
                    Height: target_size.1,
                    ArraySize: 1,
                    MipLevels: 1,
                    Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                    SampleDesc: DXGI_SAMPLE_DESC {
                        Count: 1,
                        Quality: 0,
                    },
                    BindFlags: (D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE).0 as u32,
                    ..Default::default()
                },
                None,
                Some(&mut target),
            )
        }
        .map_err(|e| format!("Cannot allocate resident video texture: {e}"))?;
        let target = target.ok_or("Missing resident video texture")?;
        let mut target_view = None;
        unsafe {
            video_device.CreateVideoProcessorOutputView(
                &target,
                &enumerator,
                &D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC {
                    ViewDimension: D3D11_VPOV_DIMENSION_TEXTURE2D,
                    ..Default::default()
                },
                Some(&mut target_view),
            )
        }
        .map_err(|e| format!("Cannot create video source output view: {e}"))?;
        let mut source = Self {
            reader,
            device,
            video_device,
            video_context,
            enumerator,
            processor,
            target,
            target_view: target_view.ok_or("Missing video source output view")?,
            current: None,
            next: None,
            submitted: None,
            end: false,
            first_pts: 0,
            last_request: None,
            media_size,
            target_size,
            crop,
            color_space,
            luma,
            decoded_frames: 0,
            converted_frames: 0,
            seeks: 0,
            _runtime: runtime,
        };
        source.current = source.read()?;
        source.first_pts = source
            .current
            .as_ref()
            .ok_or("Video stream has no frames")?
            .0;
        source.convert()?;
        Ok(source)
    }

    fn read(&mut self) -> Result<Option<(i64, IMFSample)>, String> {
        for _ in 0..128 {
            let (mut flags, mut pts, mut sample) = (0, 0, None);
            unsafe {
                self.reader.ReadSample(
                    VIDEO,
                    0,
                    None,
                    Some(&mut flags),
                    Some(&mut pts),
                    Some(&mut sample),
                )
            }
            .map_err(|e| format!("DXVA frame read failed: {e}"))?;
            if flags
                & (MF_SOURCE_READERF_ERROR.0 | MF_SOURCE_READERF_NATIVEMEDIATYPECHANGED.0) as u32
                != 0
            {
                return Err(format!(
                    "Unsupported video decoder state change 0x{flags:X}"
                ));
            }
            if flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED.0 as u32 != 0 {
                let media =
                    unsafe { self.reader.GetCurrentMediaType(VIDEO) }.map_err(|e| e.to_string())?;
                let subtype = unsafe { media.GetGUID(&MF_MT_SUBTYPE) }.ok();
                let actual_size = unsafe { media.GetUINT64(&MF_MT_FRAME_SIZE) }.ok();
                let (w, h) = self.media_size;
                // The decoder can change the reported surface to its
                // macroblock-aligned size. The visible crop stays unpadded.
                if subtype != Some(MFVideoFormat_NV12)
                    || !actual_size.is_some_and(|size| {
                        (w..=w.next_multiple_of(16)).contains(&((size >> 32) as u32))
                            && (h..=h.next_multiple_of(16)).contains(&(size as u32))
                    })
                {
                    return Err("Video changed its frame size or pixel format".into());
                }
                let (space, luma) = video_colors(&media)?;
                if self.converted_frames != 0 && self.color_space != space {
                    return Err("Video changed color space during export".into());
                }
                self.color_space = space;
                self.luma = luma;
            }
            if flags & MF_SOURCE_READERF_ENDOFSTREAM.0 as u32 != 0 {
                self.end = true;
                return Ok(None);
            }
            if let Some(sample) = sample {
                self.decoded_frames += 1;
                return Ok(Some((pts, sample)));
            }
        }
        Err("DXVA decoder did not produce a video sample".into())
    }

    fn seek(&mut self, target: i64) -> Result<(), String> {
        let position = PROPVARIANT {
            Anonymous: PROPVARIANT_0 {
                Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                    vt: VT_I8,
                    Anonymous: PROPVARIANT_0_0_0 {
                        hVal: target.max(0),
                    },
                    ..Default::default()
                }),
            },
        };
        unsafe { self.reader.SetCurrentPosition(&GUID::zeroed(), &position) }
            .map_err(|e| format!("DXVA source seek failed: {e}"))?;
        self.end = false;
        self.next = None;
        self.current = self.read()?;
        self.seeks += 1;
        Ok(())
    }

    /// Select the last presented media sample at or before this export timestamp.
    pub fn frame(&mut self, local_100ns: i64) -> Result<(&ID3D11Texture2D, i64), String> {
        self.frame_with_limit(local_100ns, None)?
            .ok_or("Video frame unexpectedly absent".into())
    }

    pub fn frame_with_limit(
        &mut self,
        local_100ns: i64,
        media_limit: Option<i64>,
    ) -> Result<Option<(&ID3D11Texture2D, i64)>, String> {
        if local_100ns < 0 {
            return Err("Native video timestamp cannot be negative".into());
        }
        let target = self
            .first_pts
            .checked_add(local_100ns)
            .ok_or("Video timestamp overflow")?;
        let previous = self.current.as_ref().map(|frame| frame.0);
        let seek = target < previous.unwrap_or(self.first_pts)
            || target.saturating_sub(self.last_request.unwrap_or(self.first_pts)) > 20_000_000;
        if seek {
            self.seek(target)?;
        }
        // FFmpeg's export filter graph uses a microsecond time base.
        let first_pts = self.first_pts;
        let same_or_before = |pts: i64| (pts - first_pts + 5) / 10 <= (local_100ns + 5) / 10;
        for _ in 0..16_384 {
            if self.next.is_none() && !self.end {
                self.next = self.read()?;
            }
            if !self.next.as_ref().is_some_and(|frame| {
                same_or_before(frame.0)
                    && media_limit.is_none_or(|limit| frame.0 - self.first_pts < limit)
            }) {
                let current = self
                    .current
                    .as_ref()
                    .ok_or("No decoded sample at video position")?
                    .0;
                if !same_or_before(current) {
                    return Err("DXVA seek skipped the required video frame".into());
                }
                // FFmpeg overlay eof_action=pass ends just past the final
                // presented timestamp, including the last frame of a trim.
                if media_limit.is_some_and(|limit| {
                    self.next
                        .as_ref()
                        .is_none_or(|next| next.0 - self.first_pts >= limit)
                        && local_100ns - (current - self.first_pts) > 10
                }) {
                    self.last_request = Some(target);
                    return Ok(None);
                }
                if seek || previous != Some(current) {
                    self.convert()?;
                }
                self.last_request = Some(target);
                return Ok(Some((&self.target, current - self.first_pts)));
            }
            self.current = self.next.take();
        }
        Err("DXVA seek exceeded the video frame budget".into())
    }

    fn convert(&mut self) -> Result<(), String> {
        let (_, sample) = self
            .current
            .as_ref()
            .ok_or("Missing decoded video sample")?;
        let buffer = unsafe { sample.GetBufferByIndex(0) }.map_err(|e| e.to_string())?;
        let surface: IMFDXGIBuffer = buffer
            .cast()
            .map_err(|_| "Video decoder produced system-memory pixels instead of a DXVA surface")?;
        let mut raw = std::ptr::null_mut();
        unsafe { surface.GetResource(&ID3D11Texture2D::IID, &mut raw) }
            .map_err(|e| format!("Cannot access the decoder's GPU surface: {e}"))?;
        if raw.is_null() {
            return Err("DXVA returned a null video surface".into());
        }
        let texture = unsafe { ID3D11Texture2D::from_raw(raw) };
        let device = unsafe { texture.GetDevice() }.map_err(|e| e.to_string())?;
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { texture.GetDesc(&mut desc) };
        if device.as_raw() != self.device.as_raw()
            || desc.Format != DXGI_FORMAT_NV12
            || desc.Width < self.media_size.0
            || desc.Height < self.media_size.1
            || desc.MipLevels != 1
            || desc.BindFlags & D3D11_BIND_DECODER.0 as u32 == 0
        {
            return Err(format!(
                "DXVA sample is not a same-device NV12 decoder surface (bind flags 0x{:X})",
                desc.BindFlags
            ));
        }
        let index = unsafe { surface.GetSubresourceIndex() }.map_err(|e| e.to_string())?;
        if index >= desc.ArraySize {
            return Err("DXVA sample references an invalid array slice".into());
        }
        let input = D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
            ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
            FourCC: 0,
            Anonymous: D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC_0 {
                Texture2D: D3D11_TEX2D_VPIV {
                    MipSlice: 0,
                    ArraySlice: index,
                },
            },
        };
        let mut input_view = None;
        let source = source_rect(self.media_size, self.target_size, self.crop);
        let dest = RECT {
            left: 0,
            top: 0,
            right: self.target_size.0 as i32,
            bottom: self.target_size.1 as i32,
        };
        unsafe {
            self.video_device
                .CreateVideoProcessorInputView(
                    &texture,
                    &self.enumerator,
                    &input,
                    Some(&mut input_view),
                )
                .map_err(|e| format!("Cannot bind the DXVA decoder surface: {e}"))?;
            self.video_context.VideoProcessorSetStreamFrameFormat(
                &self.processor,
                0,
                D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
            );
            self.video_context
                .VideoProcessorSetStreamAutoProcessingMode(&self.processor, 0, false);
            self.video_context.VideoProcessorSetStreamSourceRect(
                &self.processor,
                0,
                true,
                Some(&source),
            );
            self.video_context.VideoProcessorSetStreamDestRect(
                &self.processor,
                0,
                true,
                Some(&dest),
            );
            self.video_context.VideoProcessorSetOutputTargetRect(
                &self.processor,
                true,
                Some(&dest),
            );
            self.video_context.VideoProcessorSetStreamColorSpace1(
                &self.processor,
                0,
                self.color_space,
            );
            self.video_context.VideoProcessorSetOutputColorSpace1(
                &self.processor,
                DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709,
            );
            self.video_context.VideoProcessorSetOutputAlphaFillMode(
                &self.processor,
                D3D11_VIDEO_PROCESSOR_ALPHA_FILL_MODE_OPAQUE,
                0,
            );
            let mut stream = D3D11_VIDEO_PROCESSOR_STREAM {
                Enable: true.into(),
                pInputSurface: ManuallyDrop::new(input_view),
                ..Default::default()
            };
            let result = self.video_context.VideoProcessorBlt(
                &self.processor,
                &self.target_view,
                0,
                std::slice::from_ref(&stream),
            );
            ManuallyDrop::drop(&mut stream.pInputSurface);
            result.map_err(|e| format!("GPU decoder surface conversion failed: {e}"))?;
        }
        // Keep the decoder's output sample leased while its blit is queued.
        // Decode writes and the next blit use this same protected context.
        self.submitted = Some(sample.clone());
        self.converted_frames += 1;
        Ok(())
    }
}

fn source_rect(media: (u32, u32), target: (u32, u32), crop: bool) -> RECT {
    let (w, h) = (media.0 as i32, media.1 as i32);
    let (tw, th) = (target.0 as i32, target.1 as i32);
    let mut rect = RECT {
        left: 0,
        top: 0,
        right: w,
        bottom: h,
    };
    if crop && w * th > h * tw {
        let span = (h * tw / th).max(2) & !1;
        rect.left = ((w - span) / 2) & !1;
        rect.right = rect.left + span;
    } else if crop && w * th < h * tw {
        let span = (w * th / tw).max(2) & !1;
        rect.top = ((h - span) / 2) & !1;
        rect.bottom = rect.top + span;
    }
    rect
}

fn video_colors(media: &IMFMediaType) -> Result<(DXGI_COLOR_SPACE_TYPE, [f32; 3]), String> {
    let matrix = unsafe { media.GetUINT32(&MF_MT_YUV_MATRIX) }.unwrap_or(0);
    let range = unsafe { media.GetUINT32(&MF_MT_VIDEO_NOMINAL_RANGE) }.unwrap_or(0);
    if !matches!(range, 0..=2) {
        return Err("Native video requires full or limited SDR range".into());
    }
    let space = match (matrix, range == MFNominalRange_0_255.0 as u32) {
        (0 | 2, false) => DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P601,
        (0 | 2, true) => DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P601,
        (1, false) => DXGI_COLOR_SPACE_YCBCR_STUDIO_G22_LEFT_P709,
        (1, true) => DXGI_COLOR_SPACE_YCBCR_FULL_G22_LEFT_P709,
        _ => return Err("Native video supports SDR BT.601 and BT.709".into()),
    };
    let luma = if matrix == 1 {
        [0.2126, 0.7152, 0.0722]
    } else {
        [0.299, 0.587, 0.114]
    };
    Ok((space, luma))
}

#[cfg(test)]
mod tests {
    use super::source_rect;

    #[test]
    fn cover_crops_inside_the_visible_nv12_rectangle() {
        let uncropped = source_rect((320, 180), (240, 240), false);
        assert_eq!(
            (
                uncropped.left,
                uncropped.top,
                uncropped.right,
                uncropped.bottom
            ),
            (0, 0, 320, 180)
        );
        let square = source_rect((320, 180), (240, 240), true);
        assert_eq!(
            (square.left, square.top, square.right, square.bottom),
            (70, 0, 250, 180)
        );
        let landscape = source_rect((180, 320), (240, 120), true);
        assert_eq!(
            (
                landscape.left,
                landscape.top,
                landscape.right,
                landscape.bottom
            ),
            (0, 114, 180, 204)
        );
    }
}
