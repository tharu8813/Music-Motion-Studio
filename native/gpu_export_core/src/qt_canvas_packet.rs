//! Validate immutable Qt CanvasSnapshot BGRA layer manifests on CPU.
//! The rasterized Qt source comes from native_canvas_layer_packets.py.
//! Production exports are not affected. Every payload is SHA-256 checked
//! before the D3D11 video renderer receives any pixels.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Component, Path};

pub const MAX_LAYERS: usize = 8;
pub(crate) const MAX_TEXT_QUADS: usize = 64;
pub const MAX_FRAME_BYTES: usize = 3840 * 2160 * 4;
pub const CANVAS_PACKET_SCHEMAS: [u64; 6] = [1, 2, 3, 4, 5, 6];

#[derive(Clone, Copy, Debug)]
pub struct VideoRequest {
    pub asset: usize,
    pub media_time: Option<i64>,
    pub media_limit: Option<i64>,
    pub crop: bool,
    pub flip_horizontal: bool,
    pub flip_vertical: bool,
    pub border_radius: f32,
    pub saturation: f32,
}

#[derive(Debug)]
pub struct TextQuad {
    pub source: [f32; 4],
    pub target: [f32; 4],
    pub clip: [f32; 4],
    pub opacity: f32,
}

#[derive(Debug)]
pub struct CanvasPacketLayer {
    pub stream_key: String,
    pub opacity: f32,
    pub(crate) pixels: Vec<u8>,
    pub geometry: LayerGeometry,
    pub particle_sprites: bool,
    pub radial_segments: bool,
    pub text_quads: Vec<TextQuad>,
    pub video: Option<VideoRequest>,
    // SHA-256 computed by the strictly validated packet parser. Never
    // recompute it in the GPU resident cache: 1080p exports previously
    // hashed every received BGRA plane twice on the Rust CPU.
    //
    // crate-visible only: downstream callers must not manufacture a
    // "verified" checksum for bytes that have not passed validation.
    pub(crate) validated_sha256: [u8; 32],
}

impl CanvasPacketLayer {
    pub fn pixels(&self) -> &[u8] {
        &self.pixels
    }

    pub(crate) fn take_validated_pixels(&mut self) -> (Vec<u8>, [u8; 32]) {
        (std::mem::take(&mut self.pixels), self.validated_sha256)
    }

    pub fn composition_opacity(&self) -> f32 {
        if self.particle_sprites || self.radial_segments { 1.0 } else { self.opacity }
    }

    pub fn uses_resident_texture(&self, width: u32, height: u32) -> bool {
        self.kind().is_none() && self.geometry.is_identity(width, height)
    }

    pub fn kind(&self) -> Option<&'static str> {
        if self.video.is_some() {
            Some("video_source")
        } else if self.particle_sprites {
            Some("particle_sprites")
        } else if self.radial_segments {
            Some("radial_segments")
        } else if !self.text_quads.is_empty() {
            Some("text_quads")
        } else {
            None
        }
    }
}

fn text_quads(value: &Value, g: LayerGeometry, width: u32, height: u32) -> Result<Vec<TextQuad>, String> {
    let entries = value.as_array().filter(|items| (1..=MAX_TEXT_QUADS).contains(&items.len()))
        .ok_or("Text atlas requires 1..64 quads")?;
    let read_rect = |value: &Value, bounds: Option<(u32, u32)>| -> Result<[f32; 4], String> {
        let values = value.as_array().filter(|values| values.len() == 4)
            .ok_or("Text quad requires four rectangle coordinates")?;
        let mut rect = [0.0; 4];
        for (out, value) in rect.iter_mut().zip(values) {
            *out = value.as_f64().filter(|n| n.is_finite() && n.abs() <= 1e6)
                .ok_or("Text quad coordinates must be finite and bounded")? as f32;
            if bounds.is_some() && value.as_u64().is_none() {
                return Err("Text quad atlas and clip coordinates must be integers".into());
            }
        }
        let [l, t, r, b] = rect;
        if l >= r || t >= b || bounds.is_some_and(|(w, h)| r > w as f32 || b > h as f32) {
            return Err("Text quad rectangle outside bounds".into());
        }
        Ok(rect)
    };
    entries.iter().map(|entry| {
        let values = entry.as_array().filter(|values| values.len() == 4)
            .ok_or("Invalid text quad")?;
        Ok(TextQuad {
            source: read_rect(&values[0], Some((g.width, g.height)))?,
            target: read_rect(&values[1], None)?,
            clip: read_rect(&values[2], Some((width, height)))?,
            opacity: values[3].as_f64().filter(|n| (0.0..=1.0).contains(n))
                .ok_or("Invalid text quad opacity")? as f32,
        })
    }).collect()
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct LayerGeometry {
    pub width: u32,
    pub height: u32,
    pub inverse: [f32; 6],
    pub clip: [u32; 4],
}
impl LayerGeometry {
    pub fn identity(width: u32, height: u32) -> Self {
        Self { width, height, inverse: [1., 0., 0., 1., 0., 0.], clip: [0, 0, width, height] }
    }
    pub fn is_identity(&self, width: u32, height: u32) -> bool {
        *self == Self::identity(width, height)
    }
}

fn geometry(entry: &Value, schema: u64, width: u32, height: u32, base: bool) -> Result<LayerGeometry, String> {
    let mut g = LayerGeometry::identity(width, height);
    for (key, dimension, maximum) in [("width", &mut g.width, 3840), ("height", &mut g.height, 2160)] {
        if let Some(value) = entry.get(key) {
            let n = value.as_u64().ok_or("Source dimensions must be positive integers")?;
            if n == 0 || n > maximum { return Err("Source dimensions exceed bounds".into()); }
            *dimension = n as u32;
        }
    }
    if let Some(value) = entry.get("inverse_transform") {
        let a = value.as_array().filter(|a| a.len() == 6).ok_or("inverse_transform requires six numbers")?;
        for (dst, src) in g.inverse.iter_mut().zip(a) {
            let n = src.as_f64().ok_or("inverse_transform requires finite numbers")?;
            if !n.is_finite() || n.abs() > 1e6 { return Err("inverse_transform exceeds finite bounds".into()); }
            *dst = n as f32;
        }
        let m = g.inverse;
        if (m[0] as f64 * m[3] as f64 - m[1] as f64 * m[2] as f64).abs() < 1e-12 {
            return Err("inverse_transform must be invertible".into());
        }
    }
    if let Some(value) = entry.get("clip") {
        let a = value.as_array().filter(|a| a.len() == 4).ok_or("clip requires four integer bounds")?;
        for (dst, src) in g.clip.iter_mut().zip(a) {
            *dst = u32::try_from(src.as_u64().ok_or("clip requires nonnegative integers")?).map_err(|_| "clip overflow")?;
        }
        let [l,t,r,b] = g.clip;
        if l > r || t > b || r > width || b > height { return Err("clip must be output half-open bounds".into()); }
    }
    if (schema == 1 || base) && !g.is_identity(width, height) {
        return Err("v1 layers and v2 base require full-frame identity geometry".into());
    }
    Ok(g)
}

#[derive(Debug)]
pub struct CanvasPacketFrame {
    pub width: u32,
    pub height: u32,
    pub layers: Vec<CanvasPacketLayer>,
}

fn positive_dimension(value: &Value, key: &str, max: u32) -> Result<u32, String> {
    let raw = value[key].as_u64().ok_or_else(|| format!("Missing positive {key}"))?;
    if raw < 2 || raw > max as u64 || raw % 2 != 0 {
        return Err(format!("{key} must be even and between 2 and {max}"));
    }
    Ok(raw as u32)
}

pub fn load_qt_canvas_packet(path: &Path) -> Result<CanvasPacketFrame, String> {
    let manifest = fs::read(path).map_err(|e| format!("Cannot read Canvas manifest: {e}"))?;
    let directory = path.parent().ok_or("Canvas manifest directory missing")?;
    parse_qt_canvas_packet(&manifest, |name, size| {
        let filepath = directory.join(name);
        let metadata = fs::symlink_metadata(&filepath).map_err(|e| e.to_string())?;
        if !metadata.file_type().is_file() || metadata.len() != size as u64 {
            return Err("Canvas layer is not a regular file of expected size".into());
        }
        fs::read(filepath).map_err(|e| e.to_string())
    })
}

/// Shared validation for diagnostic files and production binary planes.
pub fn parse_qt_canvas_packet(
    manifest: &[u8], plane: impl FnMut(&str, usize) -> Result<Vec<u8>, String>,
) -> Result<CanvasPacketFrame, String> {
    if manifest.len() > 64 * 1024 {
        return Err("Canvas manifest exceeds 64 KiB".into());
    }
    let root: Value = serde_json::from_slice(manifest)
        .map_err(|e| format!("Invalid Canvas packet JSON: {e}"))?;
    parse_qt_canvas_packet_value(&root, false, plane)
}

fn verify_layer(pixels: &[u8], digest: &str, index: usize) -> Result<[u8; 32], String> {
    let actual = Sha256::digest(pixels);
    if !digest.eq_ignore_ascii_case(&format!("{actual:x}")) {
        return Err(format!("Canvas layer {index} checksum mismatch"));
    }
    if index == 0 && pixels.chunks_exact(4).any(|px| px[3] != 255) {
        return Err("Qt Canvas base must be fully opaque".into());
    }
    Ok(actual.into())
}

fn verify_layer_batch(
    layers: &mut [CanvasPacketLayer], items: &[Value], first: usize,
) -> Result<(), String> {
    for (offset, (layer, item)) in layers.iter_mut().zip(items).enumerate() {
        let digest = item["sha256"].as_str().ok_or("Layer SHA-256 missing")?;
        if layer.validated_sha256 == [0; 32] {
            layer.validated_sha256 = verify_layer(&layer.pixels, digest, first + offset)?;
        }
    }
    Ok(())
}

fn verify_layers_in_parallel(layers: &mut [CanvasPacketLayer], items: &[Value]) -> Result<(), String> {
    let total = layers.iter().map(|layer| layer.pixels.len()).sum::<usize>();
    let (index, left_bytes) = layers.iter().scan(0, |sum, layer| {
        *sum += layer.pixels.len();
        Some(*sum)
    }).take(layers.len() - 1).enumerate()
        .min_by_key(|(_, bytes)| bytes.abs_diff(total / 2))
        .ok_or("Parallel Canvas validation needs at least two layers")?;
    if left_bytes.min(total - left_bytes) < 1024 * 1024 {
        return verify_layer_batch(layers, items, 0);
    }
    let split = index + 1;
    let (left, right) = layers.split_at_mut(split);
    let (first, second) = items.split_at(split);
    let checked = std::thread::scope(|scope| {
        let worker = std::thread::Builder::new()
            .spawn_scoped(scope, || verify_layer_batch(left, first, 0)).ok()?;
        let right_result = verify_layer_batch(right, second, split);
        Some(worker.join().unwrap_or_else(|_| Err("Canvas validation worker panicked".into()))
            .and(right_result))
    });
    match checked {
        Some(result) => result,
        None => verify_layer_batch(layers, items, 0),
    }
}

/// A validated manifest can be reused by the streaming v2 layer-reference
/// reader. This avoids parsing identical JSON twice per frame while retaining
/// the entire shared geometry, SHA-256 and opaque-base validation.
pub(crate) fn parse_qt_canvas_packet_value(
    root: &Value, parallel: bool, mut plane: impl FnMut(&str, usize) -> Result<Vec<u8>, String>,
) -> Result<CanvasPacketFrame, String> {
    parse_qt_canvas_packet_with_refs(root, parallel, |name, size| Ok((plane(name, size)?, None)))
}

pub(crate) fn parse_qt_canvas_packet_with_refs(
    root: &Value, parallel: bool,
    mut plane: impl FnMut(&str, usize) -> Result<(Vec<u8>, Option<[u8; 32]>), String>,
) -> Result<CanvasPacketFrame, String> {
    let schema = root["schema"].as_u64().ok_or("Canvas schema missing")?;
    if !CANVAS_PACKET_SCHEMAS.contains(&schema) || root["format"] != format!("mms_canvas_bgra_layers_v{schema}")
        || root["pixel_format"] != "BGRA8_STRAIGHT"
        || root["coordinate_origin"] != "top_left"
        || root["qt_canvas_rasterized_on_cpu"] != true
        || root["production_export_changed"] != false
    {
        return Err("Not a supported Qt straight-alpha Canvas packet".into());
    }
    let width = positive_dimension(&root, "width", 3840)?;
    let height = positive_dimension(&root, "height", 2160)?;
    let plane_bytes = (width as usize)
        .checked_mul(height as usize)
        .and_then(|n| n.checked_mul(4))
        .ok_or("Canvas byte size overflow")?;
    if plane_bytes > MAX_FRAME_BYTES {
        return Err("Canvas BGRA plane too large".into());
    }
    let items = root["layers"].as_array().ok_or("Canvas layer list missing")?;
    if items.is_empty() || items.len() > MAX_LAYERS {
        return Err("Canvas packet requires 1..8 layers".into());
    }
    if root["native_gpu_alpha_composition_required"] != (items.len() > 1) {
        return Err("Canvas alpha-composition manifest flag contradicts layer count".into());
    }
    let parallel = parallel && items.len() > 1 && plane_bytes >= 1024 * 1024;
    let mut layers = Vec::with_capacity(items.len());
    for (i, entry) in items.iter().enumerate() {
        if entry["z_order"].as_u64() != Some(i as u64) {
            return Err(format!("Layer {i} is out of expected Z order"));
        }
        let name = entry["file"].as_str().ok_or("Layer filename missing")?;
        let mut components = Path::new(name).components();
        if !matches!(components.next(), Some(Component::Normal(_)))
            || components.next().is_some() || !name.ends_with(".bgra")
        {
            return Err(format!("Unsafe Canvas layer filename at index {i}"));
        }
        let stream_key = entry["stream_key"].as_str()
            .ok_or("Canvas stream key missing")?;
        let expected_key = if i == 0 {
            "base".to_string()
        } else {
            format!("layer:{}", i - 1)
        };
        if stream_key != expected_key {
            return Err("Canvas Z band stream key mismatch".into());
        }
        let opacity = entry["opacity"].as_f64().ok_or("Canvas layer opacity missing")?;
        if !opacity.is_finite() || !(0.0..=1.0).contains(&opacity)
            || (i == 0 && opacity != 1.0)
        {
            return Err("Canvas layer opacity is invalid".into());
        }
        let geometry = geometry(entry, schema, width, height, i == 0)?;
        let kind = entry.get("kind");
        let radial_segments = kind.is_some_and(|kind| kind == "radial_segments");
        let (particle_sprites, text_quads, video) = match kind {
            None => (false, Vec::new(), None),
            Some(kind) if kind == "particle_sprites" && schema >= 3 && i != 0 => (true, Vec::new(), None),
            Some(kind) if kind == "radial_segments" && schema >= 6 && i != 0
                && geometry.width == 6 && (8..=192).contains(&geometry.height)
                && geometry.height % 2 == 0 && entry.get("inverse_transform").is_none()
                && entry.get("clip").is_some() => (false, Vec::new(), None),
            Some(kind) if kind == "text_quads" && schema >= 4 && i != 0 && opacity == 1.0
                && entry.get("inverse_transform").is_none() && entry.get("clip").is_none() =>
                (false, text_quads(&entry["quads"], geometry, width, height)?, None),
            Some(kind) if kind == "video_source" && schema >= 5 && i != 0
                && geometry.width == 1 && geometry.height == 1
                && entry.get("inverse_transform").is_none() && entry.get("clip").is_some() => {
                let [l, t, r, b] = geometry.clip;
                if r - l < 8 || b - t < 8 || [l, t, r, b].iter().any(|v| v % 2 != 0) {
                    return Err("Native video requires even, nonempty placement".into());
                }
                let asset = entry["asset"].as_u64().filter(|i| *i < MAX_LAYERS as u64)
                    .ok_or("Invalid native video asset index")? as usize;
                let media_time = match entry.get("media_time") {
                    Some(Value::Null) => None,
                    Some(value) => Some(value.as_i64().filter(|v| (0..=864_000_000_000).contains(v))
                        .ok_or("Invalid native video timestamp")?),
                    None => return Err("Missing native video timestamp".into()),
                };
                let crop = entry["crop"].as_bool().ok_or("Missing native video fit mode")?;
                let media_limit = if entry["media_limit"].is_null() { None } else {
                    Some(entry["media_limit"].as_i64().filter(|v| (1..=864_000_000_000).contains(v))
                        .ok_or("Invalid native video trim")?)
                };
                let flip_horizontal = entry["flip_horizontal"].as_bool().ok_or("Missing video flip")?;
                let flip_vertical = entry["flip_vertical"].as_bool().ok_or("Missing video flip")?;
                let border_radius = entry["border_radius"].as_f64()
                    .filter(|value| (0.0..=f64::from((r - l).min(b - t)) / 2.0).contains(value))
                    .ok_or("Invalid native video corner radius")? as f32;
                let saturation = entry["saturation"].as_f64().filter(|value| (0.0..=3.0).contains(value))
                    .ok_or("Invalid native video saturation")? as f32;
                (false, Vec::new(), Some(VideoRequest { asset, media_time, media_limit, crop,
                    flip_horizontal, flip_vertical, border_radius, saturation }))
            }
            _ => return Err("Unsupported Canvas layer kind".into()),
        };
        if particle_sprites && (geometry.width != 4 || geometry.height > 1000
            || entry.get("inverse_transform").is_some() || entry.get("clip").is_none()) {
            return Err("Invalid particle sprite layout".into());
        }
        let plane_bytes = if video.is_some() { 0 } else { geometry.width as usize * geometry.height as usize * 4 };
        if entry["bytes"].as_u64() != Some(plane_bytes as u64) {
            return Err(format!("Layer {i} declared byte count mismatch"));
        }
        let digest = entry["sha256"].as_str().ok_or("Layer SHA-256 missing")?;
        if digest.len() != 64 || !digest.bytes().all(|ch| ch.is_ascii_hexdigit()) {
            return Err("Canvas layer SHA-256 has invalid syntax".into());
        }
        let (pixels, previous_digest) = plane(name, plane_bytes)?;
        if pixels.len() != plane_bytes {
            return Err(format!("Canvas layer {i} has unexpected byte count"));
        }
        if particle_sprites {
            let [left, top, right, bottom] = geometry.clip.map(|v| v as f32);
            for sprite in pixels.chunks_exact(16) {
                let x = f32::from_le_bytes(sprite[0..4].try_into().unwrap());
                let y = f32::from_le_bytes(sprite[4..8].try_into().unwrap());
                let radius = f32::from_le_bytes(sprite[8..12].try_into().unwrap());
                if !(left..=right).contains(&x) || !(top..=bottom).contains(&y)
                    || !(radius > 0.0 && radius <= 160.0) {
                    return Err("Invalid particle sprite position or radius".into());
                }
            }
        }
        if radial_segments {
            let [left, top, right, bottom] = geometry.clip.map(|v| v as f32);
            if left >= right || top >= bottom {
                return Err("Radial segments require a nonempty output clip".into());
            }
            for segment in pixels.chunks_exact(24) {
                let word = |i: usize| f32::from_le_bytes(segment[i..i + 4].try_into().unwrap());
                if ![word(0), word(8)].iter().all(|x| (left - 3840.0..=right + 3840.0).contains(x))
                    || ![word(4), word(12)].iter().all(|y| (top - 2160.0..=bottom + 2160.0).contains(y))
                    || !(0.5..=160.0).contains(&word(16)) {
                    return Err("Invalid radial segment position or radius".into());
                }
            }
        }
        let validated_sha256 = if let Some(previous_digest) = previous_digest {
            if !digest.eq_ignore_ascii_case(&format!("{:x}", sha2::digest::Output::<Sha256>::from(previous_digest))) {
                return Err(format!("Canvas layer {i} checksum mismatch"));
            }
            previous_digest
        } else if parallel {
            [0; 32]
        } else {
            verify_layer(&pixels, digest, i)?
        };
        layers.push(CanvasPacketLayer {
            stream_key: expected_key,
            opacity: opacity as f32,
            pixels,
            geometry,
            particle_sprites,
            radial_segments,
            text_quads,
            video,
            validated_sha256,
        });
    }
    if parallel {
        verify_layers_in_parallel(&mut layers, items)?;
    }
    Ok(CanvasPacketFrame { width, height, layers })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_missing_manifest() {
        assert!(load_qt_canvas_packet(Path::new("missing-qt-canvas-packet.json")).is_err());
    }
    #[test]
    fn parses_even_dimensions_strictly() {
        let v = serde_json::json!({"width": 320,"height": 180});
        assert_eq!(positive_dimension(&v, "width", 3840).unwrap(), 320);
        assert_eq!(positive_dimension(&v, "height", 2160).unwrap(), 180);
        assert!(positive_dimension(&serde_json::json!({"width": 319}), "width", 3840).is_err());
        assert!(positive_dimension(&serde_json::json!({"width": 10000}), "width", 3840).is_err());
    }
    #[test]
    fn validates_complete_packet_and_rejects_tampered_pixels() {
        use std::time::{SystemTime, UNIX_EPOCH};
        let nonce = SystemTime::now().duration_since(UNIX_EPOCH)
            .unwrap().as_nanos();
        let directory = std::env::temp_dir().join(
            format!("mms-canvas-packet-{}-{nonce}", std::process::id())
        );
        std::fs::create_dir(&directory).unwrap();
        let base: Vec<u8> = [18, 29, 42, 255].repeat(4 * 4);
        let alpha: Vec<u8> = [220, 80, 24, 128].repeat(4 * 4);
        let names = ["base.bgra", "lyric.bgra"];
        for (name, bytes) in names.iter().zip([&base, &alpha]) {
            std::fs::write(directory.join(name), bytes).unwrap();
        }
        let root = serde_json::json!({
            "schema":1,
            "format":"mms_canvas_bgra_layers_v1",
            "pixel_format":"BGRA8_STRAIGHT",
            "coordinate_origin":"top_left",
            "width":4, "height":4,
            "qt_canvas_rasterized_on_cpu":true,
            "native_gpu_alpha_composition_required":true,
            "production_export_changed":false,
            "layers":[
                {
                    "file":names[0], "stream_key":"base", "z_order":0,
                    "opacity":1.0, "bytes":64,
                    "sha256":format!("{:x}", Sha256::digest(&base))
                },
                {
                    "file":names[1], "stream_key":"layer:0", "z_order":1,
                    "opacity":1.0, "bytes":64,
                    "sha256":format!("{:x}", Sha256::digest(&alpha))
                }
            ]
        });
        let manifest = directory.join("frame.json");
        std::fs::write(&manifest, serde_json::to_vec(&root).unwrap()).unwrap();
        let loaded = load_qt_canvas_packet(&manifest).unwrap();
        assert_eq!((loaded.width, loaded.height), (4, 4));
        assert_eq!(loaded.layers.len(), 2);
        assert_eq!(loaded.layers[1].pixels, alpha);
        assert_eq!(loaded.layers[0].validated_sha256,
                   <[u8; 32]>::from(Sha256::digest(&base)));
        assert_eq!(loaded.layers[1].validated_sha256,
                   <[u8; 32]>::from(Sha256::digest(&alpha)));
        let mut altered = base;
        altered[0] ^= 0x01;
        std::fs::write(directory.join(names[0]), altered).unwrap();
        assert!(load_qt_canvas_packet(&manifest).unwrap_err().contains("checksum mismatch"));
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn parallel_validation_preserves_digests_and_rejects_either_invalid_batch() {
        let base = [18, 29, 42, 255].repeat(512 * 512);
        let overlay = [3, 9, 7, 128].repeat(512 * 512);
        let root = serde_json::json!({
            "schema": 1, "format": "mms_canvas_bgra_layers_v1", "pixel_format": "BGRA8_STRAIGHT",
            "coordinate_origin": "top_left", "width": 512, "height": 512,
            "qt_canvas_rasterized_on_cpu": true, "production_export_changed": false,
            "native_gpu_alpha_composition_required": true,
            "layers": [
                {"file": "base.bgra", "stream_key": "base", "z_order": 0, "opacity": 1,
                 "bytes": base.len(), "sha256": format!("{:x}", Sha256::digest(&base))},
                {"file": "overlay.bgra", "stream_key": "layer:0", "z_order": 1, "opacity": 1,
                 "bytes": overlay.len(), "sha256": format!("{:x}", Sha256::digest(&overlay))},
            ],
        });
        let read = |name: &str, _| Ok(if name == "base.bgra" { base.clone() } else { overlay.clone() });
        let serial = parse_qt_canvas_packet_value(&root, false, read).unwrap();
        let parallel = parse_qt_canvas_packet_value(&root, true, read).unwrap();
        for (left, right) in serial.layers.iter().zip(parallel.layers) {
            assert_eq!(left.validated_sha256, right.validated_sha256);
            assert_eq!(left.pixels, right.pixels);
        }
        for index in 0..2 {
            let mut bad = root.clone();
            bad["layers"][index]["sha256"] = "0".repeat(64).into();
            assert_eq!(parse_qt_canvas_packet_value(&bad, true, read).unwrap_err(),
                       format!("Canvas layer {index} checksum mismatch"));
        }
    }

    #[test]
    fn particle_rows_require_typed_schema_and_finite_geometry() {
        let base = [0, 0, 0, 255].repeat(4 * 4);
        let parse = |schema, x: f32, radius: f32| {
            let mut sprite = Vec::new();
            for value in [x, 1.0, radius] {
                sprite.extend_from_slice(&value.to_le_bytes());
            }
            sprite.extend_from_slice(&0x80c06132u32.to_le_bytes());
            let root = serde_json::json!({
                "schema": schema, "format": format!("mms_canvas_bgra_layers_v{schema}"),
                "pixel_format": "BGRA8_STRAIGHT", "coordinate_origin": "top_left",
                "width": 4, "height": 4,
                "qt_canvas_rasterized_on_cpu": true, "production_export_changed": false,
                "native_gpu_alpha_composition_required": true,
                "layers": [
                    {"file":"base.bgra", "stream_key":"base", "z_order":0, "opacity":1,
                     "bytes":base.len(), "sha256":format!("{:x}", Sha256::digest(&base))},
                    {"file":"sprites.bgra", "stream_key":"layer:0", "z_order":1, "opacity":0.5,
                     "kind":"particle_sprites", "width":4, "height":1, "clip":[0,0,4,4],
                     "bytes":sprite.len(), "sha256":format!("{:x}", Sha256::digest(&sprite))},
                ],
            });
            parse_qt_canvas_packet_value(&root, false, |name, _| {
                Ok(if name == "base.bgra" { base.clone() } else { sprite.clone() })
            })
        };
        let frame = parse(3, 1.5, 2.0).unwrap();
        assert!(frame.layers[1].particle_sprites);
        assert!(!frame.layers[1].uses_resident_texture(4, 1));
        assert_eq!(frame.layers[1].composition_opacity(), 1.0);
        assert!(parse(2, 1.5, 2.0).is_err());
        for (x, radius) in [(f32::NAN, 1.0), (4.5, 1.0), (1.0, 0.0), (1.0, 160.5)] {
            assert!(parse(3, x, radius).unwrap_err().contains("Invalid particle sprite"));
        }
    }

    #[test]
    fn radial_segments_require_schema_six_and_finite_endpoints() {
        let base = [255u8; 64];
        let parse = |schema, x: f32, radius: f32| {
            let segment: Vec<_> = [x, 0.5, 2.0, 2.0, radius].into_iter()
                .flat_map(f32::to_le_bytes).chain(0xffabcdefu32.to_le_bytes()).collect();
            let bars = segment.repeat(8);
            let manifest = serde_json::json!({
                "schema": schema, "format": format!("mms_canvas_bgra_layers_v{schema}"),
                "pixel_format": "BGRA8_STRAIGHT", "coordinate_origin": "top_left", "width": 4, "height": 4,
                "qt_canvas_rasterized_on_cpu": true, "production_export_changed": false,
                "native_gpu_alpha_composition_required": true,
                "layers": [
                    {"file":"base.bgra", "stream_key":"base", "z_order":0, "opacity":1.0,
                     "bytes":base.len(), "sha256":format!("{:x}", Sha256::digest(base))},
                    {"file":"radial.bgra", "stream_key":"layer:0", "z_order":1, "opacity":0.5,
                     "kind":"radial_segments", "width":6, "height":8, "clip":[0,0,4,4],
                     "bytes":bars.len(), "sha256":format!("{:x}", Sha256::digest(&bars))}
                ]
            });
            parse_qt_canvas_packet_value(&manifest, false, |name, _| {
                Ok(if name == "base.bgra" { base.to_vec() } else { bars.clone() })
            })
        };
        let frame = parse(6, -1.0, 1.0).unwrap();
        assert_eq!(frame.layers[1].kind(), Some("radial_segments"));
        assert_eq!(frame.layers[1].composition_opacity(), 1.0);
        assert!(parse(5, 0.0, 1.0).is_err());
        assert!(parse(6, f32::NAN, 1.0).is_err());
        assert!(parse(6, 0.0, 0.0).is_err());
    }

    #[test]
    fn text_quads_validate_atlas_clip_transform_and_opacity() {
        use serde_json::json;
        let atlas = LayerGeometry::identity(128, 64);
        let valid = json!([[[0, 0, 64, 32], [-3.5, 5.0, 40.0, 25.5], [0, 5, 40, 26], 0.5]]);
        let quads = text_quads(&valid, atlas, 64, 64).unwrap();
        assert_eq!(quads[0].target[0], -3.5);
        assert_eq!(quads[0].opacity, 0.5);
        for (field, invalid) in [
            (0, json!([0, 0, 129, 32])),
            (1, json!([0.0, 0.0, 1e9, 20.0])),
            (2, json!([0, 0, 65, 32])),
            (2, json!([0, 0, 10.5, 32])),
            (3, json!(1.01)),
        ] {
            let mut bad = valid.clone();
            bad[0][field] = invalid;
            assert!(text_quads(&bad, atlas, 64, 64).is_err());
        }
    }

    #[test]
    fn path_policy_rejects_traversal_and_nested_segments() {
        for p in ["../a.bgra", "dir/a.bgra", "/a.bgra", "a.txt"] {
            let path = Path::new(p);
            let mut it = path.components();
            let valid = matches!(it.next(), Some(Component::Normal(_)))
                && it.next().is_none() && p.ends_with(".bgra");
            assert!(!valid, "{p}");
        }
    }
}

#[cfg(test)]
mod geometry_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn v1_rejects_nonidentity_and_v2_rejects_invalid_geometry_before_pixels() {
        for e in [json!({"inverse_transform":[1,0,0,1,1,0]}), json!({"clip":[0,0,2,4]}), json!({"width":3})] {
            assert!(geometry(&e,1,4,4,false).is_err());
            assert!(geometry(&e,2,4,4,true).is_err());
        }
        for e in [json!({"inverse_transform":[0,0,0,0,0,0]}),json!({"inverse_transform":[1,0,0,1,1e20,0]}),
                  json!({"inverse_transform":[1,0]}),json!({"clip":[3,0,2,4]}),json!({"clip":[0,0,5,4]}),
                  json!({"clip":[0.5,0,2,4]}),json!({"width":0})] {
            assert!(geometry(&e,2,4,4,false).is_err(),"{e}");
        }
        let g=geometry(&json!({"width":3,"height":1,"inverse_transform":[0,1,-1,0,2,3],"clip":[1,2,4,4]}),2,4,4,false).unwrap();
        assert_eq!((g.width,g.height,g.clip),(3,1,[1,2,4,4]));
    }
}
