"""Waveform-agnostic, TLE-independent dual-receiver carrier tracking."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Sequence

import numpy as np
from scipy.signal import find_peaks

from .blind_comb import _integrate, _safe_corr, _viterbi
from .events import residual_waterfall


SCHEMA = "leo-tracker.blind-carrier-search/v1"


def _cross_validated_track(values: np.ndarray, times: np.ndarray, frequencies: np.ndarray,
                           max_step: int, threshold_db: float):
    fit_rows = np.arange(0, values.shape[0], 2)
    test_rows = np.arange(1, values.shape[0], 2)
    if min(fit_rows.size, test_rows.size) < 4:
        raise ValueError("carrier window is too short for alternating-time validation")
    fit_path = _viterbi(values[fit_rows], max_step*2, .01)
    predicted = np.rint(np.interp(test_rows, fit_rows, fit_path)).astype(int)
    predicted = np.clip(predicted, 0, values.shape[1]-1)
    heldout = values[test_rows, predicted]
    stationary_fit = int(np.argmax(np.mean(values[fit_rows], axis=0)))
    stationary_heldout = values[test_rows, stationary_fit]
    full_rows = np.arange(values.shape[0])
    full_path = np.rint(np.interp(full_rows, fit_rows, fit_path)).astype(int)
    path_hz = frequencies[full_path]
    slope = float(np.polyfit(times-np.mean(times), path_hz, 1)[0])
    return {"path": full_path, "path_hz": path_hz, "heldout_trace": heldout,
        "heldout_score_db": float(np.mean(heldout)),
        "heldout_activity_fraction": float(np.mean(heldout > threshold_db)),
        "stationary_heldout_score_db": float(np.mean(stationary_heldout)),
        "stationary_improvement_db": float(np.mean(heldout)-np.mean(stationary_heldout)),
        "fitted_drift_hz_s": slope, "net_shift_hz": float(path_hz[-1]-path_hz[0]),
        "frequency_span_hz": float(np.ptp(path_hz))}


def _profile(values: np.ndarray, rows: np.ndarray, path: np.ndarray,
             frequencies: np.ndarray, prominence_db: float):
    reference = int(np.median(path))
    aligned = np.asarray([np.roll(values[row], reference-int(bin_index))
                          for row, bin_index in zip(rows, path, strict=True)])
    profile = np.mean(aligned, axis=0)
    peaks, properties = find_peaks(profile, prominence=prominence_db, distance=2)
    order = np.argsort(properties["prominences"])[::-1][:32]
    peaks = peaks[order]; prominences = properties["prominences"][order]
    ordered = np.argsort(peaks); peaks = peaks[ordered]; prominences = prominences[ordered]
    peak_hz = frequencies[peaks]
    gaps = np.diff(peak_hz)
    return {"reference_offset_hz": float(frequencies[reference]),
        "peak_count": int(peaks.size), "peak_offsets_from_track_hz":
            (peak_hz-frequencies[reference]).tolist(),
        "peak_prominence_db": prominences.tolist(),
        "adjacent_peak_spacings_hz": gaps.tolist()}


def search_blind_carriers(
    spectra_db: np.ndarray, utc_ns: Sequence[int], frequency_offsets_hz: Sequence[float],
    *, nominal_offset_hz: float, search_half_width_hz: float = 1_000_000,
    integration_s: float = 1.0, window_s: float = 30.0, step_s: float = 5.0,
    max_drift_hz_s: float = 5_000.0, threshold_db: float = .10,
    broadband_threshold_db: float = .35, broadband_frequency_fraction: float = .2,
    maximum_broadband_row_fraction: float = .2, maximum_slope_difference_hz_s: float = 500,
    common_mode_threshold_db: float = .10, maximum_common_mode_db: float = .20,
    permutations: int = 99, seed: int = 0,
    profile_prominence_db: float = .03,
) -> dict:
    spectra = np.asarray(spectra_db, float); utc = np.asarray(utc_ns, np.int64)
    frequencies = np.asarray(frequency_offsets_hz, float)
    if spectra.ndim != 3 or spectra.shape[0] != 2 or spectra.shape[1] != utc.size:
        raise ValueError("blind carrier search requires two receivers and matching timestamps")
    if frequencies.size != spectra.shape[2] or np.any(np.diff(frequencies) <= 0):
        raise ValueError("frequency axis must be increasing and match the waterfall")
    if permutations < 0: raise ValueError("permutations cannot be negative")
    seconds = utc/1e9; integrated, times = _integrate(spectra, seconds, integration_s)
    raw_residual = np.asarray([residual_waterfall(receiver) for receiver in integrated])
    residual = raw_residual-np.median(raw_residual, axis=2, keepdims=True)
    selected = np.flatnonzero(abs(frequencies-nominal_offset_hz) <= search_half_width_hz)
    if selected.size < 32: raise ValueError("nominal carrier region contains too few bins")
    lo, hi = int(selected[0]), int(selected[-1])+1
    local = residual[:, :, lo:hi]; raw_local = raw_residual[:, :, lo:hi]
    local_frequencies = frequencies[lo:hi]
    bin_hz = float(np.median(np.diff(frequencies))); cadence = float(np.median(np.diff(times)))
    max_step = max(1, int(np.ceil(max_drift_hz_s*cadence/bin_hz)))
    starts = np.arange(times[0], max(times[0]+step_s, times[-1]-window_s+step_s), step_s)
    starts = np.unique(np.append(starts, max(times[0], times[-1]-window_s)))
    reports = []
    for start in starts:
        rows = np.flatnonzero((times >= start) & (times <= start+window_s))
        if rows.size < 8: continue
        receiver_reports, paths, traces = [], [], []
        threshold_broadband = float(np.mean(np.all(np.mean(
            np.abs(raw_local[:, rows]) > broadband_threshold_db, axis=2)
            >= broadband_frequency_fraction, axis=0)))
        common_mode_db = np.median(raw_residual[:, rows], axis=(0, 2))
        common_mode_fraction = float(np.mean(abs(common_mode_db) > common_mode_threshold_db))
        broadband = max(threshold_broadband, common_mode_fraction)
        for receiver in range(2):
            track = _cross_validated_track(local[receiver, rows], times[rows],
                                           local_frequencies, max_step, threshold_db)
            path = track.pop("path"); path_hz = track.pop("path_hz")
            trace = track.pop("heldout_trace")
            receiver_reports.append({"receiver": receiver,
                "start_offset_hz": float(path_hz[0]), "stop_offset_hz": float(path_hz[-1]),
                **track})
            paths.append(path); traces.append(trace)
        centered = [local_frequencies[path]-np.median(local_frequencies[path]) for path in paths]
        path_corr = _safe_corr(centered[0], centered[1]); trace_corr = _safe_corr(traces[0], traces[1])
        slope_difference = abs(receiver_reports[0]["fitted_drift_hz_s"]-
                               receiver_reports[1]["fitted_drift_hz_s"])
        motion_bins = min(int(round(np.ptp(local_frequencies[path])/bin_hz)) for path in paths)
        reasons = []
        if motion_bins < 3: reasons.append("measured motion is less than three bins")
        if min(item["stationary_improvement_db"] for item in receiver_reports) < .03:
            reasons.append("held-out moving carrier does not beat stationary control")
        if path_corr < .6: reasons.append("receiver carrier paths are insufficiently correlated")
        if slope_difference > maximum_slope_difference_hz_s:
            reasons.append("receiver carrier drift-rate disagreement exceeds limit")
        if broadband >= maximum_broadband_row_fraction:
            reasons.append("window is dominated by common broadband activity")
        if float(np.max(abs(common_mode_db))) > maximum_common_mode_db:
            reasons.append("window contains a strong common-mode power transition")
        reports.append({"window_start_utc": datetime.fromtimestamp(times[rows[0]], timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_stop_utc": datetime.fromtimestamp(times[rows[-1]], timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_duration_s": float(times[rows[-1]]-times[rows[0]]),
            "time_s": (times[rows]-seconds[0]).tolist(),
            "paths_hz": [local_frequencies[path].tolist() for path in paths],
            "receivers": receiver_reports, "path_correlation": path_corr,
            "heldout_trace_correlation": trace_corr,
            "drift_rate_difference_hz_s": float(slope_difference), "motion_bins": motion_bins,
            "common_broadband_row_fraction": broadband,
            "threshold_broadband_row_fraction": threshold_broadband,
            "common_mode_row_fraction": common_mode_fraction,
            "maximum_absolute_common_mode_db": float(np.max(abs(common_mode_db))),
            "qualified": not reasons, "rejection_reasons": reasons,
            "_rows": rows, "_paths": paths})
    reports.sort(key=lambda item: (item["qualified"],
        min(receiver["stationary_improvement_db"] for receiver in item["receivers"]),
        item["path_correlation"]), reverse=True)
    controls = None
    if reports:
        best = reports[0]; rng = np.random.default_rng(seed); null_scores=[]
        observed = min(item["heldout_score_db"] for item in best["receivers"])
        rows = best["_rows"]
        for _ in range(permutations):
            shuffled = rng.permutation(rows); scores=[]
            for receiver in range(2):
                track = _cross_validated_track(local[receiver, shuffled], np.arange(rows.size),
                    local_frequencies, max_step, threshold_db)
                scores.append(track["heldout_score_db"])
            null_scores.append(min(scores))
        probability = None if not permutations else ((1+sum(score >= observed for score in null_scores)) /
                                                       (permutations+1))
        best["empirical_false_alarm_probability"] = probability
        if probability is not None and probability > .1:
            best["rejection_reasons"].append("carrier does not beat time-scrambled controls")
        for candidate in reports[1:]:
            if candidate["qualified"]:
                candidate["qualified"] = False
                candidate["rejection_reasons"].append(
                    "only the strongest window receives false-alarm calibration")
        best["qualified"] = not best["rejection_reasons"]
        best["motion_compensated_profiles"] = [_profile(local[receiver], rows,
            best["_paths"][receiver], local_frequencies, profile_prominence_db)
            for receiver in range(2)]
        controls = {"permutations": permutations, "empirical_false_alarm_probability": probability,
            "observed_joint_heldout_score_db": observed,
            "null_score_p95_db": None if not null_scores else float(np.percentile(null_scores, 95))}
    for report in reports:
        report.pop("_rows", None); report.pop("_paths", None)
    return {"schema": SCHEMA, "method": "TLE- and waveform-independent alternating-time carrier search",
        "configuration": {"nominal_offset_hz": nominal_offset_hz,
            "search_half_width_hz": search_half_width_hz, "integration_s": integration_s,
            "window_s": window_s, "step_s": step_s, "max_drift_hz_s": max_drift_hz_s,
            "maximum_slope_difference_hz_s": maximum_slope_difference_hz_s,
            "common_mode_threshold_db": common_mode_threshold_db,
            "maximum_common_mode_db": maximum_common_mode_db,
            "cross_validation": "even time rows fit path; odd time rows score path"},
        "bin_width_hz": bin_hz, "window_count": len(reports),
        "qualified_count": sum(item["qualified"] for item in reports),
        "false_alarm_controls": controls, "candidates": reports}
