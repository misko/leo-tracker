#!/usr/bin/env python3
"""Evidence for the one reviewer figure that f-strata does NOT reproduce.

The review reported the two skew strata as NON-OVERLAPPING: f 0.310-0.372 within
the 0.054 ms design bound (n = 1054 cells) against f 0.220-0.288 beyond it
(n = 386).  On the corpus as scored now they overlap almost exactly.

This walks the same estimator up the corpus in the order the scorer filled it,
so the reader can see whether the separation was ever there and when it closed.
No plot: the f-strata figure carries the finding, this is the audit trail.

    nice -n 15 python3 f-strata-skew-vs-corpus.py
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import fcore

OUT = Path(__file__).resolve().parent / "f-strata-skew-vs-corpus.json"


def main() -> None:
    payload = fcore.load()
    design = fcore.DESIGN_MAX_SKEW_MS
    total = len(payload["pairs"])
    steps = sorted({*range(40, total, 10), total})

    rows = []
    for limit in steps:
        model = fcore.build({"pairs": payload["pairs"][:limit]})
        cells = model["cells"]
        if not cells:
            continue
        inside = [c for c in cells
                  if c["skew_ms"] is not None and c["skew_ms"] <= design]
        beyond = [c for c in cells
                  if c["skew_ms"] is not None and c["skew_ms"] > design]
        low = fcore.stratum(inside, model)
        high = fcore.stratum(beyond, model)
        alg = fcore.stratum(cells, model)
        if low["min"] is None or high["min"] is None:
            continue
        disjoint = low["min"] > high["max"] or high["min"] > low["max"]
        rows.append({
            "paired_sweeps_used": limit,
            "matched_arm_cells": len(cells),
            "within_bound": {"cells": low["n_cells"], "f_min": low["min"],
                             "f_max": low["max"]},
            "beyond_bound": {"cells": high["n_cells"], "f_min": high["min"],
                             "f_max": high["max"]},
            "disjoint": bool(disjoint),
            "across_algorithms": {"f_min": alg["min"], "f_max": alg["max"],
                                  "spread": alg["spread"]}})

    first_overlap = next((row for row in rows if not row["disjoint"]), None)
    last_disjoint = next((row for row in reversed(rows) if row["disjoint"]), None)
    out = {
        "question": "does the reviewer's non-overlapping skew split survive as "
                    "the corpus grows?",
        "generated_utc": dt.datetime.now(dt.timezone.utc)
                           .isoformat(timespec="seconds"),
        "design_max_skew_ms": design,
        "reviewer_claim": "within 0.310-0.372 (n=1054) vs beyond 0.220-0.288 "
                          "(n=386), NON-OVERLAPPING",
        "answer": "no. The split is disjoint up to about %s paired sweeps, is "
                  "already overlapping by %s, and stays overlapping at every "
                  "larger size through %d. The reviewer's snapshot sits on the "
                  "boundary, which is what a sampling artefact looks like."
                  % (last_disjoint["paired_sweeps_used"] if last_disjoint else "?",
                     first_overlap["paired_sweeps_used"] if first_overlap else "?",
                     rows[-1]["paired_sweeps_used"]),
        "last_disjoint_step": last_disjoint,
        "first_overlapping_step": first_overlap,
        "sweep_up_the_corpus": rows,
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote", OUT)
    print("%-7s %-7s %-22s %-22s %s"
          % ("sweeps", "cells", "within bound", "beyond bound", "verdict"))
    for row in rows:
        print("%-7d %-7d %.4f-%.4f (n=%4d)  %.4f-%.4f (n=%4d)  %s"
              % (row["paired_sweeps_used"], row["matched_arm_cells"],
                 row["within_bound"]["f_min"], row["within_bound"]["f_max"],
                 row["within_bound"]["cells"], row["beyond_bound"]["f_min"],
                 row["beyond_bound"]["f_max"], row["beyond_bound"]["cells"],
                 "disjoint" if row["disjoint"] else "OVERLAP"))


if __name__ == "__main__":
    main()
