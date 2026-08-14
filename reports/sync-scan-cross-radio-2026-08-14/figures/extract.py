#!/usr/bin/env python3
"""Stream the paired sync-* corpus into one compact table.

Replicates ``cross_radio.load_pairs`` filtering exactly (schema, manifest,
synchronised_scan.paired_sweep, exactly two radios per sweep, geometry not
irregular) but keeps only the per-point columns the figures need, because the
full sidecars are ~7 GB of JSON and this host has 4 GB of RAM.

Output: cfo-port-corpus.npz in the same directory.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cfo-port-corpus.npz")

# --- constants lifted verbatim from the pipeline -------------------------
SCORES_SCHEMA = "leo-tracker.survey-detector-comparison/v2"   # survey_scoring
SCORES_FILENAME = "scores.json"
MANIFEST_FILENAME = "manifest.json"
DEAD_RECEIVERS = ("lnb-a",)                                   # cross_radio
PILOT_BAND_HZ = 1_875_000.0
METHOD = "differential-32"


def pilot_guard_hz(rate: float) -> float:
    return float(rate) / 2.0 - PILOT_BAND_HZ / 2.0


def sweep_geometry(order_a, order_b) -> str:
    """cross_radio.sweep_geometry, verbatim."""
    if not order_a or not order_b or len(order_a) != len(order_b):
        return "irregular"
    steps = list(zip(order_a, order_b))
    if all(tuple(l) == tuple(r) for l, r in steps):
        return "same-edge"
    if all(l[0] == r[0] and l[1] != r[1] for l, r in steps):
        return "opposite-edge"
    return "irregular"


def read_one(directory: str):
    """One capture -> (status, entry-dict or None)."""
    path = os.path.join(directory, SCORES_FILENAME)
    if not os.path.isfile(path):
        return ("no_scores", None)
    try:
        with open(path) as handle:
            scores = json.load(handle)
    except (OSError, ValueError):
        return ("unreadable", None)
    if scores.get("schema") != SCORES_SCHEMA:
        return ("other_schema", None)
    manifest_path = os.path.join(directory, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        return ("no_manifest", None)
    try:
        with open(manifest_path) as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return ("unreadable", None)
    survey = (manifest.get("metadata") or {}).get("pre_dwell_survey")
    if not isinstance(survey, dict):
        return ("not_synchronised", None)
    record = survey.get("synchronised_scan")
    if not isinstance(record, dict) or not record.get("paired_sweep"):
        return ("not_synchronised", None)

    arm_rec = record.get("arm") or {}
    rate = float(scores.get("sample_rate_hz") or manifest.get("sample_rate_hz") or 0.0)
    probe_ms = scores.get("probe_ms")
    if probe_ms is None:
        probe_ms = float(arm_rec.get("probe_s") or 0.0) * 1e3
    centres = list(scores.get("receiver_centers_hz") or [])

    rows = []   # arm_is_target, label, cfo_hz, bias_hz, score
    for observation in scores.get("observations") or []:
        label = observation.get("receiver_label")
        index = observation.get("receiver")
        bias = (centres[index]
                if isinstance(index, int) and index < len(centres) else 0.0)
        bias = float(bias or 0.0)
        is_target = observation.get("arm") == "target"
        for point in observation.get("points") or []:
            cfo = point.get("cfo_hz")
            score = ((point.get("methods") or {}).get(METHOD, {}) or {}).get("score")
            rows.append((is_target,
                         label,
                         np.nan if cfo is None else float(cfo),
                         bias,
                         np.nan if score is None else float(score)))

    return ("read", {
        "dir": os.path.basename(directory),
        "radio_id": scores.get("radio_id") or manifest["identity"]["radio_id"],
        "paired_sweep": str(record["paired_sweep"]),
        "sample_order": [list(item) for item in survey.get("sample_order") or []],
        "rate": rate,
        "probe_ms": float(probe_ms),
        "centres": centres,
        "rows": rows,
    })


def main() -> None:
    dirs = sorted(glob.glob(os.path.join(CORPUS, "sync-*")))
    census = defaultdict(int)
    census["scanned"] = len(dirs)
    grouped = defaultdict(list)

    with Pool(2) as pool:
        for i, (status, entry) in enumerate(
                pool.imap_unordered(read_one, dirs, chunksize=16)):
            census[status] += 1
            if entry is not None:
                grouped[entry["paired_sweep"]].append(entry)
            if (i + 1) % 500 == 0:
                print(f"  {i + 1}/{len(dirs)}", file=sys.stderr, flush=True)

    # --- the pairing filter, as load_pairs applies it --------------------
    kept = []
    for sweep in sorted(grouped):
        radios = sorted(grouped[sweep], key=lambda item: item["radio_id"])
        if len(radios) != 2:
            census["unpaired_sweeps"] += 1
            continue
        geometry = sweep_geometry(radios[0]["sample_order"], radios[1]["sample_order"])
        if geometry == "irregular":
            census["irregular_geometry"] += 1
            continue
        census["pairs_joined"] += 1
        census[f"geom_{geometry}"] += 1
        kept.extend(radios)

    labels, rate, probe, target, cfo, bias, score, radio = [], [], [], [], [], [], [], []
    for entry in kept:
        for is_target, label, offset, port_bias, value in entry["rows"]:
            labels.append(label or "")
            rate.append(entry["rate"])
            probe.append(entry["probe_ms"])
            target.append(bool(is_target))
            cfo.append(offset)
            bias.append(port_bias)
            score.append(value)
            radio.append(entry["radio_id"])

    np.savez_compressed(
        CACHE,
        label=np.array(labels), rate=np.array(rate, float),
        probe_ms=np.array(probe, float), is_target=np.array(target, bool),
        cfo_hz=np.array(cfo, float), bias_hz=np.array(bias, float),
        score=np.array(score, float), radio=np.array(radio),
        census_keys=np.array(sorted(census)),
        census_vals=np.array([census[k] for k in sorted(census)], np.int64),
        entries=np.array(len(kept)),
    )
    print(json.dumps(dict(sorted(census.items())), indent=2))
    print(f"entries kept: {len(kept)}   points: {len(cfo)}   -> {CACHE}")


if __name__ == "__main__":
    main()
