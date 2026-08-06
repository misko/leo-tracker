"""Link sparse Starlink beacon arcs across RF-channel retunes.

This stage creates hypotheses, not satellite identities.  It compares Doppler
rate after normalizing by the actual Ku-band carrier and requires an unambiguous
nearest continuation.  The orbit stage subsequently accepts or rejects each
hypothesis against held-out TLE/SGP4 curvature.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import numpy as np

from leo_tracker.orbit.artifacts import parse_utc, utc_iso

from .continuous import TRACK_SCHEMA


CHANNEL_LINK_SCHEMA = TRACK_SCHEMA
SPEED_OF_LIGHT_M_S = 299_792_458.0


def _segment(report_path: Path, report: dict, track: dict, *,
             minimum_segment_epochs: int,
             minimum_segment_duration_s: float) -> dict | None:
    observations = [item for item in track.get("observations", [])
                    if item.get("utc") and item.get("consensus", {}).get("valid")]
    if len(observations) < minimum_segment_epochs:
        return None
    times = np.asarray([parse_utc(item["utc"]).timestamp() for item in observations])
    if float(np.ptp(times)) < minimum_segment_duration_s:
        return None
    cfo = np.asarray([item["consensus"]["receiver_referenced_cfo_hz"]
                      for item in observations], float)
    rf = float(report["signal"]["nominal_rf_hz"])
    reference = float(np.mean(times))
    slope = float(np.polyfit(times-reference, cfo, 1)[0])
    channel = int(report.get("capture_manifest", {}).get("metadata", {}).get(
        "channel_number", round((rf - 10_584_687_500) / 250_000_000) + 1))
    return {"report": str(Path(report_path).resolve()),
            "capture": report.get("capture"),
            "source_track_id": track["track_id"], "channel_number": channel,
            "nominal_rf_hz": rf, "start_s": float(np.min(times)),
            "stop_s": float(np.max(times)), "slope_hz_s": slope,
            "range_acceleration_m_s2": -SPEED_OF_LIGHT_M_S * slope / rf,
            "observations": observations}


def _joint_quadratic_fit(first: list[dict], second: list[dict]
                         ) -> tuple[float, float]:
    """Measure smooth same-tuning CFO continuity without filling an RF outage."""
    observations = first + second
    times = np.asarray([parse_utc(item["utc"]).timestamp()
                        for item in observations], float)
    frequencies = np.asarray([
        item["consensus"]["receiver_referenced_cfo_hz"]
        for item in observations], float)
    reference = float(np.mean(times))
    degree = min(2, len(np.unique(times)) - 1)
    model = np.polyfit(times - reference, frequencies, degree)
    residual = frequencies - np.polyval(model, times - reference)
    second_derivative_hz_s2 = float(2 * model[0]) if degree == 2 else 0.0
    return float(np.sqrt(np.mean(residual**2))), second_derivative_hz_s2


def link_channel_tracks(track_paths: list[Path], output: Path, *,
                        maximum_gap_s: float = 30.0,
                        maximum_acceleration_difference_m_s2: float = 35.0,
                        minimum_ambiguity_margin_m_s2: float = 5.0,
                        maximum_same_tuning_quadratic_rms_hz: float = 2_000.0,
                        maximum_same_tuning_range_jerk_m_s3: float = 5.0,
                        minimum_segment_epochs: int = 5,
                        minimum_segment_duration_s: float = .5,
                        ) -> dict:
    """Build conservative cross-channel continuation hypotheses."""
    if (not track_paths or min(maximum_gap_s,
            maximum_acceleration_difference_m_s2,
            minimum_ambiguity_margin_m_s2,
            maximum_same_tuning_quadratic_rms_hz,
            maximum_same_tuning_range_jerk_m_s3,
            minimum_segment_epochs, minimum_segment_duration_s) <= 0):
        raise ValueError("channel linking requires inputs and positive gates")
    segments = []
    source_track_count = 0
    for path in track_paths:
        report = json.loads(Path(path).read_text())
        if report.get("schema") != TRACK_SCHEMA:
            raise ValueError(f"not a continuous track artifact: {path}")
        for track in report.get("tracks", []):
            source_track_count += 1
            item = _segment(Path(path), report, track,
                minimum_segment_epochs=minimum_segment_epochs,
                minimum_segment_duration_s=minimum_segment_duration_s)
            if item is not None:
                segments.append(item)
    if not segments:
        raise ValueError("input artifacts contain no usable dual-RX segments")
    hypotheses: list[list[dict]] = []
    decisions = []
    for segment in sorted(segments, key=lambda item: item["start_s"]):
        candidates = []
        for index, hypothesis in enumerate(hypotheses):
            previous = hypothesis[-1]
            gap = segment["start_s"] - previous["stop_s"]
            if gap < 0 or gap > maximum_gap_s:
                continue
            difference = abs(segment["range_acceleration_m_s2"] -
                             previous["range_acceleration_m_s2"])
            # Consecutive capture artifacts reopen the Pluto stream but leave
            # the LNB and RF tuning unchanged.  Treat the same physical channel
            # as one tuning across recorder boundaries; the quadratic RF gate
            # then rejects an LO jump instead of silently downgrading the pair
            # to the much weaker cross-channel acceleration test.
            same_tuning = bool(
                segment["channel_number"] == previous["channel_number"] and
                abs(segment["nominal_rf_hz"] - previous["nominal_rf_hz"]) < 1.0)
            continuity_rms = None
            range_jerk = None
            if same_tuning:
                previous_observations = [observation
                    for item in hypothesis for observation in item["observations"]]
                continuity_rms, second_derivative = _joint_quadratic_fit(
                    previous_observations, segment["observations"])
                range_jerk = abs(SPEED_OF_LIGHT_M_S * second_derivative /
                                 segment["nominal_rf_hz"])
                if continuity_rms > maximum_same_tuning_quadratic_rms_hz:
                    continue
                if range_jerk > maximum_same_tuning_range_jerk_m_s3:
                    continue
            elif difference > maximum_acceleration_difference_m_s2:
                continue
            candidates.append((difference, index, gap, continuity_rms,
                               range_jerk, same_tuning))
        candidates.sort()
        unique = bool(candidates and (len(candidates) == 1 or
            candidates[1][0] - candidates[0][0] >= minimum_ambiguity_margin_m_s2))
        if unique:
            difference, selected, gap, continuity_rms, range_jerk, same_tuning = (
                candidates[0])
            hypotheses[selected].append(segment)
            decisions.append({"source": segment["report"],
                "source_track_id": segment["source_track_id"],
                "action": "linked", "hypothesis_index": selected,
                "gap_s": gap, "acceleration_difference_m_s2": difference,
                "same_tuning": same_tuning,
                "same_tuning_quadratic_residual_rms_hz": continuity_rms,
                "same_tuning_range_jerk_m_s3": range_jerk,
                "alternative_count": len(candidates) - 1})
        else:
            hypotheses.append([segment])
            decisions.append({"source": segment["report"],
                "source_track_id": segment["source_track_id"],
                "action": "new_hypothesis", "hypothesis_index": len(hypotheses)-1,
                "reason": ("ambiguous_continuation" if candidates
                           else "no_compatible_predecessor"),
                "compatible_predecessor_count": len(candidates)})
    tracks = []
    for index, hypothesis in enumerate(hypotheses):
        observations = []
        sources = []
        for segment in hypothesis:
            sources.append({key: value for key, value in segment.items()
                            if key != "observations"})
            for source in segment["observations"]:
                item = deepcopy(source)
                item["nominal_rf_hz"] = segment["nominal_rf_hz"]
                item["nuisance_group"] = segment["channel_number"]
                item["source_track"] = {"report": segment["report"],
                                        "track_id": segment["source_track_id"]}
                observations.append(item)
        observations.sort(key=lambda item: parse_utc(item["utc"]))
        start = parse_utc(observations[0]["utc"]).timestamp()
        stop = parse_utc(observations[-1]["utc"]).timestamp()
        tracks.append({"track_id": f"channel-hypothesis-{index:03d}",
            "seed": {"source": "cross_channel_rate_link"},
            "observations": observations, "source_segments": sources,
            "relative_receiver_calibration": {"available": False,
                "reason": "calibration remains local to each source segment"},
            "summary": {"observation_count": len(observations),
                "valid_observation_count": len(observations),
                "dual_valid_observation_count": len(observations),
                "valid_duration_s": stop-start, "dual_valid_duration_s": stop-start,
                "source_segment_count": len(hypothesis),
                "channel_numbers": sorted(set(item["channel_number"]
                                              for item in hypothesis))}})
    carriers = [item["nominal_rf_hz"] for item in segments]
    report = {"schema": CHANNEL_LINK_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "capture": None, "capture_manifest": {"metadata": {
            "observation_mode": "cross-channel-link"}},
        "source_track_artifacts": sorted(set(str(Path(path).resolve())
                                             for path in track_paths)),
        "source_followup": None, "source_frame_track": None,
        "signal": {"edge": "lower", "nominal_rf_hz": float(np.median(carriers)),
            "sample_rate_hz": None, "tuned_rf_center_hz": None,
            "channel_numbers": sorted(set(item["channel_number"] for item in segments))},
        "timing": {"anchor_method": "source_observation_utc"},
        "configuration": {"output_rate_hz": 10.0,
            "maximum_gap_s": maximum_gap_s,
            "maximum_acceleration_difference_m_s2":
                maximum_acceleration_difference_m_s2,
            "minimum_ambiguity_margin_m_s2": minimum_ambiguity_margin_m_s2,
            "maximum_same_tuning_quadratic_residual_rms_hz":
                maximum_same_tuning_quadratic_rms_hz,
            "maximum_same_tuning_range_jerk_m_s3":
                maximum_same_tuning_range_jerk_m_s3,
            "minimum_segment_epochs": minimum_segment_epochs,
            "minimum_segment_duration_s": minimum_segment_duration_s},
        "link_decisions": decisions, "tracks": tracks,
        "summary": {"source_track_count": source_track_count,
            "ignored_short_source_track_count": source_track_count - len(segments),
            "source_segment_count": len(segments),
            "hypothesis_count": len(tracks),
            "multi_segment_hypothesis_count": sum(
                item["summary"]["source_segment_count"] > 1 for item in tracks),
            "longest_hypothesis_duration_s": max(
                item["summary"]["dual_valid_duration_s"] for item in tracks)},
        "warning": ("rate-compatible cross-channel hypotheses are not satellite "
                    "identities; held-out TLE association is required")}
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, output)
    return report
