//! MMSB v1: little-endian <4sI Q I Q>, JSON metadata then ordered BGRA planes.
//! One frame in flight; ACK follows upload/submission, never just pipe receipt.
use std::io::Read;
use crate::qt_canvas_packet::{parse_qt_canvas_packet, parse_qt_canvas_packet_value, CanvasPacketFrame, MAX_FRAME_BYTES, MAX_LAYERS};

pub fn read_frame(reader: &mut impl Read, index: u64) -> Result<Option<CanvasPacketFrame>, String> {
    let mut header = [0u8; 28];
    loop {
        match reader.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(format!("Frame header: {e}")),
        }
    }
    reader.read_exact(&mut header[1..]).map_err(|e| format!("Truncated frame header: {e}"))?;
    let version = u32::from_le_bytes(header[4..8].try_into().unwrap());
    let pts = u64::from_le_bytes(header[8..16].try_into().unwrap());
    let meta = u32::from_le_bytes(header[16..20].try_into().unwrap()) as usize;
    let payload = u64::from_le_bytes(header[20..28].try_into().unwrap());
    if &header[..4] != b"MMSB" || version != 1 || pts != index
        || meta == 0 || meta > 65536 || payload == 0
        || payload > (MAX_FRAME_BYTES * MAX_LAYERS) as u64 {
        return Err("Invalid binary frame version, PTS or lengths".into());
    }
    let mut manifest = vec![0; meta];
    reader.read_exact(&mut manifest).map_err(|e| format!("Truncated metadata: {e}"))?;
    let mut remaining = payload;
    let frame = parse_qt_canvas_packet(&manifest, |_, size| {
        if size as u64 > remaining { return Err("Frame payload shorter than planes".into()); }
        let mut pixels = vec![0; size];
        reader.read_exact(&mut pixels).map_err(|e| format!("Truncated BGRA plane: {e}"))?;
        remaining -= size as u64;
        Ok(pixels)
    })?;
    if remaining != 0 { return Err("Frame payload longer than planes".into()); }
    Ok(Some(frame))
}


/// MMSB binary-v2: the packet metadata names previous-frame pixel planes
/// that are byte-for-byte identical. Do NOT trust the request blindly: the
/// shared v1/v2 Canvas parser revalidates exact sizes, SHA-256 and opaque base.
///
/// Only the immediately preceding frame is retained by the caller. This
/// prevents cross-project cache poisoning and unbounded worker memory.
pub fn read_frame_reuse(
    reader: &mut impl Read, index: u64, previous: Option<&CanvasPacketFrame>,
) -> Result<Option<CanvasPacketFrame>, String> {
    read_frame_with_previous(reader, index, previous, 2)
}

/// Binary-v3: optionally patch a contiguous interval of whole BGRA rows from
/// the previous validated frame. Every reconstructed plane is SHA-256 verified
/// by the same strict v1/v2 manifest parser before reaching the GPU.
pub fn read_frame_delta(
    reader: &mut impl Read, index: u64, previous: Option<&CanvasPacketFrame>,
) -> Result<Option<CanvasPacketFrame>, String> {
    read_frame_with_previous(reader, index, previous, 3)
}

fn read_frame_with_previous(
    reader: &mut impl Read, index: u64, previous: Option<&CanvasPacketFrame>,
    expected_version: u32,
) -> Result<Option<CanvasPacketFrame>, String> {
    let mut header = [0u8; 28];
    loop {
        match reader.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(format!("Frame header: {e}")),
        }
    }
    reader.read_exact(&mut header[1..]).map_err(|e| format!("Truncated frame header: {e}"))?;
    let version = u32::from_le_bytes(header[4..8].try_into().unwrap());
    let pts = u64::from_le_bytes(header[8..16].try_into().unwrap());
    let meta = u32::from_le_bytes(header[16..20].try_into().unwrap()) as usize;
    let payload = u64::from_le_bytes(header[20..28].try_into().unwrap());
    if &header[..4] != b"MMSB" || version != expected_version || pts != index
        || meta == 0 || meta > 65536
        || payload > (MAX_FRAME_BYTES * MAX_LAYERS) as u64 {
        return Err("Invalid binary-v2 frame version, PTS or lengths".into());
    }
    let mut manifest = vec![0; meta];
    reader.read_exact(&mut manifest).map_err(|e| format!("Truncated metadata: {e}"))?;
    let root: serde_json::Value = serde_json::from_slice(&manifest)
        .map_err(|e| format!("Invalid binary-v2 Canvas JSON: {e}"))?;
    let layers = root["layers"].as_array().ok_or("Missing binary-v2 layers")?;
    if layers.is_empty() || layers.len() > MAX_LAYERS {
        return Err("Invalid binary-v2 layer count".into());
    }
    let flags = root["reuse_previous_frame"].as_array()
        .ok_or("Missing binary-v2 reuse flags")?;
    if flags.len() != layers.len() || flags.iter().any(|v| !v.is_boolean()) {
        return Err("Binary reuse flags must contain one bool per layer".into());
    }
    let row_deltas = if expected_version == 3 {
        let rows = root["row_deltas"].as_array()
            .ok_or("Binary-v3 requires row_deltas for every layer")?;
        if rows.len() != layers.len() {
            return Err("Binary-v3 row delta count mismatch".into());
        }
        for (delta, reused) in rows.iter().zip(flags.iter()) {
            if delta.is_null() { continue; }
            let range = delta.as_array().filter(|r| r.len() == 2)
                .ok_or("Binary-v3 row delta must be null or [first, end]")?;
            if reused.as_bool() == Some(true)
                || range.iter().any(|n| n.as_u64().is_none()) {
                return Err("Binary-v3 patch cannot reference reused/invalid rows".into());
            }
        }
        Some(rows)
    } else {
        // Never allow a binary-v2 reader to reinterpret v3 delta payloads.
        if !root["row_deltas"].is_null() {
            return Err("Binary-v2 cannot contain row deltas".into());
        }
        None
    };
    let mut remaining = payload;
    let mut layer_index = 0usize;
    let frame = parse_qt_canvas_packet_value(&root, |_, size| {
        let i = layer_index;
        layer_index += 1;
        let previous_matching = || -> Result<&crate::qt_canvas_packet::CanvasPacketLayer, String> {
            let old = previous.and_then(|p| p.layers.get(i))
                .ok_or("Binary previous-frame layer is missing")?;
            // Never borrow pixels from the wrong track/Z band or geometry.
            if old.stream_key != layers[i]["stream_key"].as_str().unwrap_or("")
                || old.geometry.width as u64 != layers[i]["width"].as_u64()
                    .unwrap_or(root["width"].as_u64().unwrap_or(0))
                || old.geometry.height as u64 != layers[i]["height"].as_u64()
                    .unwrap_or(root["height"].as_u64().unwrap_or(0))
                || old.pixels.len() != size {
                return Err("Binary previous-frame layer key or geometry mismatch".into());
            }
            Ok(old)
        };
        if flags[i].as_bool() == Some(true) {
            Ok(previous_matching()?.pixels.clone())
        } else if let Some(range) = row_deltas
            .and_then(|rows| rows[i].as_array())
        {
            let old = previous_matching()?;
            let height = old.geometry.height as usize;
            let stride = old.geometry.width as usize * 4;
            let first = range[0].as_u64().ok_or("Invalid first patch row")? as usize;
            let end = range[1].as_u64().ok_or("Invalid end patch row")? as usize;
            if first >= end || end > height || stride * height != size {
                return Err("Binary-v3 patch rows exceed source plane".into());
            }
            let patch_size = (end - first) * stride;
            if patch_size as u64 > remaining {
                return Err("Binary-v3 patch exceeds remaining payload".into());
            }
            // Only the bounded previous frame is cloned. SHA validation after
            // applying the patch catches wrong pixels, stale cache or tampering.
            let mut pixels = old.pixels.clone();
            reader.read_exact(&mut pixels[first * stride..end * stride])
                .map_err(|e| format!("Truncated binary-v3 BGRA row patch: {e}"))?;
            remaining -= patch_size as u64;
            Ok(pixels)
        } else {
            if size as u64 > remaining {
                return Err("Binary frame payload shorter than declared planes".into());
            }
            let mut pixels = vec![0; size];
            reader.read_exact(&mut pixels)
                .map_err(|e| format!("Truncated binary BGRA plane: {e}"))?;
            remaining -= size as u64;
            Ok(pixels)
        }
    })?;
    if remaining != 0 || layer_index != layers.len() {
        return Err("Binary-v2 frame payload longer than declared planes".into());
    }
    Ok(Some(frame))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    fn packet() -> Vec<u8> {
        let pixels = [1u8, 2, 3, 255].repeat(4);
        let metadata = serde_json::to_vec(&serde_json::json!({
            "schema":1,"format":"mms_canvas_bgra_layers_v1","pixel_format":"BGRA8_STRAIGHT",
            "coordinate_origin":"top_left","width":2,"height":2,
            "qt_canvas_rasterized_on_cpu":true,"production_export_changed":false,
            "native_gpu_alpha_composition_required":false,
            "layers":[{"file":"layer-0.bgra","stream_key":"base","z_order":0,
                "opacity":1,"bytes":16,"sha256":format!("{:x}",Sha256::digest(&pixels))}]
        })).unwrap();
        let mut wire = Vec::from(&b"MMSB"[..]);
        wire.extend(1u32.to_le_bytes()); wire.extend(0u64.to_le_bytes());
        wire.extend((metadata.len() as u32).to_le_bytes()); wire.extend(16u64.to_le_bytes());
        wire.extend(metadata); wire.extend(pixels); wire
    }
    fn packet_v2(index: u64, reuse: bool, pixels: &[u8], claimed_pixels: &[u8]) -> Vec<u8> {
        let metadata = serde_json::to_vec(&serde_json::json!({
            "schema":1,"format":"mms_canvas_bgra_layers_v1",
            "pixel_format":"BGRA8_STRAIGHT", "coordinate_origin":"top_left",
            "width":2, "height":2,
            "qt_canvas_rasterized_on_cpu":true,
            "production_export_changed":false,
            "native_gpu_alpha_composition_required":false,
            "reuse_previous_frame":[reuse],
            "layers":[{"file":"layer-0.bgra","stream_key":"base","z_order":0,
                "opacity":1,"bytes":16,
                "sha256":format!("{:x}",Sha256::digest(claimed_pixels))}]
        })).unwrap();
        let mut wire = Vec::from(&b"MMSB"[..]);
        wire.extend(2u32.to_le_bytes());
        wire.extend(index.to_le_bytes());
        wire.extend((metadata.len() as u32).to_le_bytes());
        wire.extend((if reuse {0} else {pixels.len() as u64}).to_le_bytes());
        wire.extend(metadata);
        if !reuse {wire.extend(pixels);}
        wire
    }
    #[test]
    fn v2_references_reconstruct_validated_pixels_with_zero_wire_bytes() {
        let first_pixels = [14, 22, 30, 255].repeat(4);
        let new_pixels = [18, 22, 30, 255].repeat(4);
        let first_wire = packet_v2(0, false, &first_pixels, &first_pixels);
        let first = read_frame_reuse(&mut &first_wire[..], 0, None).unwrap().unwrap();
        assert_eq!(first.layers[0].pixels, first_pixels);

        let repeated = packet_v2(1, true, &[], &first_pixels);
        let second = read_frame_reuse(&mut &repeated[..], 1, Some(&first))
            .unwrap().unwrap();
        assert_eq!(second.layers[0].pixels, first_pixels);
        assert_eq!(repeated.len(), 28 + u32::from_le_bytes(repeated[16..20].try_into().unwrap()) as usize);
        let dirty = packet_v2(2, false, &new_pixels, &new_pixels);
        let third = read_frame_reuse(&mut &dirty[..], 2, Some(&second)).unwrap().unwrap();
        assert_eq!(third.layers[0].pixels, new_pixels);

        // v1 never accepts v2, v2 never accepts v1: fail closed on
        // mismatched binaries and do not send malformed PTS to NVENC.
        assert!(read_frame(&mut &repeated[..], 1).is_err());
        assert!(read_frame_reuse(&mut &packet()[..], 0, None).is_err());
    }

    #[test]
    fn v2_rejects_missing_previous_tampered_checksum_and_payload_lengths() {
        let pixels = [1, 2, 3, 255].repeat(4);
        let ref_wire = packet_v2(1, true, &[], &pixels);
        assert!(read_frame_reuse(&mut &ref_wire[..], 1, None).is_err());
        let first = read_frame_reuse(
            &mut &packet_v2(0, false, &pixels, &pixels)[..], 0, None,
        ).unwrap().unwrap();
        let different = [2, 2, 3, 255].repeat(4);
        let forged = packet_v2(1, true, &[], &different);
        assert!(read_frame_reuse(&mut &forged[..], 1, Some(&first)).is_err());
        for bad in [
            packet_v2(1, false, &pixels[..8], &pixels),
            { let mut v=packet_v2(1,true,&[],&pixels);v[20..28].copy_from_slice(&1u64.to_le_bytes());v },
            { let mut v=packet_v2(1,true,&[],&pixels);v[4..8].copy_from_slice(&1u32.to_le_bytes());v },
        ] {
            assert!(read_frame_reuse(&mut &bad[..], 1, Some(&first)).is_err());
        }
    }

    #[test]
    fn valid_fragmented_frames_and_all_truncations() {
        struct Fragments<'a>(&'a [u8]);
        impl Read for Fragments<'_> {
            fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
                let n = out.len().min(3).min(self.0.len());
                out[..n].copy_from_slice(&self.0[..n]); self.0 = &self.0[n..]; Ok(n)
            }
        }
        let bytes = packet();
        let frame = read_frame(&mut Fragments(&bytes), 0).unwrap().unwrap();
        assert_eq!(frame.layers[0].pixels, [1,2,3,255].repeat(4));
        for n in 1..bytes.len() { assert!(read_frame(&mut &bytes[..n], 0).is_err(), "{n}"); }
        assert!(read_frame(&mut &bytes[..], 1).is_err());
        for offset in [0,4,16,20,bytes.len()-1] {
            let mut invalid = bytes.clone(); invalid[offset] ^= 0xff;
            assert!(read_frame(&mut &invalid[..], 0).is_err(), "{offset}");
        }
    }
    #[test]
    fn eof_truncation_and_untrusted_lengths() {
        assert!(read_frame(&mut &b""[..], 0).unwrap().is_none());
        for size in 1..28 { assert!(read_frame(&mut &vec![0; size][..], 0).is_err()); }
        let mut h = Vec::from(&b"MMSB"[..]);
        h.extend(1u32.to_le_bytes()); h.extend(0u64.to_le_bytes());
        h.extend(65537u32.to_le_bytes()); h.extend(16u64.to_le_bytes());
        assert!(read_frame(&mut &h[..], 0).is_err());
    }
}
