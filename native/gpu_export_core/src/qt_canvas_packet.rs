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
    manifest: &[u8], mut plane: impl FnMut(&str, usize) -> Result<Vec<u8>, String>,
) -> Result<CanvasPacketFrame, String> {
    if manifest.len() > 64 * 1024 {
        return Err("Canvas manifest exceeds 64 KiB".into());
    }
    let root: Value = serde_json::from_slice(&manifest)
        .map_err(|e| format!("Invalid Canvas packet JSON: {e}"))?;
    if root["schema"] != 1 || root["format"] != "mms_canvas_bgra_layers_v1"
        || root["pixel_format"] != "BGRA8_STRAIGHT"
        || root["coordinate_origin"] != "top_left"
        || root["qt_canvas_rasterized_on_cpu"] != true
        || root["production_export_changed"] != false
    {
        return Err("Not a supported Qt straight-alpha Canvas v1 packet".into());
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
        let actual = format!("{:x}", Sha256::digest(&pixels));
        if !digest.eq_ignore_ascii_case(&actual) {
            return Err(format!("Canvas layer {i} checksum mismatch"));
        }
        // The video processor expects an opaque base, and transparent
        // overlays must be straight alpha from Qt's Format_ARGB32.
        if i == 0 && pixels.chunks_exact(4).any(|px| px[3] != 255) {
            return Err("Qt Canvas base must be fully opaque".into());
        }
        layers.push(CanvasPacketLayer {
            stream_key: expected_key,
            opacity: opacity as f32,
            pixels,
        });
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
