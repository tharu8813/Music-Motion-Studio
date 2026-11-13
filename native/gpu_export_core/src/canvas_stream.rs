//! MMSB v1: little-endian <4sI Q I Q>, JSON metadata then ordered BGRA planes.
//! One frame in flight; ACK follows upload/submission, never just pipe receipt.
use std::io::Read;
use crate::qt_canvas_packet::{parse_qt_canvas_packet, parse_qt_canvas_packet_with_refs, CanvasPacketFrame, MAX_FRAME_BYTES, MAX_LAYERS};

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
    reader: &mut impl Read, index: u64, previous: Option<&mut CanvasPacketFrame>,
) -> Result<Option<CanvasPacketFrame>, String> {
    read_frame_with_refs(reader, index, previous, 2, false)
}

/// Binary-v3: unchanged SHA-256 and metadata rules, optional LZ4 block
/// compression on *individual* changed planes. Only the wire is compressed;
/// the packet validator and GPU receive exactly the original BGRA pixels.
pub fn read_frame_adaptive(
    reader: &mut impl Read, index: u64, previous: Option<&mut CanvasPacketFrame>,
) -> Result<Option<CanvasPacketFrame>, String> {
    read_frame_with_refs(reader, index, previous, 3, false)
}

pub fn read_frame_parallel(
    reader: &mut impl Read, index: u64, previous: Option<&mut CanvasPacketFrame>, adaptive: bool,
) -> Result<Option<CanvasPacketFrame>, String> {
    read_frame_with_refs(reader, index, previous, if adaptive { 3 } else { 2 }, true)
}

fn read_frame_with_refs(
    reader: &mut impl Read, index: u64, previous: Option<&mut CanvasPacketFrame>,
    expected_version: u32,
    parallel: bool,
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
        return Err("Binary-v2 reuse flags must contain one bool per layer".into());
    }
    let mut remaining = payload;
    let mut previous = previous;
    let mut layer_index = 0usize;
    let frame = parse_qt_canvas_packet_with_refs(&root, parallel, |_, size| {
        let i = layer_index;
        layer_index += 1;
        // v3 checks encoded lengths, algorithms and the total payload before
        // trusting any decompressed data; v2 retains the original wire rules.
        let (encoded_size, algorithm) = if expected_version == 3 {
            let encoded_size = layers[i]["wire_bytes"].as_u64()
                .and_then(|n| usize::try_from(n).ok())
                .ok_or("Binary-v3 missing/overflowed plane wire length")?;
            let algorithm = layers[i]["compression"].as_str()
                .ok_or("Binary-v3 missing compression type")?;
            (encoded_size, algorithm)
        } else {
            (size, "raw")
        };
        if flags[i].as_bool() == Some(true) {
            if expected_version == 3 && (encoded_size != 0 || algorithm != "previous") {
                return Err("Binary-v3 reused plane must not carry any encoded bytes".into());
            }
            let old = previous.as_deref_mut().and_then(|p| p.layers.get_mut(i))
                .ok_or("Binary-v2 reused a plane without a previous matching frame")?;
            // Same key/order, dimensions and pixels. Opacity and transform may
            // change independently, and must come from the new manifest.
            if old.stream_key != layers[i]["stream_key"].as_str().unwrap_or("")
                || old.kind() != layers[i]["kind"].as_str()
                || old.geometry.width as u64 !=
                    layers[i]["width"].as_u64().unwrap_or(root["width"].as_u64().unwrap_or(0))
                || old.geometry.height as u64 !=
                    layers[i]["height"].as_u64().unwrap_or(root["height"].as_u64().unwrap_or(0))
                || old.pixels.len() != size {
                return Err("Binary-v2 previous layer key or geometry mismatch".into());
            }
            // The preceding frame has already been fully submitted to the
            // GPU before the worker reads this frame. Ownership transfer
            // avoids a second BGRA allocation and hash; the shared parser
            // compares the incoming hash with the original validated one.
            let (pixels, digest) = old.take_validated_pixels();
            Ok((pixels, Some(digest)))
        } else {
            if expected_version == 3 && !matches!(algorithm, "lz4" | "raw") {
                return Err("Binary-v3 unknown compression algorithm".into());
            }
            if expected_version == 3 && (
                (algorithm == "raw" && encoded_size != size)
                || (algorithm == "lz4" && (encoded_size == 0 || encoded_size >= size))
            ) {
                return Err("Binary-v3 plane compressed/raw sizes are inconsistent".into());
            }
            if encoded_size as u64 > remaining {
                return Err("Binary frame payload shorter than declared planes".into());
            }
            let pixels = if algorithm == "lz4" {
                let mut encoded = vec![0; encoded_size];
                reader.read_exact(&mut encoded)
                    .map_err(|e| format!("Truncated binary-v3 compressed plane: {e}"))?;
                let mut decoded = vec![0; size];
                let decoded_len = lz4_flex::block::decompress_into(&encoded, &mut decoded)
                    .map_err(|e| format!("Invalid binary-v3 LZ4 payload: {e}"))?;
                if decoded_len != size {
                    return Err("Binary-v3 LZ4 decoded length mismatch".into());
                }
                decoded
            } else {
                let mut pixels = vec![0; size];
                reader.read_exact(&mut pixels)
                    .map_err(|e| format!("Truncated binary plane: {e}"))?;
                pixels
            };
            remaining -= encoded_size as u64;
            Ok((pixels, None))
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
        let mut first = read_frame_reuse(&mut &first_wire[..], 0, None).unwrap().unwrap();
        assert_eq!(first.layers[0].pixels, first_pixels);

        let repeated = packet_v2(1, true, &[], &first_pixels);
        let mut second = read_frame_reuse(&mut &repeated[..], 1, Some(&mut first))
            .unwrap().unwrap();
        assert_eq!(second.layers[0].pixels, first_pixels);
        assert!(first.layers[0].pixels.is_empty(), "reused BGRA must be moved, not cloned");
        assert_eq!(repeated.len(), 28 + u32::from_le_bytes(repeated[16..20].try_into().unwrap()) as usize);
        let dirty = packet_v2(2, false, &new_pixels, &new_pixels);
        let third = read_frame_reuse(&mut &dirty[..], 2, Some(&mut second)).unwrap().unwrap();
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
        let mut first = read_frame_reuse(
            &mut &packet_v2(0, false, &pixels, &pixels)[..], 0, None,
        ).unwrap().unwrap();
        let different = [2, 2, 3, 255].repeat(4);
        let forged = packet_v2(1, true, &[], &different);
        assert!(read_frame_reuse(&mut &forged[..], 1, Some(&mut first)).is_err());
        for bad in [
            packet_v2(1, false, &pixels[..8], &pixels),
            { let mut v=packet_v2(1,true,&[],&pixels);v[20..28].copy_from_slice(&1u64.to_le_bytes());v },
            { let mut v=packet_v2(1,true,&[],&pixels);v[4..8].copy_from_slice(&1u32.to_le_bytes());v },
        ] {
            assert!(read_frame_reuse(&mut &bad[..], 1, Some(&mut first)).is_err());
        }
    }

    fn packet_v3(index: u64, compressed: bool, reuse: bool, raw: &[u8],
                 claim: &[u8]) -> Vec<u8> {
        let encoded = if reuse { vec![] } else if compressed {
            lz4_flex::block::compress(raw)
        } else {
            raw.to_vec()
        };
        let meta = serde_json::to_vec(&serde_json::json!({
            "schema":1,"format":"mms_canvas_bgra_layers_v1",
            "pixel_format":"BGRA8_STRAIGHT", "coordinate_origin":"top_left",
            "width":2, "height":2,"qt_canvas_rasterized_on_cpu":true,
            "production_export_changed":false,"native_gpu_alpha_composition_required":false,
            "reuse_previous_frame":[reuse],
            "layers":[{"file":"layer-0.bgra","stream_key":"base","z_order":0,
                "opacity":1,"bytes":16,
                "wire_bytes":encoded.len(),
                "compression":if reuse {"previous"} else if compressed {"lz4"} else {"raw"},
                "sha256":format!("{:x}",Sha256::digest(claim))}]
        })).unwrap();
        let mut v = Vec::from(&b"MMSB"[..]);
        v.extend(3u32.to_le_bytes());
        v.extend(index.to_le_bytes());
        v.extend((meta.len() as u32).to_le_bytes());
        v.extend((encoded.len() as u64).to_le_bytes());
        v.extend(meta);
        v.extend(encoded);
        v
    }

    #[test]
    fn v3_roundtrip_raw_lz4_and_previous_frame_same_pixels() {
        let pixels = [9, 8, 7, 255].repeat(4);
        let compressed = packet_v3(0, true, false, &pixels, &pixels);
        assert!(compressed.len() < packet_v3(0, false, false, &pixels, &pixels).len());
        let mut first = read_frame_adaptive(&mut &compressed[..], 0, None)
            .unwrap().unwrap();
        assert_eq!(first.layers[0].pixels, pixels);
        let reuse = packet_v3(1, false, true, &[], &pixels);
        let mut second = read_frame_adaptive(&mut &reuse[..], 1, Some(&mut first))
            .unwrap().unwrap();
        assert!(first.layers[0].pixels.is_empty());
        assert_eq!(second.layers[0].pixels, pixels);
        let raw = packet_v3(2, false, false, &pixels, &pixels);
        let final_frame = read_frame_adaptive(&mut &raw[..], 2, Some(&mut second))
            .unwrap().unwrap();
        assert_eq!(final_frame.layers[0].pixels, pixels);
        // Format version is explicitly negotiated, never interpreted as v2.
        assert!(read_frame_reuse(&mut &compressed[..], 0, None).is_err());
    }

    #[test]
    fn v3_fails_closed_on_truncated_corrupt_and_forged_blocks() {
        let original = [9, 8, 7, 255].repeat(4);
        let different = [9, 8, 6, 255].repeat(4);
        let good = packet_v3(0, true, false, &original, &original);
        let mut bad = packet_v3(0, true, false, &original, &different);
        assert!(read_frame_adaptive(&mut &bad[..], 0, None).is_err());
        // Invalid compressed bytes must not reach the GPU renderer.
        let metadata_len = u32::from_le_bytes(bad[16..20].try_into().unwrap()) as usize;
        bad = good.clone();
        bad[28 + metadata_len] ^= 0xff;
        assert!(read_frame_adaptive(&mut &bad[..], 0, None).is_err());
        for cut in 1..good.len() {
            assert!(read_frame_adaptive(&mut &good[..cut], 0, None).is_err(), "{cut}");
        }
        let mut bad_len = good.clone();
        bad_len[20..28].copy_from_slice(&(MAX_FRAME_BYTES as u64 * MAX_LAYERS as u64 + 1).to_le_bytes());
        assert!(read_frame_adaptive(&mut &bad_len[..], 0, None).is_err());
        let mut wrong_version = good.clone();
        wrong_version[4..8].copy_from_slice(&2u32.to_le_bytes());
        assert!(read_frame_adaptive(&mut &wrong_version[..], 0, None).is_err());
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
