"""Does the analysis harness recover a known f from data with a known answer?

Synthetic scores only -- no radio.  If this fails, the bug is in the way the
entries, cells and thresholds are built here, not in the physics or in
cross_radio.  Run before trusting the real run.
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
from leo_tracker.radio.beacon import cross_radio as CR   # noqa: E402

METHODS = ["anchor-8", "differential-16", "differential-32",
           "full-frame-acquire", "full-frame-full", "full-frame-verify",
           "glrt-32", "glrt-64"]
F_LEVELS = (0.15, 0.30, 0.50)
SWEEPS_PER_F = 5
SWEEP_SIZE = 100
PD = {"r183": 0.62, "r165": 0.55}       # the truth we will try to recover
SEED = 5


def make(root: Path):
    rng = np.random.default_rng(SEED)
    blocks = [("null", 0.0, f"null-{i}") for i in range(3)]
    for f in F_LEVELS:
        blocks += [("target", f, f"f{f:g}-s{s}") for s in range(SWEEPS_PER_F)]
        blocks += [("null", 0.0, f"null-{f:g}-{i}") for i in range(2)]
    for index, (kind, f_true, name) in enumerate(blocks):
        rows = []
        for k in range(SWEEP_SIZE):
            occupied = int(rng.random() < f_true) if kind == "target" else 0
            row = {"k": k, "occupied": occupied}
            for radio in A.RADIOS:
                # one shared latent per (radio, instant): a detected frame lifts
                # every algorithm together, which is what makes the eight
                # correlate the way they do on real data
                detected = occupied and (rng.random() < PD[radio])
                points = []
                for _ in range(rng.integers(1, 4)):
                    methods = {}
                    for m in METHODS:
                        base = rng.normal(0.10, 0.05)
                        if detected:
                            base += rng.normal(0.55, 0.10)
                        methods[m] = {"score": float(base)}
                    points.append({"point_id": len(points), "epoch_sample": 0,
                                   "cfo_hz": 0.0, "claimed_by": ["x"],
                                   "methods": methods})
                row[radio] = {"rms": 1.4, "pk_med": 1.2, "points": points}
            rows.append(row)
        (root / f"{index:03d}-{name}.json").write_text(json.dumps({
            "kind": kind, "f_true": f_true, "sweep": name, "index": index,
            "instants": rows}))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="selftest-", dir=str(HERE)))
    try:
        make(tmp)
        A.RUNS = tmp
        sweeps = A.load_sweeps()
        pairs = [A.pair_for(s, arm_of=A.target_arm) for s in sweeps]
        entries = [e for p in pairs for e in p["radios"]]
        methods = CR.methods_in(entries)
        assert sorted(methods) == sorted(METHODS), methods
        thresholds = CR.null_thresholds(entries)
        false_alarm = CR.cell_false_alarm(entries, thresholds)
        print(f"methods={len(methods)}  p={[round((false_alarm[m] or {}).get('rate') or 0, 4) for m in methods]}")
        ok = True
        for f_true in F_LEVELS:
            group = [p for p in pairs if p["kind"] == "target" and p["f_true"] == f_true]
            cells = [c for p in group for c in CR.join_cells(p)]
            occ = CR.occupancy(cells, thresholds, false_alarm, methods=methods)
            direct = A.direct_rates(group, sweeps, thresholds, methods)
            realised = float(np.mean([r["occupied"] for s in sweeps
                                      if s["kind"] == "target" and s["f_true"] == f_true
                                      for r in s["instants"]]))
            values = [occ["methods"][m]["pooled"]["f"] for m in methods
                      if occ["methods"][m]["pooled"].get("solvable")]
            da = [occ["methods"][m]["pooled"]["d_a"] for m in methods
                  if occ["methods"][m]["pooled"].get("solvable")]
            pda = [direct["r183"][m]["pd"] for m in methods]
            err = abs(float(np.median(values)) - realised)
            print(f"  f_true={f_true} realised={realised:.3f} "
                  f"recovered={np.median(values):.3f} (n={len(values)}/8) err={err:.3f} "
                  f"| dA solved={np.median(da):.3f} direct={np.median(pda):.3f} "
                  f"(truth {PD['r183']})")
            if err > 0.05:
                ok = False
        controls = CR.negative_controls(
            [p for p in pairs if p["kind"] == "target" and p["f_true"] == 0.5],
            thresholds, false_alarm, methods)
        for c in controls:
            print(f"  control {c['name']:12s} cells={c['cell_count']:5d} "
                  f"spread={c['f_spread'].get('spread')} "
                  f"values={len(c['f_spread'].get('values') or {})}/8 solvable")
        print("SELFTEST", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
