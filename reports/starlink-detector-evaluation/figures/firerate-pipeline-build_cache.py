#!/usr/bin/env python3
"""Reduce the compact cell table to one .npz both summary figures read.

The corpus is read ONCE (extract_cells.py, unmodified, straight off the
read-only share) and reduced here to per-cell verdicts.  Every number the two
figures plot comes out of this file, so neither figure re-reads 2,500 sidecars
and the two cannot disagree about the population.

Thresholds, the per-cell empty-sky rate and the join are the repository's own,
reached through reports/.../figures/fcore.py, which is the verified plumbing
from the full-corpus pass.  Nothing about the estimator is re-implemented here.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
sys.path.insert(0, "/home/satpi01/leo-tracker/reports/"
                   "sync-scan-cross-radio-2026-08-14/figures")

import fcore  # noqa: E402

WORK = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
            "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/summary/firerate/work")
CACHE = Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
             "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/summary/cache")
OUT = CACHE / "firerate-coincidence.npz"


def main() -> None:
    payload = fcore.load(WORK / "cells.json.gz")
    model = fcore.build(payload)
    methods = model["methods"]

    # ---- raw fire rate on TARGET observations -----------------------------
    # The count a naive ranking would use: how often this detector claims a
    # signal on sky.  Per live target observation over every paired entry, the
    # same unit the empty-sky rate p is measured in, so the two axes of the
    # fire-rate figure are the same kind of number.
    entries = [entry for pair in payload["pairs"] for entry in pair["radios"]]
    tally = {method: [0, 0] for method in methods}
    for entry in entries:
        key = (entry["rate"], entry["probe_ms"])
        for row in entry["obs"]:
            if not row["t"]:
                continue
            for method in methods:
                edge = (model["thresholds"].get((method, key)) or {}).get("threshold")
                verdict = fcore.fires(row, method, edge)
                if verdict is None:
                    continue
                tally[method][1] += 1
                tally[method][0] += int(verdict)

    cells = model["cells"]
    index = {method: i for i, method in enumerate(methods)}
    dec_a = np.full((len(cells), len(methods)), -1, dtype=np.int8)
    dec_b = np.full((len(cells), len(methods)), -1, dtype=np.int8)
    for row, cell in enumerate(cells):
        for method, verdict in cell["dec"].items():
            if verdict is None:
                continue
            dec_a[row, index[method]] = int(verdict[0])
            dec_b[row, index[method]] = int(verdict[1])

    def column(name, dtype=None):
        return np.array([cell[name] for cell in cells], dtype=dtype)

    snapshot = json.loads((WORK / "snapshot.json").read_text())
    census = {key: snapshot[key] for key in
              ("measured_utc", "sweeps_on_share", "sweeps_committed",
               "corpus_entries", "scored_sidecars", "scored_digest")}
    census["extract_read_utc"] = payload["read_utc"]
    census["extract_census"] = payload["census"]
    census["paired_sweeps"] = len(payload["pairs"])
    census["matched_arm_sweeps"] = sum(1 for p in payload["pairs"]
                                       if p["matched_arm"])
    census["scored_sidecars_in_a_pair"] = len(entries)
    census["matched_arm_cells"] = len(cells)
    census["live_target_observations"] = max(n for _, n in tally.values())
    census["null_observations"] = max(v["cells"] for v in
                                      model["false_alarm"].values())

    geometry = column("geometry")
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        OUT,
        methods=np.array(methods),
        dec_a=dec_a, dec_b=dec_b,
        geometry=geometry,
        sweep=column("sweep"),
        arm=column("arm"),
        channel=column("channel"),
        receiver_pair=column("receiver_pair"),
        rate=column("rate", float),
        probe_ms=column("probe_ms", float),
        fa_count=np.array([model["false_alarm"][m]["count"] for m in methods]),
        fa_cells=np.array([model["false_alarm"][m]["cells"] for m in methods]),
        fa_rate=np.array([model["false_alarm"][m]["rate"] for m in methods], float),
        target_fires=np.array([tally[m][0] for m in methods]),
        target_obs=np.array([tally[m][1] for m in methods]),
        thresholds=np.array(json.dumps(
            {f"{m}|{k[0]}|{k[1]}": v.get("threshold")
             for (m, k), v in sorted(model["thresholds"].items(),
                                     key=lambda kv: (kv[0][0], kv[0][1]))})),
        census=np.array(json.dumps(census)),
    )
    print(json.dumps(census, indent=2))
    print("methods", methods)
    print("cells", len(cells), "geometries",
          {g: int((geometry == g).sum()) for g in sorted(set(geometry.tolist()))})
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
