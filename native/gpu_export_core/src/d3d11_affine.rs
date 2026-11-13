//! Pixel-centred inverse affine/half-open clipping on the frame pool's D3D11 device.
//! Straight-alpha BGRA in/out; bilinear filtering interpolates premultiplied values.
use std::collections::{HashMap, HashSet};
use windows::core::{s, PCSTR};
use windows::Win32::Graphics::Direct3D::{ID3DBlob, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST};
use windows::Win32::Graphics::Direct3D::Fxc::{D3DCompile, D3DCOMPILE_ENABLE_STRICTNESS};
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use crate::d3d11_frame_pool::NativeNv12FramePool;
use crate::d3d11_sprites::SpriteRenderer;
use crate::qt_canvas_packet::{CanvasPacketFrame, LayerGeometry};
use crate::resident_layer_cache::ResidentLayerCache;

/// For an integer-positioned ROI, a D3D11 GPU copy is pixel-exact (no
/// bilinear sampling, conversion through float, or rescaling). Do not apply
/// this optimization to rotating/scaling layers or to partially clipped ROIs.
fn compact_copy_origin(g: &LayerGeometry, width: u32, height: u32) -> Option<(u32, u32)> {
    let [x, y, right, bottom] = g.clip;
    if x.checked_add(g.width)? != right || y.checked_add(g.height)? != bottom
        || right > width || bottom > height
        || !(g.width < width || g.height < height)
        || g.inverse != [1., 0., 0., 1., -(x as f32), -(y as f32)] {
        return None;
    }
    Some((x, y))
}

const SHADER: &str = r#"
cbuffer Geometry : register(b0) { float4 row0; float4 row1; float4 clipBounds; float4 sourceSize; float4 color; };
Texture2D<float4> source : register(t0);
float4 vertex(uint id : SV_VertexID) : SV_Position {
    return float4(id == 2 ? 3.0 : -1.0, id == 1 ? 3.0 : -1.0, 0.0, 1.0);
}
float4 fetch(int2 p) {
    if (any(p < 0) || any(p >= int2(sourceSize.xy))) return 0;
    float4 c = source.Load(int3(p, 0));
    return float4(c.rgb * c.a, c.a);
}
float4 pixel(float4 position : SV_Position) : SV_Target {
    float2 outp = position.xy;
    if (any(outp < clipBounds.xy) || any(outp >= clipBounds.zw)) return 0;
    float radius = sourceSize.z;
    if (radius >= 0.5) {
        float2 fromEdge = min(outp - clipBounds.xy, clipBounds.zw - outp) - 0.5;
        float2 corner = max(radius - fromEdge, 0.0);
        if (dot(corner, corner) > radius * radius) return 0;
    }
    float2 p = float2(dot(row0.xyz, float3(outp,1)), dot(row1.xyz,float3(outp,1))) - 0.5;
    int2 lo = int2(floor(p)); float2 f = frac(p);
    float4 c = lerp(lerp(fetch(lo),fetch(lo+int2(1,0)),f.x),
                    lerp(fetch(lo+int2(0,1)),fetch(lo+int2(1,1)),f.x),f.y);
    float3 rgb = c.a > 0 ? c.rgb/c.a : float3(0,0,0);
    if (color.w != 1.0) {
        float gray = dot(rgb, color.xyz);
        rgb = lerp(float3(gray, gray, gray), rgb, color.w);
    }
    return float4(rgb, c.a);
}
"#;

pub(crate) fn compile(source: &str, entry: PCSTR, profile: PCSTR) -> Result<Vec<u8>, String> {
    let mut code: Option<ID3DBlob> = None;
    let mut errors: Option<ID3DBlob> = None;
    let result = unsafe { D3DCompile(source.as_ptr().cast(), source.len(), PCSTR::null(), None,
        None, entry, profile, D3DCOMPILE_ENABLE_STRICTNESS, 0, &mut code, Some(&mut errors)) };
    if let Err(error) = result {
        let detail = errors.map(|b| unsafe {
            String::from_utf8_lossy(std::slice::from_raw_parts(b.GetBufferPointer().cast(), b.GetBufferSize())).into_owned()
        }).unwrap_or_default();
        return Err(format!("D3D11 shader compile failed: {error}: {detail}"));
    }
    let blob = code.ok_or("Missing compiled D3D11 shader")?;
    Ok(unsafe { std::slice::from_raw_parts(blob.GetBufferPointer().cast(), blob.GetBufferSize()) }.to_vec())
}

pub struct AffineLayerRenderer {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    vertex: ID3D11VertexShader,
    pixel: ID3D11PixelShader,
    constants: ID3D11Buffer,
    rasterizer: ID3D11RasterizerState,
    sprites: SpriteRenderer,
    outputs: HashMap<String, (ID3D11Texture2D, ID3D11RenderTargetView)>,
    width: u32,
    height: u32,
    pub draws: u64,
    pub exact_roi_copies: u64,
    pub particle_layers_drawn: u64,
    pub radial_layers_drawn: u64,
    pub particle_sprites_drawn: u64,
    pub text_quads_drawn: u64,
    pub text_layers_drawn: u64,
    pub allocations: u64,
    pub releases: u64,
    pub peak_output_bytes: usize,
}
impl AffineLayerRenderer {
    pub fn new(pool: &NativeNv12FramePool) -> Result<Self, String> {
        let device = pool.native_device().clone();
        let context = pool.native_context().clone();
        let mut vertex = None; let mut pixel = None; let mut constants = None; let mut rasterizer = None;
        unsafe {
            device.CreateVertexShader(&compile(SHADER,s!("vertex"),s!("vs_5_0"))?, None, Some(&mut vertex))
                .map_err(|e| format!("Affine vertex shader: {e}"))?;
            device.CreatePixelShader(&compile(SHADER,s!("pixel"),s!("ps_5_0"))?, None, Some(&mut pixel))
                .map_err(|e| format!("Affine pixel shader: {e}"))?;
            device.CreateBuffer(&D3D11_BUFFER_DESC { ByteWidth:80, Usage:D3D11_USAGE_DEFAULT,
                BindFlags:D3D11_BIND_CONSTANT_BUFFER.0 as u32, ..Default::default() }, None, Some(&mut constants))
                .map_err(|e| format!("Affine constant buffer: {e}"))?;
            device.CreateRasterizerState(&D3D11_RASTERIZER_DESC { FillMode:D3D11_FILL_SOLID,
                CullMode:D3D11_CULL_NONE, DepthClipEnable:true.into(), ..Default::default() }, Some(&mut rasterizer))
                .map_err(|e| format!("Affine rasterizer: {e}"))?;
        }
        let (width,height) = pool.dimensions();
        Ok(Self { device,context,vertex:vertex.ok_or("Missing vertex shader")?,pixel:pixel.ok_or("Missing pixel shader")?,
            constants:constants.ok_or("Missing constants")?,rasterizer:rasterizer.ok_or("Missing rasterizer")?,
            sprites: SpriteRenderer::new(pool)?, particle_layers_drawn:0, particle_sprites_drawn:0, radial_layers_drawn:0,
            text_layers_drawn:0, text_quads_drawn:0,
            outputs:HashMap::new(),width,height,draws:0,exact_roi_copies:0,allocations:0,releases:0,peak_output_bytes:0 })
    }
    pub fn create_input(&self, width:u32, height:u32) -> Result<ID3D11Texture2D,String> {
        if width == 0 || height == 0 || width > 3840 || height > 2160 { return Err("Affine input size out of bounds".into()); }
        let desc = D3D11_TEXTURE2D_DESC { Width:width,Height:height,MipLevels:1,ArraySize:1,
            Format:DXGI_FORMAT_B8G8R8A8_UNORM, SampleDesc:DXGI_SAMPLE_DESC {Count:1,Quality:0},
            Usage:D3D11_USAGE_DEFAULT,BindFlags:(D3D11_BIND_RENDER_TARGET|D3D11_BIND_SHADER_RESOURCE).0 as u32,
            ..Default::default() };
        let mut texture=None;
        unsafe { self.device.CreateTexture2D(&desc,None,Some(&mut texture)) }.map_err(|e|format!("Affine texture: {e}"))?;
        texture.ok_or("Missing affine texture".into())
    }
    pub fn upload(&self, texture:&ID3D11Texture2D, pixels:&[u8], width:u32) -> Result<(),String> {
        let mut desc=D3D11_TEXTURE2D_DESC::default(); unsafe { texture.GetDesc(&mut desc) };
        if width != desc.Width || pixels.len() != width as usize * desc.Height as usize * 4 {
            return Err("Affine upload source size mismatch".into());
        }
        unsafe { self.context.UpdateSubresource(texture,0,None,pixels.as_ptr().cast(),width*4,0) };
        Ok(())
    }
    pub fn create_output(&self) -> Result<(ID3D11Texture2D, ID3D11RenderTargetView), String> {
        let texture = self.create_input(self.width, self.height)?;
        let mut view = None;
        unsafe { self.device.CreateRenderTargetView(&texture, None, Some(&mut view)) }
            .map_err(|e| format!("Canvas output view: {e}"))?;
        Ok((texture, view.ok_or("Missing Canvas output view")?))
    }
    pub fn clear_output(&self, view: &ID3D11RenderTargetView) {
        unsafe { self.context.ClearRenderTargetView(view, &[0.; 4]) };
    }
    pub fn copy_input(&self, target: &ID3D11Texture2D, source: &ID3D11Texture2D, x: u32, y: u32) {
        unsafe { self.context.CopySubresourceRegion(target, 0, x, y, 0, source, 0, None) };
    }
    pub fn place_video(
        &self, target: &ID3D11Texture2D, view: &ID3D11RenderTargetView, source: &ID3D11Texture2D,
        clip: [u32; 4], request: crate::qt_canvas_packet::VideoRequest, luma: [f32; 3],
    ) -> Result<(), String> {
        let [x, y, r, b] = clip;
        if !request.flip_horizontal && !request.flip_vertical && request.border_radius < 0.5
            && request.saturation == 1.0 {
            self.copy_input(target, source, x, y);
            return Ok(());
        }
        let (sx, dx) = if request.flip_horizontal { (-1., r as f32) } else { (1., -(x as f32)) };
        let (sy, dy) = if request.flip_vertical { (-1., b as f32) } else { (1., -(y as f32)) };
        self.draw(source, view, &LayerGeometry {
            width: r - x, height: b - y, inverse: [sx, 0., 0., sy, dx, dy], clip,
        }, request.border_radius, [luma[0], luma[1], luma[2], request.saturation])
    }
    pub fn prepare(&mut self, frame:&CanvasPacketFrame, resident:&ResidentLayerCache<ID3D11Texture2D>) -> Result<(),String> {
        let keys:HashSet<_> = frame.layers.iter().filter(|l|l.video.is_none() && !l.uses_resident_texture(self.width,self.height))
            .map(|l|l.stream_key.as_str()).collect();
        let before=self.outputs.len();
        self.outputs.retain(|k,_|keys.contains(k.as_str()));
        self.releases+=(before-self.outputs.len()) as u64;
        for layer in &frame.layers {
            if layer.video.is_some() || layer.uses_resident_texture(self.width,self.height) { continue; }
            if !self.outputs.contains_key(&layer.stream_key) {
                let output = self.create_output()?;
                self.outputs.insert(layer.stream_key.clone(), output);
                self.allocations+=1;
                self.peak_output_bytes=self.peak_output_bytes.max(self.outputs.len()*self.width as usize*self.height as usize*4);
            }
            let source=resident.resource(&layer.stream_key)?;
            let (output, view) = &self.outputs[&layer.stream_key];
            if layer.kind().is_some() {
                unsafe { self.context.RSSetState(&self.rasterizer) };
                self.sprites.draw(source, view, layer, &self.vertex)?;
                if layer.particle_sprites {
                    self.particle_layers_drawn += 1;
                    self.particle_sprites_drawn += u64::from(layer.geometry.height);
                } else if layer.radial_segments {
                    self.radial_layers_drawn += 1;
                } else {
                    self.text_layers_drawn += 1;
                    self.text_quads_drawn += layer.text_quads.len() as u64;
                }
            } else if let Some((x, y)) = compact_copy_origin(&layer.geometry, self.width, self.height) {
                // Clearing the destination, then copying the source at its
                // exact integer pixel offset, matches a transparent Qt image
                // with the original cropped bytes pasted without scaling.
                // All operations stay ordered on the same immediate context.
                self.clear_output(view);
                self.copy_input(output, source, x, y);
                self.exact_roi_copies += 1;
            } else {
                self.draw(source,view,&layer.geometry, 0.0, [0.0, 0.0, 0.0, 1.0])?;
                self.draws+=1;
            }
        }
        Ok(())
    }
    pub fn clear(&mut self) { self.releases+=self.outputs.len() as u64; self.outputs.clear(); }
    pub fn output(&self,key:&str) -> Result<&ID3D11Texture2D,String> {
        self.outputs.get(key).map(|(t,_)|t).ok_or("Missing transformed resident output".into())
    }
    fn draw(&self,source:&ID3D11Texture2D,target:&ID3D11RenderTargetView,g:&LayerGeometry, radius:f32, color:[f32;4]) -> Result<(),String> {
        let mut srv=None;
        unsafe { self.device.CreateShaderResourceView(source,None,Some(&mut srv)) }.map_err(|e|format!("Affine input view: {e}"))?;
        let m=g.inverse; let c=g.clip;
        let data:[f32;20]=[m[0],m[2],m[4],0.,m[1],m[3],m[5],0.,c[0] as f32,c[1] as f32,c[2] as f32,c[3] as f32,g.width as f32,g.height as f32,radius,0.,color[0],color[1],color[2],color[3]];
        unsafe {
            self.context.UpdateSubresource(&self.constants,0,None,data.as_ptr().cast(),0,0);
            self.context.IASetInputLayout(None); self.context.IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            self.context.VSSetShader(&self.vertex,None); self.context.PSSetShader(&self.pixel,None);
            self.context.PSSetConstantBuffers(0,Some(&[Some(self.constants.clone())]));
            self.context.PSSetShaderResources(0,Some(&[srv]));
            self.context.RSSetState(&self.rasterizer);
            self.context.RSSetViewports(Some(&[D3D11_VIEWPORT {TopLeftX:0.,TopLeftY:0.,Width:self.width as f32,Height:self.height as f32,MinDepth:0.,MaxDepth:1.}]));
            self.context.OMSetRenderTargets(Some(&[Some(target.clone())]),None);
            self.context.OMSetBlendState(None,None,u32::MAX);
            self.context.Draw(3,0);
            self.context.PSSetShaderResources(0,Some(&[None]));
            self.context.OMSetRenderTargets(None,None);
        }
        Ok(())
    }
}

#[cfg(test)]
mod compact_roi_tests {
    use super::compact_copy_origin;
    use crate::qt_canvas_packet::LayerGeometry;

    #[test]
    fn only_exact_integer_crop_with_matching_clip_uses_gpu_copy() {
        let mut g = LayerGeometry::identity(37, 15);
        g.inverse = [1., 0., 0., 1., -12., -24.];
        g.clip = [12, 24, 49, 39];
        assert_eq!(compact_copy_origin(&g, 128, 80), Some((12, 24)));
        let mut altered = g;
        altered.inverse[4] = -12.5;
        assert_eq!(compact_copy_origin(&altered, 128, 80), None);
        altered = g;
        altered.clip[2] += 1;
        assert_eq!(compact_copy_origin(&altered, 128, 80), None);
        altered = g;
        altered.inverse[0] = 1.01;
        assert_eq!(compact_copy_origin(&altered, 128, 80), None);
        altered = g;
        altered.clip = [120, 24, 157, 39];
        assert_eq!(compact_copy_origin(&altered, 128, 80), None);
        altered = g;
        altered.clip = [12, 24, 49, 39];
        altered.inverse[5] = -23.;
        assert_eq!(compact_copy_origin(&altered, 128, 80), None);
    }
}
