"""E3 analysis: thresholds from a genuinely empty channel, and what they realise.

Also produces ``thresholds.json``, which E2, E4 and E5 all read.  Calibration
and evaluation are disjoint halves of the TX-off population, so no rate below is
in-sample -- which the on-sky figure it is compared against openly is.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis as A

OUT = Path(__file__).resolve().parent
FIG = OUT / "figures"


def main() -> None:
    _, off = A.read(OUT / "e3_off.jsonl")
    dark_rows: list = []
    if (OUT / "e3_dark.jsonl").exists():
        _, dark_rows = A.read(OUT / "e3_dark.jsonl")

    # Deterministic disjoint split by probe index, receivers kept together so a
    # calibration point and its evaluation twin are never the same IQ.
    calib = [r for r in off if r["index"] % 2 == 0]
    evaluate = [r for r in off if r["index"] % 2 == 1]

    report = {
        "experiment": "E3-true-false-alarm",
        "note": A.LOOPBACK_NOTE,
        "population": {"tx_off_observations": len(off),
                       "calibration_observations": len(calib),
                       "evaluation_observations": len(evaluate),
                       "dark_observations": len(dark_rows),
                       "points_per_cell_mean":
                           float(np.mean([len(r["points"]) for r in off])),
                       "probe_ms": 20.0, "sample_rate_hz": 5e6},
        "false_alarm_rate_requested": 0.01,
    }

    # -- is -89.75 dB genuinely empty? ------------------------------------
    if dark_rows:
        compare = {}
        for method in A.METHODS:
            a = A.point_scores(off, method)
            b = A.point_scores(dark_rows, method)
            compare[method] = {
                "off_p50": float(np.median(a)), "dark_p50": float(np.median(b)),
                "off_p99": float(np.quantile(a, 0.99)),
                "dark_p99": float(np.quantile(b, 0.99)),
                "off_n": len(a), "dark_n": len(b)}
        report["off_versus_dark"] = compare
        # Judged on the median, not the p99.  The dark arm holds ~817 points, so
        # its 99th percentile rests on about eight draws and swings by tens of
        # percent on sampling alone -- reading a leak off that comparison would
        # be reading noise.  The median is determined to well under a percent in
        # both arms and is the statistic that can carry the claim.
        p50 = [c["dark_p50"] / c["off_p50"] for c in compare.values()]
        p99 = [c["dark_p99"] / c["off_p99"] for c in compare.values()]
        report["off_versus_dark_verdict"] = {
            "p50_ratio_range": [float(min(p50)), float(max(p50))],
            "p99_ratio_range": [float(min(p99)), float(max(p99))],
            "p99_caveat": ("the dark arm's p99 rests on ~8 draws; it is reported "
                           "but not used for the verdict"),
            "reads": ("-89.75 dB with the cyclic buffer running is "
                      "indistinguishable from no waveform at all"
                      if 0.97 <= min(p50) and max(p50) <= 1.03
                      else "the minimum attenuator still leaks measurably")}

    # -- thresholds ---------------------------------------------------------
    empty = A.calibrate(calib, field="score")             # the honest null
    xedge = A.calibrate(calib, field="cross_edge_score")  # the repository's null
    rolled = A.calibrate(calib, field="control_score")    # the wrong-code null
    report["thresholds"] = {
        method: {"empty_channel": empty[method],
                 "cross_edge_on_empty": xedge[method],
                 "wrong_code_on_empty": rolled[method]}
        for method in A.METHODS}

    # -- realised rates on held-out empty input -----------------------------
    rates = {}
    for method in A.METHODS:
        t = empty[method]["threshold"]
        rates[method] = {
            "threshold": t,
            "per_point": A.point_rate(evaluate, method, t),
            "per_cell": A.cell_rate(evaluate, method, t),
            "per_cell_cross_edge_threshold":
                A.cell_rate(evaluate, method, xedge[method]["threshold"]),
            "sky_null_per_cell": A.SKY_NULL_RATE[method]}
    report["measured"] = rates

    cell_rates = [r["per_cell"]["rate"] for r in rates.values()]
    point_rates = [r["per_point"]["rate"] for r in rates.values()]
    report["summary"] = {
        "per_point_range": [min(point_rates), max(point_rates)],
        "per_cell_range": [min(cell_rates), max(cell_rates)],
        "sky_per_cell_range": [min(A.SKY_NULL_RATE.values()),
                               max(A.SKY_NULL_RATE.values())],
        "nominal_per_point": 0.01}

    # Is the cross-edge template a valid stand-in for an empty channel?
    drift = {m: (report["thresholds"][m]["cross_edge_on_empty"]["threshold"]
                 / report["thresholds"][m]["empty_channel"]["threshold"])
             for m in A.METHODS
             if report["thresholds"][m]["empty_channel"]["threshold"]}
    report["cross_edge_null_validity"] = {
        "threshold_ratio_cross_edge_over_empty": drift,
        "range": [min(drift.values()), max(drift.values())]}

    (OUT / "e3_falsealarm.json").write_text(json.dumps(report, indent=1))
    FIG.mkdir(exist_ok=True)
    (FIG / "false-alarm-empty-channel.json").write_text(json.dumps(report, indent=1))
    (OUT / "thresholds.json").write_text(json.dumps(
        {"false_alarm_rate": 0.01,
         "calibrated_on": "TX2 -89.75 dB, cabled loopback, genuinely empty, "
                          f"{len(calib)} held-out observations",
         "empty_channel": {m: empty[m]["threshold"] for m in A.METHODS},
         "cross_edge_on_empty": {m: xedge[m]["threshold"] for m in A.METHODS}},
        indent=1))

    print(f"{'method':>20} {'thr(empty)':>11} {'thr(xedge)':>11} {'pt FAR':>9} "
          f"{'cell FAR':>10} {'sky cell':>9}")
    for m in A.METHODS:
        r = rates[m]
        print(f"{m:>20} {r['threshold']:11.5f} "
              f"{xedge[m]['threshold']:11.5f} "
              f"{r['per_point']['rate']*100:8.2f}% "
              f"{r['per_cell']['rate']*100:9.2f}% "
              f"{r['sky_null_per_cell']*100:8.2f}%")
    print("\npoints/cell mean", round(report["population"]["points_per_cell_mean"], 2))
    if "off_versus_dark_verdict" in report:
        print("off vs dark:", report["off_versus_dark_verdict"]["reads"])


if __name__ == "__main__":
    main()
