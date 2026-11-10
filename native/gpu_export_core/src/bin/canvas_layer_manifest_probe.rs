//! Portable, GPU-independent validation for Qt Canvas straight-BGRA packets.
//!
//! Run before the D3D11/NVENC encoder: GitHub's hosted GPU environment
//! often lacks NVIDIA support, but malformed packets MUST still fail CI.
//!
//! cargo run --manifest-path native/gpu_export_core/Cargo.toml
//!   --bin canvas_layer_manifest_probe -- --manifest fixture/frame.json
//!   --output canvas-manifest-validation.json

use music_motion_gpu_core::canvas_layer_manifest::load_canvas_frame_packet;
use serde_json::{json, Value};
use std::env;
use std::fs;
use std::path::PathBuf;

fn main() {
    let mut manifest: Option<PathBuf> = None;
    let mut output: Option<PathBuf> = None;
    let mut arguments = env::args().skip(1);
    while let Some(flag) = arguments.next() {
        let value = arguments.next().unwrap_or_else(|| {
            eprintln!("Missing value for {flag}");
            std::process::exit(2);
        });
        match flag.as_str() {
            "--manifest" => manifest = Some(value.into()),
            "--output" => output = Some(value.into()),
            _ => {
                eprintln!("Unknown Canvas packet validator option {flag}");
                std::process::exit(2);
            }
        }
    }
    let manifest = manifest.unwrap_or_else(|| {
        eprintln!("Required: --manifest path/to/qt-frame.json");
        std::process::exit(2);
    });
    let report: Value = match load_canvas_frame_packet(&manifest) {
        Ok(packet) => json!({
            "schema": 1,
            "status": "verified",
            "manifest": manifest.display().to_string(),
            "resolution": [packet.width, packet.height],
            "layers_verified": packet.layers.len(),
            "stream_keys": packet.layers.iter()
                .map(|layer| layer.stream_key.as_str()).collect::<Vec<_>>(),
            "total_bgra_bytes": packet.layers.iter()
                .map(|layer| layer.bgra.len()).sum::<usize>(),
            "sha256_and_directory_checks_passed": true,
            "pixel_format": "BGRA8_STRAIGHT",
            "qt_canvas_painted_on_gpu": false,
            "nvidia_hardware_required": false,
            "production_export_changed": false
        }),
        Err(reason) => json!({
            "schema": 1,
            "status": "canvas_packet_validation_failed",
            "reason": reason,
            "sha256_and_directory_checks_passed": false,
            "production_export_changed": false
        }),
    };
    let serialized = serde_json::to_string_pretty(&report)
        .expect("Serialize Qt Canvas manifest validation");
    println!("{serialized}");
    if let Some(path) = output {
        if let Some(parent) = path.parent().filter(|parent| !parent.as_os_str().is_empty()) {
            fs::create_dir_all(parent).expect("Create diagnostic report directory");
        }
        fs::write(path, serialized + "\n").expect("Write Qt Canvas packet report");
    }
    if report["status"] != "verified" {
        std::process::exit(1);
    }
}
