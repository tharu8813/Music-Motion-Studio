//! Experimental Rust GPU frame-compositing core for Music Motion Studio.
//!
//! This is intentionally a separate crate so the production PyO3 wheel remains
//! 0.3.0, and existing 1080p/60 NVENC exports cannot regress. It implements
//! a REAL GPU compute pass, not merely a capability probe.
//!
//! v0.1 supports opaque RGBA8 bases plus multiple straight-alpha RGBA8
//! rectangular overlays at integer offsets. No Qt text, blur, timing, NVENC
//! device interop or alpha-premultiplied inputs are accepted yet. The entire
//! chain of overlays stays in GPU storage buffers; readback happens just ONCE.
//! Do not use this output for production until full Canvas pixel parity and a
//! shared GPU video-frame path are proven.

#[cfg(windows)]
pub mod d3d11_nv12;

#[cfg(windows)]
pub mod d3d11_frame_pool;

#[cfg(windows)]
pub mod d3d11_bgra_video;

#[cfg(windows)]
pub mod d3d11_nvenc_registration;

pub mod qt_canvas_packet;

mod session;
pub use session::{rgba_to_nv12_reference, GpuFrameSession, PixelRect};

use std::sync::mpsc;
use std::time::Duration;
use wgpu::util::DeviceExt;

pub const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_OVERLAYS: usize = 32;

#[derive(Clone, Copy)]
pub struct Overlay<'a> {
    pub pixels: &'a [u8],
    pub width: u32,
    pub height: u32,
    pub x: i32,
    pub y: i32,
}

fn checked_rgba_bytes(width: u32, height: u32) -> Result<usize, String> {
    if width == 0 || height == 0 {
        return Err("Frame dimensions must be positive".to_owned());
    }
    let bytes = (width as usize)
        .checked_mul(height as usize)
        .and_then(|x| x.checked_mul(4))
        .ok_or_else(|| "Frame dimensions overflow".to_owned())?;
    if bytes > MAX_FRAME_BYTES {
        return Err("Frame exceeds 64 MiB GPU-compositor limit".to_owned());
    }
    Ok(bytes)
}

fn validate<'a>(
    base: &[u8], width: u32, height: u32, overlays: &[Overlay<'a>],
) -> Result<usize, String> {
    let count = checked_rgba_bytes(width, height)?;
    if base.len() != count {
        return Err("Opaque base frame has incorrect RGBA8 byte length".to_owned());
    }
    if base.chunks_exact(4).any(|pixel| pixel[3] != 255) {
        return Err("Only opaque RGBA8 base frames are supported".to_owned());
    }
    if overlays.len() > MAX_OVERLAYS {
        return Err("Too many overlay layers".to_owned());
    }
    for layer in overlays {
        if layer.pixels.len() != checked_rgba_bytes(layer.width, layer.height)? {
            return Err("Overlay RGBA8 frame has incorrect byte length".to_owned());
        }
    }
    Ok(count)
}

fn blend_opaque(base: &mut [u8], top: &[u8]) {
    let alpha = u32::from(top[3]);
    if alpha == 0 {
        return;
    }
    if alpha == 255 {
        base.copy_from_slice(top);
        base[3] = 255;
        return;
    }
    let inverse = 255 - alpha;
    for channel in 0..3 {
        base[channel] =
            ((u32::from(top[channel]) * alpha
                + u32::from(base[channel]) * inverse + 127) / 255) as u8;
    }
    base[3] = 255;
}

/// Strict byte-reference for the supported opaque-base rectangular-over case.
/// Composition is straight-alpha with integer round-to-nearest arithmetic.
/// It is not a drop-in replacement for Qt's premultiplied alpha or FFmpeg YUV.
pub fn composite_reference(
    base: &[u8], width: u32, height: u32, overlays: &[Overlay<'_>],
) -> Result<Vec<u8>, String> {
    validate(base, width, height, overlays)?;
    let mut out = base.to_vec();
    for layer in overlays {
        for y in 0..layer.height {
            let dest_y = i64::from(y) + i64::from(layer.y);
            if dest_y < 0 || dest_y >= i64::from(height) {
                continue;
            }
            for x in 0..layer.width {
                let dest_x = i64::from(x) + i64::from(layer.x);
                if dest_x < 0 || dest_x >= i64::from(width) {
                    continue;
                }
                let dst = ((dest_y as usize * width as usize) + dest_x as usize) * 4;
                let src = ((y as usize * layer.width as usize) + x as usize) * 4;
                blend_opaque(&mut out[dst..dst + 4], &layer.pixels[src..src + 4]);
            }
        }
    }
    Ok(out)
}

/// GPU compute compositor. All intermediate composite surfaces are GPU
/// storage buffers, reused between overlay passes. This milestone still
/// requires a final GPU->CPU readback for its reference comparison.
pub struct GpuCompositor {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    pub adapter_name: String,
    pub adapter_backend: String,
    pub adapter_vendor_id: u32,
    pub adapter_device_id: u32,
}

impl GpuCompositor {
    pub fn new() -> Result<Self, String> {
        Self::new_internal(false)
    }

    /// CI-only path that may execute shaders on a software Vulkan adapter
    /// such as Mesa lavapipe. Production new() NEVER accepts software GPUs.
    #[cfg(test)]
    pub(crate) fn new_for_tests() -> Result<Self, String> {
        let allow_software = std::env::var("MMS_GPU_TEST_ALLOW_SOFTWARE")
            .map(|value| value == "1").unwrap_or(false);
        Self::new_internal(allow_software)
    }

    fn new_internal(allow_software: bool) -> Result<Self, String> {
        let instance = wgpu::Instance::default();
        let adapter = pollster::block_on(instance.request_adapter(
            &wgpu::RequestAdapterOptions {
                power_preference: wgpu::PowerPreference::HighPerformance,
                compatible_surface: None,
                force_fallback_adapter: allow_software,
            }
        )).map_err(|error| format!("GPU adapter unavailable: {error}"))?;
        let adapter_info = adapter.get_info();
        if !allow_software && !matches!(
            adapter_info.device_type,
            wgpu::DeviceType::DiscreteGpu | wgpu::DeviceType::IntegratedGpu
        ) {
            return Err(format!(
                "Not a hardware GPU: {:?} ({})",
                adapter_info.device_type, adapter_info.name,
            ));
        }
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor {
                label: Some("Music Motion Studio GPU export compositor"),
                ..Default::default()
            },
        )).map_err(|error| format!("GPU logical device failed: {error}"))?;
        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("Opaque RGBA8 over shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("composite.wgsl").into()),
        });
        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("Integer RGBA overlay pipeline"),
            layout: None,
            module: &shader,
            entry_point: Some("main"),
            compilation_options: Default::default(),
            cache: None,
        });
        Ok(Self {
            device,
            queue,
            pipeline,
            adapter_name: adapter_info.name,
            adapter_backend: format!("{:?}", adapter_info.backend),
            adapter_vendor_id: adapter_info.vendor,
            adapter_device_id: adapter_info.device,
        })
    }

    pub fn composite(
        &self, base: &[u8], width: u32, height: u32,
        overlays: &[Overlay<'_>],
    ) -> Result<Vec<u8>, String> {
        let bytes = validate(base, width, height, overlays)?;
        if overlays.is_empty() {
            return Ok(base.to_vec());
        }
        let initial = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("Uploaded opaque Canvas frame"),
            contents: base,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let make_scratch = |label| self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label),
            size: bytes as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });
        let scratch_a = make_scratch("Canvas overlay accumulation A");
        let scratch_b = make_scratch("Canvas overlay accumulation B");
        let staging = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("Single final GPU-to-CPU export readback"),
            size: bytes as u64,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let layout = self.pipeline.get_bind_group_layout(0);
        let mut encoder = self.device.create_command_encoder(
            &wgpu::CommandEncoderDescriptor {
                label: Some("Canvas GPU overlay frame encoder"),
            },
        );
        for (index, overlay) in overlays.iter().enumerate() {
            let overlay_gpu = self.device.create_buffer_init(
                &wgpu::util::BufferInitDescriptor {
                    label: Some("Uploaded Canvas RGBA overlay"),
                    contents: overlay.pixels,
                    usage: wgpu::BufferUsages::STORAGE,
                },
            );
            let mut geometry = [0u8; 32];
            for (offset, value) in [
                width, height, overlay.width, overlay.height,
                overlay.x as u32, overlay.y as u32, 0, 0,
            ].iter().enumerate() {
                geometry[offset * 4..offset * 4 + 4]
                    .copy_from_slice(&value.to_le_bytes());
            }
            let uniforms = self.device.create_buffer_init(
                &wgpu::util::BufferInitDescriptor {
                    label: Some("Canvas alpha layer geometry"),
                    contents: &geometry,
                    usage: wgpu::BufferUsages::UNIFORM,
                },
            );
            let input: &wgpu::Buffer = match index {
                0 => &initial,
                i if i % 2 == 1 => &scratch_a,
                _ => &scratch_b,
            };
            let output: &wgpu::Buffer =
                if index % 2 == 0 { &scratch_a } else { &scratch_b };
            let bindings = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some("Canvas alpha composition bindings"),
                layout: &layout,
                entries: &[
                    wgpu::BindGroupEntry { binding: 0, resource: input.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 1, resource: overlay_gpu.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 2, resource: output.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 3, resource: uniforms.as_entire_binding() },
                ],
            });
            {
                let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                    label: Some("GPU Canvas alpha-over"),
                    timestamp_writes: None,
                });
                pass.set_pipeline(&self.pipeline);
                pass.set_bind_group(0, &bindings, &[]);
                pass.dispatch_workgroups(width.div_ceil(16), height.div_ceil(16), 1);
            }
        }
        let final_buffer = if overlays.len() % 2 == 1 {
            &scratch_a
        } else {
            &scratch_b
        };
        encoder.copy_buffer_to_buffer(final_buffer, 0, &staging, 0, bytes as u64);
        let submission = self.queue.submit(Some(encoder.finish()));
        let (sender, receiver) = mpsc::channel();
        let slice = staging.slice(..);
        slice.map_async(wgpu::MapMode::Read, move |result| {
            let _ = sender.send(result);
        });
        self.device.poll(wgpu::PollType::Wait {
            submission_index: Some(submission),
            timeout: Some(Duration::from_secs(30)),
        }).map_err(|error| format!("GPU export readback timed out: {error:?}"))?;
        receiver.recv_timeout(Duration::from_secs(30))
            .map_err(|error| format!("GPU readback callback lost: {error}"))?
            .map_err(|error| format!("GPU readback failed: {error}"))?;
        let mapped = slice.get_mapped_range();
        let pixels = mapped.to_vec();
        drop(mapped);
        staging.unmap();
        if pixels.len() != bytes {
            return Err("GPU returned an incorrectly sized frame".to_owned());
        }
        Ok(pixels)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unsupported_layouts_and_unbounded_frames() {
        assert!(composite_reference(&[0; 7], 2, 1, &[]).is_err());
        assert!(composite_reference(&[0; 4], 0, 1, &[]).is_err());
        assert!(composite_reference(&[0; 4], 1, 1, &[]).is_err());
        assert!(checked_rgba_bytes(u32::MAX, u32::MAX).is_err());
        let solid = [100, 150, 200, 255];
        let bad_overlay = Overlay {
            pixels: &[1, 2], width: 1, height: 1, x: 0, y: 0,
        };
        assert!(composite_reference(&solid, 1, 1, &[bad_overlay]).is_err());
    }

    #[test]
    fn exact_integer_alpha_blend_and_opaque_copy() {
        let initial = [30, 100, 190, 255, 44, 50, 66, 255];
        let pixels = [255, 0, 0, 128, 0, 255, 0, 255];
        let overlay = Overlay {
            pixels: &pixels, width: 2, height: 1, x: 0, y: 0,
        };
        let result = composite_reference(&initial, 2, 1, &[overlay]).unwrap();
        assert_eq!(&result[0..4], &[143, 50, 95, 255]);
        assert_eq!(&result[4..8], &[0, 255, 0, 255]);
    }

    #[test]
    fn overlapping_layers_remain_ordered_and_clipped() {
        let base = vec![0, 0, 0, 255].repeat(16);
        let red = vec![255, 0, 0, 255].repeat(9);
        let translucent = vec![0, 255, 0, 128].repeat(4);
        let layers = [
            Overlay { pixels: &red, width: 3, height: 3, x: -1, y: 0 },
            Overlay { pixels: &translucent, width: 2, height: 2, x: 1, y: 1 },
        ];
        let result = composite_reference(&base, 4, 4, &layers).unwrap();
        assert_eq!(&result[0..4], &[255, 0, 0, 255]);
        let index = (1 * 4 + 1) * 4;
        assert_eq!(&result[index..index + 4], &[127, 128, 0, 255]);
        assert_eq!(&result[15 * 4..16 * 4], &[0, 0, 0, 255]);
    }

    #[test]
    fn gpu_matches_byte_reference_if_hardware_present() {
        // CI-hosted Windows runners often have no graphics device; a CPU/WARP
        // adapter must not masquerade as evidence of GPU speed.
        let gpu = match GpuCompositor::new_for_tests() {
            Ok(gpu) => gpu,
            Err(error) => {
                if std::env::var("MMS_GPU_TEST_REQUIRE_ADAPTER").as_deref() == Ok("1") {
                    panic!("GPU parity requires Vulkan adapter in CI: {error}");
                }
                eprintln!("GPU adapter unavailable; skipping optional parity: {error}");
                return;
            }
        };
        let base = vec![27, 46, 82, 255].repeat(64 * 48);
        let red = vec![245, 13, 35, 128].repeat(30 * 25);
        let blue = vec![0, 67, 255, 224].repeat(27 * 22);
        let layers = [
            Overlay { pixels: &red, width: 30, height: 25, x: 4, y: 2 },
            Overlay { pixels: &blue, width: 27, height: 22, x: -4, y: 18 },
        ];
        let expected = composite_reference(&base, 64, 48, &layers).unwrap();
        let actual = gpu.composite(&base, 64, 48, &layers).unwrap();
        assert_eq!(actual, expected, "GPU blend pixels diverged from Rust reference");
    }
}
