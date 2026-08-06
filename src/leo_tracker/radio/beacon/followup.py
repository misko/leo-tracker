"""Dense neighborhood replay and temporal confirmation of beacon triggers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

from .analysis import analyze_exact_window
from .artifact import BeaconCapture
from .template_learning import load_learned_beacon

FOLLOWUP_SCHEMA = "leo-tracker.starlink-beacon-followup/v1"


def _measurement_frequency(check: dict, receiver: int) -> float:
    full = check.get("full_frame_evidence", {})
    if full.get("candidate"):
        receivers = full.get("receivers", [])
        if len(receivers) == 2:
            return float(receivers[receiver]["frequency_offset_hz"])
    return float(check["receivers"][receiver]["pilot"]["frequency_offset_hz"])


def _pass_overlaps(capture_path: Path, confirmation: dict,
                   passes_path: Path | None) -> list[dict]:
    if passes_path is None or not Path(passes_path).is_file():
        return []
    capture = BeaconCapture.open(capture_path, verify=True)
    first_utc_ns = capture.manifest.get("chunks", [{}])[0].get("first_utc_ns")
    confirmed_times = [
        (item["start_s"] + item["stop_s"]) / 2
        for key in ("cross_receiver_links", "dual_receiver_links")
        for item in confirmation.get(key, [])]
    for receiver in confirmation.get("receivers", []):
        confirmed_times.extend((item["start_s"] + item["stop_s"]) / 2
                               for item in receiver.get("links", []))
    if not first_utc_ns or not confirmed_times:
        return []
    observation = datetime.fromtimestamp(
        (first_utc_ns / 1e9) + min(confirmed_times), tz=timezone.utc)
    predictions = json.loads(Path(passes_path).read_text())
    overlaps = []
    for satellite in predictions.get("satellites", []):
        for predicted in satellite.get("passes", []):
            rise = datetime.fromisoformat(predicted["rise"]["time"].replace("Z", "+00:00"))
            setting = datetime.fromisoformat(predicted["set"]["time"].replace("Z", "+00:00"))
            if rise <= observation <= setting:
                track = predicted.get("track") or []
                if not track:
                    continue
                nearest = min(track, key=lambda point: abs(
                    datetime.fromisoformat(point["time"].replace("Z", "+00:00")) - observation))
                overlaps.append({"name": satellite.get("name", "").strip(),
                    "norad_id": satellite.get("norad_id"),
                    "observation_utc": observation.isoformat(),
                    "nearest_prediction": nearest,
                    "culmination_elevation_deg": predicted["culmination"]["elevation_deg"]})
    return sorted(overlaps, key=lambda item: item["culmination_elevation_deg"], reverse=True)


def summarize_temporal_confirmation(checks: list[dict], *, interval_s: float) -> dict:
    """Require repeated exact/control evidence with stable frame epoch and CFO."""
    receivers = []
    for receiver in range(2):
        points = [item for item in checks if item.get("receiver_candidates", [False, False])[receiver]]
        links = []
        for first, second in zip(points, points[1:]):
            elapsed = second["start_s"] - first["start_s"]
            if elapsed <= 0 or elapsed > 1.6 * interval_s:
                continue
            a = first["receivers"][receiver]
            b = second["receivers"][receiver]
            period = a["acquisition"]["subband_rate_hz"] / 750
            epoch_delta = abs(a["acquisition"]["selected_epoch_sample"] -
                              b["acquisition"]["selected_epoch_sample"])
            epoch_delta = min(epoch_delta, period - epoch_delta)
            first_cfo = _measurement_frequency(first, receiver)
            second_cfo = _measurement_frequency(second, receiver)
            cfo_delta = abs(first_cfo - second_cfo)
            if epoch_delta <= 20 and cfo_delta <= 25_000 + 15_000 * elapsed:
                links.append({"start_s": first["start_s"], "stop_s": second["start_s"],
                              "epoch_delta_samples": epoch_delta,
                              "cfo_delta_hz": cfo_delta,
                              "drift_hz_s": (second_cfo - first_cfo) / elapsed})
        receivers.append({"receiver": receiver, "candidate_point_count": len(points),
                          "confirmed_link_count": len(links), "confirmed": bool(links),
                          "links": links})
    cross_links = []
    cross_points = [item for item in checks if item.get("epoch_difference_samples", np.inf) <= 20
                    and any(item.get("receiver_candidates", []))]
    for first, second in zip(cross_points, cross_points[1:]):
        elapsed = second["start_s"] - first["start_s"]
        if elapsed <= 0 or elapsed > 4.1 * interval_s:
            continue
        period = first["receivers"][0]["acquisition"]["subband_rate_hz"] / 750
        first_epoch = np.mean([item["acquisition"]["selected_epoch_sample"]
                               for item in first["receivers"]])
        second_epoch = np.mean([item["acquisition"]["selected_epoch_sample"]
                                for item in second["receivers"]])
        epoch_delta = abs(first_epoch - second_epoch)
        epoch_delta = min(epoch_delta, period - epoch_delta)
        first_cfo = np.mean([first["receivers"][index]["pilot"]["frequency_offset_hz"]
                             for index, selected in enumerate(first["receiver_candidates"])
                             if selected])
        second_cfo = np.mean([second["receivers"][index]["pilot"]["frequency_offset_hz"]
                              for index, selected in enumerate(second["receiver_candidates"])
                              if selected])
        cfo_delta = abs(first_cfo - second_cfo)
        if epoch_delta <= 20 and cfo_delta <= 25_000 + 15_000 * elapsed:
            cross_links.append({"start_s": first["start_s"], "stop_s": second["start_s"],
                                "epoch_delta_samples": float(epoch_delta),
                                "cfo_delta_hz": float(cfo_delta),
                                "drift_hz_s": float((second_cfo - first_cfo) / elapsed),
                                "candidate_receivers": [
                                    [index for index, value in enumerate(first["receiver_candidates"])
                                     if value],
                                    [index for index, value in enumerate(second["receiver_candidates"])
                                     if value]]})
    # The pilot-code likelihood has cyclic timing aliases at low SNR. A real
    # common RF signal can therefore move between local epoch aliases over time
    # while both synchronous receivers still choose exactly the same alias at
    # each instant. Confirm that case from dual-RX code evidence and matched
    # Doppler slopes instead of requiring one alias to remain globally stable.
    dual_links = []
    dual_points = [item for item in checks if item.get("candidate") and
                   item.get("epoch_difference_samples", np.inf) <= 20]
    for first, second in zip(dual_points, dual_points[1:]):
        elapsed = second["start_s"] - first["start_s"]
        if elapsed <= 0 or elapsed > 1.6 * interval_s:
            continue
        slopes = []
        for receiver in range(2):
            before = _measurement_frequency(first, receiver)
            after = _measurement_frequency(second, receiver)
            slopes.append((after - before) / elapsed)
        slope_difference = abs(slopes[0] - slopes[1])
        if max(map(abs, slopes)) <= 15_000 and slope_difference <= 2_500:
            dual_links.append({"start_s": first["start_s"],
                               "stop_s": second["start_s"],
                               "receiver_slopes_hz_s": list(map(float, slopes)),
                               "slope_difference_hz_s": float(slope_difference),
                               "first_epoch_difference_samples": float(
                                   first["epoch_difference_samples"]),
                               "second_epoch_difference_samples": float(
                                   second["epoch_difference_samples"])})
    same_receiver_confirmed = any(item["confirmed"] for item in receivers)
    cross_receiver_confirmed = bool(cross_links)
    dual_receiver_confirmed = bool(dual_links)
    dual_count = sum(item.get("candidate", False) for item in checks)
    return {"confirmed": (same_receiver_confirmed or cross_receiver_confirmed or
                           dual_receiver_confirmed),
            "same_receiver_confirmed": same_receiver_confirmed,
            "cross_receiver_confirmed": cross_receiver_confirmed,
            "dual_receiver_confirmed": dual_receiver_confirmed,
            "cross_receiver_links": cross_links,
            "dual_receiver_links": dual_links,
            "dual_candidate_point_count": dual_count, "receivers": receivers,
            "requirements": {"consecutive_max_gap_intervals": 1.6,
                             "cross_receiver_max_gap_intervals": 4.1,
                             "maximum_epoch_delta_samples": 20,
                             "maximum_drift_hz_s": 15_000,
                             "maximum_dual_slope_difference_hz_s": 2_500,
                             "cfo_slack_hz": 25_000}}


def followup_capture(capture_path: Path, analysis_path: Path, output: Path, *,
                     radius_s: float = .5, interval_s: float = .1,
                     window_s: float = .01, passes_path: Path | None = None) -> dict:
    if min(radius_s, interval_s, window_s) <= 0:
        raise ValueError("follow-up radius, interval, and window must be positive")
    analysis = json.loads(Path(analysis_path).read_text())
    triggers = [item for item in analysis.get("exact_checks", [])
                if item.get("followup_trigger") or any(item.get("receiver_candidates", []))]
    report = {"schema": FOLLOWUP_SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
              "capture": str(Path(capture_path).resolve()),
              "source_analysis": str(Path(analysis_path).resolve()),
              "settings": {"radius_s": radius_s, "interval_s": interval_s,
                           "window_s": window_s}, "trigger_count": len(triggers),
              "checks": [], "confirmation": {"confirmed": False, "receivers": []},
              "overlapping_passes": []}
    if triggers:
        capture = BeaconCapture.open(capture_path, verify=True)
        source_rate = float(capture.manifest["sample_rate_hz"])
        duration_s = capture.manifest["captured_samples_per_receiver"] / source_rate
        config = analysis.get("analysis", {})
        learned_templates = None
        learned_template_source = config.get("learned_beacon")
        if learned_template_source:
            learned_report, learned_arrays = load_learned_beacon(
                Path(learned_template_source))
            if (not learned_report.get("summary", {}).get("qualified", False) or
                    abs(float(learned_report["sample_rate_hz"]) - source_rate) > 1e-6 or
                    learned_report["region"] != capture.manifest.get(
                        "metadata", {}).get("region")):
                raise ValueError("analysis learned beacon does not match follow-up capture")
            learned_templates = tuple(learned_arrays[f"template_rx{receiver}"]
                                      for receiver in range(2))
        starts = set()
        for trigger in triggers:
            first = max(0.0, trigger["start_s"] - radius_s)
            stop = min(duration_s - window_s, trigger["start_s"] + radius_s)
            count = max(0, int(np.floor((stop - first) / interval_s)) + 1)
            starts.update(round(first + index * interval_s, 9) for index in range(count))
        region = capture.manifest.get("metadata", {}).get("region", "center")
        edge = region.removesuffix("-edge")
        for start_s in sorted(starts):
            first_sample = round(start_s * source_rate)
            values = capture.read_window(first_sample, round(window_s * source_rate))
            report["checks"].append(analyze_exact_window(values, source_rate, edge=edge,
                start_sample=first_sample,
                acquisition_span_hz=float(config.get("acquisition_span_hz", 0)),
                acquisition_step_hz=float(config.get("acquisition_step_hz", 500_000)),
                exact_subband_rate_hz=float(config.get("exact_subband_rate_hz", 2_500_000)),
                acquisition_method=str(config.get(
                    "exact_acquisition_method", "coherent_grid_v1")),
                learned_templates=learned_templates,
                learned_template_source=learned_template_source))
        report["confirmation"] = summarize_temporal_confirmation(
            report["checks"], interval_s=interval_s)
        report["overlapping_passes"] = _pass_overlaps(
            capture_path, report["confirmation"], passes_path)
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(output)
    return report


def rescore_followup(path: Path, *, passes_path: Path | None = None) -> dict:
    """Reapply current confirmation logic to saved expensive replay checks."""
    path = Path(path)
    report = json.loads(path.read_text())
    if report.get("schema") != FOLLOWUP_SCHEMA:
        raise ValueError("not a Starlink beacon follow-up report")
    interval_s = float(report.get("settings", {}).get("interval_s", .1))
    report["confirmation"] = summarize_temporal_confirmation(
        report.get("checks", []), interval_s=interval_s)
    report["overlapping_passes"] = _pass_overlaps(
        Path(report["capture"]), report["confirmation"], passes_path)
    report["rescored_utc"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(path.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return report
