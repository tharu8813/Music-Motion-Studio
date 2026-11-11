//! Validate immutable Qt CanvasSnapshot BGRA layer manifests on CPU.
//! The rasterized Qt source comes from native_canvas_layer_packets.py.
//! Production exports are not affected. Every payload is SHA-256 checked
//! before the D3D11 video renderer receives any pixels.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Component, Path};

pub const MAX_LAYERS: usize = 8;
pub const MAX_FRAME_BYTES: usize = 3840 * 2160 * 4;

#[derive(Debug)]
pub struct CanvasPacketLayer {
    pub stream_key: String,
    pub opacity: f32,
    pub pixels: Vec<u8>,
    pub geometry: LayerGeometry,
    // SHA-256 computed by the strictly validated packet parser. Never
    // recompute it in the GPU resident cache: 1080p exports previously
    // hashed every received BGRA plane twice on the Rust CPU.
    //
    // crate-visible only: downstream callers must not manufacture a
    // "verified" checksum for bytes that have not passed validation.
    pub(crate) validated_sha256: [u8; 32],
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

/// Check that every BGRA alpha byte is 0xff without allocating a staging
/// buffer. Two pixels share one 64-bit mask operation. Byte-order conversion
/// uses `from_le_bytes`, so this stays correct on any host architecture.
///
/// The packet parser has already checked the pixel buffer length. Keep this
/// helper fail-closed for malformed standalone inputs as well.
fn is_opaque_bgra(pixels: &[u8]) -> bool {
    if pixels.len() % 4 != 0 {
        return false;
    }
    const ALPHA_MASK: u64 = 0xff00_0000_ff00_0000;
    let mut pairs = pixels.chunks_exact(8);
    pairs.all(|pair| {
        let two_pixels = u64::from_le_bytes(pair.try_into().expect("eight-byte chunk"));
        (two_pixels & ALPHA_MASK) == ALPHA_MASK
    }) && pairs.remainder().chunks_exact(4).all(|pixel| pixel[3] == 255)
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
    parse_qt_canvas_packet_value(&root, plane)
}

/// A validated manifest can be reused by the streaming v2 layer-reference
/// reader. This avoids parsing identical JSON twice per frame while retaining
/// the entire shared geometry, SHA-256 and opaque-base validation.
pub(crate) fn parse_qt_canvas_packet_value(
    root: &Value, mut plane: impl FnMut(&str, usize) -> Result<Vec<u8>, String>,
) -> Result<CanvasPacketFrame, String> {
    let schema = root["schema"].as_u64().ok_or("Canvas schema missing")?;
    if !matches!(schema, 1 | 2) || root["format"] != format!("mms_canvas_bgra_layers_v{schema}")
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
        let plane_bytes = geometry.width as usize * geometry.height as usize * 4;
        if entry["bytes"].as_u64() != Some(plane_bytes as u64) {
            return Err(format!("Layer {i} declared byte count mismatch"));
        }
        let digest = entry["sha256"].as_str().ok_or("Layer SHA-256 missing")?;
        if digest.len() != 64 || !digest.bytes().all(|ch| ch.is_ascii_hexdigit()) {
            return Err("Canvas layer SHA-256 has invalid syntax".into());
        }
        let pixels = plane(name, plane_bytes)?;
        if pixels.len() != plane_bytes {
            return Err(format!("Canvas layer {i} has unexpected byte count"));
        }
        let actual = Sha256::digest(&pixels);
        // Preserve the existing case-insensitive SHA-256 wire contract.
        // Comparing the validated hex avoids introducing a second hash.
        if !digest.eq_ignore_ascii_case(&format!("{actual:x}")) {
            return Err(format!("Canvas layer {i} checksum mismatch"));
        }
        // The video processor expects an opaque base, and transparent
        // overlays must be straight alpha from Qt's Format_ARGB32.
        if i == 0 && !is_opaque_bgra(&pixels) {
            return Err("Qt Canvas base must be fully opaque".into());
        }
        layers.push(CanvasPacketLayer {
            stream_key: expected_key,
            opacity: opacity as f32,
            pixels,
            geometry,
            validated_sha256: actual.into(),
        });
    }
    Ok(CanvasPacketFrame { width, height, layers })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn opaque_bgra_wide_matches_per_pixel_validation_at_every_tail_and_offset() {
        assert!(is_opaque_bgra(&[]));
        for pixel_count in 1..48 {
            let mut pixels = Vec::with_capacity(pixel_count * 4);
            for i in 0..pixel_count {
                pixels.extend([
                    ((i * 13) % 256) as u8,
                    ((i * 19) % 256) as u8,
                    ((i * 23) % 256) as u8,
                    255,
                ]);
            }
            let scalar = |bytes: &[u8]| -> bool {
                bytes.len() % 4 == 0
                    && bytes.chunks_exact(4).all(|p| p[3] == 255)
            };
            assert!(scalar(&pixels));
            assert!(is_opaque_bgra(&pixels), "{pixel_count} opaque pixels");
            // Do not reject non-alpha changes, including boundaries between
            // adjacent 64-bit words and the final unmatched 4-byte pixel.
            for offset in (0..pixels.len()).step_by(4) {
                pixels[offset] ^= 0x80;
                assert!(is_opaque_bgra(&pixels));
                pixels[offset] ^= 0x80;
            }
            for i in 0..pixel_count {
                pixels[i * 4 + 3] = 0;
                assert_eq!(is_opaque_bgra(&pixels), scalar(&pixels));
                assert!(!is_opaque_bgra(&pixels), "opaque rejection at pixel {i}/{pixel_count}");
                pixels[i * 4 + 3] = 255;
            }
            for extra in 1..4 {
                let mut malformed = pixels.clone();
                malformed.extend(std::iter::repeat_n(255, extra));
                assert!(!is_opaque_bgra(&malformed));
            }
        }
    }

    #[test]
    fn opaque_bgra_wide_rejects_large_frame_boundary_alpha_mutations() {
        let mut pixels = vec![0u8; 1920 * 1080 * 4];
        for chunk in pixels.chunks_exact_mut(4) {
            chunk[3] = 255;
        }
        assert!(is_opaque_bgra(&pixels));
        for index in [0, 1, 1920 * 540, 1920 * 1080 - 2, 1920 * 1080 - 1] {
            let pos = index * 4 + 3;
            pixels[pos] = 254;
            assert!(!is_opaque_bgra(&pixels), "bad alpha at index {index}");
            pixels[pos] = 255;
        }
        assert!(is_opaque_bgra(&pixels));
    }

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
