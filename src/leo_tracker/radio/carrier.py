"""Memory-bounded narrow-carrier measurement from persisted IQ."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CarrierPoint:
    time_s: float
    frequency_hz: float
    power_db: float
    prominence_db: float


@dataclass(frozen=True)
class CarrierTrack:
    points: tuple[CarrierPoint, ...]
    fitted_linear_drift_hz_s: float
    fitted_intercept_hz: float
    frequency_span_hz: float
    residual_rms_hz: float
    fft_size: int
    integration_s: float
    spectra_per_integration: int
    search_low_hz: float
    search_high_hz: float


def track_carrier(iq_path: str | Path, *, sample_rate_hz: float, center_frequency_hz: float,
                  search_low_hz: float, search_high_hz: float, integration_s: float = 1.0,
                  fft_size: int = 65536, spectra_per_integration: int = 16) -> CarrierTrack:
    """Track the strongest narrow carrier in an absolute-frequency window.

    The raw capture is read through ``numpy.memmap``. At most one FFT accumulator
    and one integration's samples are resident, independent of capture duration.
    Reported power is FFT power in dB (arbitrary but internally comparable units).
    """
    path = Path(iq_path)
    if path.stat().st_size % np.dtype("<c8").itemsize:
        raise ValueError("IQ file size is not a whole number of complex64 samples")
    if sample_rate_hz <= 0 or integration_s <= 0 or fft_size < 32 or spectra_per_integration < 1:
        raise ValueError("sample rate, integration, FFT size, and spectrum count must be positive")
    if not search_low_hz < search_high_hz:
        raise ValueError("search_low_hz must be below search_high_hz")
    nyquist_low = center_frequency_hz - sample_rate_hz / 2
    nyquist_high = center_frequency_hz + sample_rate_hz / 2
    if search_low_hz < nyquist_low or search_high_hz > nyquist_high:
        raise ValueError("absolute search window exceeds the captured Nyquist band")
    iq = np.memmap(path, dtype="<c8", mode="r")
    integration_samples = round(integration_s * sample_rate_hz)
    if fft_size > integration_samples:
        raise ValueError("FFT does not fit within one integration")
    frame_count = iq.size // integration_samples
    if frame_count < 1: raise ValueError("capture contains no complete integration")
    baseband = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    absolute = center_frequency_hz + baseband
    search_bins = np.flatnonzero((absolute >= search_low_hz) & (absolute <= search_high_hz))
    if search_bins.size < 3: raise ValueError("search window contains fewer than three FFT bins")
    window = np.hanning(fft_size).astype(np.float32)
    starts = np.linspace(0, integration_samples - fft_size, spectra_per_integration, dtype=np.int64)
    bin_width = sample_rate_hz / fft_size
    points: list[CarrierPoint] = []
    for frame in range(frame_count):
        accumulator = np.zeros(fft_size, np.float64)
        origin = frame * integration_samples
        for offset in starts:
            values = np.asarray(iq[origin + offset:origin + offset + fft_size], dtype=np.complex64)
            accumulator += np.abs(np.fft.fftshift(np.fft.fft(values * window))) ** 2
        power = accumulator / spectra_per_integration
        peak = int(search_bins[np.argmax(power[search_bins])])
        delta = 0.0
        if 0 < peak < fft_size - 1:
            left, middle, right = np.log(power[peak-1:peak+2] + np.finfo(float).tiny)
            denominator = left - 2*middle + right
            if denominator: delta = float(np.clip(.5*(left-right)/denominator, -.5, .5))
        frequency = float(absolute[peak] + delta * bin_width)
        power_db = float(10*np.log10(power[peak] + np.finfo(float).tiny))
        background_db = float(10*np.log10(np.median(power[search_bins]) + np.finfo(float).tiny))
        points.append(CarrierPoint((frame + .5)*integration_s, frequency, power_db,
                                   power_db-background_db))
    times = np.array([point.time_s for point in points]); frequencies = np.array([point.frequency_hz for point in points])
    if len(points) > 1:
        drift, intercept = np.polyfit(times, frequencies, 1)
        residual_rms = float(np.sqrt(np.mean((frequencies-(drift*times+intercept))**2)))
    else:
        drift, intercept, residual_rms = 0.0, frequencies[0], 0.0
    span = float(np.max(frequencies)-np.min(frequencies))
    return CarrierTrack(tuple(points), float(drift), float(intercept), span, residual_rms,
                        fft_size, integration_s, spectra_per_integration, search_low_hz, search_high_hz)
