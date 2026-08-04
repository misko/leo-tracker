from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class TrackPoint:
    time_s: float
    frequency_hz: float
    uncertainty_hz: float
    snr_db: float
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrequencyTrack:
    points: tuple[TrackPoint, ...]
    fft_size: int
    hop_size: int


def extract_frequency_ridge(samples: npt.ArrayLike, sample_rate_hz: float, *, fft_size: int = 4096,
                            hop_size: int | None = None, search_hz: tuple[float, float] | None = None,
                            min_snr_db: float = 6.0, max_step_hz: float | None = None) -> FrequencyTrack:
    """Track the strongest spectral ridge with sub-bin parabolic interpolation."""
    x = np.asarray(samples, dtype=np.complex64)
    hop = hop_size or fft_size // 2
    if x.ndim != 1 or fft_size < 16 or hop <= 0 or x.size < fft_size:
        raise ValueError("invalid samples, fft_size, or hop_size")
    frequencies = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    mask = np.ones(fft_size, bool) if search_hz is None else ((frequencies >= search_hz[0]) & (frequencies <= search_hz[1]))
    candidates = np.flatnonzero(mask)
    if candidates.size < 3: raise ValueError("search interval contains fewer than three FFT bins")
    window = np.hanning(fft_size)
    bin_width = sample_rate_hz / fft_size
    points, previous = [], None
    for start in range(0, x.size - fft_size + 1, hop):
        frame = x[start:start + fft_size]
        power = np.abs(np.fft.fftshift(np.fft.fft(frame * window))) ** 2
        allowed = candidates
        if previous is not None and max_step_hz is not None:
            local = candidates[np.abs(frequencies[candidates] - previous) <= max_step_hz]
            if local.size >= 3: allowed = local
        peak = int(allowed[np.argmax(power[allowed])])
        delta = 0.0
        if 0 < peak < fft_size - 1:
            a, b, c = np.log(power[peak - 1:peak + 2] + np.finfo(float).tiny)
            denominator = a - 2 * b + c
            if denominator: delta = float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))
        frequency = frequencies[peak] + delta * bin_width
        noise = float(np.median(power[candidates])) + np.finfo(float).tiny
        snr_db = float(10 * np.log10(power[peak] / noise))
        uncertainty = float(bin_width / max(1.0, np.sqrt(power[peak] / noise)))
        flags = []
        if snr_db < min_snr_db: flags.append("low_snr")
        if peak in (candidates[0], candidates[-1]): flags.append("search_edge")
        # Pluto/AD9361 samples use floating containers with 12-bit ADC counts;
        # synthetic normalized signals remain far below this hardware limit.
        if np.max(np.maximum(np.abs(frame.real), np.abs(frame.imag))) >= 2047:
            flags.append("possible_clipping")
        points.append(TrackPoint((start + fft_size / 2) / sample_rate_hz, frequency, uncertainty, snr_db, tuple(flags)))
        previous = frequency
    return FrequencyTrack(tuple(points), fft_size, hop)
