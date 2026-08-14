"""Exercise the whole analysis + figure path on synthetic data.

Finds bugs in report.py and in the six figure scripts before the real run
finishes, without touching runs/ (which the experiment is writing right now).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import analyse as A                                     # noqa: E402
import selftest                                          # noqa: E402
from leo_tracker.radio.beacon import cross_radio as CR   # noqa: E402

OUT = HERE / "results_dry.json"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dryrun-", dir=str(HERE)))
    try:
        selftest.make(tmp)
        A.RUNS = tmp
        sweeps = A.load_sweeps()
        pairs = [A.pair_for(s, arm_of=A.target_arm) for s in sweeps]
        entries = [e for p in pairs for e in p["radios"]]
        methods = CR.methods_in(entries)
        thresholds = CR.null_thresholds(entries)
        false_alarm = CR.cell_false_alarm(entries, thresholds)
        result = {"methods": methods, "sweeps": len(sweeps),
                  "null_sweeps": sum(1 for s in sweeps if s["kind"] == "null"),
                  "target_sweeps": sum(1 for s in sweeps if s["kind"] == "target"),
                  "thresholds": {m: thresholds[(m, (A.FS, A.PROBE_MS))]
                                 for m in methods
                                 if (m, (A.FS, A.PROBE_MS)) in thresholds},
                  "false_alarm": false_alarm, "levels": {}}
        for f_true in sorted({s["f_true"] for s in sweeps if s["kind"] == "target"}):
            group = [p for p in pairs if p["kind"] == "target" and p["f_true"] == f_true]
            cells = [c for p in group for c in CR.join_cells(p)]
            occ = CR.occupancy(cells, thresholds, false_alarm, methods=methods)
            boot = A.bootstrap_f(cells, thresholds, false_alarm, methods, draws=120)
            direct = A.direct_rates(group, sweeps, thresholds, methods)
            controls = CR.negative_controls(group, thresholds, false_alarm, methods)
            verdict = CR.consistency_verdict(occ["f_spread"], controls)
            realised = float(np.mean([r["occupied"] for s in sweeps
                                      if s["kind"] == "target" and s["f_true"] == f_true
                                      for r in s["instants"]]))
            result["levels"][f"{f_true:g}"] = {
                "f_true": f_true, "f_realised": realised, "cells": len(cells),
                "occupancy": occ, "bootstrap": boot["per_method"],
                "spread_draws": boot["spread_draws"], "direct": direct,
                "controls": controls, "verdict": verdict}
            print(f"  dry f={f_true} realised={realised:.3f} cells={len(cells)} "
                  f"spread={occ['f_spread'].get('spread')}", flush=True)
        result["joint_null"] = A.joint_null(sweeps, methods)
        result["leak_check"] = A.leak_check(
            json.loads((HERE / "fine_sweep.json").read_text()), methods)
        OUT.write_text(json.dumps(result, indent=1))
        print("wrote", OUT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
