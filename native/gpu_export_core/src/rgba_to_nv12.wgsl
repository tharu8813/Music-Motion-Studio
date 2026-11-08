// Experimental *GPU-resident* RGBA8 -> packed NV12 converter.
// Explicit integer BT.709 limited-range approximation, NOT FFmpeg byte parity.
// Four horizontal Y samples are packed into one u32; on even rows, two
// 2x2 chroma groups become one interleaved U0,V0,U1,V1 u32.
// Width must be divisible by 4 and height by 2; no row padding required.

struct Size {
    width: u32,
    height: u32,
    reserved0: u32,
    reserved1: u32,
}

@group(0) @binding(0) var<storage, read> rgba: array<u32>;
@group(0) @binding(1) var<storage, read_write> nv12: array<u32>;
@group(0) @binding(2) var<uniform> size: Size;

fn red(p: u32) -> u32 { return p & 255u; }
fn green(p: u32) -> u32 { return (p >> 8u) & 255u; }
fn blue(p: u32) -> u32 { return (p >> 16u) & 255u; }

fn luma(p: u32) -> u32 {
    return clamp(16u + ((47u * red(p) + 157u * green(p) +
                         16u * blue(p) + 128u) >> 8u), 16u, 235u);
}

fn chroma_u(r: u32, g: u32, b: u32) -> u32 {
    let value = 128 + ((-26 * i32(r) - 87 * i32(g) +
                          113 * i32(b) + 128) / 256);
    return u32(clamp(value, 16, 240));
}

fn chroma_v(r: u32, g: u32, b: u32) -> u32 {
    let value = 128 + ((113 * i32(r) - 102 * i32(g) -
                         11 * i32(b) + 128) / 256);
    return u32(clamp(value, 16, 240));
}

fn chroma_pair(x: u32, y: u32) -> vec2<u32> {
    let first = y * size.width + x;
    let second = first + size.width;
    let p00 = rgba[first];
    let p01 = rgba[first + 1u];
    let p10 = rgba[second];
    let p11 = rgba[second + 1u];
    let r = (red(p00) + red(p01) + red(p10) + red(p11) + 2u) / 4u;
    let g = (green(p00) + green(p01) + green(p10) + green(p11) + 2u) / 4u;
    let b = (blue(p00) + blue(p01) + blue(p10) + blue(p11) + 2u) / 4u;
    return vec2<u32>(chroma_u(r, g, b), chroma_v(r, g, b));
}

@compute @workgroup_size(16, 8, 1)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = id.x * 4u;
    let y = id.y;
    if (x >= size.width || y >= size.height) {
        return;
    }

    let rgba_idx = y * size.width + x;
    let y0 = luma(rgba[rgba_idx]);
    let y1 = luma(rgba[rgba_idx + 1u]);
    let y2 = luma(rgba[rgba_idx + 2u]);
    let y3 = luma(rgba[rgba_idx + 3u]);
    nv12[rgba_idx / 4u] = y0 | (y1 << 8u) | (y2 << 16u) | (y3 << 24u);

    if ((y & 1u) == 0u) {
        let c0 = chroma_pair(x, y);
        let c1 = chroma_pair(x + 2u, y);
        let chroma_base = (size.width * size.height) / 4u;
        let chroma_index = (y / 2u) * (size.width / 4u) + id.x;
        nv12[chroma_base + chroma_index] = c0.x | (c0.y << 8u) |
                                          (c1.x << 16u) | (c1.y << 24u);
    }
}
