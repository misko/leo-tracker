"""Track finite wideband spectral features without forcing a carrier ridge."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.ndimage import binary_closing, gaussian_filter1d, label, uniform_filter1d
from scipy.signal import find_peaks


SCHEMA = "leo-tracker.wide-feature-search/v1"
LIGHT_SPEED_M_S = 299_792_458.0


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def _safe_corr(first: np.ndarray, second: np.ndarray) -> float:
    if np.std(first) == 0 or np.std(second) == 0:
        return 0.0
    value = float(np.corrcoef(first, second)[0, 1])
    return value if math.isfinite(value) else 0.0


def _internal_translation_path(rows: np.ndarray, *, bin_hz: float,
                               expected_path_hz: np.ndarray,
                               maximum_error_bins: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Measure common translation of a feature's internal spectral texture.

    Unlike a power-weighted centroid, normalized correlation is insensitive to
    an overall power change and is much less sensitive to one subchannel merely
    becoming brighter.  The centroid supplies only a bounded search window; the
    returned displacement comes from the complete internal texture.
    """
    spectra = np.asarray(rows, float)
    expected = np.asarray(expected_path_hz, float)
    if spectra.ndim != 2 or spectra.shape[0] != expected.size:
        raise ValueError("internal translation rows and expected path must align")
    # Use several initial rows so a single noisy integration cannot define the
    # reference. Remove broad envelope variation while retaining subchannels.
    reference_count = min(3, spectra.shape[0])
    reference = np.nanmedian(spectra[:reference_count], axis=0)
    reference = np.nan_to_num(reference-gaussian_filter1d(reference, 8,
                                                           mode="nearest"))
    origin_hz = float(np.nanmedian(expected[:reference_count]))
    shifts = np.full(spectra.shape[0], np.nan)
    correlations = np.full(spectra.shape[0], np.nan)
    previous_shift = 0
    for index, source in enumerate(spectra):
        texture = np.nan_to_num(source-gaussian_filter1d(source, 8, mode="nearest"))
        expected_bins = int(round((expected[index]-origin_hz)/bin_hz))
        # A physical Doppler path is continuous. Searching around the previous
        # texture displacement prevents the solution jumping from one fixed
        # subchannel to another when their relative powers cross.
        search_center = previous_shift if index else expected_bins
        best_correlation, best_shift = -np.inf, expected_bins
        search_radius = maximum_error_bins if index == 0 else min(2, maximum_error_bins)
        for shift in range(search_center-search_radius,
                           search_center+search_radius+1):
            if shift < 0:
                first, second = reference[-shift:], texture[:shift]
            elif shift > 0:
                first, second = reference[:-shift], texture[shift:]
            else:
                first, second = reference, texture
            if first.size < 8:
                continue
            first = first-np.mean(first); second = second-np.mean(second)
            denominator = float(np.linalg.norm(first)*np.linalg.norm(second))
            correlation = -np.inf if denominator == 0 else float(first@second/denominator)
            if correlation > best_correlation:
                best_correlation, best_shift = correlation, shift
        shifts[index] = origin_hz+best_shift*bin_hz
        correlations[index] = best_correlation
        previous_shift = best_shift
    return shifts, correlations


def _tle_comparisons(utc_s: np.ndarray, measured_hz: np.ndarray, catalog: dict) -> list[dict]:
    """Compare shape only: each receiver/LNB is allowed an arbitrary intercept."""
    observed = measured_hz-np.mean(measured_hz)
    stationary_rms = float(np.sqrt(np.mean(observed**2)))
    centered_time = utc_s-np.mean(utc_s)
    affine = np.polyval(np.polyfit(centered_time, observed, 1), centered_time)
    affine_rms = float(np.sqrt(np.mean((observed-affine)**2)))
    reports = []
    for satellite in catalog.get("satellites", []):
        for pass_ in satellite.get("passes", []):
            points = pass_.get("track") or [pass_["rise"], pass_["culmination"], pass_["set"]]
            point_times = np.asarray([_timestamp(point["time"]) for point in points])
            # Never let np.interp's endpoint clamping fabricate a flat
            # Doppler path outside a predicted pass.  Shape comparison is
            # valid only when the sampled TLE path covers the whole feature.
            if point_times[0] > utc_s[0] or point_times[-1] < utc_s[-1]:
                continue
            predicted = np.interp(utc_s, point_times,
                                  [point["expected_doppler_hz"] for point in points])
            predicted -= np.mean(predicted)
            predicted_affine = np.polyval(
                np.polyfit(centered_time, predicted, 1), centered_time)
            predicted_curvature_rms = float(np.sqrt(np.mean(
                (predicted-predicted_affine)**2)))
            error = observed-predicted
            reversed_error = observed-predicted[::-1]
            rms = float(np.sqrt(np.mean(error**2)))
            reports.append({
                "name": satellite["name"].strip(), "norad_id": int(satellite["norad_id"]),
                "max_elevation_deg": float(pass_["culmination"]["elevation_deg"]),
                "predicted_shift_hz": float(predicted[-1]-predicted[0]),
                "measured_shift_hz": float(observed[-1]-observed[0]),
                "rms_error_hz": rms,
                "stationary_rms_error_hz": stationary_rms,
                "stationary_improvement_hz": stationary_rms-rms,
                "affine_drift_rms_error_hz": affine_rms,
                "affine_drift_improvement_hz": affine_rms-rms,
                # This is the amount of non-linear orbital shape available in
                # the TLE over the observed interval.  When it is below the
                # measurement bin width, this arc cannot conservatively
                # distinguish that orbit from an arbitrary straight chirp.
                "predicted_curvature_rms_hz": predicted_curvature_rms,
                "time_reversed_rms_error_hz": float(np.sqrt(np.mean(reversed_error**2))),
                "time_reversal_improvement_hz": float(np.sqrt(np.mean(reversed_error**2)))-rms,
            })
    reports.sort(key=lambda item: item["rms_error_hz"])
    return reports


def estimate_global_frequency_correction(spectra_db: np.ndarray, bin_hz: float,
                                         maximum_shift_hz: float = 150_000) -> tuple[np.ndarray, np.ndarray]:
    """Register every row to persistent full-band texture.

    Returned corrections add to an observed path. A receiver-wide LNB/LO
    translation therefore disappears, while motion confined to a small RF
    feature remains.
    """
    spectra = np.asarray(spectra_db, float)
    if spectra.ndim != 3 or spectra.shape[0] != 2:
        raise ValueError("global frequency registration requires two receiver waterfalls")
    maximum_bins = int(maximum_shift_hz/bin_hz)
    if maximum_bins < 1 or spectra.shape[2] <= 4*maximum_bins:
        raise ValueError("global registration shift is incompatible with frequency width")
    corrections = np.zeros(spectra.shape[:2], float)
    correlations = np.zeros_like(corrections)
    for receiver in range(2):
        # A pointwise median blurs narrow texture when the LO itself moves.
        # Register to the middle row; the arbitrary constant reference does
        # not affect a candidate's drift or curvature.
        reference = spectra[receiver, spectra.shape[1]//2]
        reference = reference-gaussian_filter1d(reference, 20, mode="nearest")
        for row, source in enumerate(spectra[receiver]):
            texture = source-gaussian_filter1d(source, 20, mode="nearest")
            best_correlation, best_shift = -np.inf, 0
            for shift in range(-maximum_bins, maximum_bins+1):
                if shift < 0:
                    first, second = reference[-shift:], texture[:shift]
                elif shift > 0:
                    first, second = reference[:-shift], texture[shift:]
                else:
                    first, second = reference, texture
                first = first-np.mean(first); second = second-np.mean(second)
                denominator = float(np.linalg.norm(first)*np.linalg.norm(second))
                correlation = -np.inf if denominator == 0 else float(first@second/denominator)
                if correlation > best_correlation:
                    best_correlation, best_shift = correlation, shift
            # If the row's texture moved +k bins, matching reference against
            # row[k:] selects +k; adding -k to a measured path removes it.
            corrections[receiver, row] = -best_shift*bin_hz
            correlations[receiver, row] = best_correlation
    return corrections, correlations


def search_wide_features(
    residual_db: np.ndarray, elapsed_s: Sequence[float], corrected_rf_hz: np.ndarray,
    *, start_utc_s: float, pass_catalog: dict | None = None, threshold_db: float = .35,
    frequency_axis_correction_hz: np.ndarray | None = None,
    frequency_axis_registration_correlation: np.ndarray | None = None,
    minimum_duration_s: float = 8, maximum_duration_s: float = 60,
    minimum_width_hz: float = 100_000, maximum_width_hz: float = 2_000_000,
    minimum_boundary_margin_s: float = 3.0,
) -> dict:
    """Find dual-RX finite channel-like blocks and measure their motion.

    Input is persistent-RF-baseline residual, not raw PSD.  Positive and
    negative state changes are searched symmetrically.
    """
    values = np.asarray(residual_db, float)
    times = np.asarray(elapsed_s, float)
    axes = np.asarray(corrected_rf_hz, float)
    if values.ndim != 3 or values.shape[0] != 2 or values.shape[1] != times.size:
        raise ValueError("wide-feature search requires two receiver waterfalls")
    if axes.shape != (2, values.shape[2]):
        raise ValueError("corrected RF axes must match both receiver waterfalls")
    if times.size < 8 or np.any(np.diff(times) <= 0):
        raise ValueError("wide-feature timestamps must be increasing")
    if minimum_boundary_margin_s < 0:
        raise ValueError("boundary margin must be non-negative")
    bin_hz = float(np.median(np.diff(axes, axis=1)))
    rf_carrier_hz = (None if pass_catalog is None or not pass_catalog.get("carrier_hz")
                     else float(pass_catalog["carrier_hz"]))
    correction = (np.zeros(values.shape[:2], float) if frequency_axis_correction_hz is None
                  else np.asarray(frequency_axis_correction_hz, float))
    if correction.shape != values.shape[:2]:
        raise ValueError("frequency-axis corrections must match receiver and time dimensions")
    registration_correlation = (np.full(values.shape[:2], np.nan)
        if frequency_axis_registration_correlation is None else
        np.asarray(frequency_axis_registration_correlation, float))
    if registration_correlation.shape != values.shape[:2]:
        raise ValueError("frequency-axis registration correlations must match the waterfall")
    candidates = []
    for polarity, sign in (("positive", 1.0), ("negative", -1.0)):
        # Requiring both receivers prevents a feature unique to one LNB from
        # becoming an RF candidate. Closing joins tiny FFT-bin holes only.
        mask = np.all(sign*values > threshold_db, axis=0)
        mask = binary_closing(mask, structure=np.ones((3, 3), bool))
        components, count = label(mask)
        for component in range(1, count+1):
            rows, columns = np.where(components == component)
            if rows.size == 0:
                continue
            row0, row1 = int(rows.min()), int(rows.max())
            col0, col1 = int(columns.min()), int(columns.max())
            duration = float(times[row1]-times[row0])
            width = float((col1-col0+1)*bin_hz)
            if not (minimum_duration_s <= duration <= maximum_duration_s
                    and minimum_width_hz <= width <= maximum_width_hz):
                continue
            used_rows = np.arange(row0, row1+1)
            used_columns = np.arange(col0, col1+1)
            paths, widths, depths = [], [], []
            for receiver in range(2):
                receiver_paths, receiver_widths, receiver_depths = [], [], []
                for row in used_rows:
                    weights = np.maximum(sign*values[receiver, row, used_columns]
                                         -threshold_db/2, 0)
                    if np.sum(weights) <= 0:
                        receiver_paths.append(np.nan); receiver_widths.append(np.nan)
                        receiver_depths.append(np.nan); continue
                    frequency = axes[receiver, used_columns]
                    receiver_paths.append(float(np.sum(weights*frequency)/np.sum(weights)))
                    active = frequency[weights > threshold_db/2]
                    receiver_widths.append(float(active[-1]-active[0]+bin_hz)
                                           if active.size else 0.0)
                    receiver_depths.append(float(np.average(
                        sign*values[receiver, row, used_columns], weights=weights)))
                paths.append(np.asarray(receiver_paths)); widths.append(np.asarray(receiver_widths))
                depths.append(np.asarray(receiver_depths))
            finite = np.isfinite(paths[0]) & np.isfinite(paths[1])
            if np.sum(finite) < 5:
                continue
            raw_paths = np.asarray(paths)[:, finite]
            used_correction = correction[:, used_rows][:, finite]
            paths = raw_paths+used_correction
            feature_times = times[used_rows][finite]
            normalized = paths-np.median(paths, axis=1, keepdims=True)
            mean_path = np.mean(normalized, axis=0)
            utc_s = start_utc_s+feature_times
            tle = [] if pass_catalog is None else _tle_comparisons(utc_s, mean_path, pass_catalog)
            receivers = []
            internal_profiles = []
            internal_translation_paths = []
            for receiver in range(2):
                coefficient = np.polyfit(feature_times, paths[receiver], 1)
                raw_coefficient = np.polyfit(feature_times, raw_paths[receiver], 1)
                relative_hz = (np.arange(used_columns.size)-used_columns.size//2)*bin_hz
                aligned_rows = []
                for source_row, center_hz in zip(used_rows[finite], raw_paths[receiver], strict=True):
                    aligned_rows.append(np.interp(center_hz+relative_hz, axes[receiver],
                        sign*values[receiver, source_row], left=np.nan, right=np.nan))
                profile = np.nanmean(np.asarray(aligned_rows), axis=0)
                texture = profile-gaussian_filter1d(profile, 8, mode="nearest")
                peaks, properties = find_peaks(texture, prominence=.04, distance=2)
                order = np.argsort(properties["prominences"])[::-1][:32]
                peaks = np.sort(peaks[order])
                internal_profiles.append(texture)
                feature_rows = sign*values[receiver, used_rows[finite]][:, used_columns]
                translation_path, translation_correlation = _internal_translation_path(
                    feature_rows, bin_hz=bin_hz,
                    expected_path_hz=raw_paths[receiver])
                translation_path = translation_path+used_correction[receiver]
                internal_translation_paths.append(translation_path)
                translation_coefficient = np.polyfit(
                    feature_times, translation_path, 1)
                autocorrelation = np.correlate(texture-np.mean(texture),
                                                texture-np.mean(texture), mode="full")
                autocorrelation = autocorrelation[len(texture)-1:]
                lag_low, lag_high = 3, min(21, len(autocorrelation))
                dominant_lag = (None if lag_high <= lag_low else
                    int(np.argmax(autocorrelation[lag_low:lag_high])+lag_low))
                registration_values = registration_correlation[
                    receiver, used_rows][finite]
                registration_values = registration_values[np.isfinite(registration_values)]
                receivers.append({"receiver": receiver,
                    "median_center_rf_hz": float(np.median(paths[receiver])),
                    "raw_median_center_rf_hz": float(np.median(raw_paths[receiver])),
                    "median_width_hz": float(np.nanmedian(widths[receiver])),
                    "median_depth_db": float(np.nanmedian(depths[receiver])),
                    "net_shift_hz": float(paths[receiver, -1]-paths[receiver, 0]),
                    "linear_drift_hz_s": float(coefficient[0]),
                    "raw_net_shift_hz": float(raw_paths[receiver, -1]-raw_paths[receiver, 0]),
                    "raw_linear_drift_hz_s": float(raw_coefficient[0]),
                    "median_global_frequency_correction_hz": float(
                        np.median(used_correction[receiver])),
                    "global_frequency_correction_span_hz": float(
                        np.ptp(used_correction[receiver])),
                    "median_global_registration_correlation": (None if not
                        registration_values.size else float(np.median(registration_values))),
                    "internal_peak_count": int(peaks.size),
                    "internal_peak_offsets_hz": relative_hz[peaks].tolist(),
                    "adjacent_internal_peak_spacings_hz": np.diff(relative_hz[peaks]).tolist(),
                    "dominant_internal_spacing_hz": (None if dominant_lag is None else
                                                       float(dominant_lag*bin_hz)),
                    "internal_translation_drift_hz_s": float(
                        translation_coefficient[0]),
                    "internal_translation_net_shift_hz": float(
                        translation_path[-1]-translation_path[0]),
                    "median_internal_translation_correlation": float(
                        np.nanmedian(translation_correlation)),
                    "internal_translation_path_rf_hz": translation_path.tolist(),
                    "path_rf_hz": paths[receiver].tolist()})
            slope_difference = abs(receivers[0]["linear_drift_hz_s"]-
                                   receivers[1]["linear_drift_hz_s"])
            global_control_available = all(
                item["median_global_registration_correlation"] is not None and
                item["median_global_registration_correlation"] >= .4
                for item in receivers)
            global_control_passed = None
            if global_control_available:
                global_control_passed = all(
                    item["raw_linear_drift_hz_s"]*item["linear_drift_hz_s"] > 0 and
                    abs(item["linear_drift_hz_s"]) >= .5*abs(item["raw_linear_drift_hz_s"])
                    for item in receivers)
            reasons = []
            motion_reasons = []
            measurement_warnings = []
            if feature_times[0]-times[0] < minimum_boundary_margin_s:
                measurement_warnings.append(
                    "feature onset is censored by the capture start boundary")
            if times[-1]-feature_times[-1] < minimum_boundary_margin_s:
                measurement_warnings.append(
                    "feature ending is censored by the capture stop boundary")
            correlation = _safe_corr(normalized[0], normalized[1])
            path_rms_difference = float(np.sqrt(np.mean((normalized[0]-normalized[1])**2)))
            translation_paths = np.asarray(internal_translation_paths)
            translation_normalized = (translation_paths-
                np.median(translation_paths, axis=1, keepdims=True))
            translation_path_correlation = _safe_corr(
                translation_normalized[0], translation_normalized[1])
            translation_path_rms_difference = float(np.sqrt(np.mean(
                (translation_normalized[0]-translation_normalized[1])**2)))
            translation_slopes = [item["internal_translation_drift_hz_s"]
                                  for item in receivers]
            internal_translation_consistent = bool(
                translation_path_correlation >= .6 and
                translation_slopes[0]*translation_slopes[1] > 0 and
                abs(translation_slopes[0]-translation_slopes[1]) <= 1_000 and
                all(centroid*texture > 0 and
                    abs(texture-centroid) <= max(1_000, abs(centroid))
                    for centroid, texture in zip(
                        [item["linear_drift_hz_s"] for item in receivers],
                        translation_slopes, strict=True)))
            if correlation < .6:
                motion_reasons.append("dual-receiver path correlation is too low")
            if receivers[0]["linear_drift_hz_s"]*receivers[1]["linear_drift_hz_s"] <= 0:
                motion_reasons.append("receivers do not agree on drift direction")
            if slope_difference > 1_000:
                motion_reasons.append("receiver drift-rate disagreement exceeds 1 kHz/s")
            # Centroids of a structured wide feature move slightly when its
            # internal subchannels change power. Six bins is still only
            # 22.5 kHz at the live 3.75 kHz resolution and about one tenth of
            # the typical feature width. Drift direction/rate/correlation are
            # independent controls and remain mandatory.
            if path_rms_difference > 6*bin_hz:
                motion_reasons.append("receiver paths disagree by more than six FFT bins")
            if global_control_passed is False:
                motion_reasons.append("motion disappears with receiver-wide frequency drift removed")
            reasons.extend(motion_reasons)
            best_tle_margin = None; tles_within_one_bin = 0; specific_tle = False
            if not tle:
                reasons.append("no complete TLE comparison is available")
            else:
                best_tle_margin = (None if len(tle) < 2 else
                                   float(tle[1]["rms_error_hz"]-tle[0]["rms_error_hz"]))
                tles_within_one_bin = sum(item["rms_error_hz"] <=
                    tle[0]["rms_error_hz"]+bin_hz for item in tle)
                specific_tle = bool(tles_within_one_bin == 1 and
                    (best_tle_margin is None or best_tle_margin >= bin_hz))
                best = tle[0]
                if best["predicted_shift_hz"]*best["measured_shift_hz"] <= 0:
                    reasons.append("measured and predicted Doppler directions disagree")
                if best["rms_error_hz"] > 4*bin_hz:
                    reasons.append("best TLE path error exceeds four FFT bins")
                if best["stationary_improvement_hz"] < bin_hz:
                    reasons.append("best TLE does not beat stationary control by one FFT bin")
                if best["time_reversal_improvement_hz"] < bin_hz:
                    reasons.append("best TLE does not beat time-reversed control by one FFT bin")
            orbital_shape_reasons = list(reasons)
            orbital_curvature_observable = False
            if tle:
                orbital_curvature_observable = bool(
                    tle[0]["predicted_curvature_rms_hz"] >= bin_hz)
                if not orbital_curvature_observable:
                    orbital_shape_reasons.append(
                        "best TLE predicts less than one FFT bin of orbital curvature")
                if tle[0]["affine_drift_improvement_hz"] < bin_hz:
                    orbital_shape_reasons.append(
                        "best TLE does not beat affine drift control by one FFT bin")
            first_peaks = np.asarray(receivers[0]["internal_peak_offsets_hz"], float)
            second_peaks = np.asarray(receivers[1]["internal_peak_offsets_hz"], float)
            common_peaks = []; unused = set(range(second_peaks.size))
            for first_peak in first_peaks:
                if not unused:
                    break
                second_index = min(unused, key=lambda index: abs(second_peaks[index]-first_peak))
                if abs(second_peaks[second_index]-first_peak) <= bin_hz:
                    common_peaks.append(float((first_peak+second_peaks[second_index])/2))
                    unused.remove(second_index)
            common_peaks = np.asarray(sorted(common_peaks), float)
            mean_drift_hz_s = float(np.mean(
                [item["linear_drift_hz_s"] for item in receivers]))
            mean_net_shift_hz = float(np.mean([item["net_shift_hz"] for item in receivers]))
            candidates.append({"polarity": polarity,
                "start_time_s": float(feature_times[0]), "stop_time_s": float(feature_times[-1]),
                "start_utc": datetime.fromtimestamp(utc_s[0], timezone.utc).isoformat().replace("+00:00", "Z"),
                "stop_utc": datetime.fromtimestamp(utc_s[-1], timezone.utc).isoformat().replace("+00:00", "Z"),
                "duration_s": float(feature_times[-1]-feature_times[0]),
                "bounding_width_hz": width,
                "receiver_path_correlation": correlation,
                "receiver_path_rms_difference_hz": path_rms_difference,
                "receiver_drift_rate_difference_hz_s": slope_difference,
                "internal_translation_path_correlation": translation_path_correlation,
                "internal_translation_path_rms_difference_hz": (
                    translation_path_rms_difference),
                "internal_translation_drift_rate_difference_hz_s": float(
                    abs(translation_slopes[0]-translation_slopes[1])),
                "internal_translation_consistent": internal_translation_consistent,
                "global_frequency_control_available": global_control_available,
                "global_frequency_control_passed": global_control_passed,
                "internal_profile_correlation": _safe_corr(internal_profiles[0],
                                                            internal_profiles[1]),
                "common_internal_peak_count": int(common_peaks.size),
                "common_internal_peak_offsets_hz": common_peaks.tolist(),
                "common_internal_peak_spacings_hz": np.diff(common_peaks).tolist(),
                "mean_drift_hz_s": mean_drift_hz_s,
                "mean_net_shift_hz": mean_net_shift_hz,
                "radial_acceleration_m_s2": (None if rf_carrier_hz is None else
                    float(-LIGHT_SPEED_M_S*mean_drift_hz_s/rf_carrier_hz)),
                "radial_velocity_change_m_s": (None if rf_carrier_hz is None else
                    float(-LIGHT_SPEED_M_S*mean_net_shift_hz/rf_carrier_hz)),
                "receivers": receivers, "time_s": feature_times.tolist(),
                "mean_relative_path_hz": mean_path.tolist(), "tle_comparisons": tle,
                "tles_within_one_bin_of_best": tles_within_one_bin,
                "best_tle_margin_hz": best_tle_margin,
                "specific_tle_identifiable": specific_tle,
                "measured_affine_residual_rms_hz": (None if not tle else
                    tle[0]["affine_drift_rms_error_hz"]),
                "best_tle_predicted_curvature_rms_hz": (None if not tle else
                    tle[0]["predicted_curvature_rms_hz"]),
                "best_tle_curvature_resolution_bins": (None if not tle else
                    tle[0]["predicted_curvature_rms_hz"]/bin_hz),
                "orbital_curvature_observable": orbital_curvature_observable,
                "measurement_warnings": measurement_warnings,
                "moving_rf_qualified": not motion_reasons,
                "doppler_candidate_qualified": (not motion_reasons and
                                                  internal_translation_consistent),
                "leo_like_qualified": not reasons, "rejection_reasons": reasons,
                "orbital_shape_qualified": not orbital_shape_reasons,
                "orbital_shape_rejection_reasons": orbital_shape_reasons})
    candidates.sort(key=lambda item: (item["leo_like_qualified"],
        item["duration_s"]*item["bounding_width_hz"]), reverse=True)
    return {"schema": SCHEMA, "bin_width_hz": bin_hz, "threshold_db": threshold_db,
            "minimum_boundary_margin_s": minimum_boundary_margin_s,
            "rf_carrier_hz": rf_carrier_hz,
            "doppler_physics": ("frequency displacement is invariant through fixed mixing; "
                "radial conversions use original Ku-band RF carrier"),
            "candidate_count": len(candidates),
            "doppler_candidate_count": sum(
                item["doppler_candidate_qualified"] for item in candidates),
            "qualified_count": sum(item["leo_like_qualified"] for item in candidates),
            "specific_tle_count": sum(item["specific_tle_identifiable"] for item in candidates),
            "candidates": candidates}


def analyze_wide_feature_artifact(measurement: Path, baseline: Path, *,
                                  pass_catalog: dict | None = None,
                                  threshold_db: float = .35,
                                  integration_s: float = 1.0,
                                  minimum_boundary_margin_s: float = 3.0) -> tuple[dict, dict]:
    """Run persistent-baseline subtraction and wide-feature tracking together."""
    from .measurement import load_measurement_waterfall
    from .rf_baseline import analyze_rf_novelty
    from .blind_comb import _integrate

    novelty = analyze_rf_novelty(measurement, baseline, threshold_db=threshold_db,
                                 integration_s=integration_s)
    artifact = load_measurement_waterfall(measurement)
    raw_seconds = np.asarray(artifact["utc_ns"], np.int64)/1e9
    global_integration_s = min(5.0, max(integration_s,
        (raw_seconds[-1]-raw_seconds[0])/10))
    integrated, integrated_times = _integrate(
        np.asarray(artifact["psd_db_raw_per_hz"], float),
        raw_seconds, global_integration_s)
    bin_hz = float(np.median(np.diff(np.asarray(artifact["frequency_offsets_hz"], float))))
    registration_spectra = uniform_filter1d(integrated, size=min(5, integrated.shape[1]),
                                             axis=1, mode="nearest")
    coarse_correction, coarse_correlation = estimate_global_frequency_correction(
        registration_spectra, bin_hz)
    novelty_utc = np.asarray(novelty["utc_s"], float)
    correction = np.asarray([np.interp(novelty_utc, integrated_times, row)
                             for row in coarse_correction])
    registration_correlation = np.asarray([np.interp(novelty_utc, integrated_times, row)
                                            for row in coarse_correlation])
    # Never apply an unconstrained alignment. Low correlation means the raw
    # capture lacks enough persistent texture to estimate receiver-wide drift.
    reliable_receivers = np.median(registration_correlation, axis=1) >= .4
    correction[~reliable_receivers] = 0
    registration_correlation[~reliable_receivers] = np.nan
    result = search_wide_features(novelty["residual_db"], novelty["elapsed_s"],
        novelty["corrected_rf_hz"], start_utc_s=float(novelty_utc[0]),
        pass_catalog=pass_catalog, threshold_db=threshold_db,
        frequency_axis_correction_hz=correction,
        frequency_axis_registration_correlation=registration_correlation,
        minimum_boundary_margin_s=minimum_boundary_margin_s)
    result.update({"source": str(measurement), "baseline": str(baseline),
                   "integration_s": integration_s,
                   "rf_registration_shift_hz": [float(item["registration_shift_hz"])
                                                  for item in novelty["receivers"]]})
    return result, novelty


def write_wide_feature_analysis(measurement: Path, baseline: Path, output: Path, *,
                                pass_catalog_path: Path | None = None,
                                plot: Path | None = None, threshold_db: float = .35,
                                integration_s: float = 1.0,
                                minimum_boundary_margin_s: float = 3.0) -> dict:
    catalog = (None if pass_catalog_path is None else
               json.loads(Path(pass_catalog_path).read_text()))
    report, novelty = analyze_wide_feature_artifact(measurement, baseline,
        pass_catalog=catalog, threshold_db=threshold_db, integration_s=integration_s,
        minimum_boundary_margin_s=minimum_boundary_margin_s)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    if plot is not None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        residual = np.asarray(novelty["residual_db"])
        rf = np.asarray(novelty["corrected_rf_hz"])/1e9
        elapsed = np.asarray(novelty["elapsed_s"])
        fig, axes = plt.subplots(2, 1, figsize=(14, 9), constrained_layout=True)
        for receiver in range(2):
            image = axes[receiver].imshow(residual[receiver], origin="lower", aspect="auto",
                extent=[rf[receiver, 0], rf[receiver, -1], elapsed[0], elapsed[-1]],
                cmap="RdBu_r", vmin=-1, vmax=1)
            for index, candidate in enumerate(report["candidates"][:8]):
                path = np.asarray(candidate["receivers"][receiver]["path_rf_hz"])/1e9
                label_ = (f"#{index+1} {candidate['polarity']} "
                          f"{candidate['receivers'][receiver]['median_width_hz']/1e3:.0f} kHz")
                color = axes[receiver].plot(path, candidate["time_s"], lw=1.4,
                    label=label_+" centroid")[0].get_color()
                translation = candidate["receivers"][receiver].get(
                    "internal_translation_path_rf_hz")
                if translation:
                    translation_label = ("internal pattern ✓" if candidate.get(
                        "internal_translation_consistent") else "internal pattern ?")
                    axes[receiver].plot(np.asarray(translation)/1e9,
                        candidate["time_s"], lw=1.8, ls="--", color=color,
                        label=f"#{index+1} {translation_label}")
            axes[receiver].set(title=f"RX{receiver} wide-feature paths on RF-baseline residual",
                xlabel="Approx. Ku-band RF (GHz)", ylabel="Elapsed time (s)")
            if report["candidates"]:
                axes[receiver].legend(fontsize=7, loc="upper right")
            fig.colorbar(image, ax=axes[receiver], label="Novelty (dB)")
        plot = Path(plot); plot.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(plot, dpi=150); plt.close(fig)
    return report
