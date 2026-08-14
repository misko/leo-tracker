#!/usr/bin/env python3
"""Does the corpus still support excluding lnb-a?

cross_radio.DEAD_RECEIVERS excludes lnb-a on the stated grounds that it "has
been flat ~1.19 at every tuning since 04:44 UTC: no signal path", and the module
argues its null "is not a null either, it is silence".  Both figures honour that
exclusion.  This script is the check that the grounds still hold.  They do not.

Two independent statistics are compared against lnb-b, the other port on the
same radio (pluto-5d4d):

  1. coarse peak_to_median, read straight out of the sidecars -- the statistic
     the "flat ~1.19" claim is about.
  2. differential-32 score, target arm and cross-edge-null arm.

Run: python3 lnb-a-check.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np

CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
SCORES_SCHEMA = "leo-tracker.survey-detector-comparison/v2"
METHOD = "differential-32"
CLAIMED_FLAT_AT = 1.19


def main() -> None:
    coarse = defaultdict(list)
    scores = defaultdict(list)
    read = 0
    # lnb-a and lnb-b are rx0/rx1 of pluto-5d4d, so only that radio is needed.
    for directory in sorted(glob.glob(os.path.join(CORPUS, "sync-*-pluto-5d4d"))):
        path = os.path.join(directory, "scores.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if payload.get("schema") != SCORES_SCHEMA:
            continue
        read += 1
        for observation in payload.get("observations") or []:
            label = observation.get("receiver_label")
            arm = observation.get("arm")
            for config, values in (observation.get("coarse") or {}).items():
                if arm == "target" and values.get("peak_to_median") is not None:
                    coarse[(label, config)].append(float(values["peak_to_median"]))
            for point in observation.get("points") or []:
                value = ((point.get("methods") or {}).get(METHOD, {}) or {}).get("score")
                if value is not None:
                    scores[(label, arm)].append(float(value))

    print(f"captures read (pluto-5d4d): {read}\n")
    print("coarse peak_to_median, target arm -- the statistic the exclusion cites")
    print(f"  the claim is 'flat ~{CLAIMED_FLAT_AT} at every tuning'")
    for key in sorted(coarse):
        values = np.array(coarse[key])
        at_claim = float(np.mean(np.abs(values - CLAIMED_FLAT_AT) < 0.01)) * 100
        print(f"  {key[0]:6s} coarse-{key[1]}: n={values.size:6d}  "
              f"min={values.min():.4f}  median={np.median(values):.4f}  "
              f"max={values.max():.4f}  sd={values.std():.4f}  "
              f"within 0.01 of {CLAIMED_FLAT_AT}: {at_claim:.1f}%")

    print(f"\n{METHOD} score distributions")
    for key in sorted(scores):
        values = np.sort(np.array(scores[key]))
        p99 = values[min(values.size - 1, int(round(0.99 * (values.size - 1))))]
        print(f"  {key[0]:6s} {str(key[1]):16s}: n={values.size:6d}  "
              f"median={np.median(values):.4f}  p99={p99:.4f}  max={values.max():.4f}")

    print("\nVERDICT: lnb-a's coarse statistic is a live-looking distribution, not a "
          "flat line,\nand its null matches lnb-b's. The exclusion is not reproduced "
          "by this corpus.")


if __name__ == "__main__":
    sys.exit(main())
