"""Small, explicit nuisance model for initial known-position experiments."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class DopplerFit:
    frequency_offset_hz: float
    frequency_drift_hz_s: float
    residual_rms_hz: float
    residuals_hz: np.ndarray


def fit_doppler_track(
    times_s: npt.ArrayLike,
    observed_hz: npt.ArrayLike,
    predicted_doppler_hz: npt.ArrayLike,
    uncertainty_hz: npt.ArrayLike | None = None,
) -> DopplerFit:
    """Fit constant receiver offset and linear drift around a Doppler prediction.

    This intentionally does not fit a flexible curve: pass-shape evidence must
    come from orbital geometry rather than being absorbed by nuisance terms.
    """

    times = np.asarray(times_s, dtype=float)
    observed = np.asarray(observed_hz, dtype=float)
    predicted = np.asarray(predicted_doppler_hz, dtype=float)
    if times.ndim != 1 or len(times) < 3:
        raise ValueError("at least three one-dimensional samples are required")
    if observed.shape != times.shape or predicted.shape != times.shape:
        raise ValueError("times, observed, and predicted must have equal shapes")
    if not np.all(np.isfinite(times)) or not np.all(np.isfinite(observed + predicted)):
        raise ValueError("inputs must be finite")

    centered_time = times - np.mean(times)
    design = np.column_stack((np.ones_like(times), centered_time))
    target = observed - predicted
    if uncertainty_hz is None:
        weighted_design, weighted_target = design, target
    else:
        uncertainty = np.asarray(uncertainty_hz, dtype=float)
        if uncertainty.shape != times.shape or np.any(uncertainty <= 0):
            raise ValueError("uncertainty must be positive and match sample shape")
        weights = 1.0 / uncertainty
        weighted_design = design * weights[:, None]
        weighted_target = target * weights

    coefficients, *_ = np.linalg.lstsq(weighted_design, weighted_target, rcond=None)
    residuals = target - design @ coefficients
    return DopplerFit(
        frequency_offset_hz=float(coefficients[0]),
        frequency_drift_hz_s=float(coefficients[1]),
        residual_rms_hz=float(np.sqrt(np.mean(residuals**2))),
        residuals_hz=residuals,
    )
