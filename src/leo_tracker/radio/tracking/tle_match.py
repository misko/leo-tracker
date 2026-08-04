"""Post-detection comparison of blind frequency tracks with predicted TLE motion."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path

import numpy as np

from .models import JointTrack, TrackCandidate
from .observation import load_tracking_observation


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def match_joint_tracks_to_tles(candidates: list[TrackCandidate],
                               joint_tracks: list[JointTrack], catalog: dict,
                               *, capture_start_unix_s: float,
                               observed_carrier_hz: float,
                               maximum_drift_difference_hz_s: float = 2_500) -> list[dict]:
    """Rank overlapping passes by Doppler-rate agreement.

    Frequency bias is deliberately ignored: independent LNB local oscillators
    can differ by hundreds of kHz. TLE matching compares time and derivative
    only, and only for blind tracks that already passed receiver-local and
    dual-receiver controls.
    """
    if maximum_drift_difference_hz_s <= 0 or observed_carrier_hz <= 0:
        raise ValueError("carrier and drift tolerance must be positive")
    catalog_carrier = float(catalog.get("carrier_hz", observed_carrier_hz))
    scale = observed_carrier_hz/catalog_carrier
    matches = []
    for joint_index, joint in enumerate(joint_tracks):
        if not joint.qualified:
            continue
        members = [candidates[index] for index in joint.member_indexes]
        start = capture_start_unix_s+max(item.start_time_s for item in members)
        stop = capture_start_unix_s+min(item.stop_time_s for item in members)
        if stop <= start:
            continue
        observed_drift = float(np.mean([item.drift_hz_s for item in members]))
        for satellite in catalog.get("satellites", []):
            for pass_index, pass_ in enumerate(satellite.get("passes", [])):
                points = sorted((pass_[name] for name in ("rise", "culmination", "set")),
                                key=lambda point: _timestamp(point["time"]))
                times = np.asarray([_timestamp(point["time"]) for point in points])
                overlap_start, overlap_stop = max(start, times[0]), min(stop, times[-1])
                if overlap_stop <= overlap_start:
                    continue
                doppler = np.asarray([float(point["expected_doppler_hz"])
                                      for point in points])*scale
                predicted_start, predicted_stop = np.interp(
                    [overlap_start, overlap_stop], times, doppler)
                predicted_drift = float(
                    (predicted_stop-predicted_start)/(overlap_stop-overlap_start))
                difference = abs(observed_drift-predicted_drift)
                duration = stop-start
                overlap_fraction = (overlap_stop-overlap_start)/duration
                confidence = overlap_fraction*math.exp(
                    -difference/maximum_drift_difference_hz_s)
                compatible = difference <= maximum_drift_difference_hz_s
                matches.append({"joint_track_index": joint_index,
                    "tracker": joint.tracker,
                    "norad_id": int(satellite["norad_id"]),
                    "name": str(satellite["name"]).strip(),
                    "pass_index": pass_index,
                    "observed_drift_hz_s": observed_drift,
                    "predicted_drift_hz_s": predicted_drift,
                    "drift_difference_hz_s": difference,
                    "overlap_fraction": overlap_fraction,
                    "confidence": confidence,
                    "compatible": compatible, "qualified": False,
                    "catalog_carrier_hz": catalog_carrier,
                    "observed_carrier_hz": observed_carrier_hz})
    # A close rate is compatibility, not identification: dense Starlink
    # constellations often contain many trajectories with nearly identical
    # short-window derivatives. Promote a specific name only when the best
    # compatible pass has a decisive margin over every alternative.
    for joint_index in {item["joint_track_index"] for item in matches}:
        group = sorted((item for item in matches
                        if item["joint_track_index"] == joint_index),
                       key=lambda item: item["confidence"], reverse=True)
        compatible_count = sum(item["compatible"] for item in group)
        second_confidence = group[1]["confidence"] if len(group) > 1 else 0.0
        for rank, item in enumerate(group, 1):
            item["rank_within_track"] = rank
            item["compatible_pass_count"] = compatible_count
            item["confidence_margin_to_next"] = (item["confidence"]-second_confidence
                                                   if rank == 1 else None)
            item["qualified"] = bool(rank == 1 and item["compatible"] and
                item["confidence"]-second_confidence >= .15)
            item["specific_identification"] = item["qualified"]
    return sorted(matches, key=lambda item: (item["qualified"], item["compatible"],
                                              item["confidence"]), reverse=True)


def rematch_tracker_report(report_path: Path, catalog_path: Path,
                           output: Path) -> dict:
    """Reapply an archived pass catalog without rerunning signal detection."""
    report = json.loads(report_path.read_text())
    if report.get("schema") != "leo-tracker.tracker-ensemble/v1":
        raise ValueError("input is not a tracker ensemble report")
    catalog = json.loads(catalog_path.read_text())
    candidates = [TrackCandidate(**item) for item in report.get("candidates", [])]
    joint = [JointTrack(**item) for item in report.get("joint_tracks", [])]
    observation = load_tracking_observation(Path(report["source"]))
    carrier = observation.center_frequency_hz+(observation.lnb_lo_hz or 0)
    report["identifications"] = match_joint_tracks_to_tles(candidates, joint, catalog,
        capture_start_unix_s=float(observation.utc_ns[0]/1e9-observation.time_s[0]),
        observed_carrier_hz=carrier)
    report.setdefault("configuration", {})["tle_catalog"] = {
        "path": str(catalog_path), "generated_at": catalog.get("generated_at"),
        "catalog_sha256": (catalog.get("source") or {}).get("catalog_sha256")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True)+"\n")
    return report
