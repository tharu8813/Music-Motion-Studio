//! Draw ordered particle and text sprites into a shared GPU-only scratch surface.
use crate::d3d11_affine::compile;
use crate::d3d11_frame_pool::NativeNv12FramePool;
use crate::qt_canvas_packet::CanvasPacketLayer;
use windows::core::s;
use windows::Win32::Graphics::Direct3D::D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST;
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_SAMPLE_DESC};

const SHADER: &str = r#"
cbuffer Canvas : register(b0) { float4 frame; float4 clipBounds; };
Texture2D<float4> source : register(t0);

uint readWord(uint column, uint row) {
    // Each BGRA texel transports one little-endian 32-bit sprite attribute.
    uint4 bytes = (uint4)round(source.Load(int3(column, row, 0)).bgra * 255.0);
    return bytes.x | (bytes.y << 8) | (bytes.z << 16) | (bytes.w << 24);
}

struct Sprite {
    float4 position : SV_Position;
    nointerpolation float3 circle : TEXCOORD0;
    nointerpolation float4 color : COLOR0;
};

Sprite vertex(uint id : SV_VertexID, uint instance : SV_InstanceID) {
    static const float2 corners[6] = {
        float2(-1, -1), float2(1, -1), float2(-1, 1),
        float2(-1, 1), float2(1, -1), float2(1, 1)
    };
    Sprite v;
    v.circle = float3(asfloat(readWord(0, instance)), asfloat(readWord(1, instance)),
                      asfloat(readWord(2, instance)));
    uint color = readWord(3, instance);
    v.color = (float4)((color >> uint4(16, 8, 0, 24)) & 255) / 255.0;
    float2 p = v.circle.xy + corners[id] * (v.circle.z + 0.75);
    v.position = float4(p / frame.xy * float2(2, -2) + float2(-1, 1), 0, 1);
    return v;
}

struct Segment {
    float4 position : SV_Position;
    nointerpolation float4 ends : TEXCOORD0;
    nointerpolation float radius : TEXCOORD1;
    nointerpolation float4 color : COLOR0;
};

Segment segmentVertex(uint id : SV_VertexID, uint instance : SV_InstanceID) {
    static const float2 corners[6] = {
        float2(0, 0), float2(1, 0), float2(0, 1),
        float2(0, 1), float2(1, 0), float2(1, 1)
    };
    Segment v;
    v.ends = float4(asfloat(readWord(0, instance)), asfloat(readWord(1, instance)),
                    asfloat(readWord(2, instance)), asfloat(readWord(3, instance)));
    v.radius = asfloat(readWord(4, instance));
    uint color = readWord(5, instance);
    v.color = (float4)((color >> uint4(16, 8, 0, 24)) & 255) / 255.0;
    float pad = v.radius + 0.75;
    float2 p = lerp(min(v.ends.xy, v.ends.zw) - pad,
                    max(v.ends.xy, v.ends.zw) + pad, corners[id]);
    v.position = float4(p / frame.xy * float2(2, -2) + float2(-1, 1), 0, 1);
    return v;
}

float4 segmentCoverage(Segment v) : SV_Target {
    float2 p = v.position.xy;
    if (any(p < clipBounds.xy) || any(p >= clipBounds.zw)) discard;
    float2 axis = v.ends.zw - v.ends.xy;
    float inverseLengthSquared = rcp(max(dot(axis, axis), 0.000001));
    float radiusSquared = v.radius * v.radius;
    float covered = 0;
    [unroll] for (int y = 0; y < 4; ++y) {
        [unroll] for (int x = 0; x < 4; ++x) {
            float2 offset = p + (float2(x, y) - 1.5) * 0.25 - v.ends.xy;
            float2 d = offset - axis * saturate(dot(offset, axis) * inverseLengthSquared);
            covered += dot(d, d) <= radiusSquared ? 1 : 0;
        }
    }
    float alpha = v.color.a * covered / 16.0;
    return float4(v.color.rgb * alpha, alpha);
}

struct GlyphPlacement {
    float4 atlasRect;
    float4 targetRect;
    float4 glyphClip;
    float4 glyphStyle;
};
cbuffer GlyphBatch : register(b1) { GlyphPlacement glyphs[64]; };

struct Glyph {
    float4 position : SV_Position;
    float2 uv : TEXCOORD0;
    nointerpolation uint instance : TEXCOORD1;
};

Glyph glyphVertex(uint id : SV_VertexID, uint instance : SV_InstanceID) {
    static const float2 corners[6] = {
        float2(0, 0), float2(1, 0), float2(0, 1),
        float2(0, 1), float2(1, 0), float2(1, 1)
    };
    Glyph v;
    GlyphPlacement glyph = glyphs[instance];
    float2 pixelPosition = lerp(glyph.targetRect.xy, glyph.targetRect.zw, corners[id]);
    v.position = float4(pixelPosition / frame.xy * float2(2, -2) + float2(-1, 1), 0, 1);
    v.uv = lerp(glyph.atlasRect.xy, glyph.atlasRect.zw, corners[id]);
    v.instance = instance;
    return v;
}

float4 glyphTexel(int2 p, float4 atlasRect) {
    if (any(p < int2(atlasRect.xy)) || any(p >= int2(atlasRect.zw))) return 0;
    return source.Load(int3(p, 0));
}

float4 glyphPixel(Glyph v) : SV_Target {
    GlyphPlacement glyph = glyphs[v.instance];
    if (any(v.position.xy < glyph.glyphClip.xy) || any(v.position.xy >= glyph.glyphClip.zw)) discard;
    float2 uv = v.uv - 0.5;
    int2 p = int2(floor(uv));
    float2 f = frac(uv);
    float4 a = lerp(glyphTexel(p, glyph.atlasRect), glyphTexel(p + int2(1, 0), glyph.atlasRect), f.x);
    float4 b = lerp(glyphTexel(p + int2(0, 1), glyph.atlasRect),
                    glyphTexel(p + int2(1, 1), glyph.atlasRect), f.x);
    return lerp(a, b, f.y) * glyph.glyphStyle.x;
}

float4 coverage(Sprite v) : SV_Target {
    float2 p = v.position.xy;
    if (any(p < clipBounds.xy) || any(p >= clipBounds.zw)) discard;
    float2 delta = p - v.circle.xy;
    float radiusSquared = v.circle.z * v.circle.z;
    float covered = 16;
    float2 farthest = abs(delta) + 0.375;
    if (dot(farthest, farthest) > radiusSquared) {
        float2 nearest = max(abs(delta) - 0.375, 0);
        if (dot(nearest, nearest) > radiusSquared) discard;
        covered = 0;
        [unroll] for (int y = 0; y < 4; ++y) {
            [unroll] for (int x = 0; x < 4; ++x) {
                float2 sampleDelta = delta + (float2(x, y) - 1.5) * 0.25;
                covered += dot(sampleDelta, sampleDelta) <= radiusSquared ? 1 : 0;
            }
        }
    }
    float alpha = v.color.a * covered / 16.0;
    return float4(v.color.rgb * alpha, alpha);
}

float4 straight(float4 position : SV_Position) : SV_Target {
    float4 c = source.Load(int3(position.xy, 0));
    return float4(c.a > 0 ? c.rgb / c.a : float3(0, 0, 0), c.a * frame.z);
}
"#;

pub(crate) struct SpriteRenderer {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    vertex: ID3D11VertexShader,
    coverage: ID3D11PixelShader,
    segment_vertex: ID3D11VertexShader,
    segment_coverage: ID3D11PixelShader,
    straight: ID3D11PixelShader,
    blend: ID3D11BlendState,
    constants: ID3D11Buffer,
    glyph_vertex: ID3D11VertexShader,
    glyph_pixel: ID3D11PixelShader,
    glyph_constants: ID3D11Buffer,
    scratch: Option<(ID3D11RenderTargetView, ID3D11ShaderResourceView)>,
    width: u32,
    height: u32,
}

impl SpriteRenderer {
    pub(crate) fn new(pool: &NativeNv12FramePool) -> Result<Self, String> {
        let device = pool.native_device().clone();
        let context = pool.native_context().clone();
        let (width, height) = pool.dimensions();
        let (mut vertex, mut coverage, mut straight) = (None, None, None);
        let (mut segment_vertex, mut segment_coverage) = (None, None);
        let (mut constants, mut blend) = (None, None);
        let (mut glyph_vertex, mut glyph_pixel, mut glyph_constants) = (None, None, None);
        let mut blend_desc = D3D11_BLEND_DESC::default();
        blend_desc.RenderTarget[0] = D3D11_RENDER_TARGET_BLEND_DESC {
            BlendEnable: true.into(),
            SrcBlend: D3D11_BLEND_ONE,
            DestBlend: D3D11_BLEND_INV_SRC_ALPHA,
            BlendOp: D3D11_BLEND_OP_ADD,
            SrcBlendAlpha: D3D11_BLEND_ONE,
            DestBlendAlpha: D3D11_BLEND_INV_SRC_ALPHA,
            BlendOpAlpha: D3D11_BLEND_OP_ADD,
            RenderTargetWriteMask: D3D11_COLOR_WRITE_ENABLE_ALL.0 as u8,
        };
        unsafe {
            device.CreateVertexShader(&compile(SHADER, s!("vertex"), s!("vs_5_0"))?, None, Some(&mut vertex))
                .map_err(|e| format!("Particle vertex shader: {e}"))?;
            device.CreatePixelShader(&compile(SHADER, s!("coverage"), s!("ps_5_0"))?, None, Some(&mut coverage))
                .map_err(|e| format!("Particle coverage shader: {e}"))?;
            device.CreateVertexShader(&compile(SHADER, s!("segmentVertex"), s!("vs_5_0"))?, None, Some(&mut segment_vertex))
                .map_err(|e| format!("Radial bar vertex shader: {e}"))?;
            device.CreatePixelShader(&compile(SHADER, s!("segmentCoverage"), s!("ps_5_0"))?, None, Some(&mut segment_coverage))
                .map_err(|e| format!("Radial bar coverage shader: {e}"))?;
            device.CreatePixelShader(&compile(SHADER, s!("straight"), s!("ps_5_0"))?, None, Some(&mut straight))
                .map_err(|e| format!("Particle alpha conversion shader: {e}"))?;
            device.CreateBuffer(
                &D3D11_BUFFER_DESC {
                    ByteWidth: 32,
                    Usage: D3D11_USAGE_DEFAULT,
                    BindFlags: D3D11_BIND_CONSTANT_BUFFER.0 as u32,
                    ..Default::default()
                },
                None,
                Some(&mut constants),
            ).map_err(|e| format!("Particle constant buffer: {e}"))?;
            device.CreateBlendState(&blend_desc, Some(&mut blend))
                .map_err(|e| format!("Particle blend state: {e}"))?;
            device.CreateVertexShader(&compile(SHADER, s!("glyphVertex"), s!("vs_5_0"))?, None, Some(&mut glyph_vertex))
                .map_err(|e| format!("Text vertex shader: {e}"))?;
            device.CreatePixelShader(&compile(SHADER, s!("glyphPixel"), s!("ps_5_0"))?, None, Some(&mut glyph_pixel))
                .map_err(|e| format!("Text pixel shader: {e}"))?;
            device.CreateBuffer(
                &D3D11_BUFFER_DESC {
                    ByteWidth: 64 * crate::qt_canvas_packet::MAX_TEXT_QUADS as u32,
                    Usage: D3D11_USAGE_DEFAULT,
                    BindFlags: D3D11_BIND_CONSTANT_BUFFER.0 as u32,
                    ..Default::default()
                },
                None,
                Some(&mut glyph_constants),
            ).map_err(|e| format!("Text constant buffer: {e}"))?;
        }
        Ok(Self {
            device, context, width, height, scratch: None,
            vertex: vertex.ok_or("Missing particle vertex shader")?,
            coverage: coverage.ok_or("Missing particle coverage shader")?,
            segment_vertex: segment_vertex.ok_or("Missing radial bar vertex shader")?,
            segment_coverage: segment_coverage.ok_or("Missing radial bar coverage shader")?,
            straight: straight.ok_or("Missing particle alpha conversion shader")?,
            constants: constants.ok_or("Missing particle constants")?,
            blend: blend.ok_or("Missing particle blend state")?,
            glyph_vertex: glyph_vertex.ok_or("Missing text vertex shader")?,
            glyph_pixel: glyph_pixel.ok_or("Missing text pixel shader")?,
            glyph_constants: glyph_constants.ok_or("Missing text constants")?,
        })
    }

    fn create_scratch(&self) -> Result<(ID3D11RenderTargetView, ID3D11ShaderResourceView), String> {
        let desc = D3D11_TEXTURE2D_DESC {
            Width: self.width,
            Height: self.height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_R8G8B8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: (D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE).0 as u32,
            ..Default::default()
        };
        let (mut texture, mut target, mut view) = (None, None, None);
        unsafe { self.device.CreateTexture2D(&desc, None, Some(&mut texture)) }
            .map_err(|e| format!("Particle scratch texture: {e}"))?;
        let texture = texture.ok_or("Missing particle scratch texture")?;
        unsafe {
            self.device.CreateRenderTargetView(&texture, None, Some(&mut target))
                .map_err(|e| format!("Particle scratch render target: {e}"))?;
            self.device.CreateShaderResourceView(&texture, None, Some(&mut view))
                .map_err(|e| format!("Particle scratch view: {e}"))?;
        }
        Ok((target.ok_or("Missing particle scratch render target")?,
            view.ok_or("Missing particle scratch view")?))
    }

    pub(crate) fn draw(
        &mut self,
        input: &ID3D11Texture2D,
        target: &ID3D11RenderTargetView,
        layer: &CanvasPacketLayer,
        fullscreen: &ID3D11VertexShader,
    ) -> Result<(), String> {
        if self.scratch.is_none() {
            self.scratch = Some(self.create_scratch()?);
        }
        let (scratch, scratch_view) = self.scratch.as_ref().ok_or("Missing particle scratch views")?;
        let mut input_view = None;
        unsafe { self.device.CreateShaderResourceView(input, None, Some(&mut input_view)) }
            .map_err(|e| format!("Sprite input view: {e}"))?;
        let [left, top, right, bottom] = layer.geometry.clip.map(|value| value as f32);
        let data = [self.width as f32, self.height as f32, layer.opacity, 0.0, left, top, right, bottom];
        let viewport = D3D11_VIEWPORT {
            Width: self.width as f32,
            Height: self.height as f32,
            MaxDepth: 1.0,
            ..Default::default()
        };
        unsafe {
            self.context.UpdateSubresource(&self.constants, 0, None, data.as_ptr().cast(), 0, 0);
            self.context.IASetInputLayout(None);
            self.context.IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            self.context.RSSetViewports(Some(&[viewport]));
            self.context.VSSetConstantBuffers(0, Some(&[Some(self.constants.clone())]));
            self.context.PSSetConstantBuffers(0, Some(&[Some(self.constants.clone())]));
            self.context.ClearRenderTargetView(scratch, &[0.0; 4]);
            self.context.OMSetRenderTargets(Some(&[Some(scratch.clone())]), None);
            self.context.OMSetBlendState(&self.blend, None, u32::MAX);
            if layer.particle_sprites || layer.radial_segments {
                let (vertex, coverage) = if layer.radial_segments {
                    (&self.segment_vertex, &self.segment_coverage)
                } else {
                    (&self.vertex, &self.coverage)
                };
                self.context.VSSetShaderResources(0, Some(&[input_view]));
                self.context.VSSetShader(vertex, None);
                self.context.PSSetShader(coverage, None);
                self.context.DrawInstanced(6, layer.geometry.height, 0, 0);
                self.context.VSSetShaderResources(0, Some(&[None]));
            } else {
                self.context.VSSetShader(&self.glyph_vertex, None);
                self.context.PSSetShader(&self.glyph_pixel, None);
                self.context.VSSetConstantBuffers(1, Some(&[Some(self.glyph_constants.clone())]));
                self.context.PSSetConstantBuffers(1, Some(&[Some(self.glyph_constants.clone())]));
                self.context.PSSetShaderResources(0, Some(&[input_view]));
                let mut batch = [[0.0f32; 16]; crate::qt_canvas_packet::MAX_TEXT_QUADS];
                for (data, quad) in batch.iter_mut().zip(&layer.text_quads) {
                    data[..4].copy_from_slice(&quad.source);
                    data[4..8].copy_from_slice(&quad.target);
                    data[8..12].copy_from_slice(&quad.clip);
                    data[12] = quad.opacity;
                }
                self.context.UpdateSubresource(&self.glyph_constants, 0, None, batch.as_ptr().cast(), 0, 0);
                self.context.DrawInstanced(6, layer.text_quads.len() as u32, 0, 0);
            }

            self.context.OMSetRenderTargets(Some(&[Some(target.clone())]), None);
            self.context.OMSetBlendState(None, None, u32::MAX);
            self.context.VSSetShader(fullscreen, None);
            self.context.PSSetShader(&self.straight, None);
            self.context.PSSetShaderResources(0, Some(&[Some(scratch_view.clone())]));
            self.context.Draw(3, 0);
            self.context.PSSetShaderResources(0, Some(&[None]));
            self.context.OMSetRenderTargets(None, None);
        }
        Ok(())
    }
}
