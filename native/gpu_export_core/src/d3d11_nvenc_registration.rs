//! Experimental same-process D3D11 -> NVENC texture registration.
//!
//! This establishes that NVENC can open an encode session using the EXACT
//! D3D11 device owned by NativeNv12FramePool, and register/map its GPU NV12
//! frame textures without a CPU readback. It DOES NOT encode a bitstream.
//! Driver handles are unregistered before the GPU textures/device die.
//!
//! Keep this isolated from the production exporter until bitstream encoding,
//! consumer completion, timestamps, color metadata and audio are verified.
use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::ptr;
use std::time::{Duration, Instant};

use mini_nvcodec::{try_init_nvenc, API_VERSION_LADDER, NvencApi, NvencApiVersion};
use mini_nvcodec::guids::{NV_ENC_CODEC_H264_GUID, NV_ENC_PRESET_P3_GUID};
use mini_nvcodec::nvenc_sys::{
    GUID, NVENCSTATUS, NV_ENC_BUFFER_FORMAT, NV_ENC_BUFFER_USAGE,
    NV_ENC_DEVICE_TYPE, NV_ENC_INITIALIZE_PARAMS, NV_ENC_INPUT_RESOURCE_TYPE,
    NV_ENC_MAP_INPUT_RESOURCE, NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS,
    NV_ENC_PRESET_CONFIG, NV_ENC_REGISTER_RESOURCE, NV_ENC_TUNING_INFO,
    NV_ENC_CREATE_BITSTREAM_BUFFER, NV_ENC_PIC_PARAMS, NV_ENC_PIC_STRUCT,
    NV_ENC_LOCK_BITSTREAM, NV_ENC_PIC_FLAG_OUTPUT_SPSPPS, NV_ENC_PIC_FLAG_FORCEIDR,
    NV_ENC_VUI_COLOR_PRIMARIES, NV_ENC_VUI_TRANSFER_CHARACTERISTIC,
    NV_ENC_VUI_MATRIX_COEFFS,
};
use windows::core::Interface;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_TEXTURE2D_DESC, ID3D11Device, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_NV12;

use crate::d3d11_frame_pool::{NativeNv12FramePool, ReservedGpuFrame};

/// Synchronous NVENC sessions normally use a blocking bitstream lock.
/// Polling is retained only for diagnosing driver-specific behavior.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NvencBitstreamLockMode {
    Blocking,
    Polling,
}

impl NvencBitstreamLockMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Blocking => "blocking",
            Self::Polling => "polling",
        }
    }
}

struct RegisteredNv12 {
    // A COM reference must outlive NVENC's registration handle.
    texture: ID3D11Texture2D,
    handle: *mut c_void,
}

/// Single-threaded diagnostic-only NVENC session, bound to the same D3D11
/// device as the existing pool. No cross-device or CPU NV12 transfer.
pub struct NativeNvencRegistrationSession {
    api: &'static NvencApi,
    encoder: *mut c_void,
    version: NvencApiVersion,
    width: u32,
    height: u32,
    device: ID3D11Device,
    registered: HashMap<usize, RegisteredNv12>,
    successful_maps: usize,
    output_bitstream: *mut c_void,
    last_encoded_pts: Option<u64>,
    // Pending GPU input MUST NOT be unmapped/reused on encode failure.
    // Session teardown will abort and discard this pending work.
    incomplete_encode: bool,
    bitstream_lock_mode: NvencBitstreamLockMode,
}

fn check(status: NVENCSTATUS, stage: &str) -> Result<(), String> {
    if status == NVENCSTATUS::SUCCESS {
        Ok(())
    } else {
        Err(format!("{stage} failed: {status:?}"))
    }
}

fn supported_codec(api: &NvencApi, encoder: *mut c_void) -> Result<bool, String> {
    let mut count = 0;
    check(
        unsafe { (api.nvEncGetEncodeGUIDCount)(encoder, &mut count) },
        "NvEncGetEncodeGUIDCount",
    )?;
    if count == 0 || count > 64 {
        return Err(format!("Unexpected NVENC codec GUID count: {count}"));
    }
    let mut guids = vec![GUID {
        Data1: 0, Data2: 0, Data3: 0, Data4: [0; 8],
    }; count as usize];
    let mut actual = 0;
    check(
        unsafe {
            (api.nvEncGetEncodeGUIDs)(
                encoder, guids.as_mut_ptr(), count, &mut actual,
            )
        },
        "NvEncGetEncodeGUIDs",
    )?;
    Ok(guids.iter().take(actual as usize).any(|g| *g == NV_ENC_CODEC_H264_GUID))
}

fn supported_nv12(api: &NvencApi, encoder: *mut c_void) -> Result<bool, String> {
    let mut count = 0;
    check(
        unsafe {
            (api.nvEncGetInputFormatCount)(
                encoder, NV_ENC_CODEC_H264_GUID, &mut count,
            )
        },
        "NvEncGetInputFormatCount",
    )?;
    if count == 0 || count > 128 {
        return Err(format!("Unexpected NVENC input format count: {count}"));
    }
    let mut formats = vec![NV_ENC_BUFFER_FORMAT::UNDEFINED; count as usize];
    let mut actual = 0;
    check(
        unsafe {
            (api.nvEncGetInputFormats)(
                encoder, NV_ENC_CODEC_H264_GUID,
                formats.as_mut_ptr(), count, &mut actual,
            )
        },
        "NvEncGetInputFormats",
    )?;
    Ok(formats.iter().take(actual as usize).any(|f| *f == NV_ENC_BUFFER_FORMAT::NV12))
}

impl NativeNvencRegistrationSession {
    pub fn new_for_pool(pool: &NativeNv12FramePool) -> Result<Self, String> {
        let api = try_init_nvenc()
            .ok_or_else(|| "NVENC driver library or supported API unavailable".to_owned())?;
        let (width, height) = pool.dimensions();
        let mut last_error = String::from("No compatible NVENC API version");
        for version in API_VERSION_LADDER {
            if version > api.created_with {
                continue;
            }
            let mut session = ptr::null_mut();
            let mut params: NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS =
                unsafe { std::mem::zeroed() };
            params.version = version.open_encode_session_ex_params_ver();
            params.deviceType = NV_ENC_DEVICE_TYPE::DIRECTX;
            // The pointer is not a foreign-process handle. It is the
            // exact COM D3D11 device used to allocate all NV12 ring slots.
            params.device = pool.native_device().as_raw();
            params.apiVersion = version.nvencapi_version();
            let status = unsafe {
                (api.nvEncOpenEncodeSessionEx)(&mut params, &mut session)
            };
            if status == NVENCSTATUS::ERR_INVALID_VERSION {
                last_error = format!("NVENC API {version:?} rejected: invalid version");
                if !session.is_null() {
                    unsafe { (api.nvEncDestroyEncoder)(session) };
                }
                continue;
            }
            if status != NVENCSTATUS::SUCCESS {
                if !session.is_null() {
                    unsafe { (api.nvEncDestroyEncoder)(session) };
                }
                return Err(format!("NvEncOpenEncodeSessionEx failed: {status:?}"));
            }
            if session.is_null() {
                return Err("NVENC returned a null session on success".into());
            }
            // On subsequent failures Drop cleans up the partially opened
            // encoder session; it is never leaked or published.
            let result = Self {
                api, encoder: session, version, width, height,
                device: pool.native_device().clone(),
                registered: HashMap::new(), successful_maps: 0,
                output_bitstream: ptr::null_mut(), last_encoded_pts: None,
                incomplete_encode: false,
                bitstream_lock_mode: NvencBitstreamLockMode::Blocking,
            };
            if !supported_codec(api, session)? {
                return Err("NVENC device does not support H.264".into());
            }
            if !supported_nv12(api, session)? {
                return Err("NVENC H.264 session does not accept NV12 input".into());
            }

            let mut preset: NV_ENC_PRESET_CONFIG = unsafe { std::mem::zeroed() };
            preset.version = version.preset_config_ver();
            preset.presetCfg.version = version.config_ver();
            check(
                unsafe {
                    (api.nvEncGetEncodePresetConfigEx)(
                        session, NV_ENC_CODEC_H264_GUID,
                        NV_ENC_PRESET_P3_GUID, NV_ENC_TUNING_INFO::HIGH_QUALITY,
                        &mut preset,
                    )
                },
                "NvEncGetEncodePresetConfigEx",
            )?;
            // The synchronous diagnostic consumes one finished picture at
            // a time. B frames and lookahead would delay an output packet,
            // preventing a safe synchronous input-resource release.
            // Preserve all other driver-provided preset settings.
            preset.presetCfg.frameIntervalP = 1;
            preset.presetCfg.rcParams.lookaheadDepth = 0;
            preset.presetCfg.rcParams.set_enableLookahead(0);
            preset.presetCfg.rcParams.set_enableExtLookahead(0);

            // Hardware patch-center parity measured on RTX 5060:
            // D3D11 VideoProcessor -> NV12 matches BT.709 full range.
            // The H.264 decoder must receive matching SPS VUI metadata.
            let vui = unsafe {
                &mut preset.presetCfg.encodeCodecConfig.h264Config.h264VUIParameters
            };
            vui.videoSignalTypePresentFlag = 1;
            vui.videoFullRangeFlag = 1;
            vui.colourDescriptionPresentFlag = 1;
            vui.colourPrimaries = NV_ENC_VUI_COLOR_PRIMARIES::BT709;
            vui.transferCharacteristics = NV_ENC_VUI_TRANSFER_CHARACTERISTIC::BT709;
            vui.colourMatrix = NV_ENC_VUI_MATRIX_COEFFS::BT709;
            let mut init: NV_ENC_INITIALIZE_PARAMS =
                unsafe { std::mem::zeroed() };
            init.version = version.initialize_params_ver();
            init.encodeGUID = NV_ENC_CODEC_H264_GUID;
            init.presetGUID = NV_ENC_PRESET_P3_GUID;
            init.encodeWidth = width;
            init.encodeHeight = height;
            init.darWidth = width;
            init.darHeight = height;
            init.frameRateNum = 60;
            init.frameRateDen = 1;
            init.enableEncodeAsync = 0;
            init.enablePTD = 1;
            init.maxEncodeWidth = width;
            init.maxEncodeHeight = height;
            init.tuningInfo = NV_ENC_TUNING_INFO::HIGH_QUALITY;
            init.bufferFormat = NV_ENC_BUFFER_FORMAT::NV12;
            init.encodeConfig = &mut preset.presetCfg;
            check(
                unsafe { (api.nvEncInitializeEncoder)(session, &mut init) },
                "NvEncInitializeEncoder",
            )?;
            return Ok(result);
        }
        Err(last_error)
    }

    pub fn negotiated_api_version(&self) -> String {
        format!("{:?}", self.version)
    }

    pub fn registered_slot_count(&self) -> usize {
        self.registered.len()
    }

    pub fn successful_map_count(&self) -> usize {
        self.successful_maps
    }

    pub fn bitstream_lock_mode(&self) -> NvencBitstreamLockMode {
        self.bitstream_lock_mode
    }

    /// Change lock mode only before the first hardware operation. The
    /// registration-only probe retains its prior semantics.
    pub fn set_bitstream_lock_mode(
        &mut self, mode: NvencBitstreamLockMode,
    ) -> Result<(), String> {
        if self.incomplete_encode || self.last_encoded_pts.is_some() ||
            !self.output_bitstream.is_null() || !self.registered.is_empty()
        {
            return Err("NVENC bitstream lock mode cannot change after first use".into());
        }
        self.bitstream_lock_mode = mode;
        Ok(())
    }

    fn driver_status_message(&self, stage: &str, status: NVENCSTATUS) -> String {
        let name = if status == NVENCSTATUS::ERR_OUT_OF_MEMORY {
            "NV_ENC_ERR_OUT_OF_MEMORY"
        } else if status == NVENCSTATUS::ERR_LOCK_BUSY {
            "NV_ENC_ERR_LOCK_BUSY"
        } else if status == NVENCSTATUS::ERR_NEED_MORE_INPUT {
            "NV_ENC_ERR_NEED_MORE_INPUT"
        } else if status == NVENCSTATUS::ERR_INVALID_PARAM {
            "NV_ENC_ERR_INVALID_PARAM"
        } else if status == NVENCSTATUS::ERR_INVALID_VERSION {
            "NV_ENC_ERR_INVALID_VERSION"
        } else {
            "NVENC error"
        };
        // Immediately after the failing API call, retrieve the driver's
        // explanation before any other encoder call can overwrite it.
        let details = unsafe {
            let p = (self.api.nvEncGetLastErrorString)(self.encoder);
            if p.is_null() {
                String::from("(driver did not provide an error string)")
            } else {
                CStr::from_ptr(p).to_string_lossy()
                    .chars().take(2048).collect::<String>()
            }
        };
        format!("{stage}: {name} (code {}); driver: {details}", status.0)
    }

    /// Register a native GPU input once per slot and map it for a single
    /// NVENC operation. Never hand the returned map to another encoder/device.
    fn map_native_nv12(
        &mut self, frame: &ReservedGpuFrame,
    ) -> Result<NV_ENC_MAP_INPUT_RESOURCE, String> {
        // Confirm the actual COM GPU device, not just matching PCI IDs
        // or a matching texture width/format.
        let actual_device = unsafe { frame.texture.GetDevice() }
            .map_err(|e| format!("Cannot inspect encoder texture device: {e}"))?;
        if actual_device.as_raw() != self.device.as_raw() {
            return Err("NVENC input texture belongs to a different D3D11 device".into());
        }
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { frame.texture.GetDesc(&mut desc) };
        if desc.Width != self.width || desc.Height != self.height
            || desc.Format != DXGI_FORMAT_NV12
            || desc.MipLevels != 1 || desc.ArraySize != 1
        {
            return Err("NVENC input must be an exact-size NV12 D3D11 texture".into());
        }
        // Check current registered-slot COM identity before reusing
        // its persistent NVENC handle (not merely matching PCI IDs).
        if !self.registered.contains_key(&frame.slot_index) {
            let mut registration: NV_ENC_REGISTER_RESOURCE =
                unsafe { std::mem::zeroed() };
            registration.version = self.version.register_resource_ver();
            registration.resourceType = NV_ENC_INPUT_RESOURCE_TYPE::DIRECTX;
            registration.width = self.width;
            registration.height = self.height;
            registration.pitch = 0;
            registration.resourceToRegister = frame.texture.as_raw();
            registration.bufferFormat = NV_ENC_BUFFER_FORMAT::NV12;
            registration.bufferUsage = NV_ENC_BUFFER_USAGE::INPUT_IMAGE;
            check(
                unsafe {
                    (self.api.nvEncRegisterResource)(self.encoder, &mut registration)
                },
                "NvEncRegisterResource(D3D11 NV12)",
            )?;
            if registration.registeredResource.is_null() {
                return Err("NVENC returned an empty registration handle".into());
            }
            self.registered.insert(frame.slot_index, RegisteredNv12 {
                texture: frame.texture.clone(),
                handle: registration.registeredResource,
            });
        }
        let saved = self.registered.get(&frame.slot_index)
            .ok_or("NVENC slot registration was lost")?;
        if saved.texture.as_raw() != frame.texture.as_raw() {
            return Err("NVENC registered slot identity changed during reuse".into());
        }
        let mut map: NV_ENC_MAP_INPUT_RESOURCE = unsafe { std::mem::zeroed() };
        map.version = self.version.map_input_resource_ver();
        map.registeredResource = saved.handle;
        check(
            unsafe { (self.api.nvEncMapInputResource)(self.encoder, &mut map) },
            "NvEncMapInputResource(NV12)",
        )?;
        let valid_format = map.mappedBufferFmt == NV_ENC_BUFFER_FORMAT::NV12;
        if map.mappedResource.is_null() || !valid_format {
            if !map.mappedResource.is_null() {
                let _ = unsafe {
                    (self.api.nvEncUnmapInputResource)(self.encoder, map.mappedResource)
                };
            }
            return Err("NVENC mapped resource had invalid NV12 format or pointer".into());
        }
        Ok(map)
    }

    fn unmap_input(&mut self, mapped: *mut c_void) -> Result<(), String> {
        check(
            unsafe { (self.api.nvEncUnmapInputResource)(self.encoder, mapped) },
            "NvEncUnmapInputResource",
        )?;
        self.successful_maps += 1;
        Ok(())
    }

    /// Registration-only proof preserved unchanged semantically. There is
    /// no encode operation, so the map is safe to release immediately.
    pub fn probe_gpu_complete_frame(
        &mut self, frame: &ReservedGpuFrame,
    ) -> Result<(), String> {
        let map = self.map_native_nv12(frame)?;
        self.unmap_input(map.mappedResource)
    }

    fn ensure_bitstream_buffer(&mut self) -> Result<*mut c_void, String> {
        if !self.output_bitstream.is_null() {
            return Ok(self.output_bitstream);
        }
        let mut buffer: NV_ENC_CREATE_BITSTREAM_BUFFER = unsafe { std::mem::zeroed() };
        buffer.version = self.version.create_bitstream_buffer_ver();
        check(
            unsafe { (self.api.nvEncCreateBitstreamBuffer)(self.encoder, &mut buffer) },
            "NvEncCreateBitstreamBuffer",
        )?;
        if buffer.bitstreamBuffer.is_null() {
            return Err("NVENC did not return an output bitstream buffer".into());
        }
        self.output_bitstream = buffer.bitstreamBuffer;
        Ok(self.output_bitstream)
    }

    /// Encode one GPU-complete NV12 texture to a real Annex-B H.264 packet.
    ///
    /// SYNCHRONOUS PROOF ONLY: one output bitstream buffer, no B frames,
    /// no lookahead. Locking the output confirms this consumer finished
    /// reading this mapped input. A production encoder needs a bounded
    /// asynchronous packet/slot scheduler and cancellation strategy.
    ///
    /// The only CPU copy here is the *compressed H.264 packet* read from
    /// NvEncLockBitstream. The NV12 source is never read back to RAM.
    pub fn encode_gpu_complete_frame(
        &mut self, frame: &ReservedGpuFrame,
    ) -> Result<(Vec<u8>, u64), String> {
        if self.incomplete_encode {
            return Err("NVENC session has an unfinished encode; recreate session".into());
        }
        if frame.pts < 0 || self.last_encoded_pts.is_some_and(|p| frame.pts as u64 <= p) {
            return Err("NVENC frame PTS must increase strictly".into());
        }
        let output = self.ensure_bitstream_buffer()?;
        let map = self.map_native_nv12(frame)?;
        self.incomplete_encode = true;

        let mut pic: NV_ENC_PIC_PARAMS = unsafe { std::mem::zeroed() };
        pic.version = self.version.pic_params_ver();
        pic.inputWidth = self.width;
        pic.inputHeight = self.height;
        pic.inputPitch = 0; // D3D11 texture, not a CPU buffer.
        pic.inputBuffer = map.mappedResource;
        pic.outputBitstream = output;
        pic.bufferFmt = NV_ENC_BUFFER_FORMAT::NV12;
        pic.pictureStruct = NV_ENC_PIC_STRUCT::FRAME;
        pic.inputTimeStamp = frame.pts as u64;
        pic.inputDuration = 1;
        pic.frameIdx = frame.pts as u32;
        // First frame gets SPS/PPS, required by a standalone Annex-B file.
        if self.last_encoded_pts.is_none() {
            pic.encodePicFlags |= NV_ENC_PIC_FLAG_OUTPUT_SPSPPS | NV_ENC_PIC_FLAG_FORCEIDR;
        }

        let submitted = unsafe { (self.api.nvEncEncodePicture)(self.encoder, &mut pic) };
        if submitted != NVENCSTATUS::SUCCESS {
            // An accepted input may still be queued on NEED_MORE_INPUT.
            // Never unmap or recycle it before GPU completion is proven.
            let detail = self.driver_status_message("NvEncEncodePicture", submitted);
            return Err(format!("{detail}; synchronous output not available"));
        }

        let start = Instant::now();
        let bytes_and_pts = loop {
            let mut locked: NV_ENC_LOCK_BITSTREAM = unsafe { std::mem::zeroed() };
            locked.version = self.version.lock_bitstream_ver();
            locked.outputBitstream = output;
            // The encoder was initialized with enableEncodeAsync=0.
            // NVIDIA's documented synchronous path uses doNotWait=0.
            // Keep doNotWait=1 as an explicit debug-only comparison.
            locked.set_doNotWait(u32::from(
                self.bitstream_lock_mode == NvencBitstreamLockMode::Polling
            ));
            let status = unsafe { (self.api.nvEncLockBitstream)(self.encoder, &mut locked) };
            if status == NVENCSTATUS::ERR_LOCK_BUSY {
                if start.elapsed() >= Duration::from_secs(20) {
                    break Err("Timed out waiting for H.264 NVENC output".to_owned());
                }
                std::thread::sleep(Duration::from_millis(1));
                continue;
            }
            if status != NVENCSTATUS::SUCCESS {
                // OOM (10) is NOT LOCK_BUSY (13): don't mask or blindly
                // retry a driver/resource error as a transient condition.
                break Err(self.driver_status_message("NvEncLockBitstream", status));
            }
            let valid = !locked.bitstreamBufferPtr.is_null()
                && locked.bitstreamSizeInBytes > 0
                && locked.bitstreamSizeInBytes <= 32 * 1024 * 1024;
            let result = if valid {
                let bytes = unsafe {
                    std::slice::from_raw_parts(
                        locked.bitstreamBufferPtr.cast::<u8>(),
                        locked.bitstreamSizeInBytes as usize,
                    ).to_vec()
                };
                if locked.outputTimeStamp != frame.pts as u64 {
                    Err(format!(
                        "NVENC output PTS mismatch: expected {}, got {}",
                        frame.pts, locked.outputTimeStamp
                    ))
                } else {
                    Ok((bytes, locked.outputTimeStamp))
                }
            } else {
                Err("NVENC returned empty, invalid or oversized H.264 packet".into())
            };
            let unlocked = check(
                unsafe { (self.api.nvEncUnlockBitstream)(self.encoder, output) },
                "NvEncUnlockBitstream",
            );
            break result.and_then(|value| unlocked.map(|_| value));
        };

        // A successful bitstream lock means the encoder completed its input.
        // If the lock failed, the caller must abort rather than reclaiming
        // its slot: we do not claim encoder completion.
        if bytes_and_pts.is_ok() {
            self.unmap_input(map.mappedResource)?;
            self.incomplete_encode = false;
            self.last_encoded_pts = Some(frame.pts as u64);
        }
        bytes_and_pts
    }
}

impl Drop for NativeNvencRegistrationSession {
    fn drop(&mut self) {
        if self.incomplete_encode {
            // The encode might still own a mapped texture. Do not unmap,
            // unregister or recycle it prematurely. Teardown the whole
            // encoder first, which discards pending encoder resources.
            // Keep COM texture references alive until after teardown.
            if !self.encoder.is_null() {
                unsafe { (self.api.nvEncDestroyEncoder)(self.encoder) };
                self.encoder = ptr::null_mut();
            }
            self.registered.clear();
            self.output_bitstream = ptr::null_mut();
            return;
        }
        // Destroy the bitstream buffer before encoder teardown. In this
        // single-frame synchronous mode it is never locked on normal exit.
        if !self.output_bitstream.is_null() {
            unsafe {
                (self.api.nvEncDestroyBitstreamBuffer)(self.encoder, self.output_bitstream);
            }
            self.output_bitstream = ptr::null_mut();
        }
        // Never destroy a registered COM resource before unregistering it.
        for slot in self.registered.drain().map(|(_, item)| item) {
            unsafe {
                (self.api.nvEncUnregisterResource)(self.encoder, slot.handle);
            }
            drop(slot);
        }
        if !self.encoder.is_null() {
            unsafe { (self.api.nvEncDestroyEncoder)(self.encoder) };
            self.encoder = ptr::null_mut();
        }
    }
}
