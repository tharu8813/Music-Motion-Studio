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
    D3D11_VIDEO_PROCESSOR_CAPS, D3D11_VIDEO_PROCESSOR_FEATURE_CAPS_ALPHA_STREAM,
    D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT,
    D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT,
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC,
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC,
    D3D11_VIDEO_PROCESSOR_STREAM,
    D3D11_VIDEO_USAGE_PLAYBACK_NORMAL, D3D11_VPIV_DIMENSION_TEXTURE2D,
    D3D11_VPOV_DIMENSION_TEXTURE2D, ID3D11Device,
    ID3D11DeviceContext, ID3D11Texture2D, ID3D11VideoDevice,
    ID3D11VideoProcessorInputView,
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

/// One full-frame GPU BGRA Canvas layer in bottom-to-top paint order.
///
/// The alpha in each GPU pixel must be STRAIGHT (not QImage's premultiplied
/// variant). The D3D11 video processor additionally applies layer.opacity
/// as planar alpha, so no CPU bitmap flattening is needed.
pub struct NativeCanvasBgraLayer<'a> {
    pub texture: &'a ID3D11Texture2D,
    pub opacity: f32,
}

pub const MAX_CANVAS_VIDEO_STREAMS: usize = 8;

/// Pure validation independent of the Windows GPU driver. The first layer
/// must be a full-frame opaque background, and further layers carry straight
/// per-pixel alpha. Reject invalid pixels before D3D11 UpdateSubresource.
pub fn validate_canvas_bgra_layer(
    width: u32, height: u32, pixels: &[u8],
) -> Result<(), String> {
    if width < 2 || height < 2 || width > 3840 || height > 2160
        || width % 2 != 0 || height % 2 != 0
    {
        return Err("Canvas BGRA dimensions must be even and <=3840x2160".into());
    }
    let expected = (width as usize).checked_mul(height as usize)
        .and_then(|n| n.checked_mul(4))
        .ok_or("Canvas BGRA byte count overflow")?;
    if pixels.len() != expected {
        return Err(format!("Canvas BGRA buffer must contain {expected} bytes"));
    }
    Ok(())
}

fn validate_canvas_opacities(opacities: &[f32]) -> Result<(), String> {
    if opacities.is_empty() || opacities.len() > MAX_CANVAS_VIDEO_STREAMS {
        return Err("D3D11 Canvas requires 1..=8 ordered input layers".into());
    }
    if !opacities.iter().all(|alpha| alpha.is_finite() && (0.0..=1.0).contains(alpha)) {
        return Err("Canvas layer opacity must be finite and in [0,1]".into());
    }
    if opacities[0] != 1.0 {
        return Err("Canvas base layer must have opacity 1.0".into());
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

    /// Bind flags used by the synthetic BGRA input texture.
    ///
    /// A D3D11 VideoProcessorInputView cannot be created from a texture
    /// whose only bind flag is D3D11_BIND_SHADER_RESOURCE. According to
    /// CreateVideoProcessorInputView, the allowed nonzero combinations
    /// must include a video-processor-compatible flag (e.g. RENDER_TARGET).
    /// Retain SHADER_RESOURCE for future GPU scene/texture sampling.
    fn bgra_input_bind_flags() -> u32 {
        (D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET).0 as u32
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
            BindFlags: Self::bgra_input_bind_flags(),
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
        // A shader-resource-only texture is NOT valid for a video processor
        // input view. Catch this common E_INVALIDARG at the source boundary.
        // BindFlags=0 remains legal per D3D11 and is deliberately supported.
        let shader_only = D3D11_BIND_SHADER_RESOURCE.0 as u32;
        if description.BindFlags == shader_only {
            return Err(
                "BGRA texture has SHADER_RESOURCE-only bind flags: D3D11 video input views require 0 or a video-processor-compatible bind flag such as RENDER_TARGET".into()
            );
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
        }.map_err(|e| {
            let mut desc = D3D11_TEXTURE2D_DESC::default();
            unsafe { source.GetDesc(&mut desc) };
            format!(
                "Cannot create BGRA video input view: {e}; BindFlags=0x{:X}, Usage={:?}, Format={:?}, SampleCount={}",
                desc.BindFlags, desc.Usage, desc.Format, desc.SampleDesc.Count
            )
        })?;
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

    /// Upload BGRA Canvas pixels with straight alpha. Only a diagnostic
    /// bridge for Qt QImage layers: the future native GPU Canvas painter
    /// will supply resident textures and skip these CPU uploads entirely.
    /// The background image must be opaque; overlay images may have alpha.
    pub fn upload_canvas_layer_for_validation(
        &self, texture: &ID3D11Texture2D, pixels: &[u8],
    ) -> Result<(), String> {
        self.validate_source(texture)?;
        validate_canvas_bgra_layer(self.width, self.height, pixels)?;
        unsafe {
            self.context.UpdateSubresource(
                texture, 0, None, pixels.as_ptr().cast(), self.width * 4, 0,
            )
        };
        Ok(())
    }

    /// Composite native D3D11 BGRA layers in their original Canvas Z-order
    /// using the hardware video processor; write NV12 directly on this SAME
    /// device, no intermediate CPU flattened RGBA frame.
    ///
    /// The underlying video processor must expose MAX_INPUT_STREAMS and
    /// ALPHA_STREAM support. Unsupported adapters fail explicitly rather
    /// than silently producing an incorrect transparent subtitle blend.
    /// The BGRA source alpha is STRAIGHT; Qt ARGB32_Premultiplied must be
    /// converted to plain ARGB32 before GPU upload.
    ///
    /// Output is an intermediate GPU NV12 texture. The caller must issue
    /// a same-immediate-context CopyResource into its retained pool slot
    /// BEFORE calling this again, then await D3D11 query completion.
    pub fn composite_canvas_layers_to_nv12(
        &self, layers: &[NativeCanvasBgraLayer<'_>],
    ) -> Result<&ID3D11Texture2D, String> {
        let alphas: Vec<f32> = layers.iter().map(|l| l.opacity).collect();
        validate_canvas_opacities(&alphas)?;
        let mut caps = D3D11_VIDEO_PROCESSOR_CAPS::default();
        unsafe { self.enumerator.GetVideoProcessorCaps(&mut caps) }
            .map_err(|e| format!("Cannot query D3D11 multi-stream composition: {e}"))?;
        if layers.len() > caps.MaxInputStreams as usize ||
            layers.len() > caps.MaxStreamStates as usize
        {
            return Err(format!(
                "D3D11 video processor supports {} input streams and {} stream states; requested {}",
                caps.MaxInputStreams, caps.MaxStreamStates, layers.len(),
            ));
        }
        if layers.len() > 1 && caps.FeatureCaps &
            D3D11_VIDEO_PROCESSOR_FEATURE_CAPS_ALPHA_STREAM.0 as u32 == 0
        {
            return Err("D3D11 Canvas GPU layer alpha blending unsupported by the adapter".into());
        }

        let input_desc = D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC {
            FourCC: 0,
            ViewDimension: D3D11_VPIV_DIMENSION_TEXTURE2D,
            ..Default::default()
        };
        let mut views: Vec<ID3D11VideoProcessorInputView> = Vec::with_capacity(layers.len());
        for layer in layers {
            self.validate_source(layer.texture)?;
            let mut input_view = None;
            unsafe {
                self.video_device.CreateVideoProcessorInputView(
                    layer.texture, &self.enumerator, &input_desc,
                    Some(&mut input_view),
                )
            }.map_err(|e| format!("Cannot create Canvas GPU video input view: {e}"))?;
            views.push(input_view.ok_or("D3D11 Canvas input view was null")?);
        }

        // Per-pixel alpha (straight BGRA), multiplied by a planar alpha, is
        // applied by the video processor. Disable driver automatic filters
        // to avoid unspecified enhancement of Canvas text and colors.
        for (index, layer) in layers.iter().enumerate() {
            unsafe {
                self.video_context.VideoProcessorSetStreamAutoProcessingMode(
                    &self.processor, index as u32, false,
                );
                self.video_context.VideoProcessorSetStreamAlpha(
                    &self.processor, index as u32, index > 0, layer.opacity,
                );
            }
        }
        // D3D11_VIDEO_PROCESSOR_STREAM contains ManuallyDrop COM fields:
        // explicitly drop each extra cloned reference after VideoProcessorBlt.
        let mut streams: Vec<D3D11_VIDEO_PROCESSOR_STREAM> = views.iter()
            .map(|view| D3D11_VIDEO_PROCESSOR_STREAM {
                Enable: BOOL(1),
                pInputSurface: ManuallyDrop::new(Some(view.clone())),
                ..Default::default()
            })
            .collect();
        let operation = unsafe {
            self.video_context.VideoProcessorBlt(
                &self.processor, &self.output_view, 0, &streams,
            )
        };
        for stream in &mut streams {
            unsafe { ManuallyDrop::drop(&mut stream.pInputSurface) };
        }
        operation.map_err(|e| format!("D3D11 Canvas GPU layer composition failed: {e}"))?;
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
    fn canvas_layer_bytes_and_opacity_fail_closed() {
        assert!(validate_canvas_bgra_layer(4, 4, &[0; 63]).is_err());
        assert!(validate_canvas_bgra_layer(4, 4, &[0; 64]).is_ok());
        assert!(validate_canvas_bgra_layer(3, 4, &[0; 48]).is_err());
        assert!(validate_canvas_opacities(&[1.0, 0.25, 1.0]).is_ok());
        assert!(validate_canvas_opacities(&[]).is_err());
        assert!(validate_canvas_opacities(&[0.9, 1.0]).is_err());
        assert!(validate_canvas_opacities(&[1.0, f32::NAN]).is_err());
        assert!(validate_canvas_opacities(&[1.0, -0.2]).is_err());
        assert!(validate_canvas_opacities(&[1.0, 1.1]).is_err());
        assert!(validate_canvas_opacities(&[1.0; 9]).is_err());
    }

    #[test]
    fn bgra_video_input_binding_is_compatible_with_d3d11_video_processor() {
        let flags = NativeBgraNv12VideoProcessor::bgra_input_bind_flags();
        // Regression: SHADER_RESOURCE alone caused E_INVALIDARG
        // 0x80070057 on the user's RTX 5060 at CreateVideoProcessorInputView.
        assert_ne!(flags, D3D11_BIND_SHADER_RESOURCE.0 as u32);
        assert_ne!(flags & D3D11_BIND_RENDER_TARGET.0 as u32, 0);
        assert_ne!(flags & D3D11_BIND_SHADER_RESOURCE.0 as u32, 0);
    }

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
