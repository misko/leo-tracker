"""TLE-guided whole-waterfall search with stationary and dual-RX controls."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Sequence

import numpy as np

from .events import residual_waterfall


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def search_tle_doppler(
    spectra_db: np.ndarray, utc_ns: Sequence[int], frequency_offsets_hz: Sequence[float],
    catalog: dict, *, threshold_db: float = .35, minimum_doppler_bins: int = 3,
    minimum_joint_score: float = .05, minimum_stationary_improvement: float = .03,
    minimum_trace_correlation: float = .5, continuous_activity_fraction: float = .8,
    dwell_window_s: float = 30.0, dwell_step_s: float = 10.0,
    minimum_window_s: float = 8.0,
    minimum_comb_spacing_improvement: float = .03,
    comb_spacing_hz: float = 43_900.0,
    comb_wrong_spacings_hz: Sequence[float] = (35_000.0, 52_000.0),
    broadband_frequency_fraction: float = .2,
    maximum_broadband_row_fraction: float = .2,
) -> dict:
    """Search unknown per-LNB biases while requiring predicted motion and controls."""
    spectra = np.asarray(spectra_db, float)
    utc = np.asarray(utc_ns, np.int64)
    frequencies = np.asarray(frequency_offsets_hz, float)
    if dwell_window_s <= 0 or dwell_step_s <= 0 or minimum_window_s <= 0:
        raise ValueError("TLE dwell window parameters must be positive")
    if comb_spacing_hz <= 0 or any(value <= 0 for value in comb_wrong_spacings_hz):
        raise ValueError("comb spacings must be positive")
    if spectra.ndim != 3 or spectra.shape[0] != 2 or spectra.shape[1] != utc.size:
        raise ValueError("TLE search requires two receiver waterfalls and matching timestamps")
    if frequencies.ndim != 1 or spectra.shape[2] != frequencies.size or frequencies.size < 8:
        raise ValueError("frequency axis must match the waterfall")
    if utc.size < 5 or np.any(np.diff(utc) <= 0) or np.any(np.diff(frequencies) <= 0):
        raise ValueError("timestamps and frequencies must be increasing")
    bin_hz = float(np.median(np.diff(frequencies)))
    raw_residuals = np.asarray([residual_waterfall(item) for item in spectra])
    broadband_activity = np.mean(np.abs(raw_residuals) > threshold_db, axis=2)
    residuals = raw_residuals.copy()
    residuals -= np.median(residuals, axis=2, keepdims=True)
    seconds = utc / 1e9
    candidates = []
    for satellite in catalog.get("satellites", []):
        for item in satellite.get("passes", []):
            points = [item["rise"], item["culmination"], item["set"]]
            pass_times = np.asarray([_timestamp(point["time"]) for point in points])
            if pass_times[0] > seconds[-1] or pass_times[-1] < seconds[0]:
                continue
            overlap = np.flatnonzero((seconds >= pass_times[0]) & (seconds <= pass_times[-1]))
            if overlap.size < 5 or seconds[overlap[-1]]-seconds[overlap[0]] < minimum_window_s:
                continue
            windows = []
            overlap_start, overlap_stop = seconds[overlap[0]], seconds[overlap[-1]]
            if overlap_stop-overlap_start <= dwell_window_s:
                windows.append(overlap)
            else:
                starts = np.arange(overlap_start, overlap_stop-dwell_window_s+dwell_step_s,
                                   dwell_step_s)
                starts = np.unique(np.append(starts, overlap_stop-dwell_window_s))
                for window_start in starts:
                    rows = overlap[(seconds[overlap] >= window_start)
                                   & (seconds[overlap] <= window_start+dwell_window_s)]
                    if rows.size >= 5:
                        windows.append(rows)
            window_reports = []
            for rows in windows:
                predicted = np.interp(seconds[rows], pass_times,
                                      [point["expected_doppler_hz"] for point in points])
                shifts = np.rint((predicted-predicted[len(predicted)//2])/bin_hz).astype(int)
                doppler_span_bins = int(shifts.max()-shifts.min())
                low = max(0, -int(shifts.min()))
                high = min(frequencies.size, frequencies.size-int(shifts.max()))
                bases = np.arange(low, high)
                if bases.size == 0:
                    continue
                common_broadband_row_fraction = float(np.mean(np.all(
                    broadband_activity[:, rows] >= broadband_frequency_fraction, axis=0)))
                model_waterfalls = [
                    ("single-tone", "positive", [residuals[r][rows] for r in range(2)], 0, 0, []),
                    ("single-tone-negative", "negative", [-residuals[r][rows] for r in range(2)], 0, 0, [])]
                comb_offsets = np.unique(np.rint(
                    np.arange(-4, 5)*comb_spacing_hz/bin_hz).astype(int))
                if bin_hz <= 15_000 and comb_offsets.size == 9:
                    def make_comb(spacing_hz, sign=1):
                        offsets = np.unique(np.rint(
                            np.arange(-4, 5)*spacing_hz/bin_hz).astype(int))
                        low_edge, high_edge = -int(offsets.min()), int(offsets.max())
                        centers = np.arange(low_edge, frequencies.size-high_edge)
                        waterfalls = []
                        for receiver in range(2):
                            source = sign*residuals[receiver][rows]
                            combined = np.full_like(source, -np.inf)
                            combined[:, centers] = sum(
                                source[:, centers+offset] for offset in offsets) / math.sqrt(9)
                            waterfalls.append(combined)
                        return waterfalls, low_edge, high_edge
                    for sign, polarity in ((1, "positive"), (-1, "negative")):
                        comb_waterfalls, comb_low, comb_high = make_comb(comb_spacing_hz, sign)
                        wrong_combs = [make_comb(spacing, sign) for spacing in comb_wrong_spacings_hz]
                        suffix = "" if sign == 1 else "-negative"
                        model_waterfalls.append((f"nine-tone-{comb_spacing_hz/1e3:g}khz-comb{suffix}",
                            polarity, comb_waterfalls, comb_low, comb_high, wrong_combs))
                for signal_model, polarity, model_residuals, model_low, model_high, wrong_combs in model_waterfalls:
                    model_bases = bases[(bases >= model_low-int(shifts.min())) &
                                        (bases < frequencies.size-model_high-int(shifts.max()))]
                    if model_bases.size == 0:
                        continue
                    receiver_reports = []
                    traces = []
                    for receiver in range(2):
                        window_residual = model_residuals[receiver]
                        stationary_score = float(np.max(np.mean(
                            np.maximum(window_residual[:, model_low:frequencies.size-model_high or None]
                                       - threshold_db, 0), axis=0)))
                        aligned = window_residual[
                            np.arange(rows.size)[:, None], model_bases[None, :] + shifts[:, None]]
                        scores = np.mean(np.maximum(aligned-threshold_db, 0), axis=0)
                        selected = int(np.argmax(scores))
                        trace = aligned[:, selected]
                        wrong_score = None
                        if wrong_combs:
                            wrong_scores = []
                            for wrong_waterfalls, wrong_low, wrong_high in wrong_combs:
                                wrong_bases = bases[(bases >= wrong_low-int(shifts.min())) &
                                    (bases < frequencies.size-wrong_high-int(shifts.max()))]
                                wrong_aligned = wrong_waterfalls[receiver][
                                    np.arange(rows.size)[:, None],
                                    wrong_bases[None, :] + shifts[:, None]]
                                wrong_scores.append(float(np.max(np.mean(
                                    np.maximum(wrong_aligned-threshold_db, 0), axis=0))))
                            wrong_score = max(wrong_scores)
                        traces.append(trace)
                        receiver_reports.append({
                            "receiver": receiver,
                            "frequency_bias_hz": float(frequencies[model_bases[selected]]),
                            "score_db": float(scores[selected]),
                            "stationary_score_db": stationary_score,
                            "stationary_improvement_db": float(scores[selected]-stationary_score),
                            "wrong_spacing_score_db": wrong_score,
                            "comb_spacing_improvement_db": (None if wrong_score is None else
                                float(scores[selected]-wrong_score)),
                            "activity_fraction": float(np.mean(trace > threshold_db)),
                        })
                    correlation = float(np.corrcoef(traces[0], traces[1])[0, 1])
                    if not math.isfinite(correlation):
                        correlation = 0.0
                    joint_score = min(report["score_db"] for report in receiver_reports)
                    improvement = min(report["stationary_improvement_db"] for report in receiver_reports)
                    reasons = []
                    if doppler_span_bins < minimum_doppler_bins:
                        reasons.append("predicted Doppler is unresolved at this bin width")
                    if joint_score < minimum_joint_score:
                        reasons.append("aligned dual-receiver score is too low")
                    if improvement < minimum_stationary_improvement:
                        reasons.append("TLE path does not beat the stationary-path control")
                    if wrong_combs and min(
                            report["comb_spacing_improvement_db"] for report in receiver_reports
                            ) < minimum_comb_spacing_improvement:
                        reasons.append("43.9 kHz comb does not beat wrong-spacing controls")
                    if common_broadband_row_fraction > maximum_broadband_row_fraction:
                        reasons.append("window is dominated by common broadband activity")
                    joint_activity_fraction = min(report["activity_fraction"] for report in receiver_reports)
                    if (correlation < minimum_trace_correlation
                            and joint_activity_fraction < continuous_activity_fraction):
                        reasons.append("aligned receiver activity is not correlated")
                    window_reports.append({
                        "signal_model": signal_model,
                        "polarity": polarity,
                        "window_start_utc": datetime.fromtimestamp(seconds[rows[0]], timezone.utc).isoformat().replace("+00:00", "Z"),
                        "window_stop_utc": datetime.fromtimestamp(seconds[rows[-1]], timezone.utc).isoformat().replace("+00:00", "Z"),
                        "window_duration_s": float(seconds[rows[-1]]-seconds[rows[0]]),
                        "window_observations": int(rows.size),
                        "common_broadband_row_fraction": common_broadband_row_fraction,
                        "predicted_doppler_span_hz": float(predicted.max()-predicted.min()),
                        "predicted_doppler_span_bins": doppler_span_bins,
                        "trace_correlation": correlation, "joint_score_db": joint_score,
                        "joint_activity_fraction": joint_activity_fraction,
                        "stationary_improvement_db": improvement,
                        "receivers": receiver_reports,
                        "qualified": not reasons, "rejection_reasons": reasons,
                    })
            if not window_reports:
                continue
            # Prefer the physically strongest aligned model before using its
            # control improvement as a tie-breaker.  Otherwise, a weak model
            # of the opposite polarity can become the displayed rejection
            # merely because both its moving and stationary scores are near
            # zero and their noisy difference happens to be positive.
            best = max(window_reports, key=lambda value: (
                value["qualified"], value["joint_score_db"],
                value["stationary_improvement_db"]))
            candidates.append({
                "name": satellite["name"].strip(), "norad_id": int(satellite["norad_id"]),
                "rise_utc": points[0]["time"], "culmination_utc": points[1]["time"],
                "set_utc": points[2]["time"],
                "max_elevation_deg": float(item["culmination"]["elevation_deg"]),
                "predicted_points": [
                    {"time": point["time"],
                     "expected_doppler_hz": float(point["expected_doppler_hz"])}
                    for point in points],
                **best,
            })
    candidates.sort(key=lambda value: (value["qualified"], value["joint_score_db"],
                                        value["stationary_improvement_db"]), reverse=True)
    return {"bin_width_hz": bin_hz,
            "configuration": {"dwell_window_s": dwell_window_s,
                "dwell_step_s": dwell_step_s, "minimum_window_s": minimum_window_s,
                "minimum_comb_spacing_improvement": minimum_comb_spacing_improvement,
                "broadband_frequency_fraction": broadband_frequency_fraction,
                "maximum_broadband_row_fraction": maximum_broadband_row_fraction,
                "comb_spacing_hz": comb_spacing_hz,
                "comb_wrong_spacings_hz": list(comb_wrong_spacings_hz)},
            "overlapping_passes": len(candidates),
            "qualified_count": sum(item["qualified"] for item in candidates),
            "candidates": candidates}
