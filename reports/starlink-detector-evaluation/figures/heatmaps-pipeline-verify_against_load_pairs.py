#!/usr/bin/env python3
"""Prove the numpy cache is a faithful stand-in for the real corpus.

The cache exists only because a JSON mirror of 2,547 sidecars would be ~180 MB
in a tmpfs that has filled twice tonight.  A shortcut taken for space is worth
nothing unless it is shown to change no number, so this builds a REAL on-disk
mirror of a corpus slice (the shape ``cross_radio.load_pairs`` actually reads),
runs the true ``load_pairs`` on it, builds the cache over the same slice, and
asserts the two agree on every field this analysis touches:

  * the pair list: sweep, geometry, declared geometry, matched arm, skew,
    excluded receivers, radio order;
  * ``methods_in`` and every ``null_thresholds`` threshold, to full float
    precision -- these are what every fire / no-fire decision rests on;
  * ``observation_fires`` for every method on every observation;
  * ``join_cells`` / ``join_null_cells`` counts and per-cell identities.

Exit status is non-zero on any disagreement.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

from _pipeline import load as _load_pipeline  # noqa: E402
ex = _load_pipeline("extract_heatmaps")

SLICE = int(sys.argv[1]) if len(sys.argv) > 1 else 240


def mirror(names: list[str], root: Path) -> None:
    """The compact projection, written to disk in the real corpus shape."""
    for name in names:
        source = ex.CORPUS / name
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        scores = json.loads((source / cr.SCORES_FILENAME).read_text())
        (target / cr.SCORES_FILENAME).write_text(json.dumps(ex.compact(scores)))
        shutil.copyfile(source / cr.MANIFEST_FILENAME,
                        target / cr.MANIFEST_FILENAME)


def pair_identity(pairs: list[dict]) -> list[dict]:
    return [{"sweep": p["paired_sweep"], "geometry": p["geometry"],
             "declared": p["geometry_declared"], "agrees": p["geometry_agrees"],
             "matched_arm": p["matched_arm"], "arms": p["arms"],
             "skew": p["skew_ms"], "excluded": p["excluded_receivers"],
             "radios": [r["radio_id"] for r in p["radios"]],
             "captures": [r["capture"] for r in p["radios"]],
             "rates": [r["sample_rate_hz"] for r in p["radios"]],
             "probes": [r["probe_ms"] for r in p["radios"]],
             "orders": [r["sample_order"] for r in p["radios"]],
             "observations": [len(r["scores"]["observations"])
                              for r in p["radios"]]}
            for p in pairs]


def cell_identity(cells: list[dict]) -> list[tuple]:
    return [(c["paired_sweep"], c["instant"], c["peer_instant"],
             c["receiver_pair"], c["radio_pair"], c["geometry"],
             c["a"]["channel"], c["a"]["edge"], c["b"]["channel"],
             c["b"]["edge"], c["a"]["key"], c["b"]["key"]) for c in cells]


def verdicts(pairs: list[dict], methods: list[str], thresholds: dict) -> list:
    out = []
    for pair in pairs:
        for entry in pair["radios"]:
            key = cr.threshold_key(entry)
            for observation in entry["scores"]["observations"]:
                out.append([cr.observation_fires(
                    observation, method, cr._threshold_for(thresholds, method, key))
                    for method in methods])
    return out


def main() -> int:
    frozen = json.loads(ex.SNAPSHOT.read_text())
    names = frozen["scored"][:SLICE]
    problems: list[str] = []

    work = Path(tempfile.mkdtemp(prefix="verify-", dir=str(ex.HERE)))
    try:
        mirror(names, work)
        truth_pairs, truth_census = cr.load_pairs(work)

        built = ex.build(names)
        cache_path = work / "slice.npz"
        ex.save(built, cache_path)
        cache = ex.Cache(cache_path)
        cache_pairs, cache_census = cache.pairs()

        if pair_identity(truth_pairs) != pair_identity(cache_pairs):
            problems.append("pair list differs")
        for key in ("read", "unpaired_sweeps", "irregular_geometry",
                    "not_synchronised", "no_manifest", "unreadable"):
            if truth_census[key] != cache_census[key]:
                problems.append(f"census[{key}] {truth_census[key]} "
                                f"!= {cache_census[key]}")

        truth_entries = [e for p in truth_pairs for e in p["radios"]]
        cache_entries = [e for p in cache_pairs for e in p["radios"]]
        truth_methods = cr.methods_in(truth_entries)
        cache_methods = cr.methods_in(cache_entries)
        if truth_methods != cache_methods:
            problems.append(f"methods {truth_methods} != {cache_methods}")

        truth_thresholds = cr.null_thresholds(truth_entries)
        cache_thresholds = cr.null_thresholds(cache_entries)
        if set(truth_thresholds) != set(cache_thresholds):
            problems.append("threshold keys differ")
        else:
            for key, value in truth_thresholds.items():
                other = cache_thresholds[key]
                for field in ("threshold", "count", "exceedances"):
                    if value.get(field) != other.get(field):
                        problems.append(
                            f"threshold {key} field {field}: "
                            f"{value.get(field)!r} != {other.get(field)!r}")

        truth_verdicts = verdicts(truth_pairs, truth_methods, truth_thresholds)
        cache_verdicts = verdicts(cache_pairs, cache_methods, cache_thresholds)
        if truth_verdicts != cache_verdicts:
            differing = sum(1 for a, b in zip(truth_verdicts, cache_verdicts)
                            if a != b)
            problems.append(f"fire/no-fire differs on {differing} observations")

        for label, builder in (("target", cr.join_cells),
                               ("null", cr.join_null_cells)):
            left = [cell_identity(builder(p)) for p in truth_pairs]
            right = [cell_identity(builder(p)) for p in cache_pairs]
            if left != right:
                problems.append(f"{label} join cells differ")

        # The raw scores themselves, not merely the decisions drawn from them.
        raw_truth = [point["methods"][method]["score"]
                     for entry in truth_entries
                     for observation in entry["scores"]["observations"]
                     for point in observation["points"]
                     for method in truth_methods]
        raw_cache = [point["methods"][method]["score"]
                     for entry in cache_entries
                     for observation in entry["scores"]["observations"]
                     for point in observation["points"]
                     for method in cache_methods]
        if len(raw_truth) != len(raw_cache):
            problems.append(f"score count {len(raw_truth)} != {len(raw_cache)}")
        elif raw_truth != raw_cache:
            bad = sum(1 for a, b in zip(raw_truth, raw_cache) if a != b)
            problems.append(f"{bad} raw scores differ bitwise")

        report = {
            "slice_sidecars": len(names),
            "pairs": len(truth_pairs),
            "entries": len(truth_entries),
            "methods": truth_methods,
            "threshold_keys": len(truth_thresholds),
            "observations_checked": len(truth_verdicts),
            "raw_scores_compared": len(raw_truth),
            "target_cells": sum(len(cr.join_cells(p)) for p in truth_pairs),
            "null_cells": sum(len(cr.join_null_cells(p)) for p in truth_pairs),
            "problems": problems,
            "identical": not problems,
        }
        print(json.dumps(report, indent=2))
        return 0 if not problems else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
