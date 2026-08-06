"""Aggregate the randomized manual-versus-AGC beacon capture experiment."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import numpy as np


GAIN_COMPARISON_SCHEMA = "leo-tracker.beacon-gain-comparison/v1"


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _median(values) -> float | None:
    finite = [float(value) for value in values
              if value is not None and np.isfinite(value)]
    return float(np.median(finite)) if finite else None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _fingerprint_membership(root: Path) -> tuple[dict, dict]:
    index = _json(root / "reports" / "fingerprints" / "index.json")
    linked = {item.get("cluster_id") for item in index.get("clusters", [])
              if int(item.get("member_count", 0)) > 1}
    return index.get("membership", {}), linked


def _summarize(observations: list[dict]) -> dict:
    groups = {}
    for mode in ("manual", "slow_attack"):
        rows = [row for row in observations if row["assigned_gain_mode"] == mode]
        analyzed = [row for row in rows if row["analyzed"]]
        decoded = [row for row in rows if row["decoded"]]
        fingerprinted = [row for row in rows if row["fingerprint_cluster_id"]]
        readback_matches = [row for row in rows
            if row["gain_mode_readback"] is not None]
        groups[mode] = {"capture_count": len(rows),
            "sample_time_s": float(sum(row["sample_time_s"] or 0 for row in rows)),
            "analyzed_count": len(analyzed),
            "confirmed_count": sum(row["confirmed"] for row in analyzed),
            "confirmation_rate": _rate(sum(row["confirmed"] for row in analyzed), len(analyzed)),
            "decoded_count": len(decoded),
            "decode_rate": _rate(len(decoded), len(analyzed)),
            "median_exact_candidate_count": _median(
                row["exact_candidate_count"] for row in analyzed),
            "median_pilot_accuracy": _median(row["pilot_accuracy"] for row in decoded),
            "median_pilot_confidence": _median(row["pilot_confidence"] for row in decoded),
            "median_pilot_evm": _median(row["pilot_evm"] for row in decoded),
            "gain_mode_readback_count": len(readback_matches),
            "gain_mode_readback_match_count": sum(
                all(value == mode for value in row["gain_mode_readback"])
                for row in readback_matches),
            "median_hardware_gain_db": [_median(
                row["median_hardware_gain_db"][receiver] for row in rows)
                for receiver in range(2)],
            "median_rms_magnitude": [_median(
                row["rms_magnitude"][receiver] for row in rows
                if len(row["rms_magnitude"]) > receiver) for receiver in range(2)],
            "maximum_near_full_scale_fraction": [max((
                row["near_full_scale_fraction"][receiver] for row in rows
                if len(row["near_full_scale_fraction"]) > receiver and
                row["near_full_scale_fraction"][receiver] is not None), default=None)
                for receiver in range(2)],
            "fingerprinted_count": len(fingerprinted),
            "fingerprint_linked_count": sum(row["fingerprint_linked"] for row in fingerprinted),
            "fingerprint_link_rate": _rate(
                sum(row["fingerprint_linked"] for row in fingerprinted), len(fingerprinted))}
    return groups


def build_gain_comparison(root: Path, output: Path) -> dict:
    """Summarize only captures carrying randomized gain-assignment metadata."""
    root, output = Path(root).resolve(), Path(output)
    membership, linked_clusters = _fingerprint_membership(root)
    observations = []
    for manifest_path in sorted((root / "captures").glob("*/manifest.json")):
        manifest = _json(manifest_path)
        metadata = manifest.get("metadata", {})
        experiment_id = metadata.get("gain_experiment_id")
        if experiment_id is None or manifest.get("state") != "complete":
            continue
        name = manifest_path.parent.name
        analysis = _json(root / "reports" / f"{name}.json")
        followup = _json(root / "reports" / "followups" / f"{name}.json")
        decode = _json(root / "reports" / "decoded" / f"{name}.json")
        summary = analysis.get("summary", {})
        combined = decode.get("combined", {})
        pilot = combined.get("soft_dual_rx", {}).get("pilot", {})
        stats = manifest.get("sample_statistics", {}).get("receivers", [])
        gain_entries = manifest.get("gain_telemetry", {}).get("entries", [])
        gains = [[entry.get("rx_gain_db", [None, None])[receiver]
                  for entry in gain_entries
                  if len(entry.get("rx_gain_db", [])) > receiver]
                 for receiver in range(2)]
        cluster_id = membership.get(name)
        observations.append({"capture_name": name, "experiment_id": experiment_id,
            "random_draw_u32": metadata.get("gain_random_draw_u32"),
            "agc_assignment_probability": metadata.get("agc_assignment_probability"),
            "assigned_gain_mode": metadata.get("assigned_gain_mode", manifest.get("gain_mode")),
            "gain_mode_readback": manifest.get("identity", {}).get("gain_mode_readback"),
            "observation_mode": metadata.get("observation_mode"),
            "sample_rate_hz": manifest.get("sample_rate_hz"),
            "sample_time_s": manifest.get("stream_timing", {}).get("sample_time_s"),
            "analyzed": bool(analysis),
            "confirmed": bool(followup.get("confirmation", {}).get("confirmed")),
            "decoded": bool(decode),
            "exact_candidate_count": summary.get("exact_candidate_count"),
            "pilot_accuracy": pilot.get("hard_symbol_accuracy"),
            "pilot_confidence": pilot.get("soft_mean_confidence"),
            "pilot_evm": pilot.get("rms_evm"),
            "median_hardware_gain_db": [_median(values) for values in gains],
            "rms_magnitude": [item.get("rms_magnitude") for item in stats],
            "near_full_scale_fraction": [item.get("near_full_scale_fraction")
                                           for item in stats],
            "fingerprint_cluster_id": cluster_id,
            "fingerprint_linked": cluster_id in linked_clusters})

    groups = _summarize(observations)
    strata = {mode: _summarize([
        row for row in observations if row["observation_mode"] == mode])
        for mode in ("narrow", "oversample", "wide")}
    comparable_metrics = ("confirmation_rate", "decode_rate", "median_pilot_accuracy",
                          "median_pilot_confidence", "median_pilot_evm",
                          "fingerprint_link_rate")
    effects = {metric: (None if groups["slow_attack"][metric] is None or
                        groups["manual"][metric] is None else
                        groups["slow_attack"][metric] - groups["manual"][metric])
               for metric in comparable_metrics}
    experiment_ids = sorted({row["experiment_id"] for row in observations})
    report = {"schema": GAIN_COMPARISON_SCHEMA,
        "created_utc": datetime.now(timezone.utc).isoformat(), "root": str(root),
        "experiment_ids": experiment_ids, "randomized_capture_count": len(observations),
        "groups": groups, "strata": strata, "agc_minus_manual": effects,
        "observations": observations,
        "decision_guidance": {"minimum_analyzed_per_group": 30,
            "ready": min(groups[mode]["analyzed_count"] for mode in groups) >= 30,
            "primary_metrics": ["confirmation_rate", "median_pilot_accuracy",
                                "median_pilot_confidence", "median_pilot_evm"],
            "note": "Wait for balanced analyzed groups and compare like-for-like observation-mode strata; lower EVM is better, while higher rates/accuracy/confidence are better."}}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".next")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, output)
    return report
