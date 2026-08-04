"""Waveform-agnostic broadband envelope, edge and texture trackers."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .controls import safe_correlation
from .models import TrackCandidate
from .observation import TrackingObservation


def _fit_candidate(tracker: str, receiver: int, times: np.ndarray, path: np.ndarray,
                   low: float, high: float, score: float, *, supporting: int = 1,
                   qualified: bool = False, warnings=(), diagnostics=None):
    centered = times-np.mean(times)
    order = 2 if len(times) >= 5 else 1
    coefficients = np.polyfit(centered, path, order)
    slope = coefficients[-2]
    curvature = None if order == 1 else 2*coefficients[0]
    return TrackCandidate(tracker, receiver, float(times[0]), float(times[-1]),
        tuple(times.tolist()), tuple(path.tolist()), float(slope),
        None if curvature is None else float(curvature), float(low), float(high),
        supporting, float(score), qualified=qualified, warnings=tuple(warnings),
        diagnostics=diagnostics or {})


def detect_activity_windows(observation: TrackingObservation, *, threshold_db: float = .12,
                            minimum_duration_s: float = 2,
                            padding_s: float = 1) -> list[tuple[float, float]]:
    residual = observation.spectra_db-np.median(
        observation.spectra_db, axis=1, keepdims=True)
    activity = np.median(np.abs(residual), axis=(0, 2)) >= threshold_db
    transitions = np.diff(np.r_[False, activity, False].astype(int))
    starts, stops = np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)-1
    result = []
    for start, stop in zip(starts, stops, strict=True):
        if observation.time_s[stop]-observation.time_s[start] >= minimum_duration_s:
            result.append((max(float(observation.time_s[0]),
                               float(observation.time_s[start]-padding_s)),
                           min(float(observation.time_s[-1]),
                               float(observation.time_s[stop]+padding_s))))
    return result


def track_envelope_and_edges(observation: TrackingObservation,
                             windows: list[tuple[float, float]], *,
                             threshold_db: float = .12,
                             minimum_width_hz: float = 100_000) -> list[TrackCandidate]:
    baseline = np.median(observation.spectra_db, axis=1, keepdims=True)
    residual = observation.spectra_db-baseline
    candidates = []
    for start, stop in windows:
        rows = np.flatnonzero((observation.time_s >= start)&(observation.time_s <= stop))
        if rows.size < 4:
            continue
        times = observation.time_s[rows]
        for receiver in range(residual.shape[0]):
            centers, lowers, uppers, strengths = [], [], [], []
            for row in rows:
                weights = np.maximum(np.abs(residual[receiver, row])-threshold_db/2, 0)
                active = np.flatnonzero(weights >= threshold_db/2)
                if (active.size < 2 or
                        observation.frequency_hz[active[-1]]-
                        observation.frequency_hz[active[0]] < minimum_width_hz):
                    centers.append(np.nan); lowers.append(np.nan); uppers.append(np.nan)
                    strengths.append(0); continue
                centers.append(float(np.average(observation.frequency_hz, weights=weights)))
                lowers.append(float(observation.frequency_hz[active[0]]))
                uppers.append(float(observation.frequency_hz[active[-1]]))
                strengths.append(float(np.mean(np.abs(residual[receiver, row, active]))))
            centers, lowers, uppers = map(np.asarray, (centers, lowers, uppers))
            finite = np.isfinite(centers)
            if finite.sum() < 4:
                continue
            width = float(np.nanmedian(uppers-lowers))
            finite_times = times[finite]
            lower_values, upper_values = lowers[finite], uppers[finite]
            center_values = centers[finite]
            lower_slope = float(np.polyfit(finite_times-np.mean(finite_times),
                                           lower_values, 1)[0])
            upper_slope = float(np.polyfit(finite_times-np.mean(finite_times),
                                           upper_values, 1)[0])
            slope_difference = abs(lower_slope-upper_slope)
            lower_fit = np.polyval(np.polyfit(finite_times, lower_values, 1), finite_times)
            upper_fit = np.polyval(np.polyfit(finite_times, upper_values, 1), finite_times)
            edge_linearity_rms_bins = float(np.sqrt(np.mean(np.r_[
                lower_values-lower_fit, upper_values-upper_fit]**2)) /
                observation.bin_width_hz)
            widths = upper_values-lower_values
            width_mad_fraction = float(1.4826*np.median(abs(widths-np.median(widths))) /
                                       max(width, observation.bin_width_hz))
            motion_bins = (min(np.ptp(lower_values), np.ptp(upper_values)) /
                           observation.bin_width_hz)
            edge_consistent = bool(motion_bins >= 3 and slope_difference <= max(
                1_000, .2*max(abs(lower_slope), abs(upper_slope))) and
                width_mad_fraction <= .2 and edge_linearity_rms_bins <= 2.5)
            controls = {"median_width_hz": width,
                "lower_drift_hz_s": lower_slope, "upper_drift_hz_s": upper_slope,
                "edge_drift_difference_hz_s": slope_difference,
                "width_mad_fraction": width_mad_fraction,
                "edge_linearity_rms_bins": edge_linearity_rms_bins,
                "motion_bins": float(motion_bins)}
            warnings = []
            if motion_bins < 3: warnings.append("edge motion below three bins")
            if slope_difference > max(1_000, .2*max(abs(lower_slope), abs(upper_slope))):
                warnings.append("lower and upper edges do not share common motion")
            if width_mad_fraction > .2: warnings.append("bandwidth is not stable")
            if edge_linearity_rms_bins > 2.5:
                warnings.append("edge motion is not a continuous linear translation")
            envelope = _fit_candidate("broadband-envelope/v1", receiver, finite_times,
                center_values, float(np.nanmin(lowers)), float(np.nanmax(uppers)),
                float(np.mean(np.asarray(strengths)[finite])), qualified=False,
                warnings=tuple(warnings)+(
                    "centroid remains sensitive to subchannel power redistribution",),
                diagnostics=controls)
            candidates.append(envelope)
            lower = _fit_candidate("broadband-lower-edge/v1", receiver, times[finite],
                lowers[finite], float(np.nanmin(lowers)), float(np.nanmax(uppers)),
                envelope.signal_score, qualified=edge_consistent, warnings=warnings,
                diagnostics=controls)
            upper = _fit_candidate("broadband-upper-edge/v1", receiver, times[finite],
                uppers[finite], float(np.nanmin(lowers)), float(np.nanmax(uppers)),
                envelope.signal_score, qualified=edge_consistent, warnings=warnings,
                diagnostics=controls)
            candidates.extend((lower, upper))
    return candidates


def track_spectral_translation(observation: TrackingObservation,
                               windows: list[tuple[float, float]], *,
                               maximum_step_bins: int = 12,
                               minimum_correlation: float = .25) -> list[TrackCandidate]:
    candidates = []
    for start, stop in windows:
        rows = np.flatnonzero((observation.time_s >= start)&(observation.time_s <= stop))
        if rows.size < 4:
            continue
        times = observation.time_s[rows]
        for receiver in range(observation.spectra_db.shape[0]):
            values = observation.spectra_db[receiver, rows]
            textures = values-gaussian_filter1d(values, 20, axis=1, mode="nearest")
            cumulative = [0]; correlations = []
            for previous, current in zip(textures[:-1], textures[1:], strict=True):
                scores = []
                for shift in range(-maximum_step_bins, maximum_step_bins+1):
                    if shift < 0: first, second = previous[-shift:], current[:shift]
                    elif shift > 0: first, second = previous[:-shift], current[shift:]
                    else: first, second = previous, current
                    scores.append(safe_correlation(first, second))
                best = int(np.argmax(scores))-maximum_step_bins
                cumulative.append(cumulative[-1]+best); correlations.append(max(scores))
            path = np.asarray(cumulative, float)*observation.bin_width_hz
            median_correlation = float(np.median(correlations))
            movement_bins = int(np.ptp(cumulative))
            qualified = median_correlation >= minimum_correlation and movement_bins >= 3
            warnings = []
            if median_correlation < minimum_correlation: warnings.append("internal texture is not stable")
            if movement_bins < 3: warnings.append("translation below three bins")
            candidates.append(_fit_candidate("spectral-texture-translation/v1", receiver,
                times, path, float(observation.frequency_hz[0]),
                float(observation.frequency_hz[-1]), median_correlation,
                qualified=qualified, warnings=warnings, diagnostics={
                    "median_adjacent_profile_correlation": median_correlation,
                    "movement_bins": movement_bins,
                    "path_is_relative": True}))
    return candidates


def consensus_pilot_tracks(candidates: list[TrackCandidate], *,
                           slope_tolerance_hz_s: float = 1_000,
                           minimum_support: int = 3) -> list[TrackCandidate]:
    """Cluster independent ridge detections into common-motion populations."""
    ridges = [item for item in candidates if item.tracker.startswith(
        ("dedoppler", "viterbi")) and item.qualified]
    result = []
    for receiver in (0, 1):
        remaining = [item for item in ridges if item.receiver == receiver]
        while remaining:
            seed = max(remaining, key=lambda item: item.signal_score)
            members = [item for item in remaining
                if abs(item.drift_hz_s-seed.drift_hz_s) <= slope_tolerance_hz_s and
                min(item.stop_time_s, seed.stop_time_s) >=
                    max(item.start_time_s, seed.start_time_s)]
            remaining = [item for item in remaining if item not in members]
            if len(members) < minimum_support:
                continue
            drift = float(np.median([item.drift_hz_s for item in members]))
            start = max(item.start_time_s for item in members)
            stop = min(item.stop_time_s for item in members)
            if stop <= start:
                continue
            times = np.linspace(start, stop, 16)
            reference = float(np.median([np.median(item.frequency_hz) for item in members]))
            path = reference+drift*(times-np.mean(times))
            scatter = float(1.4826*np.median(abs(
                np.asarray([item.drift_hz_s for item in members])-drift)))
            result.append(_fit_candidate("multi-pilot-consensus/v1", receiver, times,
                path, min(item.frequency_low_hz for item in members),
                max(item.frequency_high_hz for item in members),
                float(np.mean([item.signal_score for item in members])),
                supporting=len(members), qualified=True,
                diagnostics={"slope_mad_hz_s": scatter,
                    "member_trackers": [item.tracker for item in members]}))
    return result
