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

use windows::Win32::Graphics::Direct3D11::ID3D11Texture2D;
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
            entries.push(FrameSlot {
                texture: source.allocate_same_device_texture()?,
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
        self.slots[index].state = FrameSlotState::Ready { pts };
        self.last_submitted_pts = Some(pts);
        self.submitted += 1;
        Ok(index)
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
        let slot = self.slots.get_mut(slot_index)
            .ok_or_else(|| "Frame slot index is out of bounds".to_owned())?;
        if slot.state != (FrameSlotState::InFlight { pts }) {
            return Err("Frame slot is not encoding at that exact PTS".to_owned());
        }
        slot.state = FrameSlotState::Free;
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
    }
}
