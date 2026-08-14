"""Run the whole analysis over the completed dual-rig run and write results.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import analyse as A                                    # noqa: E402
from leo_tracker.radio.beacon import cross_radio as CR  # noqa: E402

OUT = HERE / "results.json"
FS, PROBE_MS = A.FS, A.PROBE_MS


def main():
    sweeps = A.load_sweeps()
    pairs = [A.pair_for(s, arm_of=A.target_arm) for s in sweeps]
    entries = [entry for pair in pairs for entry in pair["radios"]]
    methods = CR.methods_in(entries)

    # thresholds + p on the null arm: dedicated silent sweeps, never the cells
    # the estimator scores
    thresholds = CR.null_thresholds(entries)
    false_alarm = CR.cell_false_alarm(entries, thresholds)

    result = {"methods": methods,
              "sweeps": len(sweeps),
              "null_sweeps": sum(1 for s in sweeps if s["kind"] == "null"),
              "target_sweeps": sum(1 for s in sweeps if s["kind"] == "target"),
              "thresholds": {f"{m}": thresholds[(m, (FS, PROBE_MS))]
                             for m in methods if (m, (FS, PROBE_MS)) in thresholds},
              "false_alarm": false_alarm,
              "levels": {}}

    for f_true in A.__dict__.get("F_LEVELS", None) or sorted(
            {s["f_true"] for s in sweeps if s["kind"] == "target"}):
        group = [p for p in pairs if p["kind"] == "target" and p["f_true"] == f_true]
        if not group:
            continue
        cells = [cell for pair in group for cell in CR.join_cells(pair)]
        occ = CR.occupancy(cells, thresholds, false_alarm, methods=methods)
        boot = A.bootstrap_f(cells, thresholds, false_alarm, methods)
        direct = A.direct_rates(group, sweeps, thresholds, methods)
        controls = CR.negative_controls(group, thresholds, false_alarm, methods)
        verdict = CR.consistency_verdict(occ["f_spread"], controls)
        truth_occ = float(np.mean([row["occupied"]
                                   for s in sweeps
                                   if s["kind"] == "target" and s["f_true"] == f_true
                                   for row in s["instants"]]))
        result["levels"][f"{f_true:g}"] = {
            "f_true": f_true, "f_realised": truth_occ, "cells": len(cells),
            "occupancy": occ, "bootstrap": boot["per_method"],
            "spread_draws": boot["spread_draws"],
            "direct": direct, "controls": controls, "verdict": verdict}
        print(f"f_true={f_true} realised={truth_occ:.4f} cells={len(cells)} "
              f"spread={occ['f_spread'].get('spread')}", flush=True)

    result["null_by_radio"] = A.null_by_radio(pairs, thresholds, methods)
    result["joint_null"] = A.joint_null(sweeps, methods)
    result["leak_check"] = A.leak_check(
        json.loads((HERE / "fine_sweep.json").read_text()), methods)
    OUT.write_text(json.dumps(result, indent=1))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
