//! Reusable GPU resources for repeated export frames.
//!
//! A session allocates upload, overlay, scratch, uniform, bind-group, and
//! readback resources once. Dynamic RGBA8 frames can update the existing
//! buffers. Composition without readback remains in GPU storage; the optional
//! readback is solely for parity tests and the legacy CPU output boundary.

use std::sync::mpsc;
use std::time::Duration;

use wgpu::util::DeviceExt;

use super::{checked_rgba_bytes, validate, GpuCompositor, Overlay};

/// Persistent buffers for one immutable geometry/layout.
///
/// Prepared layers keep their original order. The pixel data and base frame
/// may change via queue writes, but resizing or changing layer geometry
/// requires creating a new session.
pub struct GpuFrameSession {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    width: u32,
    height: u32,
    bytes: usize,
    base_buffer: wgpu::Buffer,
    overlay_buffers: Vec<wgpu::Buffer>,
    overlay_byte_lengths: Vec<usize>,
    overlay_sizes: Vec<(u32, u32)>,
    // Retain static geometry buffers through the lifetime of all bind groups.
    _geometry_buffers: Vec<wgpu::Buffer>,
    bind_groups: Vec<wgpu::BindGroup>,
    _scratch_a: wgpu::Buffer,
    _scratch_b: wgpu::Buffer,
    final_buffer: wgpu::Buffer,
    staging: wgpu::Buffer,
    nv12: Option<ResidentNv12>,
}

struct ResidentNv12 {
    pipeline: wgpu::ComputePipeline,
    bind_group: wgpu::BindGroup,
    // Keep the uniform allocation alive for the bind group.
    _geometry: wgpu::Buffer,
    _output: wgpu::Buffer,
    staging: wgpu::Buffer,
    bytes: usize,
}

impl GpuCompositor {
    /// Prepare once, then compose any number of same-layout frames.
    ///
    /// Overlay pixels are pre-uploaded, appropriate for static backgrounds
    /// and text overlays. Call update_overlay when pixels actually change.
    pub fn prepare_session(
        &self,
        base: &[u8],
        width: u32,
        height: u32,
        overlays: &[Overlay<'_>],
    ) -> Result<GpuFrameSession, String> {
        let bytes = validate(base, width, height, overlays)?;
        if overlays.is_empty() {
            return Err("GPU session requires at least one overlay layer".to_owned());
        }
        let device = self.device.clone();
        let queue = self.queue.clone();
        let pipeline = self.pipeline.clone();
        let base_buffer = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("Persistent Canvas RGBA base"),
            contents: base,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        });
        let allocate_scratch = |label| device.create_buffer(&wgpu::BufferDescriptor {
            label: Some(label),
            size: bytes as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });
        let scratch_a = allocate_scratch("Persistent Canvas compositor A");
        let scratch_b = allocate_scratch("Persistent Canvas compositor B");
        let final_buffer = if overlays.len() % 2 == 1 {
            scratch_a.clone()
        } else {
            scratch_b.clone()
        };
        let staging = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("Reusable final-frame CPU readback"),
            size: bytes as u64,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let layout = pipeline.get_bind_group_layout(0);
        let mut overlay_buffers = Vec::with_capacity(overlays.len());
        let mut overlay_byte_lengths = Vec::with_capacity(overlays.len());
        let mut overlay_sizes = Vec::with_capacity(overlays.len());
        let mut geometry_buffers = Vec::with_capacity(overlays.len());
        let mut bind_groups = Vec::with_capacity(overlays.len());
        for (index, layer) in overlays.iter().enumerate() {
            let gpu_layer = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("Persistent Canvas RGBA overlay"),
                contents: layer.pixels,
                usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
            });
            let values: [u32; 8] = [
                width, height, layer.width, layer.height,
                layer.x as u32, layer.y as u32, 0, 0,
            ];
            let mut geometry = [0u8; 32];
            for (i, value) in values.iter().enumerate() {
                geometry[i * 4..i * 4 + 4].copy_from_slice(&value.to_le_bytes());
            }
            let uniforms = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("Persistent layer geometry"),
                contents: &geometry,
                usage: wgpu::BufferUsages::UNIFORM,
            });
            let input = match index {
                0 => &base_buffer,
                n if n % 2 == 1 => &scratch_a,
                _ => &scratch_b,
            };
            let output = if index % 2 == 0 { &scratch_a } else { &scratch_b };
            let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some("Persistent RGBA overlay bindings"),
                layout: &layout,
                entries: &[
                    wgpu::BindGroupEntry { binding: 0, resource: input.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 1, resource: gpu_layer.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 2, resource: output.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 3, resource: uniforms.as_entire_binding() },
                ],
            });
            overlay_byte_lengths.push(layer.pixels.len());
            overlay_sizes.push((layer.width, layer.height));
            overlay_buffers.push(gpu_layer);
            geometry_buffers.push(uniforms);
            bind_groups.push(bind_group);
        }
        Ok(GpuFrameSession {
            device,
            queue,
            pipeline,
            width,
            height,
            bytes,
            base_buffer,
            overlay_buffers,
            overlay_byte_lengths,
            overlay_sizes,
            _geometry_buffers: geometry_buffers,
            bind_groups,
            _scratch_a: scratch_a,
            _scratch_b: scratch_b,
            final_buffer,
            staging,
            nv12: None,
        })
    }
}

/// A tightly packed RGBA8 subrectangle in buffer-local pixel coordinates.
///
/// The GPU buffers are linear and lack row padding. For a rectangle narrower
/// than the source, queue.write_buffer is issued once per row. Full-width
/// rectangles use one contiguous buffer write. This saves *bytes*, not always
/// calls or elapsed time; RTX performance needs A/B measurement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PixelRect {
    pub x: u32,
    pub y: u32,
    pub width: u32,
    pub height: u32,
}

fn validate_patch(
    canvas: (u32, u32), patch: PixelRect, bytes: &[u8], opaque: bool,
) -> Result<(), String> {
    if patch.width == 0 || patch.height == 0
        || patch.x.checked_add(patch.width).is_none_or(|x| x > canvas.0)
        || patch.y.checked_add(patch.height).is_none_or(|y| y > canvas.1)
    {
        return Err("RGBA patch rectangle is empty or outside source bounds".into());
    }
    let expected = (patch.width as usize)
        .checked_mul(patch.height as usize)
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| "RGBA patch dimensions overflow".to_owned())?;
    if expected != bytes.len() {
        return Err("RGBA patch must contain tightly packed width*height*4 bytes".into());
    }
    if opaque && bytes.chunks_exact(4).any(|p| p[3] != 255) {
        return Err("Opaque Canvas base patch contains non-opaque pixels".into());
    }
    Ok(())
}

fn write_patch(
    queue: &wgpu::Queue, buffer: &wgpu::Buffer, canvas: (u32, u32),
    rect: PixelRect, pixels: &[u8], opaque: bool,
) -> Result<usize, String> {
    // All preconditions are validated before the first GPU write; failed
    // requests cannot partially alter the prior frame.
    validate_patch(canvas, rect, pixels, opaque)?;
    let stride = rect.width as usize * 4;
    if rect.x == 0 && rect.width == canvas.0 {
        let offset = rect.y as u64 * canvas.0 as u64 * 4;
        queue.write_buffer(buffer, offset, pixels);
        return Ok(pixels.len());
    }
    for row in 0..rect.height as usize {
        let dst = ((rect.y as u64 + row as u64) * canvas.0 as u64
            + rect.x as u64) * 4;
        queue.write_buffer(
            buffer, dst, &pixels[row * stride .. (row + 1) * stride],
        );
    }
    Ok(pixels.len())
}

impl GpuFrameSession {
    pub fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    pub fn overlay_count(&self) -> usize {
        self.overlay_buffers.len()
    }

    /// Update only changed pixel contents, preserving all GPU allocations.
    pub fn update_base(&mut self, pixels: &[u8]) -> Result<(), String> {
        if pixels.len() != self.bytes || pixels.chunks_exact(4).any(|p| p[3] != 255) {
            return Err("Updated Canvas base must be the same-sized opaque RGBA8 image".to_owned());
        }
        self.queue.write_buffer(&self.base_buffer, 0, pixels);
        Ok(())
    }

    /// Preserve layer count, size, placement, alpha contract, and ordering.
    pub fn update_overlay(&mut self, index: usize, pixels: &[u8]) -> Result<(), String> {
        let expected = self.overlay_byte_lengths.get(index)
            .ok_or_else(|| "Overlay index out of bounds".to_owned())?;
        if pixels.len() != *expected {
            return Err("Updated overlay byte length changed; rebuild the GPU session".to_owned());
        }
        self.queue.write_buffer(&self.overlay_buffers[index], 0, pixels);
        Ok(())
    }

    /// Update a dirty base-frame rectangle without reuploading all pixels.
    /// Only opaque RGBA8 is supported for the base.
    pub fn update_base_rect(
        &mut self, rect: PixelRect, rgba: &[u8],
    ) -> Result<usize, String> {
        write_patch(
            &self.queue, &self.base_buffer, (self.width, self.height),
            rect, rgba, true,
        )
    }

    /// Update a dirty rectangle of an existing alpha overlay in-place.
    /// Layer size, location, order and GPU bindings remain unchanged.
    pub fn update_overlay_rect(
        &mut self, index: usize, rect: PixelRect, rgba: &[u8],
    ) -> Result<usize, String> {
        let dims = self.overlay_sizes.get(index)
            .copied().ok_or_else(|| "Overlay index out of bounds".to_owned())?;
        write_patch(
            &self.queue, &self.overlay_buffers[index], dims,
            rect, rgba, false,
        )
    }

    fn dispatch(&self, readback: bool) -> Result<Option<Vec<u8>>, String> {
        let mut encoder = self.device.create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("Reusable Canvas GPU compositor"),
        });
        for group in &self.bind_groups {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("Persistent Canvas alpha-over"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, group, &[]);
            pass.dispatch_workgroups(self.width.div_ceil(16), self.height.div_ceil(16), 1);
        }
        if readback {
            encoder.copy_buffer_to_buffer(
                &self.final_buffer, 0, &self.staging, 0, self.bytes as u64,
            );
        }
        let submission = self.queue.submit(Some(encoder.finish()));
        if !readback {
            // This waits for all GPU work to finish. It is NOT simply
            // measuring queue-submission latency.
            self.device.poll(wgpu::PollType::Wait {
                submission_index: Some(submission),
                timeout: Some(Duration::from_secs(30)),
            }).map_err(|error| format!("GPU resident execution failed: {error:?}"))?;
            return Ok(None);
        }

        // A staging buffer must be unmapped before it is reused by the next
        // GPU copy. Mapping also forces synchronization for frame validation.
        let (sender, receiver) = mpsc::channel();
        let view = self.staging.slice(..);
        view.map_async(wgpu::MapMode::Read, move |result| {
            let _ = sender.send(result);
        });
        self.device.poll(wgpu::PollType::Wait {
            submission_index: Some(submission),
            timeout: Some(Duration::from_secs(30)),
        }).map_err(|error| format!("GPU frame download failed: {error:?}"))?;
        receiver.recv_timeout(Duration::from_secs(30))
            .map_err(|error| format!("GPU frame readback callback lost: {error}"))?
            .map_err(|error| format!("GPU readback failed: {error}"))?;
        let mapped = view.get_mapped_range();
        let result = mapped.to_vec();
        drop(mapped);
        self.staging.unmap();
        if result.len() != self.bytes {
            return Err("GPU session returned an incorrectly sized frame".to_owned());
        }
        Ok(Some(result))
    }

    /// Submit all overlay passes and wait for GPU completion, **without**
    /// downloading frame bytes into system RAM.
    ///
    /// The final result remains in self.final_buffer for future native
    /// encoder interop. NVENC interop is NOT implemented in this milestone.
    pub fn compose_gpu_only(&self) -> Result<(), String> {
        self.dispatch(false).map(|_| ())
    }

    /// Diagnostic equivalent of compose_gpu_only with one final readback.
    /// This is never the production pipeline until full Canvas parity exists.
    pub fn compose_and_readback(&self) -> Result<Vec<u8>, String> {
        self.dispatch(true)?.ok_or_else(|| "No GPU image returned".to_owned())
    }
}

/// Convert an opaque RGBA8 frame into tightly-packed NV12 using a specified
/// integer BT.709-limited approximation. This is an independent CPU reference
/// for the GPU shader, NOT a claim of byte parity with FFmpeg swscale.
///
/// Y = clamp(16 + round((47R + 157G + 16B)/256), 16, 235)
/// U = clamp(128 + (-26R -87G +113B +128)/256, 16, 240)
/// V = clamp(128 + (113R -102G -11B +128)/256, 16, 240)
/// Chroma samples use a 2x2 arithmetic RGB average before conversion.
pub fn rgba_to_nv12_reference(
    rgba: &[u8], width: u32, height: u32,
) -> Result<Vec<u8>, String> {
    nv12_byte_count(width, height)?;
    let expected = checked_rgba_bytes(width, height)?;
    if rgba.len() != expected {
        return Err("RGBA input size does not match NV12 frame dimensions".to_owned());
    }
    if rgba.chunks_exact(4).any(|p| p[3] != 255) {
        return Err("NV12 encoder input must be an opaque RGBA8 frame".to_owned());
    }
    let pixels = width as usize * height as usize;
    let mut result = vec![0; pixels + pixels / 2];
    for y in 0..height as usize {
        for x in 0..width as usize {
            let src = (y * width as usize + x) * 4;
            let r = i32::from(rgba[src]);
            let g = i32::from(rgba[src + 1]);
            let b = i32::from(rgba[src + 2]);
            let value = 16 + (47 * r + 157 * g + 16 * b + 128) / 256;
            result[y * width as usize + x] = value.clamp(16, 235) as u8;
        }
    }
    for y in (0..height as usize).step_by(2) {
        for x in (0..width as usize).step_by(2) {
            let mut sum = [0i32; 3];
            for cy in 0..2 {
                for cx in 0..2 {
                    let src = ((y + cy) * width as usize + x + cx) * 4;
                    for ch in 0..3 {
                        sum[ch] += i32::from(rgba[src + ch]);
                    }
                }
            }
            let r = (sum[0] + 2) / 4;
            let g = (sum[1] + 2) / 4;
            let b = (sum[2] + 2) / 4;
            let u = 128 + (-26 * r - 87 * g + 113 * b + 128) / 256;
            let v = 128 + (113 * r - 102 * g - 11 * b + 128) / 256;
            let uv = pixels + y / 2 * width as usize + x;
            result[uv] = u.clamp(16, 240) as u8;
            result[uv + 1] = v.clamp(16, 240) as u8;
        }
    }
    Ok(result)
}

fn nv12_byte_count(width: u32, height: u32) -> Result<usize, String> {
    let rgba_bytes = checked_rgba_bytes(width, height)?;
    if width % 4 != 0 || height % 2 != 0 {
        return Err("GPU NV12 requires width divisible by four and even height".to_owned());
    }
    let pixel_count = rgba_bytes / 4;
    Ok(pixel_count * 3 / 2)
}

impl GpuFrameSession {
    /// Once per session, allocate an on-device NV12 output buffer and
    /// compile the conversion pipeline that reads the completed RGBA frame.
    /// The NV12 buffer remains GPU resident across all following frames.
    pub fn prepare_nv12(&mut self) -> Result<(), String> {
        if self.nv12.is_some() {
            return Ok(());
        }
        let bytes = nv12_byte_count(self.width, self.height)?;
        let shader = self.device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("RGBA to NV12 GPU conversion shader"),
            source: wgpu::ShaderSource::Wgsl(
                include_str!("rgba_to_nv12.wgsl").into(),
            ),
        });
        let pipeline = self.device.create_compute_pipeline(
            &wgpu::ComputePipelineDescriptor {
                label: Some("Encoder-ready NV12 GPU conversion"),
                layout: None,
                module: &shader,
                entry_point: Some("main"),
                compilation_options: Default::default(),
                cache: None,
            },
        );
        let output = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("Resident packed NV12 encoder staging surface"),
            size: bytes as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });
        let staging = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("Optional NV12 diagnostic readback"),
            size: bytes as u64,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let mut size = [0u8; 16];
        size[0..4].copy_from_slice(&self.width.to_le_bytes());
        size[4..8].copy_from_slice(&self.height.to_le_bytes());
        let geometry = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("NV12 format geometry"),
            contents: &size,
            usage: wgpu::BufferUsages::UNIFORM,
        });
        let bind_group = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("Composed RGBA -> persistent NV12 buffers"),
            layout: &pipeline.get_bind_group_layout(0),
            entries: &[
                wgpu::BindGroupEntry {
                    binding: 0, resource: self.final_buffer.as_entire_binding(),
                },
                wgpu::BindGroupEntry {
                    binding: 1, resource: output.as_entire_binding(),
                },
                wgpu::BindGroupEntry {
                    binding: 2, resource: geometry.as_entire_binding(),
                },
            ],
        });
        self.nv12 = Some(ResidentNv12 {
            pipeline,
            bind_group,
            _geometry: geometry,
            _output: output,
            staging,
            bytes,
        });
        Ok(())
    }

    fn dispatch_nv12(&self, readback: bool) -> Result<Option<Vec<u8>>, String> {
        let nv12 = self.nv12.as_ref()
            .ok_or_else(|| "Call prepare_nv12() before GPU NV12 conversion".to_owned())?;
        let mut encoder = self.device.create_command_encoder(
            &wgpu::CommandEncoderDescriptor {
                label: Some("GPU resident overlay + NV12 export"),
            },
        );
        // All overlays and the format conversion are submitted together to
        // one queue; no CPU frame download exists between those GPU stages.
        for group in &self.bind_groups {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("Compositing RGBA overlay for NV12"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, group, &[]);
            pass.dispatch_workgroups(self.width.div_ceil(16), self.height.div_ceil(16), 1);
        }
        {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("Convert composed RGBA to NV12"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&nv12.pipeline);
            pass.set_bind_group(0, &nv12.bind_group, &[]);
            pass.dispatch_workgroups(
                (self.width / 4).div_ceil(16), self.height.div_ceil(8), 1,
            );
        }
        if readback {
            encoder.copy_buffer_to_buffer(
                &nv12._output, 0, &nv12.staging, 0, nv12.bytes as u64,
            );
        }
        let submission = self.queue.submit(Some(encoder.finish()));
        if !readback {
            self.device.poll(wgpu::PollType::Wait {
                submission_index: Some(submission),
                timeout: Some(Duration::from_secs(30)),
            }).map_err(|error| format!("GPU NV12 render failed: {error:?}"))?;
            return Ok(None);
        }
        let (sender, receiver) = mpsc::channel();
        let slice = nv12.staging.slice(..);
        slice.map_async(wgpu::MapMode::Read, move |result| {
            let _ = sender.send(result);
        });
        self.device.poll(wgpu::PollType::Wait {
            submission_index: Some(submission),
            timeout: Some(Duration::from_secs(30)),
        }).map_err(|error| format!("GPU NV12 readback timed out: {error:?}"))?;
        receiver.recv_timeout(Duration::from_secs(30))
            .map_err(|error| format!("GPU NV12 callback lost: {error}"))?
            .map_err(|error| format!("GPU NV12 readback failed: {error}"))?;
        let mapped = slice.get_mapped_range();
        let pixels = mapped.to_vec();
        drop(mapped);
        nv12.staging.unmap();
        if pixels.len() != nv12.bytes {
            return Err("GPU NV12 byte count changed".to_owned());
        }
        Ok(Some(pixels))
    }

    /// Real GPU RGBA composition + NV12 formatting, without reading results
    /// back to the CPU. The output is currently wgpu storage memory, NOT
    /// an NVENC-shareable D3D11 texture or AVHWFramesContext.
    pub fn compose_nv12_gpu_only(&self) -> Result<(), String> {
        self.dispatch_nv12(false).map(|_| ())
    }

    /// Diagnostic NV12 bytes for exact parity and future FFmpeg comparison.
    pub fn compose_nv12_and_readback(&self) -> Result<Vec<u8>, String> {
        self.dispatch_nv12(true)?
            .ok_or_else(|| "GPU NV12 output unavailable".to_owned())
    }

    pub fn nv12_byte_count(&self) -> Result<usize, String> {
        nv12_byte_count(self.width, self.height)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn required_test_adapter() -> Option<GpuCompositor> {
        match GpuCompositor::new_for_tests() {
            Ok(gpu) => Some(gpu),
            Err(error) => {
                if std::env::var("MMS_GPU_TEST_REQUIRE_ADAPTER").as_deref() == Ok("1") {
                    panic!("CI GPU shader test requires an adapter: {error}");
                }
                eprintln!("GPU unavailable, skipping hardware shader test: {error}");
                None
            }
        }
    }

    #[test]
    fn prepared_session_validates_overlay_and_opaque_base_before_allocating() {
        let Some(gpu) = required_test_adapter() else {
            return;
        };
        let base = vec![10, 25, 50, 255].repeat(4);
        assert!(gpu.prepare_session(&base, 2, 2, &[]).is_err());
        let overlay = vec![0, 240, 100, 128].repeat(4);
        let layers = [Overlay { pixels: &overlay, width: 2, height: 2, x: 0, y: 0 }];
        let session = gpu.prepare_session(&base, 2, 2, &layers).unwrap();
        assert_eq!(session.dimensions(), (2, 2));
        assert_eq!(session.overlay_count(), 1);
    }

    #[test]
    fn persistent_session_preserves_pixels_across_updates_and_multiple_frames() {
        let Some(gpu) = required_test_adapter() else {
            return;
        };
        let base = vec![27, 46, 82, 255].repeat(64 * 48);
        let red = vec![245, 13, 35, 128].repeat(30 * 25);
        let blue = vec![0, 67, 255, 224].repeat(27 * 22);
        let layers = [
            Overlay { pixels: &red, width: 30, height: 25, x: 4, y: 2 },
            Overlay { pixels: &blue, width: 27, height: 22, x: -4, y: 18 },
        ];
        let mut session = gpu.prepare_session(&base, 64, 48, &layers).unwrap();
        for _ in 0..3 {
            session.compose_gpu_only().unwrap();
            assert_eq!(
                session.compose_and_readback().unwrap(),
                super::super::composite_reference(&base, 64, 48, &layers).unwrap(),
            );
        }
        let replacement = vec![80, 20, 13, 255].repeat(64 * 48);
        let replacement_blue = vec![230, 180, 10, 65].repeat(27 * 22);
        assert!(session.update_base(&[1, 2, 3, 4]).is_err());
        assert!(session.update_overlay(9, &replacement_blue).is_err());
        assert!(session.update_overlay(0, &replacement_blue).is_err());
        session.update_base(&replacement).unwrap();
        session.update_overlay(1, &replacement_blue).unwrap();
        let updated_layers = [
            Overlay { pixels: &red, width: 30, height: 25, x: 4, y: 2 },
            Overlay {
                pixels: &replacement_blue, width: 27, height: 22, x: -4, y: 18,
            },
        ];
        assert_eq!(
            session.compose_and_readback().unwrap(),
            super::super::composite_reference(&replacement, 64, 48, &updated_layers).unwrap(),
        );
    }

    fn reference_patch(
        frame: &mut [u8], stride: u32, rect: PixelRect, patch: &[u8],
    ) {
        let rowbytes = rect.width as usize * 4;
        for row in 0..rect.height as usize {
            let destination = ((rect.y as usize + row) * stride as usize
                + rect.x as usize) * 4;
            frame[destination..destination + rowbytes]
                .copy_from_slice(&patch[row * rowbytes..(row + 1) * rowbytes]);
        }
    }

    #[test]
    fn dirty_rectangle_validation_is_atomic_and_bounded() {
        let original = vec![10, 20, 30, 255].repeat(8 * 4);
        let right_size = vec![60, 70, 80, 255].repeat(3 * 2);
        let rect = PixelRect { x: 2, y: 1, width: 3, height: 2 };
        assert!(validate_patch((8, 4), rect, &right_size, true).is_ok());
        assert!(validate_patch((8, 4), rect, &right_size[..12], true).is_err());
        assert!(validate_patch((8, 4), PixelRect { x: 7, ..rect }, &right_size, true).is_err());
        assert!(validate_patch((8, 4), PixelRect { y: 3, ..rect }, &right_size, true).is_err());
        assert!(validate_patch((8, 4), PixelRect { width: 0, ..rect }, &[], true).is_err());
        let mut transparent = right_size.clone();
        transparent[3] = 123;
        assert!(validate_patch((8, 4), rect, &transparent, true).is_err());
        assert!(validate_patch((8, 4), rect, &transparent, false).is_ok());
        let mut result = original.clone();
        reference_patch(&mut result, 8, rect, &right_size);
        assert_eq!(&result[(1 * 8 + 2) * 4..(1 * 8 + 5) * 4], &right_size[..12]);
        assert_eq!(&result[..4], &original[..4]);
        assert_eq!(&result[(3 * 8) * 4..], &original[(3 * 8) * 4..]);
    }

    #[test]
    fn dirty_gpu_updates_match_full_upload_and_nv12_after_animation() {
        let Some(gpu) = required_test_adapter() else { return };
        let width = 64u32;
        let height = 48u32;
        let mut base = vec![20, 35, 100, 255].repeat((width * height) as usize);
        let mut overlay = vec![35, 160, 210, 80].repeat(32 * 20);
        let layers = [Overlay { pixels: &overlay, width: 32, height: 20, x: 4, y: 3 }];
        let mut session = gpu.prepare_session(&base, width, height, &layers).unwrap();
        session.prepare_nv12().unwrap();
        let base_rect = PixelRect { x: 14, y: 15, width: 8, height: 5 };
        let base_patch = vec![100, 20, 240, 255].repeat(8 * 5);
        let overlay_rect = PixelRect { x: 7, y: 8, width: 10, height: 6 };
        let overlay_patch = vec![255, 80, 10, 170].repeat(10 * 6);
        assert_eq!(
            session.update_base_rect(base_rect, &base_patch).unwrap(),
            base_patch.len(),
        );
        assert_eq!(
            session.update_overlay_rect(0, overlay_rect, &overlay_patch).unwrap(),
            overlay_patch.len(),
        );
        reference_patch(&mut base, width, base_rect, &base_patch);
        reference_patch(&mut overlay, 32, overlay_rect, &overlay_patch);
        let layers = [Overlay { pixels: &overlay, width: 32, height: 20, x: 4, y: 3 }];
        let reference = super::super::composite_reference(
            &base, width, height, &layers,
        ).unwrap();
        assert_eq!(session.compose_and_readback().unwrap(), reference);
        assert_eq!(
            session.compose_nv12_and_readback().unwrap(),
            rgba_to_nv12_reference(&reference, width, height).unwrap(),
        );

        // All invalid input paths must reject *before* issuing GPU writes.
        let broken = PixelRect { x: 30, y: 0, width: 8, height: 1 };
        assert!(session.update_overlay_rect(0, broken, &[1; 32]).is_err());
        assert!(session.update_overlay_rect(4, overlay_rect, &overlay_patch).is_err());
        assert!(session.update_base_rect(base_rect, &[1; 4]).is_err());
        assert_eq!(session.compose_and_readback().unwrap(), reference);

        // Verify both modes converge after a full replacement.
        session.update_base(&base).unwrap();
        session.update_overlay(0, &overlay).unwrap();
        assert_eq!(session.compose_and_readback().unwrap(), reference);
        session.compose_nv12_gpu_only().unwrap();
    }

    #[test]
    fn nv12_reference_validates_shape_alpha_and_limited_range() {
        assert!(rgba_to_nv12_reference(&[], 4, 2).is_err());
        assert!(rgba_to_nv12_reference(&[0; 12], 4, 2).is_err());
        assert!(rgba_to_nv12_reference(&[0; 16], 3, 2).is_err());
        let mut transparent = vec![0, 0, 0, 255].repeat(8);
        transparent[7] = 0;
        assert!(rgba_to_nv12_reference(&transparent, 4, 2).is_err());
        let black = vec![0, 0, 0, 255].repeat(8);
        let out = rgba_to_nv12_reference(&black, 4, 2).unwrap();
        assert_eq!(out.len(), 12);
        assert_eq!(&out[0..8], &[16u8; 8]);
        assert_eq!(&out[8..12], &[128u8; 4]);
        let white = vec![255, 255, 255, 255].repeat(8);
        let out = rgba_to_nv12_reference(&white, 4, 2).unwrap();
        assert_eq!(&out[0..8], &[235u8; 8]);
        assert_eq!(&out[8..12], &[128u8; 4]);
    }

    #[test]
    fn nv12_reference_is_deterministic_for_nonuniform_pixels() {
        let width = 12;
        let height = 6;
        let mut frame = vec![0u8; width * height * 4];
        for (idx, px) in frame.chunks_exact_mut(4).enumerate() {
            px[0] = (idx * 13 % 256) as u8;
            px[1] = (idx * 17 % 256) as u8;
            px[2] = (idx * 19 % 256) as u8;
            px[3] = 255;
        }
        let first = rgba_to_nv12_reference(&frame, width as u32, height as u32).unwrap();
        let second = rgba_to_nv12_reference(&frame, width as u32, height as u32).unwrap();
        assert_eq!(first, second);
        assert_eq!(first.len(), width * height * 3 / 2);
        for y in &first[..width * height] {
            assert!((16..=235).contains(y));
        }
        for uv in &first[width * height..] {
            assert!((16..=240).contains(uv));
        }
    }

    #[test]
    fn dynamic_gpu_nv12_frames_match_independent_rust_reference_if_hardware_present() {
        let Some(gpu) = required_test_adapter() else {
            return;
        };
        let width = 64u32;
        let height = 48u32;
        let base = vec![50, 60, 70, 255].repeat((width * height) as usize);
        let overlay_a = vec![245, 50, 15, 130].repeat(32 * 20);
        let overlay_b = vec![10, 200, 210, 90].repeat(24 * 16);
        let layers = [
            Overlay {
                pixels: &overlay_a, width: 32, height: 20, x: 5, y: 7,
            },
            Overlay {
                pixels: &overlay_b, width: 24, height: 16, x: -6, y: 30,
            },
        ];
        let mut session = gpu.prepare_session(&base, width, height, &layers).unwrap();
        session.prepare_nv12().unwrap();
        session.prepare_nv12().unwrap();
        assert_eq!(session.nv12_byte_count().unwrap(), 64 * 48 * 3 / 2);
        let cpu_rgba = super::super::composite_reference(&base, width, height, &layers).unwrap();
        let expected = rgba_to_nv12_reference(&cpu_rgba, width, height).unwrap();
        assert_eq!(session.compose_nv12_and_readback().unwrap(), expected);
        session.compose_nv12_gpu_only().unwrap();

        let replacement = vec![90, 120, 170, 255].repeat((width * height) as usize);
        let replacement_overlay = vec![255, 0, 70, 220].repeat(32 * 20);
        session.update_base(&replacement).unwrap();
        session.update_overlay(0, &replacement_overlay).unwrap();
        let new_layers = [
            Overlay {
                pixels: &replacement_overlay, width: 32, height: 20, x: 5, y: 7,
            },
            Overlay {
                pixels: &overlay_b, width: 24, height: 16, x: -6, y: 30,
            },
        ];
        let cpu_rgba = super::super::composite_reference(
            &replacement, width, height, &new_layers,
        ).unwrap();
        let expected = rgba_to_nv12_reference(&cpu_rgba, width, height).unwrap();
        assert_eq!(session.compose_nv12_and_readback().unwrap(), expected);
    }

    #[test]
    fn input_validation_rejects_unsupported_frame_dimensions() {
        assert!(checked_rgba_bytes(0, 10).is_err());
        assert!(checked_rgba_bytes(0xffff_ffff, 0xffff_ffff).is_err());
    }
}
