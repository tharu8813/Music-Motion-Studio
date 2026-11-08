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
    // Retain static geometry buffers through the lifetime of all bind groups.
    _geometry_buffers: Vec<wgpu::Buffer>,
    bind_groups: Vec<wgpu::BindGroup>,
    _scratch_a: wgpu::Buffer,
    _scratch_b: wgpu::Buffer,
    final_buffer: wgpu::Buffer,
    staging: wgpu::Buffer,
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
            _geometry_buffers: geometry_buffers,
            bind_groups,
            _scratch_a: scratch_a,
            _scratch_b: scratch_b,
            final_buffer,
            staging,
        })
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepared_session_validates_overlay_and_opaque_base_before_allocating() {
        let Ok(gpu) = GpuCompositor::new() else {
            eprintln!("Hardware GPU unavailable: session validation requires adapter");
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
        let Ok(gpu) = GpuCompositor::new() else {
            eprintln!("Hardware GPU unavailable; skipping optional session GPU test");
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

    #[test]
    fn input_validation_rejects_unsupported_frame_dimensions() {
        assert!(checked_rgba_bytes(0, 10).is_err());
        assert!(checked_rgba_bytes(0xffff_ffff, 0xffff_ffff).is_err());
    }
}
