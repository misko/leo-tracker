from __future__ import annotations

from collections.abc import Callable
import numpy as np


def _signal(frequency_hz: np.ndarray, sample_rate_hz: float, amplitude: float,
            noise_std: float, seed: int | None) -> np.ndarray:
    phase = 2 * np.pi * np.cumsum(frequency_hz) / sample_rate_hz
    result = amplitude * np.exp(1j * phase)
    if noise_std:
        rng = np.random.default_rng(seed)
        result += noise_std / np.sqrt(2) * (rng.standard_normal(result.size) + 1j * rng.standard_normal(result.size))
    return result.astype(np.complex64)


def tone(frequency_hz: float, sample_rate_hz: float, duration_s: float, *,
         amplitude: float = 1.0, noise_std: float = 0.0, seed: int | None = None) -> np.ndarray:
    n = round(duration_s * sample_rate_hz)
    return _signal(np.full(n, frequency_hz), sample_rate_hz, amplitude, noise_std, seed)


def linear_chirp(start_hz: float, stop_hz: float, sample_rate_hz: float, duration_s: float, **kwargs) -> np.ndarray:
    return doppler_signal(lambda t: start_hz + (stop_hz - start_hz) * t / duration_s,
                          sample_rate_hz, duration_s, **kwargs)


def doppler_signal(frequency: Callable[[np.ndarray], np.ndarray] | np.ndarray,
                   sample_rate_hz: float, duration_s: float, *, amplitude: float = 1.0,
                   noise_std: float = 0.0, seed: int | None = None) -> np.ndarray:
    n = round(duration_s * sample_rate_hz)
    times = np.arange(n) / sample_rate_hz
    frequencies = np.asarray(frequency(times) if callable(frequency) else frequency, dtype=float)
    if frequencies.shape != (n,): raise ValueError("frequency must provide one value per sample")
    if np.any(np.abs(frequencies) >= sample_rate_hz / 2): raise ValueError("frequency exceeds Nyquist interval")
    return _signal(frequencies, sample_rate_hz, amplitude, noise_std, seed)
