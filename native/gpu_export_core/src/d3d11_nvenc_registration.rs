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
use std::ffi::c_void;
use std::ptr;

use mini_nvcodec::{try_init_nvenc, API_VERSION_LADDER, NvencApi, NvencApiVersion};
use mini_nvcodec::guids::{NV_ENC_CODEC_H264_GUID, NV_ENC_PRESET_P3_GUID};
use mini_nvcodec::nvenc_sys::{
    GUID, NVENCSTATUS, NV_ENC_BUFFER_FORMAT, NV_ENC_BUFFER_USAGE,
    NV_ENC_DEVICE_TYPE, NV_ENC_INITIALIZE_PARAMS, NV_ENC_INPUT_RESOURCE_TYPE,
    NV_ENC_MAP_INPUT_RESOURCE, NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS,
    NV_ENC_PRESET_CONFIG, NV_ENC_REGISTER_RESOURCE, NV_ENC_TUNING_INFO,
};
use windows::core::Interface;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_TEXTURE2D_DESC, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_NV12;

use crate::d3d11_frame_pool::{NativeNv12FramePool, ReservedGpuFrame};

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
    registered: HashMap<usize, RegisteredNv12>,
    successful_maps: usize,
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
            let mut result = Self {
                api, encoder: session, version, width, height,
                registered: HashMap::new(), successful_maps: 0,
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
            // Use the driver-provided preset, not hand-built codec state.
            // Only initialize the encoder; no output buffer or bitstream
            // creation is performed in this compatibility diagnostic.
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

    /// Register once per pool slot and map/unmap the GPU texture to prove
    /// it can be consumed by the in-process encoder. No actual picture
    /// submission is performed, so unmapping is immediately safe here.
    ///
    /// Safety boundary: the caller MUST have acquired this exact slot
    /// through acquire_gpu_complete_ready(), guaranteeing producer-copy
    /// completion; real encoding needs separate consumer completion.
    pub fn probe_gpu_complete_frame(
        &mut self, frame: &ReservedGpuFrame,
    ) -> Result<(), String> {
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { frame.texture.GetDesc(&mut desc) };
        if desc.Width != self.width || desc.Height != self.height
            || desc.Format != DXGI_FORMAT_NV12
            || desc.MipLevels != 1 || desc.ArraySize != 1
        {
            return Err("NVENC input must be an exact-size NV12 D3D11 texture".into());
        }
        // Same-device verification is inherent to NVENC registration, but
        // check the current registered slot texture is byte-identical COM
        // identity before reusing its persistent registered handle.
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
        // Even on unexpected output, unmap before returning an error.
        let format_matches = map.mappedBufferFmt == NV_ENC_BUFFER_FORMAT::NV12;
        let had_pointer = !map.mappedResource.is_null();
        if had_pointer {
            check(
                unsafe {
                    (self.api.nvEncUnmapInputResource)(
                        self.encoder, map.mappedResource,
                    )
                },
                "NvEncUnmapInputResource",
            )?;
        }
        if !format_matches || !had_pointer {
            return Err("NVENC mapped resource had invalid NV12 format or pointer".into());
        }
        self.successful_maps += 1;
        Ok(())
    }
}

impl Drop for NativeNvencRegistrationSession {
    fn drop(&mut self) {
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
