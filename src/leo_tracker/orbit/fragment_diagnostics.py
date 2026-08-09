"""Auditable model comparison for gapped Starlink Doppler hypotheses.

This module never promotes a satellite identity.  It combines already-frozen
continuous tracks and TLE rankings into a shadow diagnostic that distinguishes
identity agreement from instrument/transmitter continuity.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

import numpy as np

from .artifacts import parse_utc, utc_iso
from .association import ASSOCIATION_SCHEMA
from leo_tracker.radio.beacon.continuous import TRACK_SCHEMA


FRAGMENT_DIAGNOSTIC_SCHEMA = "leo-tracker.starlink-fragment-diagnostic/v1"


def _read(path: Path) -> dict:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _valid_observations(track: dict) -> list[dict]:
    return [item for item in track.get("observations", [])
            if item.get("utc") and len(item.get("receivers", [])) == 2 and
            all(receiver.get("valid") for receiver in item["receivers"])]


def _endpoint_model(observations: list[dict], receiver: int, *, end: bool,
                    span_s: float = 5.0) -> tuple[np.ndarray, float]:
    times = np.asarray([parse_utc(item["utc"]).timestamp()
                        for item in observations], float)
    values = np.asarray([item["receivers"][receiver]["frequency_offset_hz"]
                         for item in observations], float)
    edge = times[-1] if end else times[0]
    selected = np.flatnonzero(times >= edge-span_s if end else times <= edge+span_s)
    if selected.size < 3:
        selected = (np.arange(max(0, len(times)-3), len(times)) if end else
                    np.arange(min(3, len(times))))
    if selected.size < 2 or np.ptp(times[selected]) <= 0:
        raise ValueError("fragment has insufficient receiver epochs for a gap model")
    reference = float(np.mean(times[selected]))
    coefficients = np.polyfit(times[selected]-reference, values[selected], 1)
    # Return global intercept form so both fragment lines can be evaluated at
    # the same gap midpoint without subtracting epoch-sized timestamps.
    return coefficients, reference


def _gap_diagnostic(first: dict, second: dict) -> dict:
    left, right = _valid_observations(first), _valid_observations(second)
    if len(left) < 2 or len(right) < 2:
        return {"available": False, "reason": "insufficient_dual_receiver_epochs"}
    left_stop = parse_utc(left[-1]["utc"]).timestamp()
    right_start = parse_utc(right[0]["utc"]).timestamp()
    gap = right_start-left_stop
    if gap < 0:
        return {"available": False, "reason": "overlapping_fragments"}
    midpoint = (left_stop+right_start)/2
    steps, before_slopes, after_slopes = [], [], []
    try:
        for receiver in range(2):
            before, before_ref = _endpoint_model(left, receiver, end=True)
            after, after_ref = _endpoint_model(right, receiver, end=False)
            before_value = float(np.polyval(before, midpoint-before_ref))
            after_value = float(np.polyval(after, midpoint-after_ref))
            steps.append(after_value-before_value)
            before_slopes.append(float(before[0])); after_slopes.append(float(after[0]))
    except (KeyError, TypeError, ValueError):
        return {"available": False, "reason": "receiver_endpoint_fit_failed"}
    common = float(np.mean(steps)); differential = float(steps[1]-steps[0])
    return {"available": True, "gap_s": gap, "midpoint_utc": utc_iso(
        datetime.fromtimestamp(midpoint, timezone.utc)),
        "receiver_step_hz": steps, "common_step_hz": common,
        "differential_step_hz": differential,
        "differential_to_common_fraction": abs(differential)/max(abs(common), 1.0),
        "slope_before_hz_s": before_slopes, "slope_after_hz_s": after_slopes}


def _candidate_summary(candidate: dict) -> dict:
    return {key: candidate.get(key) for key in (
        "rank", "name", "norad_id", "holdout_residual_rms_hz",
        "train_residual_rms_hz", "epoch_adjustment_s", "epoch_at_search_boundary")}


def _separation(candidates: list[dict]) -> dict:
    if len(candidates) < 2:
        return {"available": False}
    first = float(candidates[0]["holdout_residual_rms_hz"])
    second = float(candidates[1]["holdout_residual_rms_hz"])
    margin = second-first
    return {"available": True, "margin_hz": margin,
            "margin_over_winner_rms": margin/max(first, 1e-12),
            "runner_up_over_winner_rms": second/max(first, 1e-12)}


def _weighted_rms(rows: list[tuple[int, float]]) -> float | None:
    rows = [(count, rms) for count, rms in rows if count > 0 and math.isfinite(rms)]
    if not rows:
        return None
    return float(np.sqrt(sum(count*rms*rms for count, rms in rows) /
                         sum(count for count, _ in rows)))


def _bic_proxy(rms: float | None, observations: int, parameters: int) -> float | None:
    if rms is None or observations <= parameters or rms <= 0:
        return None
    return float(observations*math.log(rms*rms) + parameters*math.log(observations))


def diagnose_fragments(tracks_path: Path, links_path: Path,
                       joint_association_path: Path,
                       fragment_association_path: Path, output: Path, *,
                       high_separation_ratio: float = 20.0,
                       maximum_fragment_rms_hz: float = 500.0,
                       maximum_differential_fraction: float = .15) -> dict:
    """Compare same-identity and independent-identity fragment explanations."""
    if min(high_separation_ratio, maximum_fragment_rms_hz,
           maximum_differential_fraction) <= 0:
        raise ValueError("fragment diagnostic gates must be positive")
    tracks = _read(tracks_path); links = _read(links_path)
    joint = _read(joint_association_path); fragments = _read(fragment_association_path)
    if tracks.get("schema") != TRACK_SCHEMA or links.get("schema") != TRACK_SCHEMA:
        raise ValueError("fragment diagnostics require continuous track artifacts")
    if (joint.get("schema") != ASSOCIATION_SCHEMA or
            fragments.get("schema") != ASSOCIATION_SCHEMA):
        raise ValueError("fragment diagnostics require TLE association artifacts")
    source_tracks = {item["track_id"]: item for item in tracks.get("tracks", [])}
    fragment_associations = {item["track_id"]: item
                             for item in fragments.get("associations", [])}
    joint_associations = {item["track_id"]: item
                          for item in joint.get("associations", [])}
    hypotheses = []
    for linked_track in links.get("tracks", []):
        sources = linked_track.get("source_segments", [])
        if len(sources) < 2:
            continue
        joint_row = joint_associations.get(linked_track["track_id"], {})
        joint_norad = joint_row.get("best_norad_id")
        fragment_rows, identities, same_candidate_rms, switch_rms = [], [], [], []
        usable = True
        for source in sources:
            track_id = source["source_track_id"]
            association = fragment_associations.get(track_id, {})
            candidates = association.get("candidates", [])
            track = source_tracks.get(track_id)
            count = int((track or {}).get("summary", {}).get(
                "dual_valid_observation_count", 0))
            if not candidates or track is None:
                usable = False
                fragment_rows.append({"track_id": track_id, "available": False,
                    "reason": association.get("reason", "missing_fragment_track")})
                continue
            best = candidates[0]; identities.append(best.get("norad_id"))
            target = next((item for item in candidates
                           if item.get("norad_id") == joint_norad), None)
            switch_rms.append((count, float(best["holdout_residual_rms_hz"])))
            if target is not None:
                same_candidate_rms.append((count, float(
                    target["holdout_residual_rms_hz"])))
            else:
                usable = False
            fragment_rows.append({"track_id": track_id, "available": True,
                "duration_s": association.get("duration_s"),
                "dual_epoch_count": association.get("dual_epoch_count"),
                "best_candidate": _candidate_summary(best),
                "separation": _separation(candidates),
                "joint_candidate": (_candidate_summary(target) if target else None),
                "joint_candidate_rank": target.get("rank") if target else None,
                "top_candidates": [_candidate_summary(item) for item in candidates[:5]]})
        gaps = []
        for first, second in zip(sources, sources[1:]):
            left = source_tracks.get(first["source_track_id"])
            right = source_tracks.get(second["source_track_id"])
            gaps.append(_gap_diagnostic(left, right) if left and right else
                        {"available": False, "reason": "missing_source_track"})
        top_agreement = bool(usable and identities and len(set(identities)) == 1)
        joint_candidates = joint_row.get("candidates", [])
        joint_rms = joint_row.get("best_holdout_residual_rms_hz")
        observation_count = int(linked_track.get("summary", {}).get(
            "dual_valid_observation_count", 0))
        same_piecewise_rms = _weighted_rms(same_candidate_rms)
        independent_rms = _weighted_rms(switch_rms)
        fragment_count = len(sources)
        bic = {"same_continuous": _bic_proxy(joint_rms, observation_count, 3),
               "same_identity_piecewise": _bic_proxy(
                   same_piecewise_rms, observation_count, 3*fragment_count),
               "independent_identities": _bic_proxy(
                   independent_rms, observation_count, 3*fragment_count),
               "warning": ("diagnostic Gaussian BIC proxy; candidate search, correlated "
                           "errors and oscillator priors require null calibration")}
        all_fragment_rms_good = bool(usable and all(
            row.get("best_candidate", {}).get("holdout_residual_rms_hz", math.inf) <=
            maximum_fragment_rms_hz for row in fragment_rows))
        receiver_common = bool(gaps and all(item.get("available") and
            item["differential_to_common_fraction"] <= maximum_differential_fraction
            for item in gaps))
        if joint_row.get("qualified"):
            classification = "same_continuous_supported"
        elif top_agreement and all_fragment_rms_good and receiver_common:
            classification = "same_identity_discontinuity_candidate"
        elif usable and len(set(identities)) == len(identities) and all_fragment_rms_good:
            classification = "satellite_switch_candidate"
        else:
            classification = "indeterminate"
        separation = _separation(joint_candidates)
        hypotheses.append({"track_id": linked_track["track_id"],
            "shadow_classification": classification,
            "production_qualification_affected": False,
            "source_segment_count": fragment_count,
            "fragment_top_identity_agreement": top_agreement,
            "fragment_top_norad_ids": identities,
            "receiver_common_mode_consistent": receiver_common,
            "gaps": gaps, "fragments": fragment_rows,
            "joint_association": {"qualified": joint_row.get("qualified", False),
                "best_norad_id": joint_norad,
                "best_candidate": (_candidate_summary(joint_candidates[0])
                                   if joint_candidates else None),
                "separation": separation,
                "experimental_high_separation": bool(
                    separation.get("margin_over_winner_rms", -math.inf) >=
                    high_separation_ratio)},
            "model_comparison": {"same_continuous_holdout_rms_hz": joint_rms,
                "same_identity_piecewise_holdout_rms_hz": same_piecewise_rms,
                "independent_identities_holdout_rms_hz": independent_rms,
                "bic_proxy": bic}})
    result = {"schema": FRAGMENT_DIAGNOSTIC_SCHEMA,
        "created_utc": utc_iso(datetime.now(timezone.utc)),
        "sources": {"tracks": str(Path(tracks_path).resolve()),
            "links": str(Path(links_path).resolve()),
            "joint_association": str(Path(joint_association_path).resolve()),
            "fragment_association": str(Path(fragment_association_path).resolve())},
        "configuration": {"high_separation_ratio": high_separation_ratio,
            "maximum_fragment_rms_hz": maximum_fragment_rms_hz,
            "maximum_differential_fraction": maximum_differential_fraction},
        "hypotheses": hypotheses,
        "summary": {"hypothesis_count": len(hypotheses),
            "same_continuous_supported_count": sum(item["shadow_classification"] ==
                "same_continuous_supported" for item in hypotheses),
            "same_identity_discontinuity_candidate_count": sum(
                item["shadow_classification"] ==
                "same_identity_discontinuity_candidate" for item in hypotheses),
            "satellite_switch_candidate_count": sum(item["shadow_classification"] ==
                "satellite_switch_candidate" for item in hypotheses),
            "indeterminate_count": sum(item["shadow_classification"] ==
                "indeterminate" for item in hypotheses),
            "production_qualification_affected": False}}
    _atomic_json(output, result)
    return result
