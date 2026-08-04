"""Structured, raw-segment-first Doppler observations for every capture."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .events import detect_receiver_events
from .joint_tracking import associate_receiver_events
from .measurement import load_measurement_waterfall

SCHEMA = "leo-tracker.doppler-observations/v1"


def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns/1e9, timezone.utc).isoformat().replace("+00:00", "Z")


def _score(error: float, limit: float) -> float:
    return max(0.0, 1.0-abs(float(error))/limit)


def _value_at(event: dict, time_s: float) -> float:
    return float(np.interp(time_s, np.asarray(event["time_s"], float),
                           np.asarray(event["centroid_hz"], float)))


def _fit_value(event: dict, time_s: float) -> tuple[float, float]:
    times = np.asarray(event["time_s"], float)
    values = np.asarray(event["centroid_hz"], float)
    slope, intercept = np.polyfit(times-times.mean(), values, 1)
    return float(intercept+slope*(time_s-times.mean())), float(slope)


def associate_boundary_tracks(before: list[dict], after: list[dict], *,
                              center_delta_hz: float,
                              boundary_time_s: float,
                              maximum_candidate_error_hz: float = 100_000,
                              maximum_candidate_slope_difference_hz_s: float = 3_000,
                              maximum_qualified_error_hz: float = 20_000,
                              maximum_qualified_slope_difference_hz_s: float = 500,
                              maximum_extrapolation_s: float = 5.0) -> list[dict]:
    """Compare raw-baseband tracks under sky-fixed and baseband-fixed hypotheses."""
    candidates = []
    for first in before:
        for second in after:
            pre_extrapolation = boundary_time_s-float(first["stop_elapsed_s"])
            post_extrapolation = float(second["start_elapsed_s"])-boundary_time_s
            if (not 0 <= pre_extrapolation <= maximum_extrapolation_s or
                    not 0 <= post_extrapolation <= maximum_extrapolation_s):
                continue
            receiver_rows = []
            for receiver in range(2):
                left = first["receivers"][receiver]; right = second["receivers"][receiver]
                left_hz, left_slope = _fit_value(left, boundary_time_s)
                right_hz, right_slope = _fit_value(right, boundary_time_s)
                observed_step = right_hz-left_hz
                receiver_rows.append({"receiver": receiver,
                    "predicted_pre_baseband_hz": left_hz,
                    "predicted_post_baseband_hz": right_hz,
                    "observed_raw_step_hz": observed_step,
                    "expected_sky_step_hz": -center_delta_hz,
                    "sky_step_error_hz": observed_step+center_delta_hz,
                    "baseband_step_error_hz": observed_step,
                    "pre_drift_hz_s": left_slope, "post_drift_hz_s": right_slope,
                    "slope_difference_hz_s": abs(right_slope-left_slope)})
            sky_error = max(abs(row["sky_step_error_hz"]) for row in receiver_rows)
            baseband_error = max(abs(row["baseband_step_error_hz"]) for row in receiver_rows)
            slope_error = max(row["slope_difference_hz_s"] for row in receiver_rows)
            if min(sky_error, baseband_error) > maximum_candidate_error_hz:
                continue
            if slope_error > maximum_candidate_slope_difference_hz_s:
                continue
            sky_qualified = (sky_error <= maximum_qualified_error_hz and
                             slope_error <= maximum_qualified_slope_difference_hz_s)
            baseband_qualified = (baseband_error <= maximum_qualified_error_hz and
                                  slope_error <= maximum_qualified_slope_difference_hz_s)
            classification = ("sky-fixed" if sky_qualified and not baseband_qualified else
                              "baseband-fixed" if baseband_qualified and not sky_qualified else
                              "ambiguous")
            step_score = max(_score(sky_error if classification == "sky-fixed" else
                                    baseband_error, maximum_qualified_error_hz), 0.0)
            slope_score = _score(slope_error, maximum_qualified_slope_difference_hz_s)
            agreement_score = min(float(first["confidence"]["receiver_path_correlation"]),
                                  float(second["confidence"]["receiver_path_correlation"]))
            candidates.append({"before_track_id": first["track_id"],
                "after_track_id": second["track_id"],
                "boundary_elapsed_s": boundary_time_s,
                "pre_extrapolation_s": pre_extrapolation,
                "post_extrapolation_s": post_extrapolation,
                "center_delta_hz": center_delta_hz,
                "classification": classification,
                "sky_fixed_qualified": sky_qualified,
                "baseband_fixed_qualified": baseband_qualified,
                "receivers": receiver_rows,
                "confidence": {"step_score": step_score, "slope_score": slope_score,
                    "receiver_agreement_score": agreement_score,
                    "overall": float(np.mean((step_score, slope_score, agreement_score)))},
                "rejection_reasons": ([] if classification != "ambiguous" else
                    ["boundary step or slope is not uniquely consistent with one hypothesis"])})
    # One-to-one greedy association, strongest evidence first.
    selected, used_before, used_after = [], set(), set()
    for item in sorted(candidates, key=lambda row: row["confidence"]["overall"], reverse=True):
        if item["before_track_id"] in used_before or item["after_track_id"] in used_after:
            continue
        selected.append(item); used_before.add(item["before_track_id"])
        used_after.add(item["after_track_id"])
    return selected


def _segments(centers: np.ndarray) -> list[tuple[int, int]]:
    changes = np.flatnonzero(np.diff(centers) != 0)+1
    boundaries = np.concatenate(([0], changes, [len(centers)]))
    return [(int(a), int(b)) for a, b in zip(boundaries[:-1], boundaries[1:])]


def analyze_doppler_observations(path: Path, *, threshold_db: float = .35,
                                 event_frequency_bins: int = 1024,
                                 stable_guard_s: float = .75,
                                 minimum_track_duration_s: float = 3.0,
                                 minimum_abs_drift_hz_s: float = 250,
                                 maximum_abs_drift_hz_s: float = 10_000,
                                 assume_all_shifts_doppler: bool = False) -> dict[str, Any]:
    artifact = load_measurement_waterfall(path)
    spectra = np.asarray(artifact["psd_db_raw_per_hz"], float)
    utc_ns = np.asarray(artifact["utc_ns"], np.int64)
    elapsed = (utc_ns-utc_ns[0])/1e9
    frequencies = np.asarray(artifact["frequency_offsets_hz"], float)
    sample_rate = float(artifact["sample_rate_hz"])
    nominal_center = float(artifact["center_frequency_hz"])
    lnb_lo = float(artifact.get("lnb_lo_hz", 0))
    centers = np.asarray(artifact.get("center_frequency_hz_by_snapshot",
                                     np.full(elapsed.size, nominal_center)), float)
    if event_frequency_bins < spectra.shape[2]:
        if spectra.shape[2] % event_frequency_bins:
            raise ValueError("event bins must divide stored frequency bins")
        width = spectra.shape[2]//event_frequency_bins
        linear = np.power(10.0, spectra/10.0)
        spectra = 10*np.log10(linear.reshape(
            *spectra.shape[:2], event_frequency_bins, width).mean(axis=3))
        frequencies = frequencies.reshape(event_frequency_bins, width).mean(axis=1)
    segment_reports, all_tracks = [], []
    for segment_index, (first_index, stop_index) in enumerate(_segments(centers)):
        segment_start = float(elapsed[first_index]); selected = np.arange(first_index, stop_index)
        selected = selected[elapsed[selected] >= segment_start+stable_guard_s]
        center = float(centers[first_index])
        segment = {"segment_index": segment_index, "state": (
            "nominal" if center == nominal_center else "shifted"),
            "center_frequency_hz": center,
            "start_utc": _utc(int(utc_ns[first_index])),
            "stop_utc": _utc(int(utc_ns[stop_index-1])),
            "start_elapsed_s": float(elapsed[first_index]),
            "stop_elapsed_s": float(elapsed[stop_index-1]),
            "stable_guard_s": stable_guard_s, "retained_snapshots": int(selected.size),
            "monitored_if_low_hz": center-sample_rate/2,
            "monitored_if_high_hz": center+sample_rate/2,
            "monitored_rf_low_hz": lnb_lo+center-sample_rate/2,
            "monitored_rf_high_hz": lnb_lo+center+sample_rate/2}
        tracks = []
        if selected.size >= 3:
            events = detect_receiver_events(spectra[:, selected], elapsed[selected], frequencies,
                threshold_db=threshold_db, min_time_bins=3, min_frequency_bins=3)
            pairs = associate_receiver_events(events[0], events[1])
            for pair_index, pair in enumerate(pairs):
                left, right = events[0][pair.rx0_index].to_dict(), events[1][pair.rx1_index].to_dict()
                duration = min(float(left["duration_s"]), float(right["duration_s"]))
                mean_drift = (pair.rx0_drift_hz_s+pair.rx1_drift_hz_s)/2
                reasons = []
                if pair.broadband: reasons.append("broadband event")
                if duration < minimum_track_duration_s: reasons.append("track is too short")
                if not minimum_abs_drift_hz_s <= abs(mean_drift) <= maximum_abs_drift_hz_s:
                    reasons.append("drift outside tracked LEO range")
                if pair.centered_path_correlation < .7: reasons.append("receiver paths disagree")
                if pair.drift_difference_hz_s > 1_500: reasons.append("receiver slopes disagree")
                start = max(left["start_time_s"], right["start_time_s"])
                stop = min(left["stop_time_s"], right["stop_time_s"])
                receiver_rows = []
                for receiver, event, drift in ((0, left, pair.rx0_drift_hz_s),
                                                (1, right, pair.rx1_drift_hz_s)):
                    start_bb, stop_bb = _value_at(event, start), _value_at(event, stop)
                    path_times = [float(value) for value in event["time_s"]]
                    path_baseband = [float(value) for value in event["centroid_hz"]]
                    receiver_rows.append({"receiver": receiver, "time_s": event["time_s"],
                        "centroid_hz": event["centroid_hz"], "start_baseband_hz": start_bb,
                        "stop_baseband_hz": stop_bb, "start_rf_hz": lnb_lo+center+start_bb,
                        "stop_rf_hz": lnb_lo+center+stop_bb, "drift_hz_s": drift,
                        "peak_residual_db": event["peak_residual_db"],
                        "path_utc": [_utc(int(utc_ns[0]+round(value*1e9)))
                                     for value in path_times],
                        "path_rf_hz_assuming_lnb_lo": [lnb_lo+center+value
                                                       for value in path_baseband]})
                track = {"track_id": f"s{segment_index:03d}-t{pair_index:05d}",
                    "segment_index": segment_index, "start_utc": _utc(int(
                        utc_ns[0]+round(start*1e9))), "stop_utc": _utc(int(
                        utc_ns[0]+round(stop*1e9))), "start_elapsed_s": start,
                    "stop_elapsed_s": stop, "duration_s": duration,
                    "mean_drift_hz_s": mean_drift, "accepted": not reasons,
                    "validation_passed": not reasons,
                    "assumed_doppler": bool(assume_all_shifts_doppler),
                    "processed_as_doppler": bool(assume_all_shifts_doppler or not reasons),
                    "rejection_reasons": reasons, "receivers": receiver_rows,
                    "confidence": {"receiver_path_correlation": pair.centered_path_correlation,
                        "receiver_slope_agreement": _score(pair.drift_difference_hz_s, 1_500),
                        "association_score": pair.association_score,
                        "overall": float(np.mean((max(0.0, pair.centered_path_correlation),
                            _score(pair.drift_difference_hz_s, 1_500), pair.association_score)))}}
                tracks.append(track); all_tracks.append(track)
        segment["track_count"] = len(tracks)
        segment["accepted_track_count"] = sum(item["accepted"] for item in tracks)
        segment_reports.append(segment)
    boundary_tests = []
    accepted_by_segment = {index: [item for item in all_tracks
        if item["segment_index"] == index and item["accepted"]]
        for index in range(len(segment_reports))}
    for index in range(len(segment_reports)-1):
        first, second = segment_reports[index], segment_reports[index+1]
        boundary = (float(first["stop_elapsed_s"])+float(second["start_elapsed_s"]))/2
        tests = associate_boundary_tracks(accepted_by_segment[index],
            accepted_by_segment[index+1],
            center_delta_hz=float(second["center_frequency_hz"]-first["center_frequency_hz"]),
            boundary_time_s=boundary)
        for item in tests:
            item["boundary_index"] = index
            item["boundary_utc"] = _utc(int(utc_ns[0]+round(boundary*1e9)))
        boundary_tests.extend(tests)
    identity = json.loads(str(artifact.get("identity_json", "{}")))
    retained_s = (elapsed.size*int(artifact["samples_per_snapshot"])/sample_rate)
    observation_span_s = float(elapsed[-1]+int(artifact["samples_per_snapshot"])/sample_rate)
    gains = np.asarray(artifact.get("hardware_gain_db", []), float)
    clips = np.asarray(artifact.get("clip_fraction", []), float)
    configured_gain = float(artifact["configured_gain_db"])
    configured_gain = configured_gain if math.isfinite(configured_gain) else None
    gain_reports = []
    if gains.size:
        for receiver in range(gains.shape[0]):
            finite = gains[receiver][np.isfinite(gains[receiver])]
            gain_reports.append({"receiver": receiver,
                "minimum": None if not finite.size else float(np.min(finite)),
                "median": None if not finite.size else float(np.median(finite)),
                "maximum": None if not finite.size else float(np.max(finite))})
    return {"schema": SCHEMA, "processing_policy": {
        "mode": ("assume-all-detected-shifts-are-doppler"
                 if assume_all_shifts_doppler else "validated-only"),
        "assume_all_shifts_doppler": bool(assume_all_shifts_doppler),
        "note": ("Validation results and rejection reasons are preserved; every detected "
                 "dual-receiver frequency-shift track is processed as Doppler."
                 if assume_all_shifts_doppler else
                 "Only tracks passing the configured validation gates are processed as Doppler.")},
        "capture": {"artifact": str(path),
        "capture_id": Path(path).stem, "start_utc": _utc(int(utc_ns[0])),
        "stop_utc": _utc(int(utc_ns[-1])), "receiver_count": int(spectra.shape[0]),
        "retained_sample_time_s": retained_s,
        "observation_span_s": observation_span_s,
        "duty_fraction": retained_s/observation_span_s,
        "gain_mode": str(artifact["gain_mode"]),
        "configured_gain_db": configured_gain,
        "hardware_gain_db": gain_reports,
        "maximum_clip_fraction": None if not clips.size or np.all(np.isnan(clips))
            else float(np.nanmax(clips)),
        "identity": identity}, "monitoring": {"sample_rate_hz": sample_rate,
        "bandwidth_hz": float(artifact["bandwidth_hz"]), "lnb_lo_hz": lnb_lo,
        "fft_size": int(artifact["fft_size"]),
        "stored_frequency_bins": int(np.asarray(
            artifact["frequency_offsets_hz"]).size),
        "event_frequency_bins": int(frequencies.size),
        "samples_per_snapshot": int(artifact["samples_per_snapshot"]),
        "psd_quantization_db": (None if "psd_db_quantization_db" not in artifact
            else float(artifact["psd_db_quantization_db"])),
        "frequency_bin_width_hz": sample_rate/frequencies.size,
        "nominal_center_frequency_hz": nominal_center, "segments": segment_reports},
        "detections": {"frequency_shift_tracks": all_tracks,
            "boundary_tests": boundary_tests}, "summary": {
            "segment_count": len(segment_reports), "track_count": len(all_tracks),
            "accepted_track_count": sum(item["accepted"] for item in all_tracks),
            "processed_as_doppler_count": sum(item["processed_as_doppler"]
                                               for item in all_tracks),
            "assumed_doppler_count": sum(item["assumed_doppler"] for item in all_tracks),
            "boundary_test_count": len(boundary_tests),
            "sky_fixed_count": sum(item["sky_fixed_qualified"] for item in boundary_tests),
            "baseband_fixed_count": sum(item["baseband_fixed_qualified"] for item in boundary_tests),
            "ambiguous_boundary_count": sum(item["classification"] == "ambiguous"
                                              for item in boundary_tests)}}


def write_doppler_observations(path: Path, output: Path, **kwargs) -> dict[str, Any]:
    report = analyze_doppler_observations(path, **kwargs)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True,
                                 allow_nan=False)+"\n")
    return report
