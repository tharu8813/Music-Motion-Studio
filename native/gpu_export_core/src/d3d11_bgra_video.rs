//! GPU-only conversion of a finished BGRA D3D11 texture to a native NV12
//! video surface, on the EXACT SAME Direct3D11 device as the frame pool.
//!
//! Unlike the existing wgpu Vulkan storage-buffer experiment, this backend
//! accepts a native BGRA ID3D11Texture2D and produces a hardware NV12
//! ID3D11Texture2D. No GPU->CPU download is used in convert_to_nv12().
//!
//! This is an experimental pixel-format bridge, NOT NVENC or a Vulkan/D3D11
//! shared-resource implementation. The driver video processor chooses the
//! precise chroma coefficients/ranges until a separate color-parity gate is
//! validated. Never silently switch production exports to this backend.

use std::mem::ManuallyDrop;

use windows::core::{BOOL, Interface};
use windows::Win32::Graphics::Direct3D11::{
    D3D11_BIND_RENDER_TARGET, D3D11_BIND_SHADER_RESOURCE,
    D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT,
    D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE, D3D11_VIDEO_PROCESSOR_CONTENT_DESC,
    D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT,
    D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT,
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC,
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC,
    D3D11_VIDEO_PROCESSOR_STREAM,
    D3D11_VIDEO_USAGE_PLAYBACK_NORMAL, D3D11_VPIV_DIMENSION_TEXTURE2D,
    D3D11_VPOV_DIMENSION_TEXTURE2D, ID3D11Device,
    ID3D11DeviceContext, ID3D11Texture2D, ID3D11VideoDevice,
    ID3D11VideoContext, ID3D11VideoProcessor,
    ID3D11VideoProcessorEnumerator, ID3D11VideoProcessorOutputView,
};
use windows::Win32::Graphics::Dxgi::Common::{
    DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_NV12, DXGI_RATIONAL,
    DXGI_SAMPLE_DESC,
};

use crate::d3d11_frame_pool::NativeNv12FramePool;

pub fn validate_opaque_bgra(
    width: u32, height: u32, bytes: &[u8],
) -> Result<(), String> {
    if width < 2 || height < 2 || width > 3840 || height > 2160 ||
        width % 2 != 0 || height % 2 != 0
    {
        return Err("GPU BGRA dimensions must be even and within 2..3840x2..2160".into());
    }
    let expected = (width as usize).checked_mul(height as usize)
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| "BGRA byte count overflow".to_owned())?;
    if bytes.len() != expected {
        return Err("BGRA pixels have incorrect length".to_owned());
    }
    if bytes.chunks_exact(4).any(|px| px[3] != 255) {
        return Err("GPU NV12 video processor only accepts opaque BGRA".to_owned());
    }
    Ok(())
}

pub struct NativeBgraNv12VideoProcessor {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    video_device: ID3D11VideoDevice,
    video_context: ID3D11VideoContext,
    enumerator: ID3D11VideoProcessorEnumerator,
    processor: ID3D11VideoProcessor,
    output: ID3D11Texture2D,
    output_view: ID3D11VideoProcessorOutputView,
    width: u32,
    height: u32,
}

impl NativeBgraNv12VideoProcessor {
    /// Construct on the same D3D11 device as the NV12 frame pool. This
    /// completely avoids comparing just PCI vendor/device IDs for access.
    pub fn new_for_pool(pool: &NativeNv12FramePool) -> Result<Self, String> {
        let (width, height) = pool.dimensions();
        let device = pool.native_device().clone();
        let context = pool.native_context().clone();
        let video_device: ID3D11VideoDevice = device.cast()
            .map_err(|e| format!("D3D11 hardware video device missing: {e}"))?;
        let video_context: ID3D11VideoContext = context.cast()
            .map_err(|e| format!("D3D11 hardware video context missing: {e}"))?;
        let content = D3D11_VIDEO_PROCESSOR_CONTENT_DESC {
            InputFrameFormat: D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE,
            InputFrameRate: DXGI_RATIONAL { Numerator: 60, Denominator: 1 },
            InputWidth: width,
            InputHeight: height,
            OutputFrameRate: DXGI_RATIONAL { Numerator: 60, Denominator: 1 },
            OutputWidth: width,
            OutputHeight: height,
            Usage: D3D11_VIDEO_USAGE_PLAYBACK_NORMAL,
        };
        let enumerator = unsafe {
            video_device.CreateVideoProcessorEnumerator(&content)
        }.map_err(|e| format!("D3D11 video processor unsupported: {e}"))?;
        let bgra_flags = unsafe {
            enumerator.CheckVideoProcessorFormat(DXGI_FORMAT_B8G8R8A8_UNORM)
        }.map_err(|e| format!("D3D11 BGRA input capability query failed: {e}"))?;
        let nv12_flags = unsafe {
            enumerator.CheckVideoProcessorFormat(DXGI_FORMAT_NV12)
        }.map_err(|e| format!("D3D11 NV12 output capability query failed: {e}"))?;
        if bgra_flags & (D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT.0 as u32) == 0 {
            return Err("D3D11 video processor does not accept BGRA inputs".to_owned());
        }
        if nv12_flags & (D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT.0 as u32) == 0 {
            return Err("D3D11 video processor does not generate NV12 outputs".to_owned());
        }
        let processor = unsafe { video_device.CreateVideoProcessor(&enumerator, 0) }
            .map_err(|e| format!("Cannot create D3D11 BGRA->NV12 processor: {e}"))?;

        // The video processor requires a render-target-bindable NV12 output.
        // This intermediate is a GPU texture, and is reused across frames.
        // The frame pool's video surfaces are copied to on the SAME GPU.
        let desc = D3D11_TEXTURE2D_DESC {
            Width: width, Height: height, MipLevels: 1, ArraySize: 1,
            Format: DXGI_FORMAT_NV12,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: D3D11_BIND_RENDER_TARGET.0 as u32,
            CPUAccessFlags: 0, MiscFlags: 0,
        };
        let mut output = None;
        unsafe { device.CreateTexture2D(&desc, None, Some(&mut output)) }
            .map_err(|e| format!("Cannot create GPU NV12 video processor output: {e}"))?;
        let output = output.ok_or("D3D11 processor returned no NV12 surface")?;
        let view_desc = D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC {
            ViewDimension: D3D11_VPOV_DIMENSION_TEXTURE2D,
            ..Default::default()
        };
        let mut output_view = None;
        unsafe {
            video_device.CreateVideoProcessorOutputView(
                &output, &enumerator, &view_desc, Some(&mut output_view),
            )
        }.map_err(|e| format!("Cannot create NV12 video output view: {e}"))?;
        let output_view = output_view.ok_or("Missing NV12 video output view")?;
        Ok(Self {
            device, context, video_device, video_context, enumerator,
            processor, output, output_view, width, height,
        })
    }

    /// Create an opaque full-frame BGRA input texture on this same D3D11
    /// device. A future native D3D11 renderer can instead provide its own
    /// BGRA texture made on this device.
    pub fn create_bgra_input_texture(&self) -> Result<ID3D11Texture2D, String> {
        let desc = D3D11_TEXTURE2D_DESC {
            Width: self.width, Height: self.height, MipLevels: 1, ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: D3D11_BIND_SHADER_RESOURCE.0 as u32,
            CPUAccessFlags: 0, MiscFlags: 0,
        };
        let mut texture = None;
        unsafe { self.device.CreateTexture2D(&desc, None, Some(&mut texture)) }
            .map_err(|e| format!("Unable to allocate BGRA GPU source texture: {e}"))?;
        texture.ok_or_else(|| "D3D11 returned no BGRA texture".into())
    }

    fn validate_source(&self, texture: &ID3D11Texture2D) -> Result<(), String> {
        let parent = unsafe { texture.GetDevice() }
            .map_err(|e| format!("Cannot inspect video source GPU device: {e}"))?;
        if parent.as_raw() != self.device.as_raw() {
            return Err("BGRA input texture belongs to a different D3D11 device".into());
        }
        let mut description = D3D11_TEXTURE2D_DESC::default();
        unsafe { texture.GetDesc(&mut description) };
        if description.Width != self.width || description.Height != self.height
            || description.Format != DXGI_FORMAT_B8G8R8A8_UNORM
            || description.MipLevels != 1 || description.ArraySize != 1
        {
            return Err("D3D11 source must be matching full-size BGRA Texture2D".into());
        }
        Ok(())
    }

    /// Synthetic-input helper only: uploads CPU-generated opaque BGRA bytes.
    /// The video processor itself never downloads frames from the GPU.
    pub fn upload_bgra_for_validation(
        &self, texture: &ID3D11Texture2D, pixels: &[u8],
    ) -> Result<(), String> {
        self.validate_source(texture)?;
        validate_opaque_bgra(self.width, self.height, pixels)?;
        unsafe {
            self.context.UpdateSubresource(
                texture, 0, None, pixels.as_ptr().cast(),
                self.width * 4, 0,
            );
        }
        Ok(())
    }

    /// GPU->GPU video conversion: source BGRA texture -> persistent NV12
    /// intermediate. No system-memory readback or raw FFmpeg pipe.
    /// The conversion is ordered by the same D3D11 immediate context.
    pub fn convert_gpu_bgra_to_nv12(
        &self, source: &ID3D11Texture2D,
    ) -> Result<&ID3D11Texture2D, String> {
        self.validate_source(source)?;
        let input_desc = D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
            FourCC: 0, ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
            ..Default::default()
        };
        let mut input_view = None;
        unsafe {
            self.video_device.CreateVideoProcessorInputView(
                source, &self.enumerator, &input_desc, Some(&mut input_view),
            )
        }.map_err(|e| format!("Cannot create BGRA video input view: {e}"))?;
        let input_view = input_view.ok_or("D3D11 returned no BGRA video input view")?;

        // D3D11_VIDEO_PROCESSOR_STREAM's COM field is ManuallyDrop; release
        // its additional reference after the blit. The original input_view
        // stays alive until after VideoProcessorBlt.
        let mut stream = D3D11_VIDEO_PROCESSOR_STREAM {
            Enable: BOOL(1),
            pInputSurface: ManuallyDrop::new(Some(input_view.clone())),
            ..Default::default()
        };
        let result = unsafe {
            self.video_context.VideoProcessorBlt(
                &self.processor, &self.output_view, 0, std::slice::from_ref(&stream),
            )
        };
        unsafe { ManuallyDrop::drop(&mut stream.pInputSurface) };
        result.map_err(|e| format!("D3D11 GPU BGRA->NV12 conversion failed: {e}"))?;
        Ok(&self.output)
    }

    pub(crate) fn device(&self) -> &ID3D11Device {
        &self.device
    }

    pub fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_invalid_opaque_bgra_before_gpu_work() {
        assert!(validate_opaque_bgra(0, 2, &[]).is_err());
        assert!(validate_opaque_bgra(3, 2, &[0; 24]).is_err());
        assert!(validate_opaque_bgra(4, 2, &[0; 31]).is_err());
        assert!(validate_opaque_bgra(4, 2, &[0; 32]).is_err());
        let solid = vec![17, 42, 90, 255].repeat(8);
        assert!(validate_opaque_bgra(4, 2, &solid).is_ok());
    }
}
