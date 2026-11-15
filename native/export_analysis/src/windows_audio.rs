//! Decode local Windows audio files through a synchronous Media Foundation source reader.
use pyo3::{
    exceptions::{PyInterruptedError, PyRuntimeError, PyValueError},
    prelude::*,
    types::PyBytes,
};
use std::{ffi::c_void, mem::ManuallyDrop, path::PathBuf, ptr};
use windows::core::{s, w, GUID, HRESULT, HSTRING, PCSTR, PCWSTR};
use windows::Win32::Foundation::{FreeLibrary, HMODULE, RPC_E_CHANGED_MODE};
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::StructuredStorage::{
    PropVariantClear, PropVariantToUInt64, PROPVARIANT, PROPVARIANT_0, PROPVARIANT_0_0,
    PROPVARIANT_0_0_0,
};
use windows::Win32::System::Com::{CoInitializeEx, CoUninitialize, COINIT_MULTITHREADED};
use windows::Win32::System::LibraryLoader::{
    GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32,
};
use windows::Win32::System::Variant::VT_I8;

const AUDIO: u32 = MF_SOURCE_READER_FIRST_AUDIO_STREAM.0 as u32;
const TICKS_PER_SECOND: f64 = 10_000_000.0;

struct SystemDll(HMODULE);

impl SystemDll {
    fn open(name: PCWSTR) -> PyResult<Self> {
        unsafe { LoadLibraryExW(name, None, LOAD_LIBRARY_SEARCH_SYSTEM32) }
            .map(Self)
            .map_err(decode_error)
    }

    fn proc(&self, name: PCSTR) -> PyResult<unsafe extern "system" fn() -> isize> {
        unsafe { GetProcAddress(self.0, name) }
            .ok_or_else(|| PyRuntimeError::new_err("Windows Media Foundation API is unavailable"))
    }
}

impl Drop for SystemDll {
    fn drop(&mut self) {
        let _ = unsafe { FreeLibrary(self.0) };
    }
}

struct MfSession {
    shutdown: unsafe extern "system" fn() -> HRESULT,
    uninitialize_com: bool,
}

impl Drop for MfSession {
    fn drop(&mut self) {
        unsafe {
            let _ = (self.shutdown)();
            if self.uninitialize_com {
                CoUninitialize();
            }
        }
    }
}

fn decode_error(error: windows::core::Error) -> PyErr {
    PyRuntimeError::new_err(format!("Windows audio decoding failed: {error}"))
}

fn check_cancel(cancelled: Option<&Py<PyAny>>) -> PyResult<()> {
    if let Some(callback) = cancelled {
        let cancelled = Python::attach(|py| callback.call0(py)?.extract::<bool>(py))?;
        if cancelled {
            return Err(PyInterruptedError::new_err("Audio decoding was cancelled"));
        }
    }
    Ok(())
}

type AudioData = (u32, u32, Option<u64>, Vec<u8>);

fn decode(
    path: PathBuf,
    start: f64,
    duration: Option<f64>,
    cancelled: Option<&Py<PyAny>>,
) -> PyResult<AudioData> {
    check_cancel(cancelled)?;
    // Resolve the optional Windows media APIs lazily so the extension also loads on Windows N.
    let platform = SystemDll::open(w!("mfplat.dll"))?;
    let source = SystemDll::open(w!("mfreadwrite.dll"))?;
    let startup: unsafe extern "system" fn(u32, u32) -> HRESULT =
        unsafe { std::mem::transmute(platform.proc(s!("MFStartup"))?) };
    let shutdown: unsafe extern "system" fn() -> HRESULT =
        unsafe { std::mem::transmute(platform.proc(s!("MFShutdown"))?) };
    let create_reader: unsafe extern "system" fn(
        PCWSTR,
        *mut c_void,
        *mut Option<IMFSourceReader>,
    ) -> HRESULT = unsafe { std::mem::transmute(source.proc(s!("MFCreateSourceReaderFromURL"))?) };
    let create_type: unsafe extern "system" fn(*mut Option<IMFMediaType>) -> HRESULT =
        unsafe { std::mem::transmute(platform.proc(s!("MFCreateMediaType"))?) };
    let com = unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) };
    if com != RPC_E_CHANGED_MODE {
        com.ok().map_err(decode_error)?;
    }
    let uninitialize_com = com.is_ok();
    if let Err(error) = unsafe { startup(MF_VERSION, MFSTARTUP_NOSOCKET).ok() } {
        if uninitialize_com {
            unsafe { CoUninitialize() };
        }
        return Err(decode_error(error));
    }
    let _session = MfSession {
        shutdown,
        uninitialize_com,
    };
    unsafe {
        let filename = HSTRING::from(path.as_os_str());
        let mut reader = None;
        create_reader(PCWSTR(filename.as_ptr()), ptr::null_mut(), &mut reader)
            .ok()
            .map_err(decode_error)?;
        let reader =
            reader.ok_or_else(|| PyRuntimeError::new_err("Missing Windows audio reader"))?;
        reader
            .SetStreamSelection(MF_SOURCE_READER_ALL_STREAMS.0 as u32, false)
            .map_err(decode_error)?;
        reader
            .SetStreamSelection(AUDIO, true)
            .map_err(decode_error)?;
        let mut media_type = None;
        create_type(&mut media_type).ok().map_err(decode_error)?;
        let media_type = media_type
            .ok_or_else(|| PyRuntimeError::new_err("Missing Windows audio media type"))?;
        media_type
            .SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Audio)
            .map_err(decode_error)?;
        media_type
            .SetGUID(&MF_MT_SUBTYPE, &MFAudioFormat_Float)
            .map_err(decode_error)?;
        reader
            .SetCurrentMediaType(AUDIO, None, &media_type)
            .map_err(decode_error)?;
        let current = reader.GetCurrentMediaType(AUDIO).map_err(decode_error)?;
        let rate = current
            .GetUINT32(&MF_MT_AUDIO_SAMPLES_PER_SECOND)
            .map_err(decode_error)?;
        let channels = current
            .GetUINT32(&MF_MT_AUDIO_NUM_CHANNELS)
            .map_err(decode_error)?;
        if rate == 0 || !(1..=2).contains(&channels) {
            return Err(PyRuntimeError::new_err(
                "Windows audio decoding requires mono or stereo",
            ));
        }
        let total_frames = reader
            .GetPresentationAttribute(MF_SOURCE_READER_MEDIASOURCE.0 as u32, &MF_PD_DURATION)
            .ok()
            .and_then(|mut value| {
                let ticks = PropVariantToUInt64(&value).ok();
                let _ = PropVariantClear(&mut value);
                ticks
                    .map(|ticks| (ticks as f64 * f64::from(rate) / TICKS_PER_SECOND).round() as u64)
            })
            .filter(|frames| *frames != 0);
        let adts = path
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("aac"));
        let pcm = if duration == Some(0.0) {
            Vec::new()
        } else {
            read_samples(&reader, rate, channels, adts, start, duration, cancelled)?
        };
        Ok((rate, channels, total_frames, pcm))
    }
}

fn read_samples(
    reader: &IMFSourceReader,
    rate: u32,
    channels: u32,
    adts: bool,
    start: f64,
    duration: Option<f64>,
    cancelled: Option<&Py<PyAny>>,
) -> PyResult<Vec<u8>> {
    let first = (start * f64::from(rate)).round() as i64;
    let stop = duration.map_or(i64::MAX, |seconds| {
        ((start + seconds) * f64::from(rate)).round() as i64
    });
    if !adts && start > 0.5 {
        // Begin before the requested sample to allow decoder preroll.
        let position = PROPVARIANT {
            Anonymous: PROPVARIANT_0 {
                Anonymous: ManuallyDrop::new(PROPVARIANT_0_0 {
                    vt: VT_I8,
                    Anonymous: PROPVARIANT_0_0_0 {
                        hVal: ((start - 0.5) * TICKS_PER_SECOND).round() as i64,
                    },
                    ..Default::default()
                }),
            },
        };
        let _ = unsafe { reader.SetCurrentPosition(&GUID::zeroed(), &position) };
    }
    let frame_size = channels as usize * size_of::<f32>();
    let mut output = Vec::new();
    let mut next = first;
    let mut decoded_until = 0;
    while next < stop {
        check_cancel(cancelled)?;
        let (mut flags, mut timestamp, mut sample) = (0, 0, None);
        unsafe {
            reader.ReadSample(
                AUDIO,
                0,
                None,
                Some(&mut flags),
                Some(&mut timestamp),
                Some(&mut sample),
            )
        }
        .map_err(decode_error)?;
        if flags & MF_SOURCE_READERF_ERROR.0 as u32 != 0 {
            return Err(PyRuntimeError::new_err(
                "Windows audio decoder reported a stream error",
            ));
        }
        if flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED.0 as u32 != 0 {
            let current = unsafe { reader.GetCurrentMediaType(AUDIO) }.map_err(decode_error)?;
            let matches = unsafe {
                current
                    .GetUINT32(&MF_MT_AUDIO_NUM_CHANNELS)
                    .map_err(decode_error)?
                    == channels
                    && current
                        .GetUINT32(&MF_MT_AUDIO_SAMPLES_PER_SECOND)
                        .map_err(decode_error)?
                        == rate
                    && current.GetGUID(&MF_MT_SUBTYPE).map_err(decode_error)? == MFAudioFormat_Float
            };
            if !matches {
                return Err(PyRuntimeError::new_err(
                    "Windows audio format changed during decoding",
                ));
            }
        }
        if let Some(sample) = sample {
            let buffer = unsafe { sample.ConvertToContiguousBuffer() }.map_err(decode_error)?;
            let (mut pointer, mut size) = (ptr::null_mut(), 0);
            unsafe { buffer.Lock(&mut pointer, None, Some(&mut size)) }.map_err(decode_error)?;
            let size = size as usize;
            if size % frame_size != 0 || (size != 0 && pointer.is_null()) {
                let _ = unsafe { buffer.Unlock() };
                return Err(PyRuntimeError::new_err("Invalid Windows PCM buffer"));
            }
            // ADTS seeks are approximate and their timestamps can accumulate
            // rounding error. Read from the beginning and count decoded frames.
            let buffer_start = if adts {
                decoded_until
            } else {
                (timestamp as f64 * f64::from(rate) / TICKS_PER_SECOND).round() as i64
            };
            let buffer_stop = buffer_start.saturating_add((size / frame_size) as i64);
            decoded_until = buffer_stop;
            let from = next.max(buffer_start);
            let to = stop.min(buffer_stop);
            if from < to && !pointer.is_null() {
                let offset = (from - buffer_start) as usize * frame_size;
                let length = (to - from) as usize * frame_size;
                output.extend_from_slice(unsafe {
                    std::slice::from_raw_parts(pointer.add(offset), length)
                });
                next = to;
            }
            unsafe { buffer.Unlock() }.map_err(decode_error)?;
        }
        if flags & MF_SOURCE_READERF_ENDOFSTREAM.0 as u32 != 0 {
            break;
        }
    }
    Ok(output)
}

#[pyfunction]
#[pyo3(signature = (path, start=0.0, duration=None, cancelled=None))]
pub(super) fn decode_windows_audio<'py>(
    py: Python<'py>,
    path: PathBuf,
    start: f64,
    duration: Option<f64>,
    cancelled: Option<Py<PyAny>>,
) -> PyResult<(u32, u32, Option<u64>, Bound<'py, PyBytes>)> {
    if !path.is_absolute()
        || !path.is_file()
        || !start.is_finite()
        || start < 0.0
        || duration.is_some_and(|value| !value.is_finite() || value < 0.0)
    {
        return Err(PyValueError::new_err(
            "Invalid local audio file or decode window",
        ));
    }
    let (rate, channels, frames, pcm) =
        py.detach(|| decode(path, start, duration, cancelled.as_ref()))?;
    Ok((rate, channels, frames, PyBytes::new(py, &pcm)))
}
