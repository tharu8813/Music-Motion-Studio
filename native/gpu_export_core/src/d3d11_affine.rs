//! Pixel-centred inverse affine/half-open clipping on the frame pool's D3D11 device.
//! Straight-alpha BGRA in/out; bilinear filtering interpolates premultiplied values.
use std::collections::{HashMap, HashSet};
use windows::core::{s, PCSTR};
use windows::Win32::Graphics::Direct3D::{ID3DBlob, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST};
use windows::Win32::Graphics::Direct3D::Fxc::{D3DCompile, D3DCOMPILE_ENABLE_STRICTNESS};
use windows::Win32::Graphics::Direct3D11::*;
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use crate::d3d11_frame_pool::NativeNv12FramePool;
use crate::qt_canvas_packet::{CanvasPacketFrame, LayerGeometry};
use crate::resident_layer_cache::ResidentLayerCache;

const SHADER: &str = r#"
cbuffer Geometry : register(b0) { float4 row0; float4 row1; float4 clipBounds; float4 sourceSize; };
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
    float2 p = float2(dot(row0.xyz, float3(outp,1)), dot(row1.xyz,float3(outp,1))) - 0.5;
    int2 lo = int2(floor(p)); float2 f = frac(p);
    float4 c = lerp(lerp(fetch(lo),fetch(lo+int2(1,0)),f.x),
                    lerp(fetch(lo+int2(0,1)),fetch(lo+int2(1,1)),f.x),f.y);
    return float4(c.a > 0 ? c.rgb/c.a : float3(0,0,0), c.a);
}
"#;

fn compile(entry: PCSTR, profile: PCSTR) -> Result<Vec<u8>, String> {
    let mut code: Option<ID3DBlob> = None;
    let mut errors: Option<ID3DBlob> = None;
    let result = unsafe { D3DCompile(SHADER.as_ptr().cast(), SHADER.len(), PCSTR::null(), None,
        None, entry, profile, D3DCOMPILE_ENABLE_STRICTNESS, 0, &mut code, Some(&mut errors)) };
    if let Err(error) = result {
        let detail = errors.map(|b| unsafe {
            String::from_utf8_lossy(std::slice::from_raw_parts(b.GetBufferPointer().cast(), b.GetBufferSize())).into_owned()
        }).unwrap_or_default();
        return Err(format!("D3D11 affine shader compile failed: {error}: {detail}"));
    }
    let blob = code.ok_or("Missing compiled affine shader")?;
    Ok(unsafe { std::slice::from_raw_parts(blob.GetBufferPointer().cast(), blob.GetBufferSize()) }.to_vec())
}

pub struct AffineLayerRenderer {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    vertex: ID3D11VertexShader,
    pixel: ID3D11PixelShader,
    constants: ID3D11Buffer,
    rasterizer: ID3D11RasterizerState,
    outputs: HashMap<String, (ID3D11Texture2D, ID3D11RenderTargetView)>,
    width: u32,
    height: u32,
    pub draws: u64,
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
            device.CreateVertexShader(&compile(s!("vertex"),s!("vs_5_0"))?, None, Some(&mut vertex))
                .map_err(|e| format!("Affine vertex shader: {e}"))?;
            device.CreatePixelShader(&compile(s!("pixel"),s!("ps_5_0"))?, None, Some(&mut pixel))
                .map_err(|e| format!("Affine pixel shader: {e}"))?;
            device.CreateBuffer(&D3D11_BUFFER_DESC { ByteWidth:64, Usage:D3D11_USAGE_DEFAULT,
                BindFlags:D3D11_BIND_CONSTANT_BUFFER.0 as u32, ..Default::default() }, None, Some(&mut constants))
                .map_err(|e| format!("Affine constant buffer: {e}"))?;
            device.CreateRasterizerState(&D3D11_RASTERIZER_DESC { FillMode:D3D11_FILL_SOLID,
                CullMode:D3D11_CULL_NONE, DepthClipEnable:true.into(), ..Default::default() }, Some(&mut rasterizer))
                .map_err(|e| format!("Affine rasterizer: {e}"))?;
        }
        let (width,height) = pool.dimensions();
        Ok(Self { device,context,vertex:vertex.ok_or("Missing vertex shader")?,pixel:pixel.ok_or("Missing pixel shader")?,
            constants:constants.ok_or("Missing constants")?,rasterizer:rasterizer.ok_or("Missing rasterizer")?,
            outputs:HashMap::new(),width,height,draws:0,allocations:0,releases:0,peak_output_bytes:0 })
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
    pub fn prepare(&mut self, frame:&CanvasPacketFrame, resident:&ResidentLayerCache<ID3D11Texture2D>) -> Result<(),String> {
        let keys:HashSet<_> = frame.layers.iter().filter(|l|!l.geometry.is_identity(self.width,self.height))
            .map(|l|l.stream_key.as_str()).collect();
        let before=self.outputs.len();
        self.outputs.retain(|k,_|keys.contains(k.as_str()));
        self.releases+=(before-self.outputs.len()) as u64;
        for layer in &frame.layers {
            if layer.geometry.is_identity(self.width,self.height) { continue; }
            if !self.outputs.contains_key(&layer.stream_key) {
                let texture=self.create_input(self.width,self.height)?; let mut view=None;
                unsafe { self.device.CreateRenderTargetView(&texture,None,Some(&mut view)) }
                    .map_err(|e|format!("Affine output view: {e}"))?;
                self.outputs.insert(layer.stream_key.clone(),(texture,view.ok_or("Missing affine output view")?));
                self.allocations+=1;
                self.peak_output_bytes=self.peak_output_bytes.max(self.outputs.len()*self.width as usize*self.height as usize*4);
            }
            let source=resident.resource(&layer.stream_key)?;
            self.draw(source,&self.outputs[&layer.stream_key].1,&layer.geometry)?;
            self.draws+=1;
        }
        Ok(())
    }
    pub fn clear(&mut self) { self.releases+=self.outputs.len() as u64; self.outputs.clear(); }
    pub fn output(&self,key:&str) -> Result<&ID3D11Texture2D,String> {
        self.outputs.get(key).map(|(t,_)|t).ok_or("Missing transformed resident output".into())
    }
    fn draw(&self,source:&ID3D11Texture2D,target:&ID3D11RenderTargetView,g:&LayerGeometry) -> Result<(),String> {
        let mut srv=None;
        unsafe { self.device.CreateShaderResourceView(source,None,Some(&mut srv)) }.map_err(|e|format!("Affine input view: {e}"))?;
        let m=g.inverse; let c=g.clip;
        let data:[f32;16]=[m[0],m[2],m[4],0.,m[1],m[3],m[5],0.,c[0] as f32,c[1] as f32,c[2] as f32,c[3] as f32,g.width as f32,g.height as f32,0.,0.];
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
