"""Physically labelled Doppler fits over event-supported intervals only."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import least_squares

from .physics import doppler_radial_acceleration_m_s2


@dataclass(frozen=True)
class SupportedDopplerFit:
    start_time_s: float
    stop_time_s: float
    points: int
    polynomial_order: int
    coefficients_hz: tuple[float, ...]
    reference_time_s: float
    drift_at_reference_hz_s: float
    radial_acceleration_m_s2: float
    residual_rms_hz: float
    drift_uncertainty_hz_s: float
    fitted_hz: tuple[float, ...]
    channel_hop_indices: tuple[int, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def channel_hop_indices(frequencies_hz: Sequence[float], *, threshold_hz: float) -> tuple[int, ...]:
    if threshold_hz <= 0:
        raise ValueError("channel-hop threshold must be positive")
    values = np.asarray(frequencies_hz, float)
    return tuple((np.flatnonzero(np.abs(np.diff(values)) > threshold_hz) + 1).tolist())


def fit_supported_doppler(times_s: Sequence[float], frequencies_hz: Sequence[float], *,
                          carrier_hz: float, uncertainty_hz: Sequence[float] | None = None,
                          order: int = 2, hop_threshold_hz: float = 250_000) -> SupportedDopplerFit:
    times = np.asarray(times_s, float); frequencies = np.asarray(frequencies_hz, float)
    if times.ndim != 1 or times.size < max(5, order + 2) or frequencies.shape != times.shape:
        raise ValueError("supported Doppler fitting needs equal one-dimensional arrays and at least five points")
    if order not in (1, 2):
        raise ValueError("Doppler polynomial order must be one or two")
    if np.any(np.diff(times) <= 0) or not np.all(np.isfinite(times + frequencies)):
        raise ValueError("times must be finite and strictly increasing")
    hops = channel_hop_indices(frequencies, threshold_hz=hop_threshold_hz)
    if hops:
        raise ValueError(f"channel hop detected at event indexes {hops}; fit segments separately")
    reference = float(np.mean(times)); centered = times - reference
    design = np.column_stack([centered**power for power in range(order + 1)])
    if uncertainty_hz is None:
        uncertainty = np.ones_like(times)
    else:
        uncertainty = np.asarray(uncertainty_hz, float)
        if uncertainty.shape != times.shape or np.any(uncertainty <= 0):
            raise ValueError("uncertainty must be positive and match the track")
    initial, *_ = np.linalg.lstsq(design / uncertainty[:, None], frequencies / uncertainty, rcond=None)
    fit = least_squares(lambda coefficients: (design @ coefficients - frequencies) / uncertainty,
                        initial, loss="soft_l1")
    fitted = design @ fit.x; residual = frequencies - fitted
    dof = max(1, times.size - len(fit.x))
    weighted_design = design / uncertainty[:, None]
    covariance = np.linalg.pinv(weighted_design.T @ weighted_design) * float(np.sum(
        (residual / uncertainty)**2) / dof)
    drift_uncertainty = float(np.sqrt(max(0, covariance[1, 1])))
    drift = float(fit.x[1])
    return SupportedDopplerFit(
        float(times[0]), float(times[-1]), int(times.size), order,
        tuple(float(value) for value in fit.x), reference, drift,
        doppler_radial_acceleration_m_s2(drift, carrier_hz),
        float(np.sqrt(np.mean(residual**2))), drift_uncertainty,
        tuple(float(value) for value in fitted), hops)


def fit_doppler_segments(times_s: Sequence[float], frequencies_hz: Sequence[float], *,
                         carrier_hz: float, order: int = 2,
                         hop_threshold_hz: float = 250_000,
                         minimum_points: int = 5) -> list[SupportedDopplerFit]:
    times = np.asarray(times_s, float); frequencies = np.asarray(frequencies_hz, float)
    hops = channel_hop_indices(frequencies, threshold_hz=hop_threshold_hz)
    boundaries = (0,) + hops + (len(times),)
    fits = []
    for start, stop in zip(boundaries[:-1], boundaries[1:]):
        if stop - start >= minimum_points:
            fits.append(fit_supported_doppler(times[start:stop], frequencies[start:stop],
                carrier_hz=carrier_hz, order=order, hop_threshold_hz=hop_threshold_hz))
    return fits
