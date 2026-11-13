//! Decode known media timestamps and write only the selected PTS for validation.
#[cfg(windows)]
fn run() -> Result<(), String> {
    use music_motion_gpu_core::d3d11_frame_pool::NativeNv12FramePool;
    use music_motion_gpu_core::d3d11_video_source::{GpuVideoSource, VideoDecodeRuntime};
    let mut arguments = std::env::args_os().skip(1);
    let path = std::path::PathBuf::from(arguments.next().ok_or("Missing absolute MP4 path")?);
    let pool = NativeNv12FramePool::new(320, 180, 2)?;
    let runtime = VideoDecodeRuntime::new(&pool)?;
    let mut source = GpuVideoSource::open(&runtime, &pool, &path, (320, 180), false)?;
    for position in arguments {
        let position: i64 = position
            .to_str()
            .ok_or("Invalid video time")?
            .parse()
            .map_err(|_| "Expected video time in 100 ns units")?;
        let (_, actual) = source.frame(position)?;
        println!("{position} {actual}");
    }
    println!(
        "DXVA decoded={}, converted={}, seeks={}",
        source.decoded_frames, source.converted_frames, source.seeks
    );
    Ok(())
}

#[cfg(windows)]
fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("Windows D3D11 required");
    std::process::exit(1);
}
