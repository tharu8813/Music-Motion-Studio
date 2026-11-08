//! Windows-only native Direct3D 11 NV12 texture experiment.
//!
//! This is an interoperable *resource type* for a future NVENC AVFrame, not
//! a wgpu Vulkan buffer -> D3D11 cross-API share and not an encoder.
//!
//! D3D11 texture ownership stays inside one Rust struct. The reference
//! upload/readback exercise uses CPU staging textures to verify exact NV12
//! plane bytes, including driver-specific row pitch. Future NVENC integration
//! must share this device and texture with an in-process FFmpeg hwframes
//! context (or NVIDIA Video Codec SDK). The production Python exporter is
//! completely unaffected.

use windows::Win32::Foundation::HMODULE;
use windows::Win32::Graphics::Direct3D::{
    D3D_DRIVER_TYPE, D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL, D3D_FEATURE_LEVEL_11_0,
};
use windows::Win32::Graphics::Direct3D11::{
    D3D11_CPU_ACCESS_READ, D3D11_CPU_ACCESS_WRITE,
    D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_FORMAT_SUPPORT_TEXTURE2D,
    D3D11_MAP_READ, D3D11_MAP_WRITE, D3D11_MAPPED_SUBRESOURCE,
    D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT,
    D3D11_USAGE_STAGING, D3D11CreateDevice,
    ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::{
    IDXGIAdapter1, IDXGIDevice, DXGI_ADAPTER_FLAG_SOFTWARE,
};
use windows::Win32::Graphics::Dxgi::Common::{
    DXGI_FORMAT_NV12, DXGI_SAMPLE_DESC,
};
use windows::core::Interface;

/// Returns the packed size for NV12: height Y rows followed by height/2 UV
/// rows. Both planes are width bytes across; real D3D11 staging memory
/// may add padding to each row.
pub fn nv12_size(width: u32, height: u32) -> Result<usize, String> {
    if width < 2 || height < 2 || width > 3840 || height > 2160
        || width % 2 != 0 || height % 2 != 0
    {
        return Err("NV12 dimensions must be even and within 2..3840x2..2160".into());
    }
    let count = usize::try_from(width).unwrap()
        .checked_mul(height as usize)
        .and_then(|n| n.checked_mul(3))
        .and_then(|n| n.checked_div(2))
        .ok_or_else(|| "NV12 frame byte count overflow".to_owned())?;
    Ok(count)
}

fn validate_frame(width: u32, height: u32, bytes: &[u8]) -> Result<(), String> {
    if bytes.len() != nv12_size(width, height)? {
        return Err("NV12 frame is not exactly width*height*3/2 bytes".into());
    }
    Ok(())
}

pub struct NativeD3d11Nv12Surface {
    // All three textures were created by the same D3D11 device. Keeping
    // the device/context alive is essential for future encoder interop.
    _device: ID3D11Device,
    context: ID3D11DeviceContext,
    texture: ID3D11Texture2D,
    upload: ID3D11Texture2D,
    readback: ID3D11Texture2D,
    width: u32,
    height: u32,
    adapter_name: String,
    adapter_vendor_id: u32,
    adapter_device_id: u32,
}

impl NativeD3d11Nv12Surface {
    /// Hardware adapter only, not WARP. A local RTX is required for a
    /// meaningful NVENC compatibility gate.
    pub fn new(width: u32, height: u32) -> Result<Self, String> {
        Self::new_for_driver(width, height, D3D_DRIVER_TYPE_HARDWARE)
    }

    fn new_for_driver(
        width: u32, height: u32, driver: D3D_DRIVER_TYPE,
    ) -> Result<Self, String> {
        nv12_size(width, height)?;
        let mut device = None;
        let mut context = None;
        let mut feature_level = D3D_FEATURE_LEVEL::default();
        // Fails clearly on machines without Direct3D11. Do not silently
        // substitute software/WARP in production hardware measurements.
        unsafe {
            D3D11CreateDevice(
                None,
                driver,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                Some(&[D3D_FEATURE_LEVEL_11_0]),
                D3D11_SDK_VERSION,
                Some(&mut device),
                Some(&mut feature_level),
                Some(&mut context),
            )
        }.map_err(|e| format!("Could not create native D3D11 device: {e}"))?;
        let device = device.ok_or("D3D11 returned no device")?;
        let context = context.ok_or("D3D11 returned no immediate context")?;

        // D3D_DRIVER_TYPE_HARDWARE can still resolve to a Microsoft Basic
        // Render Driver in hosted Windows environments. Inspect DXGI instead
        // of blindly presenting this as a real NVIDIA GPU measurement.
        let dxgi: IDXGIDevice = device.cast()
            .map_err(|e| format!("Cannot query DXGI adapter: {e}"))?;
        let adapter = unsafe { dxgi.GetAdapter() }
            .map_err(|e| format!("Cannot find D3D11 adapter: {e}"))?;
        let adapter: IDXGIAdapter1 = adapter.cast()
            .map_err(|e| format!("Cannot inspect D3D11 adapter type: {e}"))?;
        let identity = unsafe { adapter.GetDesc1() }
            .map_err(|e| format!("Cannot query D3D11 adapter identity: {e}"))?;
        let name_length = identity.Description.iter()
            .position(|&c| c == 0).unwrap_or(identity.Description.len());
        let adapter_name = String::from_utf16_lossy(
            &identity.Description[..name_length],
        );
        if identity.Flags & DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32 != 0
            || identity.VendorId == 0x1414
        {
            return Err(format!(
                "D3D11 software adapter is not real GPU hardware: {} (vendor {:04x})",
                adapter_name, identity.VendorId,
            ));
        }

        let support = unsafe { device.CheckFormatSupport(DXGI_FORMAT_NV12) }
            .map_err(|e| format!("NV12 format support query failed: {e}"))?;
        if support & D3D11_FORMAT_SUPPORT_TEXTURE2D.0 as u32 == 0 {
            return Err("D3D11 adapter does not support NV12 Texture2D".into());
        }
        let desc = D3D11_TEXTURE2D_DESC {
            Width: width,
            Height: height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_NV12,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: 0,
            CPUAccessFlags: 0,
            MiscFlags: 0,
        };
        let mut texture = None;
        unsafe { device.CreateTexture2D(&desc, None, Some(&mut texture)) }
            .map_err(|e| format!("Unable to allocate native NV12 GPU texture: {e}"))?;
        let texture = texture.ok_or("D3D11 returned no NV12 GPU texture")?;

        let mut staging_desc = desc;
        staging_desc.Usage = D3D11_USAGE_STAGING;
        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE.0 as u32;
        let mut upload = None;
        unsafe { device.CreateTexture2D(&staging_desc, None, Some(&mut upload)) }
            .map_err(|e| format!("Unable to allocate NV12 upload staging: {e}"))?;
        let upload = upload.ok_or("D3D11 returned no upload staging texture")?;

        staging_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ.0 as u32;
        let mut readback = None;
        unsafe { device.CreateTexture2D(&staging_desc, None, Some(&mut readback)) }
            .map_err(|e| format!("Unable to allocate NV12 readback staging: {e}"))?;
        let readback = readback.ok_or("D3D11 returned no readback staging texture")?;

        Ok(Self {
            _device: device, context, texture, upload, readback,
            width, height,
            adapter_name,
            adapter_vendor_id: identity.VendorId,
            adapter_device_id: identity.DeviceId,
        })
    }

    pub fn dimensions(&self) -> (u32, u32) { (self.width, self.height) }
    pub fn adapter_name(&self) -> &str { &self.adapter_name }
    pub fn adapter_vendor_id(&self) -> u32 { self.adapter_vendor_id }
    pub fn adapter_device_id(&self) -> u32 { self.adapter_device_id }

    /// This is a GPU-resident DXGI_FORMAT_NV12 Texture2D, not a software
    /// NV12 buffer. It could be wrapped in an in-process AV_PIX_FMT_D3D11
    /// frame after device and lifetime ownership are implemented.
    pub fn texture(&self) -> &ID3D11Texture2D { &self.texture }

    pub fn upload_reference(&self, packed_nv12: &[u8]) -> Result<(), String> {
        validate_frame(self.width, self.height, packed_nv12)?;
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe {
            self.context.Map(
                &self.upload, 0, D3D11_MAP_WRITE, 0, Some(&mut mapped),
            )
        }.map_err(|e| format!("NV12 upload staging map failed: {e}"))?;
        let stride = mapped.RowPitch as usize;
        let width = self.width as usize;
        let total_rows = self.height as usize * 3 / 2;
        if mapped.pData.is_null() || stride < width {
            unsafe { self.context.Unmap(&self.upload, 0) };
            return Err("Invalid D3D11 NV12 staging pitch or buffer".into());
        }
        // SAFETY: Map guarantees a valid staging pointer until Unmap.
        // For NV12, DXGI specifies RowPitch*(height + height/2) bytes.
        unsafe {
            let dest = mapped.pData.cast::<u8>();
            for row in 0..total_rows {
                std::ptr::copy_nonoverlapping(
                    packed_nv12.as_ptr().add(row * width),
                    dest.add(row * stride),
                    width,
                );
            }
            self.context.Unmap(&self.upload, 0);
            self.context.CopyResource(&self.texture, &self.upload);
        }
        Ok(())
    }

    /// Diagnostic only. Reads the hardware texture into a separate staging
    /// texture to verify all Y and UV bytes survive a native D3D11 GPU copy.
    /// Do NOT call this method in a zero-copy NVENC production fast path.
    pub fn readback_reference(&self) -> Result<Vec<u8>, String> {
        unsafe { self.context.CopyResource(&self.readback, &self.texture) };
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe {
            self.context.Map(
                &self.readback, 0, D3D11_MAP_READ, 0, Some(&mut mapped),
            )
        }.map_err(|e| format!("NV12 readback staging map failed: {e}"))?;
        let stride = mapped.RowPitch as usize;
        let width = self.width as usize;
        let total_rows = self.height as usize * 3 / 2;
        if mapped.pData.is_null() || stride < width {
            unsafe { self.context.Unmap(&self.readback, 0) };
            return Err("Invalid D3D11 NV12 readback pitch or buffer".into());
        }
        let mut packed = vec![0u8; nv12_size(self.width, self.height)?];
        // SAFETY: Mapped NV12 staging buffer remains valid through Unmap.
        unsafe {
            let source = mapped.pData.cast::<u8>();
            for row in 0..total_rows {
                std::ptr::copy_nonoverlapping(
                    source.add(row * stride),
                    packed.as_mut_ptr().add(row * width),
                    width,
                );
            }
            self.context.Unmap(&self.readback, 0);
        }
        Ok(packed)
    }

    /// Intended only for a disposable diagnostic, not an export. Tests upload,
    /// NV12 texture allocation, GPU copy, and readback byte parity.
    pub fn verify_frame(&self, nv12: &[u8]) -> Result<(), String> {
        self.upload_reference(nv12)?;
        let actual = self.readback_reference()?;
        if actual != nv12 {
            let diff = actual.iter().zip(nv12).filter(|(a,b)| a != b).count();
            return Err(format!("Native D3D11 NV12 texture lost {diff} bytes"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reject_odd_dimensions_truncated_frames_and_overflow() {
        assert!(nv12_size(0, 2).is_err());
        assert!(nv12_size(1919, 1080).is_err());
        assert!(nv12_size(1920, 1079).is_err());
        assert!(nv12_size(3840, 2160).is_ok());
        assert_eq!(nv12_size(1920, 1080).unwrap(), 3_110_400);
        assert!(validate_frame(4, 2, &[0; 11]).is_err());
        assert!(validate_frame(4, 2, &[0; 12]).is_ok());
    }

    #[test]
    fn native_d3d11_nv12_round_trip_if_device_supports_it() {
        let surface = match NativeD3d11Nv12Surface::new(64, 48) {
            Ok(s) => s,
            Err(e) => {
                if std::env::var("MMS_REQUIRE_D3D11").as_deref() == Ok("1") {
                    panic!("Required hardware D3D11 NV12 surface unavailable: {e}");
                }
                eprintln!("Skipping native hardware D3D11 test: {e}");
                return;
            }
        };
        let mut packed = vec![0; nv12_size(64, 48).unwrap()];
        for (index, byte) in packed.iter_mut().enumerate() {
            *byte = ((index * 17 + 31) & 255) as u8;
        }
        surface.verify_frame(&packed).unwrap();
        for byte in &mut packed { *byte = byte.wrapping_add(31); }
        surface.verify_frame(&packed).unwrap();
    }
}
