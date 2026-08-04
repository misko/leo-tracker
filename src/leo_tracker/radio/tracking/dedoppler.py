"""Incoherent shift-and-sum search for unknown linearly drifting carriers.

This is the transparent reference implementation.  It follows the same basic
principle as tree de-Doppler searches, but deliberately uses a direct slope
bank so its numerical behavior is easy to test before optimization.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import find_peaks

from .models import TrackCandidate
from .observation import TrackingObservation


def integrate_observation(observation: TrackingObservation,
                          integration_s: float) -> tuple[np.ndarray, np.ndarray]:
    groups = np.floor((observation.time_s-observation.time_s[0])/integration_s).astype(int)
    spectra, times = [], []
    for group in np.unique(groups):
        selected = groups == group
        if selected.sum() < 2:
            continue
        power = np.power(10.0, observation.spectra_db[:, selected]/10.0)
        spectra.append(10*np.log10(np.mean(power, axis=1)+np.finfo(float).tiny))
        times.append(float(np.mean(observation.time_s[selected])))
    if len(spectra) < 4:
        raise ValueError("observation is too short for requested integration")
    return np.stack(spectra, axis=1), np.asarray(times)


def _window_starts(times: np.ndarray, window_s: float, step_s: float) -> np.ndarray:
    if times[-1]-times[0] <= window_s:
        return np.asarray([times[0]])
    starts = np.arange(times[0], times[-1]-window_s+step_s, step_s)
    return np.unique(np.append(starts, times[-1]-window_s))


def search_dedoppler(observation: TrackingObservation, *, integration_s: float = .5,
                     window_s: float = 10.0, step_s: float = 5.0,
                     minimum_drift_hz_s: float = -15_000,
                     maximum_drift_hz_s: float = 15_000,
                     drift_step_hz_s: float = 500,
                     prominence_db: float = .08,
                     maximum_candidates_per_receiver: int = 32,
                     maximum_peaks_per_slope: int = 16,
                     false_alarm_permutations: int = 19,
                     seed: int = 0) -> list[TrackCandidate]:
    if not (integration_s > 0 and window_s > 0 and step_s > 0 and drift_step_hz_s > 0):
        raise ValueError("integration, window, step and drift spacing must be positive")
    spectra, times = integrate_observation(observation, integration_s)
    residual = spectra-np.median(spectra, axis=1, keepdims=True)
    residual -= np.median(residual, axis=2, keepdims=True)
    frequencies = observation.frequency_hz
    bin_hz = observation.bin_width_hz
    slopes = np.arange(minimum_drift_hz_s,
        maximum_drift_hz_s+drift_step_hz_s/2, drift_step_hz_s)
    if false_alarm_permutations < 0 or maximum_peaks_per_slope < 1:
        raise ValueError("false-alarm permutations and peak limits are invalid")
    rng = np.random.default_rng(seed)
    candidates: list[TrackCandidate] = []
    for receiver in range(residual.shape[0]):
        receiver_candidates = []
        for start in _window_starts(times, window_s, step_s):
            rows = np.flatnonzero((times >= start) & (times <= start+window_s))
            if rows.size < 4:
                continue
            local_times = times[rows]; centered_time = local_times-local_times[0]
            for polarity, sign in (("positive", 1.0), ("negative", -1.0)):
                values = sign*residual[receiver, rows]
                for slope in slopes:
                    shifts = np.rint(slope*centered_time/bin_hz).astype(int)
                    aligned = np.asarray([np.roll(row, -shift)
                                          for row, shift in zip(values, shifts, strict=True)])
                    # Rolled edge bins contain unrelated data; suppress every
                    # intercept that would leave the original band.
                    valid_low = max(0, -int(shifts.min()))
                    valid_high = min(values.shape[1], values.shape[1]-int(shifts.max()))
                    if valid_high-valid_low < 16:
                        continue
                    train = np.arange(0, rows.size, 2)
                    heldout = np.arange(1, rows.size, 2)
                    if train.size < 2 or heldout.size < 2:
                        continue
                    # Fit intercepts only on alternating training spectra; all
                    # qualification scores below use unseen spectra.
                    profile = np.mean(aligned[train], axis=0)
                    peaks, properties = find_peaks(profile[valid_low:valid_high],
                                                    prominence=prominence_db)
                    peak_indexes = peaks+valid_low
                    prominences = properties["prominences"]
                    if peak_indexes.size > maximum_peaks_per_slope:
                        strongest = np.argpartition(
                            prominences, -maximum_peaks_per_slope)[-maximum_peaks_per_slope:]
                        order = strongest[np.argsort(prominences[strongest])[::-1]]
                        peak_indexes, prominences = peak_indexes[order], prominences[order]
                    null_maxima = []
                    for _ in range(false_alarm_permutations):
                        shuffled = rng.permutation(heldout)
                        permutation_scores = []
                        for peak in peak_indexes:
                            path = peak+shifts
                            score = float(np.mean(values[shuffled, path[heldout]]-
                                                  values[shuffled, peak]))
                            permutation_scores.append(score)
                        null_maxima.append(max(permutation_scores, default=-np.inf))
                    for peak, prominence in zip(peak_indexes, prominences, strict=True):
                        path = peak+shifts
                        trace = values[np.arange(rows.size), path]
                        stationary = values[:, peak]
                        moving_score = float(np.mean(trace[heldout]))
                        stationary_score = float(np.mean(stationary[heldout]))
                        improvement = moving_score-stationary_score
                        false_alarm = (None if not null_maxima else
                            (1+sum(item >= improvement for item in null_maxima)) /
                            (len(null_maxima)+1))
                        movement_bins = int(np.ptp(path))
                        qualified = bool(movement_bins >= 3 and improvement >= .02 and
                                         moving_score >= prominence_db/2 and
                                         (false_alarm is None or false_alarm <= .1))
                        warnings = []
                        if movement_bins < 3: warnings.append("motion below three bins")
                        if improvement < .02: warnings.append("does not beat stationary control")
                        if false_alarm is not None and false_alarm > .1:
                            warnings.append("does not beat time-scrambled control")
                        receiver_candidates.append(TrackCandidate(
                            tracker="dedoppler-linear/v1", receiver=receiver,
                            start_time_s=float(local_times[0]), stop_time_s=float(local_times[-1]),
                            time_s=tuple(local_times.tolist()),
                            frequency_hz=tuple(frequencies[path].tolist()),
                            drift_hz_s=float(slope),
                            frequency_low_hz=float(frequencies[path].min()),
                            frequency_high_hz=float(frequencies[path].max()),
                            signal_score=moving_score,
                            false_alarm_probability=false_alarm, qualified=qualified,
                            warnings=tuple(warnings), diagnostics={
                                "polarity": polarity, "prominence_db": float(prominence),
                                "stationary_score_db": stationary_score,
                                "stationary_improvement_db": improvement,
                                "heldout_trace_db": trace[heldout].tolist(),
                                "heldout_time_s": local_times[heldout].tolist(),
                                "false_alarm_permutations": false_alarm_permutations,
                                "false_alarm_null": "maximum intercept score per permutation",
                                "motion_bins": movement_bins,
                                "bin_width_hz": bin_hz, "integration_s": integration_s}))
        # Non-maximum suppression in time/frequency/drift prevents adjacent
        # slope-bank cells from becoming separate scientific candidates.
        selected = []
        for candidate in sorted(receiver_candidates,
                key=lambda item: (item.qualified, item.signal_score), reverse=True):
            middle_t = (candidate.start_time_s+candidate.stop_time_s)/2
            middle_f = float(np.median(candidate.frequency_hz))
            duplicate = any(abs(middle_t-(other.start_time_s+other.stop_time_s)/2) < step_s and
                abs(middle_f-np.median(other.frequency_hz)) < 4*bin_hz and
                abs(candidate.drift_hz_s-other.drift_hz_s) <= drift_step_hz_s
                for other in selected)
            if not duplicate:
                selected.append(candidate)
            if len(selected) >= maximum_candidates_per_receiver:
                break
        candidates.extend(selected)
    return candidates
