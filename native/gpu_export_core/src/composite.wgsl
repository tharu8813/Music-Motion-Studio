// RGBA8 values are packed into u32 little-endian. Keeping alpha composition
// in integer arithmetic gives an exact byte-level reference for supported
// opaque-base, straight-alpha overlays, independent of shader FP rounding.
//
// This first milestone intentionally consumes RGBA8 CPU buffers for upload.
// Frames remain GPU-resident across all overlay passes and are read back only
// once for validation. Native NVENC shared-hardware frames are future work.

struct Geometry {
  canvas_width: u32,
  canvas_height: u32,
  overlay_width: u32,
  overlay_height: u32,
  offset_x: i32,
  offset_y: i32,
  reserved0: u32,
  reserved1: u32,
}

@group(0) @binding(0) var<storage, read> base_pixels: array<u32>;
@group(0) @binding(1) var<storage, read> overlay_pixels: array<u32>;
@group(0) @binding(2) var<storage, read_write> destination: array<u32>;
@group(0) @binding(3) var<uniform> bounds: Geometry;

fn channel(pixel: u32, shift: u32) -> u32 {
    return (pixel >> shift) & 255u;
}

fn composite_pixel(bottom: u32, top: u32) -> u32 {
    let alpha = channel(top, 24u);
    if (alpha == 0u) {
        return bottom;
    }
    if (alpha == 255u) {
        return top | 0xff000000u;
    }
    let inverse = 255u - alpha;
    let red = (channel(top, 0u) * alpha + channel(bottom, 0u) * inverse + 127u) / 255u;
    let green = (channel(top, 8u) * alpha + channel(bottom, 8u) * inverse + 127u) / 255u;
    let blue = (channel(top, 16u) * alpha + channel(bottom, 16u) * inverse + 127u) / 255u;
    return red | (green << 8u) | (blue << 16u) | 0xff000000u;
}

@compute @workgroup_size(16, 16, 1)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if (id.x >= bounds.canvas_width || id.y >= bounds.canvas_height) {
        return;
    }
    let index = id.y * bounds.canvas_width + id.x;
    var result = base_pixels[index];
    let overlay_x = i32(id.x) - bounds.offset_x;
    let overlay_y = i32(id.y) - bounds.offset_y;
    if (overlay_x >= 0 && overlay_y >= 0 &&
        overlay_x < i32(bounds.overlay_width) &&
        overlay_y < i32(bounds.overlay_height)) {
        let index_overlay = u32(overlay_y) * bounds.overlay_width + u32(overlay_x);
        result = composite_pixel(result, overlay_pixels[index_overlay]);
    }
    destination[index] = result;
}
