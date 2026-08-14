"""Fold E3b (the cross-edge NULL ARM on an empty channel) into the E3 story."""
from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as A

OUT = Path(__file__).resolve().parent


def main() -> None:
    _, arm = A.read(OUT / "e3b_nullarm.jsonl")
    _, off = A.read(OUT / "e3_off.jsonl")
    evaluate = [r for r in off if r["index"] % 2 == 1]

    # Threshold the way cross_radio.null_thresholds does: 1% per point on the
    # null arm's own `score`, then applied to the target arm's `score`.
    null_arm = A.calibrate(arm, field="score")
    report = {"experiment": "E3b-null-arm", "note": A.LOOPBACK_NOTE,
              "null_arm_observations": len(arm),
              "evaluated_on_target_arm_cells": len(evaluate),
              "construction": ("cross_radio.null_thresholds: threshold from the "
                               "separate cross-edge-null ARM, which runs its own "
                               "search_observation with the opposite edge's bank "
                               "and takes its own distinct_points"),
              "thresholds": {m: null_arm[m] for m in A.METHODS},
              "per_cell_at_null_arm_threshold": {
                  m: A.cell_rate(evaluate, m, null_arm[m]["threshold"])
                  for m in A.METHODS},
              "per_point_at_null_arm_threshold": {
                  m: A.point_rate(evaluate, m, null_arm[m]["threshold"])
                  for m in A.METHODS}}

    e3 = json.loads((OUT / "e3_falsealarm.json").read_text())
    report["threshold_ratio_null_arm_over_empty"] = {
        m: null_arm[m]["threshold"] / e3["thresholds"][m]["empty_channel"]["threshold"]
        for m in A.METHODS}
    (OUT / "e3b_summary.json").write_text(json.dumps(report, indent=1))

    print(f"{'method':>20} {'thr(nullarm)':>13} {'thr(empty)':>11} {'ratio':>7} "
          f"{'cell FAR':>9} {'sky cell':>9}")
    for m in A.METHODS:
        print(f"{m:>20} {null_arm[m]['threshold']:13.5f} "
              f"{e3['thresholds'][m]['empty_channel']['threshold']:11.5f} "
              f"{report['threshold_ratio_null_arm_over_empty'][m]:7.3f} "
              f"{report['per_cell_at_null_arm_threshold'][m]['rate']*100:8.2f}% "
              f"{A.SKY_NULL_RATE[m]*100:8.2f}%")


if __name__ == "__main__":
    main()
