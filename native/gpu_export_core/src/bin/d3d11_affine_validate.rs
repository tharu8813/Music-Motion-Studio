//! Explicit diagnostic readback only. Production stream never calls this binary.
#[cfg(not(windows))]
fn main() { eprintln!("Windows D3D11 required"); std::process::exit(1); }
#[cfg(windows)]
fn main() {
    if let Err(e) = run() { eprintln!("{e}"); std::process::exit(1); }
}
#[cfg(windows)]
fn run() -> Result<(),String> {
    use music_motion_gpu_core::{d3d11_affine::AffineLayerRenderer,d3d11_frame_pool::NativeNv12FramePool,
        qt_canvas_packet::load_qt_canvas_packet,resident_layer_cache::ResidentLayerCache};
    use windows::Win32::Graphics::Direct3D11::*;
    use std::{path::Path,io::Write};
    let args:Vec<_>=std::env::args().skip(1).collect();
    if args.len()!=3 && !(args.len()==4 && args[3]=="--nv12") { return Err("Usage: d3d11_affine_validate MANIFEST LAYER NEW_OUTPUT [--nv12]".into()); }
    let frame=load_qt_canvas_packet(Path::new(&args[0]))?;
    let index:usize=args[1].parse().map_err(|_|"Invalid layer index")?;
    let layer=frame.layers.get(index).ok_or("Layer index out of range")?;
    if Path::new(&args[2]).exists() { return Err("Validation output already exists".into()); }
    let mut pool=NativeNv12FramePool::new(frame.width,frame.height,2)?;
    let mut shader=AffineLayerRenderer::new(&pool)?;
    let mut resident=ResidentLayerCache::new(frame.width,frame.height);
    resident.update_sized(&frame,|w,h|shader.create_input(w,h),|t,p,w|shader.upload(t,p,w))?;
    shader.prepare(&frame,&resident)?;
    if args.len()==4 {
        use music_motion_gpu_core::d3d11_bgra_video::{NativeBgraNv12VideoProcessor,NativeCanvasBgraLayer};
        let processor=NativeBgraNv12VideoProcessor::new_for_pool(&pool)?;
        let layers=frame.layers.iter().map(|l| Ok(NativeCanvasBgraLayer {
            texture:if l.uses_resident_texture(frame.width,frame.height) {resident.resource(&l.stream_key)?} else {shader.output(&l.stream_key)?},
            opacity:l.composition_opacity(),
        })).collect::<Result<Vec<_>,String>>()?;
        let slot=pool.queue_gpu_canvas_layers(&processor,&layers,0)?;
        pool.wait_for_gpu_copy(slot,std::time::Duration::from_secs(15))?;
        let bytes=pool.readback_slot_for_validation(slot)?;
        std::fs::OpenOptions::new().write(true).create_new(true).open(&args[2]).map_err(|e|e.to_string())?
            .write_all(&bytes).map_err(|e|e.to_string())?;
        println!("{}",serde_json::json!({"status":"validated_readback","format":"NV12","bytes":bytes.len(),"shader_draws":shader.draws,"adapter":pool.adapter_name()}));
        return Ok(());
    }
    let output=if layer.uses_resident_texture(frame.width,frame.height) { resident.resource(&layer.stream_key)? }
        else { shader.output(&layer.stream_key)? };
    let mut desc=D3D11_TEXTURE2D_DESC::default(); unsafe {output.GetDesc(&mut desc)};
    desc.Usage=D3D11_USAGE_STAGING; desc.BindFlags=0;desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ.0 as u32;
    let mut staging=None;
    let device=unsafe {output.GetDevice()}.map_err(|e|e.to_string())?;
    unsafe {device.CreateTexture2D(&desc,None,Some(&mut staging))}.map_err(|e|e.to_string())?;
    let staging=staging.ok_or("Missing validation staging texture")?;
    let context=unsafe {device.GetImmediateContext()}.map_err(|e|e.to_string())?;
    unsafe {context.CopyResource(&staging,output)};
    let mut mapped=D3D11_MAPPED_SUBRESOURCE::default();
    unsafe {context.Map(&staging,0,D3D11_MAP_READ,0,Some(&mut mapped))}.map_err(|e|e.to_string())?;
    let mut bytes=Vec::with_capacity(frame.width as usize*frame.height as usize*4);
    for y in 0..frame.height as usize {
        bytes.extend_from_slice(unsafe {std::slice::from_raw_parts((mapped.pData as *const u8).add(y*mapped.RowPitch as usize),frame.width as usize*4)});
    }
    unsafe {context.Unmap(&staging,0)};
    std::fs::OpenOptions::new().write(true).create_new(true).open(&args[2]).map_err(|e|e.to_string())?
        .write_all(&bytes).map_err(|e|e.to_string())?;
    println!("{}",serde_json::json!({"status":"validated_readback","bytes":bytes.len(),"shader_draws":shader.draws,"adapter":pool.adapter_name()}));
    Ok(())
}
