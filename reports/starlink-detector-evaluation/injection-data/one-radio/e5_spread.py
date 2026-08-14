"""How wide is the eight-algorithm f-spread when f is known to be one number?

The report treats the spread of ``f`` across the eight detectors as the
coincidence model's own consistency check -- ``f`` is a property of the sky, so
all eight must return it -- and reports 0.3388-0.3788, a spread of 0.0400, as
that check going unmet.

Here f is 0.2775 by construction, identical for all eight, because a seeded
Bernoulli draw decided it.  So whatever spread the eight return is the
estimator's, not the sky's.  The statistic is bootstrapped as a whole (all eight
refitted on the same resampled cells each iteration), because the eight
estimates share their cells and their sampling errors are strongly correlated --
comparing eight marginal intervals would badly overstate how well the spread is
determined.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "figures"))
import analysis as A

from leo_tracker.radio.beacon.cross_radio import solve_coincidence

RUN = HERE / "e5_occupancy.jsonl"
DRAWS = 600
REPORT_SKY_SPREAD = 0.0400
REPORT_SKY_RANGE = (0.3388, 0.3788)


def rates(pairs, method, threshold):
    a = b = both = n = 0
    for pair in pairs:
        left, right = pair.get(1), pair.get(2)
        if left is None or right is None:
            continue
        fa = A.observation_fires(left, method, threshold)
        fb = A.observation_fires(right, method, threshold)
        if fa is None or fb is None:
            continue
        n += 1
        a += int(fa); b += int(fb); both += int(fa and fb)
    return (n, a / n, b / n, both / n) if n else (0, 0.0, 0.0, 0.0)


def solve_all(pairs, thresholds):
    empty = [p for p in pairs if not next(iter(p.values()))["occupied"]]
    out = {}
    for method in A.METHODS:
        t = thresholds[method]
        n, p_a, p_b, p_ab = rates(pairs, method, t)
        _, e_a, e_b, _ = rates(empty, method, t)
        if not n:
            return None
        solved = solve_coincidence(p_a, p_b, p_ab, 0.5 * (e_a + e_b))
        if solved.get("f") is None:
            return None
        out[method] = solved["f"]
    return out


def main() -> None:
    header, rows = A.read(RUN)
    thresholds = json.loads((HERE / "thresholds.json").read_text())["empty_channel"]
    f_true = float(header["f_true"])

    by_probe: dict = {}
    for r in rows:
        by_probe.setdefault(r["index"], {})[r["receiver"]] = r
    pairs = list(by_probe.values())

    point = solve_all(pairs, thresholds)
    observed = max(point.values()) - min(point.values())

    rng = np.random.default_rng(77)
    spreads, biases = [], []
    for _ in range(DRAWS):
        picked = [pairs[i] for i in rng.integers(0, len(pairs), len(pairs))]
        solved = solve_all(picked, thresholds)
        if solved is None:
            continue
        values = list(solved.values())
        spreads.append(max(values) - min(values))
        biases.append(float(np.mean(values)) - f_true)
    spreads = np.array(spreads)
    biases = np.array(biases)

    report = {
        "experiment": "E5-spread",
        "note": A.LOOPBACK_NOTE,
        "f_true": f_true,
        "cells": len(pairs),
        "tx2_gain_db": header["tx2_gain_db"],
        "f_by_method": point,
        "observed_spread": observed,
        "spread_ci": [float(np.quantile(spreads, 0.025)),
                      float(np.quantile(spreads, 0.975))],
        "spread_median": float(np.median(spreads)),
        "mean_bias": float(np.mean(list(point.values())) - f_true),
        "mean_bias_ci": [float(np.quantile(biases, 0.025)),
                         float(np.quantile(biases, 0.975))],
        "share_of_draws_all_eight_high": float(np.mean(biases > 0)),
        "report_sky_spread": REPORT_SKY_SPREAD,
        "report_sky_range": list(REPORT_SKY_RANGE),
        "spread_exceeds_sky_share": float(np.mean(spreads > REPORT_SKY_SPREAD)),
        "bootstrap_draws": int(spreads.size),
        "reads": ("the eight-algorithm f-spread the report treats as the model's "
                  "failed consistency check is reproduced here, at a comparable "
                  "size, on data where f is identical for all eight by "
                  "construction"),
    }
    (HERE / "e5_spread.json").write_text(json.dumps(report, indent=1))
    (HERE / "figures" / "coincidence-spread.json").write_text(
        json.dumps(report, indent=1))

    print(f"f_true                {f_true:.4f}  ({len(pairs)} cells)")
    print(f"recovered per method  {min(point.values()):.4f}–{max(point.values()):.4f}")
    print(f"observed spread       {observed:.4f}  "
          f"95% CI [{report['spread_ci'][0]:.4f}, {report['spread_ci'][1]:.4f}]")
    print(f"report's sky spread   {REPORT_SKY_SPREAD:.4f}  "
          f"(range {REPORT_SKY_RANGE[0]}–{REPORT_SKY_RANGE[1]})")
    print(f"P(loopback spread > sky spread) = {report['spread_exceeds_sky_share']:.3f}")
    print(f"mean bias in f        {report['mean_bias']:+.4f}  "
          f"95% CI [{report['mean_bias_ci'][0]:+.4f}, {report['mean_bias_ci'][1]:+.4f}]")


if __name__ == "__main__":
    main()
