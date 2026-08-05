"""Blind structural evidence for the published 750 Hz Starlink frame."""
from __future__ import annotations

import numpy as np

STARLINK_FRAME_RATE_HZ = 750.0
STARLINK_FRAME_DURATION_S = 1 / STARLINK_FRAME_RATE_HZ


def _lag_correlation(values: np.ndarray, lag: int) -> float:
    if lag <= 0 or lag >= values.size:
        return 0.0
    first, second = values[:-lag], values[lag:]
    first = first - np.mean(first); second = second - np.mean(second)
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return 0.0 if denominator == 0 else float(abs(np.vdot(first, second)) / denominator)


def frame_period_score(samples: np.ndarray, sample_rate_hz: float, *,
                       frame_rate_hz: float = STARLINK_FRAME_RATE_HZ) -> dict:
    """Compare complex autocorrelation at a fractional frame lag with controls."""
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1 or values.size < sample_rate_hz / frame_rate_hz * 4:
        raise ValueError("at least four candidate frame periods are required")
    nominal = sample_rate_hz / frame_rate_hz
    candidate_lags = sorted({int(np.floor(nominal)), int(np.ceil(nominal))})
    scores = {lag: _lag_correlation(values, lag) for lag in candidate_lags}
    best_lag = max(scores, key=scores.get)
    controls = []
    for rate in (675, 700, 725, 775, 800, 825):
        lag = round(sample_rate_hz / rate)
        controls.append(_lag_correlation(values, lag))
    control_median = float(np.median(controls))
    return {"frame_rate_hz": frame_rate_hz, "nominal_lag_samples": nominal,
        "best_lag_samples": best_lag, "best_correlation": scores[best_lag],
        "candidate_correlations": {str(k): v for k, v in scores.items()},
        "control_correlations": controls, "control_median_correlation": control_median,
        "correlation_excess": scores[best_lag] - control_median}


def analyze_frame_period(receivers: np.ndarray, sample_rate_hz: float) -> dict:
    values = np.asarray(receivers, np.complex64)
    if values.ndim == 1: values = values[None, :]
    if values.ndim != 2:
        raise ValueError("samples must be receiver x sample")
    reports = [frame_period_score(row, sample_rate_hz) for row in values]
    correlations = [item["best_correlation"] for item in reports]
    excess = [item["correlation_excess"] for item in reports]
    return {"schema": "leo-tracker.starlink-frame-period/v1", "receivers": reports,
        "minimum_receiver_correlation": min(correlations),
        "minimum_receiver_excess": min(excess),
        "qualified": bool(min(correlations) >= .05 and min(excess) >= .02)}
