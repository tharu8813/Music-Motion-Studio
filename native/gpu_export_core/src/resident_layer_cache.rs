//! Bounded export-local resident resources. Opacity/order are frame state,
//! never part of pixel identity; changed pixels invalidate only their stream.
use std::collections::{HashMap, HashSet};
use crate::qt_canvas_packet::CanvasPacketFrame;

#[derive(Default, Debug)]
pub struct CacheCounters {
    pub allocations: u64,
    pub uploads: u64,
    pub upload_bytes: u64,
    // Byte count of redundant SHA-256 CPU input scans avoided by using the
    // parser-validated digest (not bytes removed from the pipe).
    pub digest_reuse_bytes: u64,
    pub reuse_hits: u64,
    pub releases: u64,
    pub peak_resident_bytes: usize,
}

struct Entry<T> { digest: [u8; 32], dimensions: (u32, u32), resource: T }

pub struct ResidentLayerCache<T> {
    entries: HashMap<String, Entry<T>>,
    dimensions: (u32, u32),
    pub counters: CacheCounters,
}

impl<T> ResidentLayerCache<T> {
    pub fn new(width: u32, height: u32) -> Self {
        Self { entries: HashMap::new(), dimensions: (width, height), counters: CacheCounters::default() }
    }

    /// Update on the same immediate context used for composition. Command
    /// ordering makes reuse safe while older NV12 output slots encode.
    pub fn update(
        &mut self, frame: &CanvasPacketFrame,
        mut allocate: impl FnMut() -> Result<T, String>,
        mut upload: impl FnMut(&T, &[u8]) -> Result<(), String>,
    ) -> Result<(), String> {
        if (frame.width, frame.height) != self.dimensions
            || frame.width < 2 || frame.width > 3840 || frame.width % 2 != 0
            || frame.height < 2 || frame.height > 2160 || frame.height % 2 != 0
            || frame.layers.is_empty()
            || frame.layers.len() > 8 {
            return Err("Resident layer dimensions/count changed or invalid".into());
        }
        if frame.layers.iter().any(|l| (l.geometry.width, l.geometry.height) != self.dimensions) {
            return Err("Full-frame cache API received a sized source".into());
        }
        self.update_sized(frame, |_, _| allocate(), |r, p, _| upload(r, p))
    }

    pub fn update_sized(
        &mut self, frame: &CanvasPacketFrame,
        mut allocate: impl FnMut(u32, u32) -> Result<T, String>,
        mut upload: impl FnMut(&T, &[u8], u32) -> Result<(), String>,
    ) -> Result<(), String> {
        if (frame.width, frame.height) != self.dimensions || frame.layers.is_empty() || frame.layers.len() > 8 {
            return Err("Resident frame shape changed".into());
        }
        let mut keys = HashSet::new();
        for layer in &frame.layers {
            if !keys.insert(layer.stream_key.as_str()) || layer.geometry.width == 0 || layer.geometry.height == 0
                || layer.geometry.width > 3840 || layer.geometry.height > 2160
                || layer.pixels.len() != layer.geometry.width as usize * layer.geometry.height as usize * 4 {
                return Err("Resident layer duplicate key or invalid pixel length".into());
            }
        }
        let before = self.entries.len();
        self.entries.retain(|key, _| keys.contains(key.as_str()));
        self.counters.releases += (before - self.entries.len()) as u64;
        for layer in &frame.layers {
            let plane = layer.pixels.len();
            let dimensions = (layer.geometry.width, layer.geometry.height);
            if self.entries.get(&layer.stream_key).is_some_and(|e| e.dimensions != dimensions) {
                self.entries.remove(&layer.stream_key);
                self.counters.releases += 1;
            }
            // The packet parser has already hashed and verified this exact
            // immutable BGRA buffer against its manifest. Rehashing the
            // 1080p/4K planes here was a second full CPU scan per frame.
            let digest = layer.validated_sha256;
            self.counters.digest_reuse_bytes += plane as u64;
            if let Some(entry) = self.entries.get_mut(&layer.stream_key) {
                if entry.digest == digest {
                    self.counters.reuse_hits += 1;
                    continue;
                }
                upload(&entry.resource, &layer.pixels, layer.geometry.width)?;
                entry.digest = digest;
            } else {
                let resource = allocate(dimensions.0, dimensions.1)?;
                upload(&resource, &layer.pixels, layer.geometry.width)?;
                self.entries.insert(layer.stream_key.clone(), Entry { digest, dimensions, resource });
                self.counters.allocations += 1;
            }
            self.counters.uploads += 1;
            self.counters.upload_bytes += plane as u64;
        }
        self.counters.peak_resident_bytes = self.counters.peak_resident_bytes.max(self.entries.values().map(|e| e.dimensions.0 as usize * e.dimensions.1 as usize * 4).sum());
        Ok(())
    }

    pub fn resource(&self, key: &str) -> Result<&T, String> {
        self.entries.get(key).map(|entry| &entry.resource)
            .ok_or_else(|| format!("Resident stream not uploaded: {key}"))
    }

    pub fn clear(&mut self) {
        self.counters.releases += self.entries.len() as u64;
        self.entries.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use crate::qt_canvas_packet::{CanvasPacketLayer, LayerGeometry};
    fn frame(keys: &[&str], pixel: u8) -> CanvasPacketFrame {
        CanvasPacketFrame { width: 2, height: 2, layers: keys.iter().map(|key| {
            let pixels = vec![pixel; 16];
            let validated_sha256 = Sha256::digest(&pixels).into();
            CanvasPacketLayer {
                stream_key: key.to_string(), opacity: 1.0, pixels,
                geometry: LayerGeometry::identity(2, 2), validated_sha256,
            }
        }).collect() }
    }
    #[test]
    fn repeats_skip_upload_and_opacity_and_z_order_are_not_pixel_identity() {
        let mut cache = ResidentLayerCache::new(2, 2);
        let mut first = frame(&["base", "layer:0"], 255);
        for _ in 0..60 { cache.update(&first, || Ok(()), |_, _| Ok(())).unwrap(); }
        first.layers[1].opacity = 0.25;
        first.layers.swap(0, 1);
        cache.update(&first, || Ok(()), |_, _| panic!("unchanged pixels uploaded")).unwrap();
        assert_eq!(cache.counters.uploads, 2); // uncached: 122
        assert_eq!(cache.counters.digest_reuse_bytes, 2 * 16 * 61);
        assert_eq!(cache.counters.reuse_hits, 120);
        assert_eq!(cache.counters.peak_resident_bytes, 32);
    }
    #[test]
    fn dirty_missing_and_failed_updates_never_return_stale_success() {
        let mut cache = ResidentLayerCache::new(2, 2);
        cache.update(&frame(&["base", "layer:0"], 0), || Ok(()), |_, _| Ok(())).unwrap();
        assert!(cache.update(&frame(&["base"], 1), || Ok(()), |_, _| Err("lost device".into())).is_err());
        assert!(cache.resource("layer:0").is_err());
        cache.update(&frame(&["base"], 1), || Ok(()), |_, _| Ok(())).unwrap();
        assert_eq!(cache.counters.allocations, 2);
        assert_eq!(cache.counters.uploads, 3);
        cache.clear();
        assert!(cache.resource("base").is_err());
        assert_eq!(cache.counters.releases, 2);
    }
    #[test]
    fn invalid_shape_and_duplicate_keys_fail_before_mutation() {
        let mut cache = ResidentLayerCache::<()>::new(2, 2);
        for bad in [frame(&["base", "base"], 0), frame(&[], 0), frame(&["x"; 9], 0)] {
            assert!(cache.update(&bad, || panic!(), |_, _| panic!()).is_err());
        }
        let mut bad = frame(&["base"], 0);
        bad.width = 4;
        assert!(cache.update(&bad, || panic!(), |_, _| panic!()).is_err());
    }

    #[test]
    fn resource_ownership_stays_bounded_and_releases_on_drop_and_error() {
        use std::rc::Rc;
        use std::cell::Cell;
        struct Resource(Rc<Cell<usize>>);
        impl Drop for Resource {
            fn drop(&mut self) { self.0.set(self.0.get() - 1); }
        }
        let live = Rc::new(Cell::new(0));
        {
            let mut cache = ResidentLayerCache::new(2, 2);
            for frame_index in 0..20 {
                let keys = (0..8).map(|i| format!("{frame_index}:{i}")).collect::<Vec<_>>();
                let packet = frame(&keys.iter().map(String::as_str).collect::<Vec<_>>(), 255);
                cache.update(&packet, || {
                    live.set(live.get() + 1);
                    assert!(live.get() <= 8);
                    Ok(Resource(live.clone()))
                }, |_, _| Ok(())).unwrap();
                assert_eq!(live.get(), 8);
            }
            assert!(cache.update(&frame(&["failed"], 0), || {
                live.set(live.get() + 1);
                Ok(Resource(live.clone()))
            }, |_, _| Err("upload failure".into())).is_err());
            assert_eq!(live.get(), 0);
            cache.update(&frame(&["last"], 0), || {
                live.set(live.get() + 1);
                Ok(Resource(live.clone()))
            }, |_, _| Ok(())).unwrap();
            assert_eq!(live.get(), 1);
        }
        assert_eq!(live.get(), 0);
    }

    #[cfg(windows)]
    #[test]
    fn resident_gpu_pixels_match_fresh_uploads_over_repeats_dirty_alpha_and_order() {
        use crate::d3d11_bgra_video::{NativeBgraNv12VideoProcessor, NativeCanvasBgraLayer};
        use crate::d3d11_frame_pool::NativeNv12FramePool;
        use std::time::{Duration, Instant};
        let mut pool = match NativeNv12FramePool::new(128, 128, 2) {
            Ok(pool) => pool,
            Err(reason) => { eprintln!("GPU resident parity unavailable: {reason}"); return; }
        };
        let processor = NativeBgraNv12VideoProcessor::new_for_pool(&pool).unwrap();
        let mut cache = ResidentLayerCache::new(128, 128);
        let mut packet = CanvasPacketFrame { width: 128, height: 128, layers: vec![
            CanvasPacketLayer { stream_key: "base".into(), opacity: 1.0,
                pixels: [32, 64, 96, 255].repeat(128 * 128), geometry: LayerGeometry::identity(128, 128) },
            CanvasPacketLayer { stream_key: "layer:0".into(), opacity: 0.5,
                pixels: [160, 32, 64, 96].repeat(128 * 128), geometry: LayerGeometry::identity(128, 128) },
            CanvasPacketLayer { stream_key: "layer:1".into(), opacity: 0.8,
                pixels: [16, 192, 80, 128].repeat(128 * 128), geometry: LayerGeometry::identity(128, 128) },
        ] };
        let start = Instant::now();
        for index in 0..60 {
            if index == 20 { packet.layers[1].pixels[0..4].copy_from_slice(&[255, 0, 0, 255]); }
            if index == 30 { packet.layers[2].opacity = 0.2; }
            if index == 40 { packet.layers.swap(1, 2); }
            cache.update(&packet, || processor.create_bgra_input_texture(),
                |texture, pixels| processor.upload_canvas_layer_for_validation(texture, pixels)).unwrap();
            let layers = packet.layers.iter().map(|source| NativeCanvasBgraLayer {
                texture: cache.resource(&source.stream_key).unwrap(), opacity: source.opacity,
            }).collect::<Vec<_>>();
            let cached_slot = pool.queue_gpu_canvas_layers(&processor, &layers, index * 2).unwrap();
            pool.wait_for_gpu_copy(cached_slot, Duration::from_secs(15)).unwrap();
            let cached = pool.readback_slot_for_validation(cached_slot).unwrap();
            drop(pool.acquire_gpu_complete_ready().unwrap().unwrap());
            pool.complete_encode(cached_slot, index * 2).unwrap();
            let textures = packet.layers.iter().map(|source| {
                let texture = processor.create_bgra_input_texture().unwrap();
                processor.upload_canvas_layer_for_validation(&texture, &source.pixels).unwrap();
                texture
            }).collect::<Vec<_>>();
            let layers = textures.iter().zip(&packet.layers).map(|(texture, source)|
                NativeCanvasBgraLayer { texture, opacity: source.opacity }).collect::<Vec<_>>();
            let fresh_slot = pool.queue_gpu_canvas_layers(&processor, &layers, index * 2 + 1).unwrap();
            pool.wait_for_gpu_copy(fresh_slot, Duration::from_secs(15)).unwrap();
            let fresh = pool.readback_slot_for_validation(fresh_slot).unwrap();
            drop(pool.acquire_gpu_complete_ready().unwrap().unwrap());
            pool.complete_encode(fresh_slot, index * 2 + 1).unwrap();
            assert_eq!(cached, fresh, "resident NV12 differs from fresh upload at {index}");
        }
        assert_eq!(cache.counters.uploads, 4);
        assert_eq!(cache.counters.allocations, 3);
        assert_eq!(cache.counters.reuse_hits, 176);
        eprintln!("GPU parity: 60 frames, cached uploads=4 vs fresh=180, cached allocations=3 vs fresh=180, elapsed={:?}", start.elapsed());
        cache.clear();
        assert_eq!(cache.counters.releases, 3);
    }
}
