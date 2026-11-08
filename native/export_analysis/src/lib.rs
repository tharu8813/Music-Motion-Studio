//! Optional native reduction of export visualizer FFT magnitudes.
//! The existing NumPy FFT remains authoritative; only the per-frame
//! interpolation and max-reduction of frequency bands moves to Rust.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

fn interpolated(frequencies: &[f64], magnitudes: &[f64], x: f64) -> f64 {
    // Match NumPy's np.interp endpoint behavior.
    if x <= frequencies[0] {
        return magnitudes[0];
    }
    let last = frequencies.len() - 1;
    if x >= frequencies[last] {
        return magnitudes[last];
    }
    let right = frequencies.partition_point(|frequency| *frequency <= x);
    let left = right - 1;
    let dx = frequencies[right] - frequencies[left];
    if dx == 0.0 {
        return magnitudes[right];
    }
    magnitudes[left] + (magnitudes[right] - magnitudes[left])
        * ((x - frequencies[left]) / dx)
}

/// Reduce float64 FFT magnitudes to float32, clipped per-band levels.
///
/// The first argument contains contiguous, little-endian float64 spectra
/// (rows * columns). Each band has exactly 5 probes, plus actual FFT-bin
/// indices to ensure narrow-frequency peaks are not lost. The output is
/// packed little-endian float32 (rows * band_count).
#[pyfunction]
fn reduce_spectra<'py>(
    py: Python<'py>,
    raw_spectra: &Bound<'py, PyBytes>,
    rows: usize,
    columns: usize,
    frequencies: Vec<f64>,
    probes: Vec<f64>,
    band_bins: Vec<Vec<usize>>,
) -> PyResult<Bound<'py, PyBytes>> {
    let byte_count = rows
        .checked_mul(columns)
        .and_then(|n| n.checked_mul(8))
        .ok_or_else(|| PyValueError::new_err("Spectra dimensions overflow"))?;
    if columns < 2 || frequencies.len() != columns || raw_spectra.as_bytes().len() != byte_count {
        return Err(PyValueError::new_err("Invalid spectra dimensions"));
    }
    if band_bins.is_empty() || probes.len() != band_bins.len() * 5 {
        return Err(PyValueError::new_err("Expected five probes per frequency band"));
    }
    if !frequencies.windows(2).all(|x| x[0].is_finite() && x[1].is_finite() && x[0] < x[1])
        || !probes.iter().all(|x| x.is_finite())
        || band_bins.iter().flatten().any(|&index| index >= columns)
    {
        return Err(PyValueError::new_err("Invalid frequencies, probes, or band indices"));
    }
    let output_bytes = rows
        .checked_mul(band_bins.len())
        .and_then(|n| n.checked_mul(4))
        .ok_or_else(|| PyValueError::new_err("Output dimensions overflow"))?;
    let mut output = Vec::<u8>::with_capacity(output_bytes);
    let row_width = columns * 8;
    for row in raw_spectra.as_bytes().chunks_exact(row_width) {
        // Decode one row at a time, not the entire audio track.
        let magnitudes: Vec<f64> = row
            .chunks_exact(8)
            .map(|bytes| f64::from_le_bytes(bytes.try_into().unwrap()))
            .collect();
        for (band, indices) in band_bins.iter().enumerate() {
            let mut peak = f64::NEG_INFINITY;
            for probe in &probes[(band * 5)..((band + 1) * 5)] {
                let value = interpolated(&frequencies, &magnitudes, *probe);
                if value.is_nan() {
                    peak = f64::NAN;
                    break;
                }
                peak = peak.max(value);
            }
            if !peak.is_nan() {
                for index in indices {
                    peak = peak.max(magnitudes[*index]);
                }
            }
            // NumPy first casts the band result to float32, then clips to 0..2.
            let normalized = (peak as f32).clamp(0.0, 2.0);
            output.extend_from_slice(&normalized.to_le_bytes());
        }
    }
    Ok(PyBytes::new(py, &output))
}


fn checked_pcm_shape(raw: &[u8], fps: usize, sample_rate: usize, points: usize) -> PyResult<(usize, usize)> {
    if raw.len() % 4 != 0 || fps == 0 || sample_rate == 0 || points == 0 || points > 8192 {
        return Err(PyValueError::new_err("Invalid PCM format, frame rate or point count"));
    }
    let sample_count = raw.len() / 4;
    let total = sample_count
        .checked_mul(fps)
        .ok_or_else(|| PyValueError::new_err("Frame count overflow"))?;
    let frames = total
        .checked_add(sample_rate - 1)
        .ok_or_else(|| PyValueError::new_err("Frame count overflow"))?
        / sample_rate;
    let frames = frames.max(1);
    frames
        .checked_mul(points)
        .and_then(|n| n.checked_mul(4))
        .filter(|&n| n <= 512 * 1024 * 1024)
        .ok_or_else(|| PyValueError::new_err("PCM result is too large"))?;
    Ok((sample_count, frames))
}

fn python_round_boundary(frame: usize, sample_rate: usize, fps: usize) -> usize {
    // Python round() uses ties-to-even on nonnegative frame boundaries.
    // Use rational integer arithmetic so 29.97/59.94-like ratios do not drift.
    let numerator = (frame as u128) * (sample_rate as u128);
    let divisor = fps as u128;
    let quotient = numerator / divisor;
    let remainder = numerator % divisor;
    let round_up = remainder * 2 > divisor
        || (remainder * 2 == divisor && quotient % 2 == 1);
    (quotient + u128::from(round_up)).min(usize::MAX as u128) as usize
}

fn pcm_sample(raw: &[u8], index: usize) -> f32 {
    let start = index * 4;
    f32::from_le_bytes(raw[start..start + 4].try_into().unwrap())
}

fn pcm_window(index: usize, sample_rate: usize, fps: usize, count: usize) -> (usize, usize) {
    let start = python_round_boundary(index, sample_rate, fps).min(count);
    let end = python_round_boundary(index + 1, sample_rate, fps).min(count);
    (start, end.max(start))
}

/// Compute per-frame RMS of a little-endian float32 PCM sequence.
/// Returns little-endian float32 values using the same frame boundaries
/// and float32 final precision as the original NumPy implementation.
#[pyfunction]
fn pcm_rms<'py>(
    py: Python<'py>,
    raw_pcm: &Bound<'py, PyBytes>,
    fps: usize,
    sample_rate: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let raw = raw_pcm.as_bytes();
    let (count, frames) = checked_pcm_shape(raw, fps, sample_rate, 1)?;
    let mut out = Vec::with_capacity(frames * 4);
    for frame in 0..frames {
        let (start, end) = pcm_window(frame, sample_rate, fps, count);
        let rms = if start == end {
            0.0_f32
        } else {
            let mut squares = 0.0_f64;
            for index in start..end {
                let sample = pcm_sample(raw, index);
                squares += (sample * sample) as f64;
            }
            // NumPy's mean and sqrt use float32 for float32 input.
            (squares / (end - start) as f64) as f32
        };
        let rms = rms.sqrt();
        out.extend_from_slice(&rms.to_le_bytes());
    }
    Ok(PyBytes::new(py, &out))
}

/// Interpolate a signed PCM waveform per output frame. The result contains
/// (frames, points) little-endian float32 values, in row-major order.
#[pyfunction]
fn pcm_waveform<'py>(
    py: Python<'py>,
    raw_pcm: &Bound<'py, PyBytes>,
    fps: usize,
    sample_rate: usize,
    points: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let raw = raw_pcm.as_bytes();
    let (count, frames) = checked_pcm_shape(raw, fps, sample_rate, points)?;
    let mut out = vec![0_u8; frames * points * 4];
    for frame in 0..frames {
        let (start, end) = pcm_window(frame, sample_rate, fps, count);
        let width = end - start;
        if width < 2 {
            continue;
        }
        for point in 0..points {
            let pos = if points == 1 {
                0.0_f64
            } else {
                (point as f64) * ((width - 1) as f64) / ((points - 1) as f64)
            };
            let lower = (pos.floor() as usize).min(width - 1);
            let fraction = pos - lower as f64;
            let value = if lower >= width - 1 {
                pcm_sample(raw, start + width - 1) as f64
            } else {
                let a = pcm_sample(raw, start + lower) as f64;
                let b = pcm_sample(raw, start + lower + 1) as f64;
                a + (b - a) * fraction
            };
            let offset = (frame * points + point) * 4;
            out[offset..offset + 4].copy_from_slice(&(value as f32).to_le_bytes());
        }
    }
    Ok(PyBytes::new(py, &out))
}

/// Pack a contiguous, little-endian QImage::Format_RGB32 byte buffer from
/// B,G,R,X (4 bytes) into FFmpeg bgr24 (3 bytes). No pixel math is changed:
/// only the unused opaque alpha/padding channel is omitted.
///
/// Accept the Python buffer protocol so a PySide6 QImage.constBits() memoryview
/// is read directly without allocating a full 32-bit input copy.
#[pyfunction]
fn pack_bgr24<'py>(
    py: Python<'py>,
    input: &Bound<'py, PyAny>,
    width: usize,
    height: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    use pyo3::buffer::PyBuffer;

    if width == 0 || height == 0 {
        return Err(PyValueError::new_err("Frame dimensions must be positive"));
    }
    let pixels = width
        .checked_mul(height)
        .ok_or_else(|| PyValueError::new_err("Frame dimensions overflow"))?;
    let input_bytes = pixels
        .checked_mul(4)
        .ok_or_else(|| PyValueError::new_err("Input frame byte count overflow"))?;
    let output_bytes = pixels
        .checked_mul(3)
        .ok_or_else(|| PyValueError::new_err("Output frame byte count overflow"))?;
    if output_bytes > 512 * 1024 * 1024 {
        return Err(PyValueError::new_err("Frame is too large"));
    }

    let buffer = PyBuffer::<u8>::get(input)?;
    let source = buffer.as_slice(py).ok_or_else(|| {
        PyValueError::new_err("Expected a contiguous unsigned 8-bit image buffer")
    })?;
    if source.len() != input_bytes {
        return Err(PyValueError::new_err(
            "Expected exactly width * height * 4 tightly packed RGB32 bytes",
        ));
    }
    PyBytes::new_with(py, output_bytes, |output| {
        for (pixel, destination) in source.chunks_exact(4).zip(output.chunks_exact_mut(3)) {
            destination[0] = pixel[0].get();
            destination[1] = pixel[1].get();
            destination[2] = pixel[2].get();
        }
        Ok(())
    })
}

#[pymodule]
fn music_motion_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(reduce_spectra, m)?)?;
    m.add_function(wrap_pyfunction!(pcm_rms, m)?)?;
    m.add_function(wrap_pyfunction!(pcm_waveform, m)?)?;
    m.add_function(wrap_pyfunction!(pack_bgr24, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{interpolated, python_round_boundary};

    #[test]
    fn frame_round_uses_python_ties_even() {
        assert_eq!(python_round_boundary(1, 1, 2), 0);
        assert_eq!(python_round_boundary(3, 1, 2), 2);
        assert_eq!(python_round_boundary(5, 1, 2), 2);
        assert_eq!(python_round_boundary(7, 1, 2), 4);
    }

    #[test]
    fn interpolation_and_endpoints() {
        let f = [0.0, 10.0, 20.0];
        let v = [1.0, 3.0, 9.0];
        assert_eq!(interpolated(&f, &v, -1.0), 1.0);
        assert_eq!(interpolated(&f, &v, 0.0), 1.0);
        assert_eq!(interpolated(&f, &v, 5.0), 2.0);
        assert_eq!(interpolated(&f, &v, 15.0), 6.0);
        assert_eq!(interpolated(&f, &v, 30.0), 9.0);
    }
}
