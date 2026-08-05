"""Exact published Starlink synchronization and pilot templates.

The PSS is from Humphreys et al., IEEE TAES 2023, equations 35--37.
"""
from __future__ import annotations

import numpy as np
from fractions import Fraction
from scipy.signal import fftconvolve, resample_poly

from .channels import starlink_edge_pilot_offset_hz
from .structure import STARLINK_FRAME_DURATION_S

PSS_HEX = "C1B5D191024D3DC3F8EC52FAA16F3958"


def pss_time_samples() -> np.ndarray:
    """Return the exact 1056 complex PSS samples at the native 240 MHz rate."""
    encoded = int(PSS_HEX, 16)
    result = np.empty(1056, np.complex64)
    for output_index, k in enumerate(range(-32, 1024)):
        position = k % 128
        cumulative = sum(2 * ((encoded >> bit) & 1) - 1
                         for bit in range(position + 1))
        phase_pi = (1.0 if k < 128 else 0.0) - .25 - .5 * cumulative
        result[output_index] = np.exp(1j * np.pi * phase_pi)
    return result


def pss_subsequence_phase_states() -> np.ndarray:
    """Return q in p=exp(j*pi*(1/4+q/2)) for the 128-sample subsequence."""
    subsequence = pss_time_samples()[32 + 128:32 + 256]
    reference = np.exp(1j * np.pi / 4)
    return np.rint(np.angle(subsequence / reference) / (np.pi / 2)).astype(int) % 4


def pss_subband_samples(sample_rate_hz: float, edge: str = "lower") -> np.ndarray:
    """Return the PSS portion visible in a narrow capture of an edge-pilot band."""
    if sample_rate_hz <= 0 or sample_rate_hz > 240_000_000:
        raise ValueError("sample rate must be in (0, 240 MHz]")
    native = pss_time_samples().astype(np.complex128)
    time_s = np.arange(native.size) / 240_000_000
    translated = native * np.exp(-2j * np.pi * starlink_edge_pilot_offset_hz(edge) * time_s)
    ratio = Fraction(sample_rate_hz / 240_000_000).limit_denominator(100_000)
    result = resample_poly(translated, ratio.numerator, ratio.denominator)
    result = np.asarray(result, np.complex64)
    norm = np.linalg.norm(result)
    return result if norm == 0 else result / norm


def acquire_pss_epoch(samples: np.ndarray, sample_rate_hz: float, *, edge: str = "lower") -> dict:
    """Noncoherently fold exact-PSS match power at the 750 Hz frame cadence."""
    values = np.asarray(samples, np.complex64)
    template = pss_subband_samples(sample_rate_hz, edge)
    period = sample_rate_hz * STARLINK_FRAME_DURATION_S
    if values.ndim != 1 or values.size < round(4 * period):
        raise ValueError("at least four frames of one-dimensional samples are required")
    correlation = fftconvolve(values, np.conj(template[::-1]), mode="valid")
    energy = fftconvolve(np.abs(values) ** 2, np.ones(template.size), mode="valid")
    match_power = np.abs(correlation) ** 2 / np.maximum(energy, 1e-20)
    epoch_count = round(period)
    folded = np.zeros(epoch_count)
    support = np.zeros(epoch_count, int)
    for epoch in range(epoch_count):
        indexes = np.rint(epoch + np.arange(np.ceil((match_power.size-epoch)/period)) * period).astype(int)
        indexes = indexes[indexes < match_power.size]
        if indexes.size:
            folded[epoch] = float(np.mean(match_power[indexes]))
            support[epoch] = indexes.size
    best = int(np.argmax(folded))
    median = float(np.median(folded))
    return {"schema": "leo-tracker.starlink-pss-epoch/v1", "edge": edge,
            "epoch_sample": best, "epoch_s": best / sample_rate_hz,
            "frame_period_samples": period, "folded_score": float(folded[best]),
            "folded_median": median,
            "peak_to_median": float(folded[best] / max(median, 1e-20)),
            "frame_support": int(support[best]), "template_samples": int(template.size)}
