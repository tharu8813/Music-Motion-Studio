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

#[pymodule]
fn music_motion_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(reduce_spectra, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::interpolated;

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
