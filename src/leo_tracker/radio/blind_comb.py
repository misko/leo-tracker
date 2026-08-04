"""TLE-independent measurement of moving Starlink-like spectral combs."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Sequence

import numpy as np

from .events import residual_waterfall


SCHEMA = "leo-tracker.blind-comb-search/v1"


def _integrate(spectra: np.ndarray, seconds: np.ndarray, integration_s: float):
    groups = np.floor((seconds-seconds[0])/integration_s).astype(int)
    rows, times = [], []
    for group in np.unique(groups):
        selected = groups == group
        if selected.sum() < 2:
            continue
        power = np.power(10.0, spectra[:, selected]/10.0)
        rows.append(10*np.log10(np.mean(power, axis=1)+np.finfo(float).tiny))
        times.append(float(np.mean(seconds[selected])))
    if len(rows) < 8:
        raise ValueError("waterfall is too short for blind comb integration")
    return np.stack(rows, axis=1), np.asarray(times)


def _comb(values: np.ndarray, spacing_hz: float, bin_hz: float,
          teeth: Sequence[int] = tuple(range(-4, 5))):
    tooth_numbers = np.asarray(teeth, int)
    offsets = np.rint(tooth_numbers*spacing_hz/bin_hz).astype(int)
    if np.unique(offsets).size != tooth_numbers.size:
        raise ValueError("frequency resolution cannot resolve requested comb teeth")
    result = np.full_like(values, -np.inf)
    low, high = -int(offsets.min()), int(offsets.max())
    centers = np.arange(low, values.shape[1]-high)
    result[:, centers] = sum(values[:, centers+offset] for offset in offsets)/math.sqrt(len(offsets))
    return result, offsets


def _viterbi(score: np.ndarray, max_step: int, motion_penalty: float):
    accumulated = score[0].astype(float)
    back = np.zeros(score.shape, np.int16)
    for row in range(1, score.shape[0]):
        choices = []
        for shift in range(-max_step, max_step+1):
            shifted = np.roll(accumulated, shift)-motion_penalty*abs(shift)
            if shift < 0: shifted[shift:] = -np.inf
            elif shift > 0: shifted[:shift] = -np.inf
            choices.append(shifted)
        choices = np.asarray(choices)
        selected = np.argmax(choices, axis=0)
        accumulated = score[row]+choices[selected, np.arange(score.shape[1])]
        back[row] = selected-max_step
    path = np.empty(score.shape[0], int); path[-1] = int(np.argmax(accumulated))
    for row in range(score.shape[0]-1, 0, -1):
        path[row-1] = path[row]-int(back[row, path[row]])
    return path


def _safe_corr(first, second):
    value = float(np.corrcoef(first, second)[0, 1])
    return value if math.isfinite(value) else 0.0


def search_blind_comb(
    spectra_db: np.ndarray, utc_ns: Sequence[int], frequency_offsets_hz: Sequence[float],
    *, nominal_offset_hz: float, search_half_width_hz: float = 1_000_000,
    integration_s: float = 1.0, window_s: float = 30.0, step_s: float = 5.0,
    comb_spacing_hz: float = 43_949.5,
    wrong_spacings_hz: Sequence[float] = (35_000.0, 52_000.0),
    max_drift_hz_s: float = 5_000.0, threshold_db: float = .10,
    broadband_threshold_db: float = .35, broadband_frequency_fraction: float = .2,
    maximum_broadband_row_fraction: float = .2,
    permutations: int = 19, seed: int = 0,
) -> dict:
    """Find motion first; no orbit or TLE information is accepted by this API."""
    spectra = np.asarray(spectra_db, float)
    utc = np.asarray(utc_ns, np.int64); frequencies = np.asarray(frequency_offsets_hz, float)
    if spectra.ndim != 3 or spectra.shape[0] != 2 or spectra.shape[1] != utc.size:
        raise ValueError("blind comb search requires two receivers and matching timestamps")
    if frequencies.size != spectra.shape[2] or np.any(np.diff(frequencies) <= 0):
        raise ValueError("frequency axis must be increasing and match the waterfall")
    if min(integration_s, window_s, step_s, search_half_width_hz, max_drift_hz_s) <= 0:
        raise ValueError("search durations, width, and maximum drift must be positive")
    if permutations < 0:
        raise ValueError("permutations cannot be negative")
    seconds = utc/1e9
    integrated, times = _integrate(spectra, seconds, integration_s)
    residual = np.asarray([residual_waterfall(receiver) for receiver in integrated])
    broadband_residual = residual.copy()
    residual -= np.median(residual, axis=2, keepdims=True)
    selected = np.flatnonzero(np.abs(frequencies-nominal_offset_hz) <= search_half_width_hz)
    if selected.size < 32:
        raise ValueError("nominal search region contains too few frequency bins")
    lo, hi = int(selected[0]), int(selected[-1])+1
    local_frequencies = frequencies[lo:hi]
    local = residual[:, :, lo:hi]
    broadband_local = broadband_residual[:, :, lo:hi]
    bin_hz = float(np.median(np.diff(frequencies)))
    cadence = float(np.median(np.diff(times)))
    max_step = max(1, int(np.ceil(max_drift_hz_s*cadence/bin_hz)))
    train_teeth, test_teeth = (-4, -2, 0, 2, 4), (-3, -1, 1, 3)
    correct = [_comb(local[receiver], comb_spacing_hz, bin_hz, train_teeth)[0]
               for receiver in range(2)]
    correct_test = [_comb(local[receiver], comb_spacing_hz, bin_hz, test_teeth)[0]
                    for receiver in range(2)]
    wrong = [[(_comb(local[receiver], spacing, bin_hz, train_teeth)[0],
               _comb(local[receiver], spacing, bin_hz, test_teeth)[0])
              for spacing in wrong_spacings_hz]
             for receiver in range(2)]
    starts = np.arange(times[0], max(times[0]+step_s, times[-1]-window_s+step_s), step_s)
    starts = np.unique(np.append(starts, max(times[0], times[-1]-window_s)))
    reports = []
    for start in starts:
        rows = np.flatnonzero((times >= start) & (times <= start+window_s))
        if rows.size < 8:
            continue
        paths, receiver_reports, traces = [], [], []
        broadband = np.mean(np.all(np.mean(np.abs(broadband_local[:, rows]) > broadband_threshold_db,
                                            axis=2) >= broadband_frequency_fraction, axis=0))
        for receiver in range(2):
            values = correct[receiver][rows]
            path = _viterbi(values, max_step, motion_penalty=.01)
            test_values = correct_test[receiver][rows]
            trace = test_values[np.arange(rows.size), path]
            stationary = float(np.max(np.mean(test_values, axis=0)))
            wrong_scores = []
            for wrong_train, wrong_test in wrong[receiver]:
                wrong_train = wrong_train[rows]; wrong_test = wrong_test[rows]
                wrong_path = _viterbi(wrong_train, max_step, .01)
                wrong_scores.append(float(np.mean(
                    wrong_test[np.arange(rows.size), wrong_path])))
            wrong_score = max(wrong_scores)
            path_hz = local_frequencies[path]
            slope = float(np.polyfit(times[rows]-np.mean(times[rows]), path_hz, 1)[0])
            tooth_offsets = np.rint(np.arange(-4, 5)*comb_spacing_hz/bin_hz).astype(int)
            tooth_scores = [float(np.mean(local[receiver, rows,
                np.clip(path+offset, 0, local.shape[2]-1)])) for offset in tooth_offsets]
            receiver_reports.append({"receiver": receiver,
                "start_offset_hz": float(path_hz[0]), "stop_offset_hz": float(path_hz[-1]),
                "net_shift_hz": float(path_hz[-1]-path_hz[0]),
                "frequency_span_hz": float(np.ptp(path_hz)), "fitted_drift_hz_s": slope,
                "score_db": float(np.mean(trace)),
                "score_semantics": "held-out odd teeth; path fitted with even teeth",
                "stationary_score_db": stationary,
                "stationary_improvement_db": float(np.mean(trace)-stationary),
                "wrong_spacing_score_db": wrong_score,
                "comb_spacing_improvement_db": float(np.mean(trace)-wrong_score),
                "tooth_scores_db": tooth_scores,
                "detected_tooth_count": int(np.sum(np.asarray(tooth_scores) > threshold_db))})
            paths.append(path_hz); traces.append(trace)
        centered = [path-np.median(path) for path in paths]
        path_corr = _safe_corr(centered[0], centered[1])
        trace_corr = _safe_corr(traces[0], traces[1])
        slope_difference = abs(receiver_reports[0]["fitted_drift_hz_s"]-
                               receiver_reports[1]["fitted_drift_hz_s"])
        motion_bins = min(int(round(np.ptp(path)/bin_hz)) for path in paths)
        reasons = []
        if motion_bins < 3: reasons.append("measured motion is less than three bins")
        if min(item["stationary_improvement_db"] for item in receiver_reports) < .03:
            reasons.append("moving path does not beat stationary control")
        if min(item["comb_spacing_improvement_db"] for item in receiver_reports) < .03:
            reasons.append("43.9495 kHz comb does not beat wrong-spacing controls")
        if path_corr < .6: reasons.append("receiver paths are insufficiently correlated")
        if slope_difference > 500: reasons.append("receiver drift-rate disagreement exceeds 500 Hz/s")
        if broadband > maximum_broadband_row_fraction:
            reasons.append("window is dominated by common broadband activity")
        reports.append({"window_start_utc": datetime.fromtimestamp(times[rows[0]], timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_stop_utc": datetime.fromtimestamp(times[rows[-1]], timezone.utc).isoformat().replace("+00:00", "Z"),
            "window_duration_s": float(times[rows[-1]]-times[rows[0]]),
            "time_s": (times[rows]-seconds[0]).tolist(),
            "paths_hz": [path.tolist() for path in paths], "receivers": receiver_reports,
            "path_correlation": path_corr, "trace_correlation": trace_corr,
            "drift_rate_difference_hz_s": float(slope_difference),
            "motion_bins": motion_bins, "common_broadband_row_fraction": float(broadband),
            "qualified": not reasons, "rejection_reasons": reasons})
    reports.sort(key=lambda item: (item["qualified"],
        min(receiver["stationary_improvement_db"] for receiver in item["receivers"]),
        item["path_correlation"]), reverse=True)
    false_alarm = None
    if reports:
        best = reports[0]
        for candidate in reports[1:]:
            if candidate["qualified"]:
                candidate["qualified"] = False
                candidate["rejection_reasons"].append(
                    "only the strongest window receives false-alarm calibration")
        wanted = np.asarray(best["time_s"])+seconds[0]
        best_rows = np.asarray([int(np.argmin(abs(times-value))) for value in wanted])
        observed_score = min(item["score_db"] for item in best["receivers"])
        rng = np.random.default_rng(seed)
        null_scores = []
        for _ in range(permutations):
            shuffled = rng.permutation(best_rows)
            scores = []
            for receiver in range(2):
                values = correct[receiver][shuffled]
                path = _viterbi(values, max_step, .01)
                test_values = correct_test[receiver][shuffled]
                scores.append(float(np.mean(
                    test_values[np.arange(values.shape[0]), path])))
            null_scores.append(min(scores))
        probability = (None if not permutations else
            (1+sum(score >= observed_score for score in null_scores))/(permutations+1))

        off_frequency = []
        for direction in (-1, 1):
            control_center = nominal_offset_hz+direction*3*search_half_width_hz
            control_indexes = np.flatnonzero(
                np.abs(frequencies-control_center) <= search_half_width_hz)
            if control_indexes.size < selected.size*.8:
                continue
            c_lo, c_hi = int(control_indexes[0]), int(control_indexes[-1])+1
            receiver_scores = []
            for receiver in range(2):
                source = residual[receiver, :, c_lo:c_hi]
                values = _comb(source, comb_spacing_hz, bin_hz, train_teeth)[0][best_rows]
                test_values = _comb(source, comb_spacing_hz, bin_hz, test_teeth)[0][best_rows]
                path = _viterbi(values, max_step, .01)
                receiver_scores.append(float(np.mean(
                    test_values[np.arange(values.shape[0]), path])))
            off_frequency.append({"center_offset_hz": float(control_center),
                "joint_score_db": min(receiver_scores),
                "receiver_scores_db": receiver_scores})
        strongest_off = max((item["joint_score_db"] for item in off_frequency),
                            default=-math.inf)
        off_advantage = float(observed_score-strongest_off)
        best["empirical_false_alarm_probability"] = probability
        best["off_frequency_advantage_db"] = off_advantage
        if probability is not None and probability > .1:
            best["rejection_reasons"].append("track does not beat time-scrambled controls")
        if off_frequency and off_advantage < .03:
            best["rejection_reasons"].append("nominal region does not beat off-frequency controls")
        best["qualified"] = not best["rejection_reasons"]
        false_alarm = {"permutations": permutations,
            "empirical_false_alarm_probability": probability,
            "observed_joint_score_db": observed_score,
            "null_score_p95_db": (None if not null_scores else
                                   float(np.percentile(null_scores, 95))),
            "off_frequency_regions": off_frequency,
            "off_frequency_advantage_db": off_advantage}
    spacing_scan = []
    if reports:
        best = reports[0]
        wanted = np.asarray(best["time_s"])+seconds[0]
        rows = np.asarray([int(np.argmin(abs(times-value))) for value in wanted])
        # Exploratory only: this scan has a multiple-comparisons advantage and
        # must not be used as a qualification gate.
        spacings = np.unique(np.concatenate((np.arange(30_000, 82_501, 2_500),
                                             np.arange(180_000, 240_001, 5_000))))
        for spacing in spacings:
            receiver_scores = []
            for receiver in range(2):
                values = _comb(local[receiver], float(spacing), bin_hz)[0][rows]
                path = _viterbi(values, max_step, .01)
                receiver_scores.append(float(np.mean(values[np.arange(rows.size), path])))
            spacing_scan.append({"spacing_hz": float(spacing),
                "joint_score_db": min(receiver_scores), "receiver_scores_db": receiver_scores})
        spacing_scan.sort(key=lambda item: item["joint_score_db"], reverse=True)
    return {"schema": SCHEMA, "method": "TLE-independent nine-tooth comb Viterbi search",
        "configuration": {"nominal_offset_hz": nominal_offset_hz,
            "search_half_width_hz": search_half_width_hz, "integration_s": integration_s,
            "window_s": window_s, "step_s": step_s, "comb_spacing_hz": comb_spacing_hz,
            "wrong_spacings_hz": list(wrong_spacings_hz), "max_drift_hz_s": max_drift_hz_s,
            "cross_validation": {"path_fit_teeth": list(train_teeth),
                                 "held_out_score_teeth": list(test_teeth)}},
        "bin_width_hz": bin_hz, "window_count": len(reports),
        "qualified_count": sum(item["qualified"] for item in reports), "candidates": reports,
        "false_alarm_controls": false_alarm,
        "exploratory_spacing_scan": {"multiple_comparisons_corrected": False,
                                     "best": spacing_scan[:10]}}
