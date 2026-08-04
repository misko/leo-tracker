from __future__ import annotations

import json
from pathlib import Path
import statistics


def _paths(inputs):
    for source in inputs:
        source = Path(source)
        if source.is_dir():
            yield from sorted(source.glob("*.json"))
        else:
            yield source


def summarize_tracker_reports(inputs) -> dict:
    reports = []; coherent_reports = []
    for path in _paths(inputs):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if report.get("schema") == "leo-tracker.tracker-ensemble/v1":
            reports.append(report)
        elif report.get("schema") == "leo-tracker.coherent-doppler-ensemble/v1":
            coherent_reports.append(report)
    trackers = sorted({item.get("tracker") for report in reports
                       for item in report.get("candidates", []) if item.get("tracker")})
    rows = []
    for tracker in trackers:
        candidates = [item for report in reports for item in report.get("candidates", [])
                      if item.get("tracker") == tracker]
        qualified = [item for item in candidates if item.get("qualified")]
        joint = [item for report in reports for item in report.get("joint_tracks", [])
                 if item.get("tracker") == tracker]
        joint_qualified = [item for item in joint if item.get("qualified")]
        identifications = [item for report in reports
            for item in report.get("identifications", []) if item.get("tracker") == tracker]
        runtime_keys = {tracker}
        if tracker.startswith("broadband-"):
            runtime_keys.add("broadband-envelope-and-edges/v1")
        runtimes = [next((value for key in runtime_keys if (value :=
                    (report.get("metrics", {}).get("runtime_s_by_tracker", {}) or {}).get(key))
                    is not None), None)
                    for report in reports]
        runtimes = [float(item) for item in runtimes if item is not None]
        durations = [sum(max(0, float(stop)-float(start)) for start, stop in
                         report.get("configuration", {}).get("analysis_windows_s", []))
                     for report in reports]
        observed_hours = sum(durations)/3600
        slopes = [float(item["drift_hz_s"]) for item in qualified
                  if item.get("drift_hz_s") is not None]
        false_alarms = [float(item["false_alarm_probability"]) for item in candidates
                        if item.get("false_alarm_probability") is not None]
        rows.append({"tracker": tracker, "report_count": len(reports),
            "candidate_count": len(candidates), "qualified_count": len(qualified),
            "joint_track_count": len(joint),
            "qualified_joint_track_count": len(joint_qualified),
            "joint_qualification_fraction": (None if not joint else
                                               len(joint_qualified)/len(joint)),
            "tle_identification_count": len(identifications),
            "tle_compatible_count": sum(bool(item.get("compatible"))
                                         for item in identifications),
            "qualified_tle_identification_count": sum(
                bool(item.get("qualified")) for item in identifications),
            "observed_hours": observed_hours,
            "candidates_per_observed_hour": (None if observed_hours == 0 else
                                               len(candidates)/observed_hours),
            "qualified_per_observed_hour": (None if observed_hours == 0 else
                                               len(qualified)/observed_hours),
            "median_runtime_s": None if not runtimes else statistics.median(runtimes),
            "median_false_alarm_probability": (None if not false_alarms else
                                                  statistics.median(false_alarms)),
            "median_qualified_drift_hz_s": None if not slopes else statistics.median(slopes)})
    coherent_rows = []
    for method in ("fll", "polynomial_phase", "repetition"):
        values = [receiver.get(method) for report in coherent_reports
                  for block in report.get("blocks", [])
                  for receiver in block.get("receivers", []) if receiver.get(method)]
        drifts = [float(item["drift_hz_s"]) for item in values
                  if item.get("drift_hz_s") is not None]
        quality = ([float(item["median_coherence"]) for item in values]
                   if method == "fll" else
                   [float(item["phase_residual_rms_rad"]) for item in values]
                   if method == "polynomial_phase" else
                   [float(item["best_correlation"]) for item in values])
        coherent_rows.append({"tracker": ({"fll": "conjugate-product-fll/v1",
            "polynomial_phase": "polynomial-phase-pll/v1",
            "repetition": "blind-repetition-correlation/v1"})[method],
            "report_count": len(coherent_reports), "estimate_count": len(values),
            "median_drift_hz_s": None if not drifts else statistics.median(drifts),
            "quality_metric": ({"fll": "median_coherence",
                "polynomial_phase": "phase_residual_rms_rad",
                "repetition": "best_correlation"})[method],
            "median_quality": None if not quality else statistics.median(quality)})
    inter_block = [track for report in coherent_reports
                   for track in report.get("receiver_tracks", [])
                   if track.get("drift_hz_s") is not None]
    coherent_rows.append({"tracker": "inter-block-fll-track/v1",
        "report_count": len(coherent_reports), "estimate_count": len(inter_block),
        "qualified_count": sum(bool(item.get("qualified")) for item in inter_block),
        "median_drift_hz_s": (None if not inter_block else statistics.median(
            float(item["drift_hz_s"]) for item in inter_block)),
        "quality_metric": "residual_rms_hz",
        "median_quality": (None if not inter_block else statistics.median(
            float(item["residual_rms_hz"]) for item in inter_block))})
    return {"schema": "leo-tracker.tracker-performance/v1",
        "report_count": len(reports), "coherent_report_count": len(coherent_reports),
        "trackers": rows, "coherent_trackers": coherent_rows}


def write_tracker_summary(inputs, output: Path) -> dict:
    report = summarize_tracker_reports(inputs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    return report
