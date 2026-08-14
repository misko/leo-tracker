#!/usr/bin/env python3
"""Stream the scored sync corpus into one compact cell table.

Why this exists: each ``scores.json`` is 1-4 MB and there are hundreds of them,
so ``cross_radio.load_pairs`` (which holds every parsed sidecar in memory) does
not fit in the 4 GB on this Pi alongside a live collector.  This pass reads one
sidecar at a time, keeps only what the coincidence estimator actually needs --
per-observation max score per method for the target arm, per-point scores for
the cross-edge null arm -- and drops the rest before opening the next file.

The join, the thresholds, the false-alarm rate and the estimator are the ones in
src/leo_tracker/radio/beacon/cross_radio.py, imported rather than re-implemented
wherever the shape of the data allows it.

Output: cells.json.gz in this directory, consumed by f-strata.py.
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/home/satpi01/leo-tracker/src")

from leo_tracker.radio.beacon.cross_radio import (  # noqa: E402
    DEAD_RECEIVERS, MANIFEST_FILENAME, SCORES_FILENAME, SCORES_SCHEMA,
    sweep_geometry,
)

CORPUS = Path("/mnt/qnap01/mouse9911/leo/surveys/corpus")
OUT = Path(__file__).resolve().parent / "cells.json.gz"


def compact(directory: Path) -> dict | None:
    """One radio of one sweep, reduced to the fields the estimator reads."""
    scores_path = directory / SCORES_FILENAME
    manifest_path = directory / MANIFEST_FILENAME
    if not scores_path.is_file() or not manifest_path.is_file():
        return None
    try:
        scores = json.loads(scores_path.read_text())
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return {"unreadable": True}
    if scores.get("schema") != SCORES_SCHEMA:
        return {"other_schema": str(scores.get("schema"))}
    survey = (manifest.get("metadata") or {}).get("pre_dwell_survey") or {}
    record = survey.get("synchronised_scan")
    if not isinstance(record, dict) or not record.get("paired_sweep"):
        return {"not_synchronised": True}
    arm = record.get("arm") or {}
    rate = float(scores.get("sample_rate_hz") or manifest.get("sample_rate_hz") or 0.0)
    probe_ms = scores.get("probe_ms")
    if probe_ms is None:
        probe_ms = float(arm.get("probe_s") or 0.0) * 1e3

    observations = []
    for observation in scores.get("observations") or []:
        label = observation.get("receiver_label")
        if label in DEAD_RECEIVERS:
            continue
        target = observation.get("arm") == "target"
        best: dict[str, float] = {}
        points: dict[str, list[float]] = defaultdict(list)
        for point in observation.get("points") or []:
            for method, values in (point.get("methods") or {}).items():
                value = values.get("score")
                if value is None:
                    continue
                value = float(value)
                if method not in best or value > best[method]:
                    best[method] = value
                if not target:
                    points[method].append(value)
        row = {"t": int(target), "i": observation.get("iq_index"),
               "ch": observation.get("channel"), "eg": observation.get("edge"),
               "rx": observation.get("receiver"), "lb": label, "max": best}
        if not target:
            row["pts"] = dict(points)
        observations.append(row)

    return {
        "capture": scores.get("capture") or directory.name,
        "radio_id": scores.get("radio_id") or manifest["identity"]["radio_id"],
        "rate": rate, "probe_ms": float(probe_ms),
        "receiver_labels": list(manifest["identity"].get("receiver_labels") or []),
        "receiver_centers_hz": list(scores.get("receiver_centers_hz") or []),
        "sample_order": [list(item) for item in survey.get("sample_order") or []],
        "edge_order": record.get("edge_order"),
        "arm_name": arm.get("name"),
        "pilot_band_fits": arm.get("pilot_band_fits"),
        "matched_arm": bool(record.get("matched_arm")),
        "paired_sweep": record.get("paired_sweep"),
        "skew_ms": record.get("skew_ms") or {},
        "obs": observations,
    }


def main() -> None:
    census = {"scanned": 0, "read": 0, "unreadable": 0, "other_schema": 0,
              "not_synchronised": 0, "unpaired_sweeps": 0,
              "irregular_geometry": 0}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for directory in sorted(CORPUS.iterdir()):
        if not (directory / SCORES_FILENAME).is_file():
            continue
        census["scanned"] += 1
        entry = compact(directory)
        if entry is None:
            continue
        for flag in ("unreadable", "other_schema", "not_synchronised"):
            if flag in entry:
                census[flag] += 1
                entry = None
                break
        if entry is None:
            continue
        census["read"] += 1
        grouped[str(entry["paired_sweep"])].append(entry)

    pairs = []
    for sweep in sorted(grouped):
        radios = sorted(grouped[sweep], key=lambda item: item["radio_id"])
        if len(radios) != 2:
            census["unpaired_sweeps"] += 1
            continue
        left, right = radios
        geometry = sweep_geometry(left["sample_order"], right["sample_order"])
        if geometry == "irregular":
            census["irregular_geometry"] += 1
            continue
        pairs.append({"paired_sweep": sweep, "geometry": geometry,
                      "matched_arm": left["matched_arm"] and right["matched_arm"],
                      "skew_ms": left["skew_ms"] or right["skew_ms"],
                      "radios": radios})

    import datetime as dt
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with gzip.open(OUT, "wt") as handle:
        json.dump({"read_utc": stamp, "census": census, "pairs": pairs}, handle)
    print(json.dumps(census, indent=2))
    print("pairs", len(pairs), "->", OUT)


if __name__ == "__main__":
    main()
