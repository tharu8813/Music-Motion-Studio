//! Same-device D3D11 NV12 texture ring for future in-process NVENC frames.
//!
//! This is an experimental GPU resource/lifetime boundary, not an encoder.
//! A small fixed pool avoids per-frame D3D11 texture allocation and allows
//! multiple frames to remain GPU resident while another frame is consumed.
//! The test input is still CPU-generated; future GPU composition must render
//! into the same D3D11 device's NV12-compatible surfaces directly.
//!
//! Never reclaim an InFlight slot until the real encoder signals that it has
//! finished using its GPU texture. GPU fences/synchronization and FFmpeg
//! AVHWFramesContext are explicitly NOT implemented here.

use std::time::{Duration, Instant};
use windows::core::Interface;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_QUERY_DESC, D3D11_QUERY_EVENT,
    ID3D11Device, ID3D11DeviceContext, ID3D11Query, ID3D11Texture2D,
};
use crate::d3d11_bgra_video::{NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer};
use windows::Win32::Graphics::Dxgi::Common::DXGI_FORMAT_NV12;
use crate::d3d11_nv12::{nv12_size, NativeD3d11Nv12Surface};

pub const MIN_SLOTS: usize = 2;
pub const MAX_SLOTS: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameSlotState {
    Free,
    Ready { pts: i64 },
    InFlight { pts: i64 },
}

impl FrameSlotState {
    pub fn pts(&self) -> Option<i64> {
        match self {
            Self::Free => None,
            Self::Ready { pts } | Self::InFlight { pts } => Some(*pts),
        }
    }
}

struct FrameSlot {
    texture: ID3D11Texture2D,
    // EVENT query is ended after CopyResource, so it confirms the GPU
    // finished writing the texture. It does NOT confirm NVENC has read it.
    copy_done: ID3D11Query,
    copy_query_issued: bool,
    state: FrameSlotState,
}

/// A *borrowed* NVENC resource candidate; the texture is kept alive by
/// its COM reference. Dropping this struct doesn't release the pool slot:
/// the future encoder must call complete_encode ONLY after GPU completion.
///
/// There is no actual encoder in this prototype; this is used for testing
/// the allocation, sequencing and GPU-only copy contracts.
pub struct ReservedGpuFrame {
    pub slot_index: usize,
    pub pts: i64,
    pub texture: ID3D11Texture2D,
}

pub struct NativeNv12FramePool {
    source: NativeD3d11Nv12Surface,
    slots: Vec<FrameSlot>,
    last_submitted_pts: Option<i64>,
    submitted: u64,
    completed: u64,
}

fn validate_pts(last: Option<i64>, pts: i64) -> Result<(), String> {
    if pts < 0 {
        return Err("Video frame PTS must be nonnegative".to_owned());
    }
    if last.is_some_and(|previous| pts <= previous) {
        return Err("Video frame PTS must be strictly increasing".to_owned());
    }
    Ok(())
}

fn first_free(slots: &[FrameSlotState]) -> Option<usize> {
    slots.iter().position(|slot| *slot == FrameSlotState::Free)
}

fn oldest_ready(slots: &[FrameSlotState]) -> Option<usize> {
    slots.iter().enumerate()
        .filter_map(|(index, state)| match state {
            FrameSlotState::Ready { pts } => Some((index, *pts)),
            _ => None,
        })
        .min_by_key(|(_, pts)| *pts)
        .map(|(index, _)| index)
}

impl NativeNv12FramePool {
    /// Allocate a bounded ring of NV12 textures on exactly one hardware
    /// D3D11 device, without allocating another D3D11 device per frame.
    pub fn new(width: u32, height: u32, slots: usize) -> Result<Self, String> {
        if !(MIN_SLOTS..=MAX_SLOTS).contains(&slots) {
            return Err(format!("D3D11 pool capacity must be {MIN_SLOTS}..={MAX_SLOTS}"));
        }
        let source = NativeD3d11Nv12Surface::new(width, height)?;
        let mut entries = Vec::with_capacity(slots);
        for _ in 0..slots {
            let query_desc = D3D11_QUERY_DESC {
                Query: D3D11_QUERY_EVENT, MiscFlags: 0,
            };
            let mut query = None;
            unsafe { source.device().CreateQuery(&query_desc, Some(&mut query)) }
                .map_err(|e| format!("Cannot allocate D3D11 GPU copy-completion query: {e}"))?;
            entries.push(FrameSlot {
                texture: source.allocate_same_device_texture()?,
                copy_done: query.ok_or("D3D11 returned no copy-completion query")?,
                copy_query_issued: false,
                state: FrameSlotState::Free,
            });
        }
        Ok(Self {
            source,
            slots: entries,
            last_submitted_pts: None,
            submitted: 0,
            completed: 0,
        })
    }

    pub fn dimensions(&self) -> (u32, u32) {
        self.source.dimensions()
    }

    pub(crate) fn native_device(&self) -> &ID3D11Device {
        self.source.device()
    }

    pub(crate) fn native_context(&self) -> &ID3D11DeviceContext {
        self.source.context()
    }

    pub fn adapter_name(&self) -> &str {
        self.source.adapter_name()
    }

    pub fn adapter_vendor_id(&self) -> u32 {
        self.source.adapter_vendor_id()
    }

    pub fn adapter_device_id(&self) -> u32 {
        self.source.adapter_device_id()
    }

    pub fn capacity(&self) -> usize {
        self.slots.len()
    }

    pub fn frame_bytes(&self) -> Result<usize, String> {
        let (w, h) = self.dimensions();
        nv12_size(w, h)
    }

    pub fn submitted_count(&self) -> u64 {
        self.submitted
    }

    pub fn completed_count(&self) -> u64 {
        self.completed
    }

    pub fn states(&self) -> Vec<FrameSlotState> {
        self.slots.iter().map(|slot| slot.state).collect()
    }

    /// Upload an opaque preformatted NV12 test frame to a scratch source
    /// texture, then issue GPU->GPU CopyResource to a free ring slot.
    ///
    /// No CPU download occurs here. The upload is still CPU->GPU, so this
    /// is NOT proof of zero-copy rendering or NVENC interop.
    pub fn queue_cpu_nv12(&mut self, bytes: &[u8], pts: i64) -> Result<usize, String> {
        validate_pts(self.last_submitted_pts, pts)?;
        if bytes.len() != self.frame_bytes()? {
            return Err("NV12 pool input has incorrect packed byte length".to_owned());
        }
        let states = self.states();
        let index = first_free(&states)
            .ok_or_else(|| "D3D11 frame pool is full; encoder backpressure required".to_owned())?;

        // Publish the frame as ready only after the upload and GPU copy
        // are enqueued in submission order on the same immediate context.
        self.source.upload_reference(bytes)?;
        self.source.copy_to_same_device_texture(&self.slots[index].texture);
        unsafe { self.source.context().End(&self.slots[index].copy_done) };
        self.slots[index].copy_query_issued = true;
        self.slots[index].state = FrameSlotState::Ready { pts };
        self.last_submitted_pts = Some(pts);
        self.submitted += 1;
        Ok(index)
    }

    /// Enqueue a completed same-device BGRA GPU texture directly through
    /// native GPU BGRA->NV12 video conversion and a GPU-only CopyResource
    /// into a free persistent NV12 frame slot.
    ///
    /// No readback or rawvideo pipe is used. Input may originate from a
    /// future *D3D11* Canvas renderer, but not a raw wgpu Vulkan buffer.
    /// Do not claim NVENC zero-copy until a native encoder consumes this
    /// texture, with verified completion fences.
    pub fn queue_gpu_bgra(
        &mut self,
        bridge: &NativeBgraNv12VideoProcessor,
        input: &ID3D11Texture2D,
        pts: i64,
    ) -> Result<usize, String> {
        validate_pts(self.last_submitted_pts, pts)?;
        if bridge.dimensions() != self.dimensions() {
            return Err("D3D11 GPU converter dimensions differ from frame pool".into());
        }
        if bridge.device().as_raw() != self.native_device().as_raw() {
            return Err("Cannot share BGRA and NV12 textures across D3D11 devices".into());
        }
        let index = first_free(&self.states())
            .ok_or_else(|| "D3D11 GPU NV12 frame pool full; encoder backpressure required".to_owned())?;

        // VideoProcessorBlt and CopyResource share one D3D11 immediate
        // context. Ready means *queued in order*, not GPU completed.
        let converted = bridge.convert_gpu_bgra_to_nv12(input)?;
        unsafe {
            self.source.context().CopyResource(&self.slots[index].texture, converted);
            self.source.context().End(&self.slots[index].copy_done);
        }
        self.slots[index].copy_query_issued = true;
        self.slots[index].state = FrameSlotState::Ready { pts };
        self.last_submitted_pts = Some(pts);
        self.submitted += 1;
        Ok(index)
    }

    /// Queue an already GPU-composited NV12 texture from the same D3D11
    /// device. The producer may be a Canvas GPU compositor or decoder;
    /// all NV12 pixels stay on the GPU. GPU copy completion and subsequent
    /// real NVENC bitstream completion remain separate lifetime events.
    pub fn queue_gpu_nv12(
        &mut self, input: &ID3D11Texture2D, pts: i64,
    ) -> Result<usize, String> {
        validate_pts(self.last_submitted_pts, pts)?;
        let index = first_free(&self.states())
            .ok_or_else(|| "D3D11 NV12 frame pool full; encoder backpressure required".to_owned())?;
        let device = unsafe { input.GetDevice() }
            .map_err(|e| format!("Cannot inspect source NV12 device: {e}"))?;
        if device.as_raw() != self.native_device().as_raw() {
            return Err("Canvas NV12 source belongs to a different D3D11 device".into());
        }
        let mut desc = windows::Win32::Graphics::Direct3D11::D3D11_TEXTURE2D_DESC::default();
        unsafe { input.GetDesc(&mut desc) };
        let (width, height) = self.dimensions();
        if desc.Width != width || desc.Height != height ||
            desc.Format != DXGI_FORMAT_NV12 || desc.MipLevels != 1 ||
            desc.ArraySize != 1 || desc.SampleDesc.Count != 1
        {
            return Err("Canvas NV12 source must be a matching single-plane-array GPU texture".into());
        }
        // The producer and this pool share one immediate context. The
        // queued CopyResource therefore occurs after video compositing.
        unsafe {
            self.source.context().CopyResource(&self.slots[index].texture, input);
            self.source.context().End(&self.slots[index].copy_done);
        }
        self.slots[index].copy_query_issued = true;
        self.slots[index].state = FrameSlotState::Ready { pts };
        self.last_submitted_pts = Some(pts);
        self.submitted += 1;
        Ok(index)
    }

    /// Composite a background plus ordered Canvas/lyric BGRA textures
    /// directly into the NV12 GPU frame pool. No QImage readback or
    /// CPU-side blending is performed in this D3D11 stage.
    ///
    /// This is a GPU compositing experiment; actual Qt Canvas text and
    /// images have not yet been moved to a native GPU paint engine.
    pub fn queue_gpu_canvas_layers(
        &mut self,
        compositor: &NativeBgraNv12VideoProcessor,
        layers: &[NativeCanvasBgraLayer<'_>],
        pts: i64,
    ) -> Result<usize, String> {
        // Fail *before* issuing GPU commands when the ring is full.
        validate_pts(self.last_submitted_pts, pts)?;
        if first_free(&self.states()).is_none() {
            return Err("D3D11 Canvas frame pool full; encoder backpressure required".into());
        }
        if compositor.dimensions() != self.dimensions() ||
            compositor.device().as_raw() != self.native_device().as_raw()
        {
            return Err("D3D11 Canvas compositor and NV12 pool must share one device and size".into());
        }
        let nv12 = compositor.composite_canvas_layers_to_nv12(layers)?;
        self.queue_gpu_nv12(nv12, pts)
    }

    /// Non-blocking GPU *copy* completion query. Unlike the old Ready state,
    /// true means D3D11 finished all commands up through CopyResource.
    ///
    /// This is not an NVENC completion signal. The future encoder must
    /// separately signal that it has released a frame before recycling it.
    /// GetData uses flags=0 so that the driver can flush queued commands;
    /// a non-flushing query can remain pending forever.
    pub fn gpu_copy_complete(&self, slot_index: usize) -> Result<bool, String> {
        let slot = self.slots.get(slot_index)
            .ok_or_else(|| "D3D11 copy-completion slot index is out of range".to_owned())?;
        if slot.state == FrameSlotState::Free || !slot.copy_query_issued {
            return Err("No outstanding D3D11 copy for that frame slot".to_owned());
        }
        // On an EVENT query GetData writes Win32 BOOL=TRUE when complete.
        // It returns S_FALSE while pending (mapped to Ok by windows-rs);
        // initialize the data to FALSE and inspect it, not Result::is_ok().
        let mut done: i32 = 0;
        unsafe {
            self.source.context().GetData(
                &slot.copy_done,
                Some((&mut done as *mut i32).cast()),
                std::mem::size_of::<i32>() as u32, 0,
            )
        }.map_err(|e| format!("D3D11 GPU copy-completion query failed: {e}"))?;
        Ok(done != 0)
    }

    /// Wait only for an explicit, bounded diagnostic/scheduling deadline.
    /// Never busy-spin: GPU work must be allowed to run independently.
    pub fn wait_for_gpu_copy(
        &self, slot_index: usize, timeout: Duration,
    ) -> Result<(), String> {
        let started = Instant::now();
        loop {
            if self.gpu_copy_complete(slot_index)? {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(format!(
                    "Timed out waiting for D3D11 copy completion on slot {slot_index}"
                ));
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    /// Acquire only the oldest PTS frame once its GPU-side copy is complete.
    /// This is the safer handoff for an eventual separate hardware encoder.
    pub fn acquire_gpu_complete_ready(
        &mut self,
    ) -> Result<Option<ReservedGpuFrame>, String> {
        let Some(index) = oldest_ready(&self.states()) else {
            return Ok(None);
        };
        if !self.gpu_copy_complete(index)? {
            return Ok(None);
        }
        Ok(self.acquire_ready())
    }

    /// Diagnostic-only readback for inspecting the native video processor's
    /// output. It is explicitly excluded from the GPU→NVENC fast path.
    pub fn readback_slot_for_validation(
        &self, index: usize,
    ) -> Result<Vec<u8>, String> {
        let slot = self.slots.get(index)
            .ok_or_else(|| "GPU NV12 slot index out of range".to_owned())?;
        if slot.state == FrameSlotState::Free {
            return Err("Cannot read an uninitialized GPU NV12 slot".to_owned());
        }
        self.source.readback_same_device_texture(&slot.texture)
    }

    /// Reserve the oldest frame for an eventual in-process encoder. Call
    /// complete_encode only after the actual GPU encode finishes; merely
    /// passing a GPU texture pointer to an encoder is not synchronization.
    pub fn acquire_ready(&mut self) -> Option<ReservedGpuFrame> {
        let index = oldest_ready(&self.states())?;
        let FrameSlotState::Ready { pts } = self.slots[index].state else {
            unreachable!("Only ready slots can be acquired");
        };
        self.slots[index].state = FrameSlotState::InFlight { pts };
        Some(ReservedGpuFrame {
            slot_index: index,
            pts,
            texture: self.slots[index].texture.clone(),
        })
    }

    /// Simulate the encoder's eventual completion event and recycle one
    /// slot. Do not call early when actual NVENC is connected later.
    pub fn complete_encode(&mut self, slot_index: usize, pts: i64)
        -> Result<(), String>
    {
        let state = self.slots.get(slot_index)
            .ok_or_else(|| "Frame slot index is out of bounds".to_owned())?.state;
        if state != (FrameSlotState::InFlight { pts }) {
            return Err("Frame slot is not encoding at that exact PTS".to_owned());
        }
        // Do not permit reuse until D3D11 itself confirms the copy is done,
        // even if a caller incorrectly signals encoder completion early.
        // This is necessary but NOT sufficient for real NVENC safety.
        if !self.gpu_copy_complete(slot_index)? {
            return Err("Cannot reclaim frame before GPU copy completion".to_owned());
        }
        self.slots[slot_index].copy_query_issued = false;
        self.slots[slot_index].state = FrameSlotState::Free;
        self.completed += 1;
        Ok(())
    }

    /// Diagnostic only. Compare the exact bytes of one queued GPU texture
    /// against the provided CPU reference. This method downloads the frame
    /// into RAM, and must NEVER be called in the future NVENC fast path.
    pub fn verify_slot(&self, slot_index: usize, expected: &[u8])
        -> Result<(), String>
    {
        let slot = self.slots.get(slot_index)
            .ok_or_else(|| "Frame slot index is out of bounds".to_owned())?;
        if slot.state == FrameSlotState::Free {
            return Err("Cannot inspect an uninitialized frame slot".to_owned());
        }
        if expected.len() != self.frame_bytes()? {
            return Err("Expected NV12 verification frame has incorrect length".to_owned());
        }
        let actual = self.source.readback_same_device_texture(&slot.texture)?;
        if actual != expected {
            let differing = actual.iter().zip(expected).filter(|(a, b)| a != b).count();
            return Err(format!("Same-device D3D11 NV12 slot lost {differing} bytes"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pts_and_slot_ring_validation_never_needs_gpu() {
        assert!(validate_pts(None, 0).is_ok());
        assert!(validate_pts(None, -1).is_err());
        assert!(validate_pts(Some(0), 0).is_err());
        assert!(validate_pts(Some(7), 3).is_err());
        assert!(validate_pts(Some(7), 8).is_ok());

        let states = [
            FrameSlotState::Ready { pts: 40 },
            FrameSlotState::Free,
            FrameSlotState::Ready { pts: 20 },
            FrameSlotState::InFlight { pts: 10 },
        ];
        assert_eq!(first_free(&states), Some(1));
        assert_eq!(oldest_ready(&states), Some(2));
        assert_eq!(FrameSlotState::Free.pts(), None);
        assert_eq!(FrameSlotState::Ready { pts: 40 }.pts(), Some(40));
        assert_eq!(first_free(&[FrameSlotState::InFlight { pts: 1 }]), None);
        assert_eq!(oldest_ready(&[FrameSlotState::Free]), None);
    }

    #[test]
    fn hardware_pool_preserves_frames_and_backpressure_if_supported() {
        let mut pool = match NativeNv12FramePool::new(64, 48, 2) {
            Ok(pool) => pool,
            Err(error) => {
                if std::env::var("MMS_REQUIRE_D3D11").as_deref() == Ok("1") {
                    panic!("Required hardware NV12 texture pool is unavailable: {error}");
                }
                eprintln!("Skipping hardware pool exercise: {error}");
                return;
            }
        };
        let byte_count = pool.frame_bytes().unwrap();
        let first: Vec<u8> = (0..byte_count).map(|i| (i * 7 + 3) as u8).collect();
        let second: Vec<u8> = (0..byte_count).map(|i| (i * 11 + 31) as u8).collect();
        let third: Vec<u8> = (0..byte_count).map(|i| (i * 13 + 17) as u8).collect();

        let index_first = pool.queue_cpu_nv12(&first, 100).unwrap();
        let index_second = pool.queue_cpu_nv12(&second, 101).unwrap();
        assert_ne!(index_first, index_second);
        assert_eq!(pool.capacity(), 2);
        assert_eq!(pool.submitted_count(), 2);
        assert!(pool.queue_cpu_nv12(&third, 102).is_err());
        assert!(pool.queue_cpu_nv12(&third, 101).is_err());
        assert!(pool.queue_cpu_nv12(&third[..12], 102).is_err());

        pool.verify_slot(index_first, &first).unwrap();
        pool.verify_slot(index_second, &second).unwrap();

        let acquired = pool.acquire_ready().unwrap();
        assert_eq!(acquired.pts, 100);
        assert_eq!(acquired.slot_index, index_first);
        assert!(pool.complete_encode(acquired.slot_index, 101).is_err());
        assert!(pool.queue_cpu_nv12(&third, 102).is_err());

        drop(acquired);
        pool.complete_encode(index_first, 100).unwrap();
        let index_third = pool.queue_cpu_nv12(&third, 102).unwrap();
        assert_eq!(index_third, index_first);
        pool.verify_slot(index_second, &second).unwrap();
        pool.verify_slot(index_third, &third).unwrap();

        let acquired_second = pool.acquire_ready().unwrap();
        assert_eq!(acquired_second.pts, 101);
        drop(acquired_second);
        pool.complete_encode(index_second, 101).unwrap();
        let acquired_third = pool.acquire_ready().unwrap();
        assert_eq!(acquired_third.pts, 102);
        drop(acquired_third);
        pool.complete_encode(index_third, 102).unwrap();

        assert_eq!(pool.completed_count(), 3);
        assert_eq!(pool.states(), vec![FrameSlotState::Free; 2]);
        assert!(pool.gpu_copy_complete(index_first).is_err());
        assert!(pool.acquire_gpu_complete_ready().unwrap().is_none());
    }
}
