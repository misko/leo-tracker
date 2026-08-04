"""Population-level summaries for finite dual-receiver RF features."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
import re
from statistics import median
from typing import Iterable

import numpy as np
from scipy.stats import chi2


SCHEMA = "leo-tracker.wide-feature-population/v1"
_CHANNEL_CARRIERS_HZ = {3: 11_325_117_187.5, 4: 11_575_117_187.5}


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc).timestamp()


def _median(values: list[float]) -> float | None:
    return None if not values else float(median(values))


def _poisson_rate_interval(count: int, exposure_s: float, confidence: float = .95) -> list[float] | None:
    if exposure_s <= 0:
        return None
    alpha = 1-confidence; hours = exposure_s/3600
    lower = 0.0 if count == 0 else .5*chi2.ppf(alpha/2, 2*count)/hours
    upper = .5*chi2.ppf(1-alpha/2, 2*(count+1))/hours
    return [float(lower), float(upper)]


def _family(candidate: dict, narrow_width_hz: float) -> str:
    widths = [float(item["median_width_hz"]) for item in candidate.get("receivers", [])]
    return "narrow_swept" if widths and median(widths) <= narrow_width_hz else "broad_state"


def _carrier_for_source(source: str | None, reported: float | None) -> float | None:
    match = None if source is None else re.search(r"-ch([34])-", Path(source).name)
    return _CHANNEL_CARRIERS_HZ[int(match.group(1))] if match else reported


def summarize_wide_reports(reports: Iterable[tuple[Path, dict]], *,
                           narrow_width_hz: float = 45_000) -> dict:
    """Summarize qualified features without treating each as a satellite ID.

    The family label is morphological.  ``narrow_swept`` means only that the
    instantaneous feature width is narrow; it is not a protocol classifier.
    """
    rows: list[dict] = []
    report_count = 0
    exposure_by_carrier: dict[str, float] = {}
    measured_sources: set[str] = set()
    morphology: list[dict] = []
    for path, report in reports:
        report_count += 1
        source = report.get("source")
        carrier = _carrier_for_source(source, report.get("rf_carrier_hz"))
        if source and source not in measured_sources:
            try:
                with np.load(source, allow_pickle=False) as stored:
                    utc = np.asarray(stored["utc_ns"], np.int64)
                    duration = ((utc[-1]-utc[0])/1e9+
                                float(stored["samples_per_snapshot"])/
                                float(stored["sample_rate_hz"]))
                key = "unknown" if carrier is None else f"{float(carrier):.1f}"
                exposure_by_carrier[key] = exposure_by_carrier.get(key, 0.0)+float(duration)
                measured_sources.add(source)
            except (OSError, KeyError, ValueError):
                pass
        for index, candidate in enumerate(report.get("candidates", [])):
            receivers = candidate.get("receivers", [])
            widths = [float(item["median_width_hz"]) for item in receivers]
            instantaneous_width = _median(widths)
            morphology.append({"family": _family(candidate, narrow_width_hz),
                "qualified": bool(candidate.get("leo_like_qualified", False)),
                "moving_rf_qualified": bool(candidate.get("moving_rf_qualified",
                                                            candidate.get("leo_like_qualified", False))),
                "doppler_candidate_qualified": bool(candidate.get(
                    "doppler_candidate_qualified", False)),
                "orbital_shape_qualified": bool(candidate.get("orbital_shape_qualified", False)),
                "orbital_curvature_observable": bool(candidate.get(
                    "orbital_curvature_observable", False)),
                "duration_s": float(candidate["duration_s"]),
                "instantaneous_width_hz": instantaneous_width,
                "abs_mean_drift_hz_s": abs(float(candidate.get("mean_drift_hz_s") or
                    np.mean([item["linear_drift_hz_s"] for item in receivers]))),
                "receiver_path_correlation": float(candidate["receiver_path_correlation"]),
                "global_frequency_control_passed": candidate.get(
                    "global_frequency_control_passed"),
                "rejection_reasons": candidate.get("rejection_reasons", [])})
            if not candidate.get("leo_like_qualified", False):
                continue
            centers = [float(item["median_center_rf_hz"]) for item in receivers]
            drifts = [float(item["linear_drift_hz_s"]) for item in receivers]
            mean_drift = candidate.get("mean_drift_hz_s")
            if mean_drift is None and drifts:
                mean_drift = sum(drifts)/len(drifts)
            carrier = _carrier_for_source(source, report.get("rf_carrier_hz"))
            center = None if not centers else sum(centers)/len(centers)
            rows.append({
                "report": str(path), "candidate_index": index,
                "source": report.get("source"), "family": _family(candidate, narrow_width_hz),
                "polarity": candidate.get("polarity"),
                "start_utc": candidate.get("start_utc"),
                "duration_s": float(candidate["duration_s"]),
                "instantaneous_width_hz": instantaneous_width,
                "swept_bounding_width_hz": float(candidate["bounding_width_hz"]),
                "mean_center_rf_hz": center, "rf_carrier_hz": carrier,
                "center_offset_from_channel_hz": (None if center is None or carrier is None
                                                    else center-float(carrier)),
                "mean_drift_hz_s": None if mean_drift is None else float(mean_drift),
                "radial_acceleration_m_s2": candidate.get("radial_acceleration_m_s2"),
                "receiver_path_correlation": float(candidate["receiver_path_correlation"]),
                "global_frequency_control_available": candidate.get(
                    "global_frequency_control_available", False),
                "global_frequency_control_passed": candidate.get(
                    "global_frequency_control_passed"),
                "specific_tle_identifiable": bool(candidate.get("specific_tle_identifiable", False)),
                "tles_within_one_bin_of_best": int(candidate.get(
                    "tles_within_one_bin_of_best", 0)),
                "orbital_curvature_observable": bool(candidate.get(
                    "orbital_curvature_observable", False)),
                "best_tle_curvature_resolution_bins": candidate.get(
                    "best_tle_curvature_resolution_bins"),
                "common_internal_peak_count": int(candidate.get("common_internal_peak_count", 0)),
                "common_internal_peak_spacings_hz": candidate.get(
                    "common_internal_peak_spacings_hz", []),
            })
    rows.sort(key=lambda row: (row["start_utc"] or "", row["report"], row["candidate_index"]))
    families = []
    for name in ("narrow_swept", "broad_state"):
        selected = [row for row in rows if row["family"] == name]
        if not selected:
            continue
        family_exposure = sum(exposure_by_carrier.values())
        by_carrier = []
        for carrier_key, exposure_s in sorted(exposure_by_carrier.items()):
            count = sum(row["rf_carrier_hz"] is not None and
                        f"{float(row['rf_carrier_hz']):.1f}" == carrier_key for row in selected)
            by_carrier.append({"rf_carrier_hz": (None if carrier_key == "unknown"
                                                  else float(carrier_key)),
                               "count": count, "observation_s": exposure_s,
                               "features_per_observed_hour": (None if exposure_s <= 0 else
                                                               count/(exposure_s/3600)),
                               "features_per_observed_hour_95pct_poisson_interval":
                                   _poisson_rate_interval(count, exposure_s)})
        families.append({
            "family": name, "count": len(selected),
            "median_duration_s": _median([row["duration_s"] for row in selected]),
            "median_instantaneous_width_hz": _median([
                row["instantaneous_width_hz"] for row in selected
                if row["instantaneous_width_hz"] is not None]),
            "median_abs_drift_hz_s": _median([abs(row["mean_drift_hz_s"]) for row in selected
                                               if row["mean_drift_hz_s"] is not None]),
            "median_receiver_path_correlation": _median([
                row["receiver_path_correlation"] for row in selected]),
            "global_control_pass_count": sum(row["global_frequency_control_passed"] is True
                                               for row in selected),
            "specific_tle_count": sum(row["specific_tle_identifiable"] for row in selected),
            "features_per_observed_hour": (None if family_exposure <= 0 else
                                            len(selected)/(family_exposure/3600)),
            "features_per_observed_hour_95pct_poisson_interval": _poisson_rate_interval(
                len(selected), family_exposure),
            "by_rf_carrier": by_carrier,
        })
    starts = [row["start_utc"] for row in rows if row["start_utc"]]
    event_times = sorted(_timestamp(value) for value in starts)
    morphology_audit = []
    for family_name in ("narrow_swept", "broad_state"):
        selected = [item for item in morphology if item["family"] == family_name]
        qualified_items = [item for item in selected if item["qualified"]]
        rejected_items = [item for item in selected if not item["qualified"]]
        reasons = Counter(reason for item in rejected_items for reason in item["rejection_reasons"])
        def metrics(items):
            return {"count": len(items),
                "median_duration_s": _median([item["duration_s"] for item in items]),
                "median_instantaneous_width_hz": _median([
                    item["instantaneous_width_hz"] for item in items
                    if item["instantaneous_width_hz"] is not None]),
                "median_abs_drift_hz_s": _median([item["abs_mean_drift_hz_s"] for item in items]),
                "median_receiver_path_correlation": _median([
                    item["receiver_path_correlation"] for item in items]),
                "global_control_pass_count": sum(
                    item["global_frequency_control_passed"] is True for item in items)}
        morphology_audit.append({"family": family_name, "all": metrics(selected),
            "qualified": metrics(qualified_items), "rejected": metrics(rejected_items),
            "moving_rf_qualified_count": sum(item["moving_rf_qualified"] for item in selected),
            "doppler_candidate_qualified_count": sum(
                item["doppler_candidate_qualified"] for item in selected),
            "orbital_curvature_observable_count": sum(
                item["orbital_curvature_observable"] for item in selected),
            "orbital_shape_qualified_count": sum(item["orbital_shape_qualified"] for item in selected),
            "rejection_reason_counts": dict(sorted(reasons.items()))})
    return {
        "schema": SCHEMA, "report_count": report_count,
        "qualified_feature_count": len(rows),
        "doppler_candidate_count": sum(
            item["doppler_candidate_qualified"] for item in morphology),
        "narrow_width_threshold_hz": narrow_width_hz,
        "classification_scope": "morphology only; neither family label identifies a protocol or spacecraft",
        "frequency_basis": ("frequency shifts remain unchanged through fixed LNB mixing; radial "
                            "quantities use each report's original Ku-band RF carrier"),
        "exposure": {"total_observation_s": float(sum(exposure_by_carrier.values())),
                     "by_rf_carrier_s": exposure_by_carrier,
                     "source_count": len(measured_sources)},
        "event_timing": {"first_start_utc": None if not starts else min(starts),
                         "last_start_utc": None if not starts else max(starts),
                         "successive_start_gaps_s": np.diff(event_times).tolist()},
        "morphology_audit": morphology_audit,
        "families": families, "features": rows,
    }


def write_wide_population(inputs: Iterable[Path], output: Path, *,
                          narrow_width_hz: float = 45_000) -> dict:
    paths: list[Path] = []
    for input_path in inputs:
        input_path = Path(input_path)
        paths.extend(sorted(input_path.glob("*.json")) if input_path.is_dir() else [input_path])
    reports = []
    for path in paths:
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read wide-feature report {path}: {exc}") from exc
        if report.get("schema") != "leo-tracker.wide-feature-search/v1":
            continue
        reports.append((path, report))
    result = summarize_wide_reports(reports, narrow_width_hz=narrow_width_hz)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    return result
