"""Continuity-constrained ridge tracking adapter for the common API."""
from __future__ import annotations

import numpy as np

from ..blind_comb import _viterbi
from .dedoppler import integrate_observation
from .models import TrackCandidate
from .observation import TrackingObservation


def search_viterbi_ridges(observation: TrackingObservation, *,
                          windows: list[tuple[float, float]], integration_s: float = .5,
                          maximum_drift_hz_s: float = 15_000,
                          minimum_stationary_improvement_db: float = .02,
                          paths_per_window: int = 3) -> list[TrackCandidate]:
    spectra, times = integrate_observation(observation, integration_s)
    residual = spectra-np.median(spectra, axis=1, keepdims=True)
    residual -= np.median(residual, axis=2, keepdims=True)
    bin_hz = observation.bin_width_hz
    cadence = float(np.median(np.diff(times)))
    max_step = max(1, int(np.ceil(maximum_drift_hz_s*cadence/bin_hz)))
    candidates = []
    for start, stop in windows:
        rows = np.flatnonzero((times >= start)&(times <= stop))
        if rows.size < 4:
            continue
        for receiver in range(residual.shape[0]):
            for polarity, sign in (("positive", 1), ("negative", -1)):
                score = sign*residual[receiver, rows].copy()
                for _ in range(paths_per_window):
                    path = _viterbi(score, max_step, .01)
                    trace = score[np.arange(rows.size), path]
                    stationary = float(np.max(np.mean(score, axis=0)))
                    improvement = float(np.mean(trace)-stationary)
                    path_hz = observation.frequency_hz[path]
                    slope = float(np.polyfit(times[rows]-np.mean(times[rows]), path_hz, 1)[0])
                    movement = int(np.ptp(path))
                    qualified = movement >= 3 and improvement >= minimum_stationary_improvement_db
                    warnings = []
                    if movement < 3: warnings.append("motion below three bins")
                    if improvement < minimum_stationary_improvement_db:
                        warnings.append("does not beat stationary control")
                    candidates.append(TrackCandidate("viterbi-ridge/v1", receiver,
                        float(times[rows[0]]), float(times[rows[-1]]),
                        tuple(times[rows].tolist()), tuple(path_hz.tolist()), slope,
                        frequency_low_hz=float(path_hz.min()),
                        frequency_high_hz=float(path_hz.max()),
                        signal_score=float(np.mean(trace)), qualified=qualified,
                        warnings=tuple(warnings), diagnostics={"polarity": polarity,
                            "stationary_score_db": stationary,
                            "stationary_improvement_db": improvement,
                            "motion_bins": movement}))
                    # Suppress this ridge before requesting another path.
                    for row, column in enumerate(path):
                        score[row, max(0, column-2):column+3] = -np.inf
    return candidates
