"""Build each figure's data JSON, then run its .py to render the PNG.

The .py files are the only thing that draws; they read only their own .json.
That keeps every published PNG reproducible from the pair beside it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
FIG = HERE / "figures"
FIG.mkdir(exist_ok=True)

RES = json.loads(Path(os.environ.get("DUALRIG_RESULTS",
                                     HERE / "results.json")).read_text())
FINE = json.loads((HERE / "fine_sweep.json").read_text())
COARSE = json.loads((HERE / "snr_sweep.json").read_text())
META = json.loads((HERE / "runs" / "meta.json").read_text())
RADIOS = ("r183", "r165")
NAME_A, NAME_B = ".183 (radio A)", ".165 (radio B)"


def render(name: str, data: dict):
    (FIG / f"{name}.json").write_text(json.dumps(data, indent=1))
    out = subprocess.run([sys.executable, str(FIG / f"{name}.py")],
                         capture_output=True, text=True)
    print(out.stdout.strip() or out.stderr.strip()[-2500:])
    return out.returncode == 0


def pd_from(rows, thresholds):
    """Mean/min/max detection probability over the eight algorithms."""
    values = []
    for method, block in sorted(thresholds.items()):
        cut = block["threshold"]
        if cut is None:
            continue
        values.append(sum(1 for r in rows if r["scores"].get(method, -1e9) > cut)
                      / len(rows))
    return (float(np.mean(values)), float(np.min(values)), float(np.max(values)))


# ---------------------------------------------------------------- calibration
def calibration():
    dead_thresholds = FINE["thresholds_dead"]
    curve = {}
    for radio in RADIOS:
        rows = []
        for gain_key, block in COARSE["levels"].items():
            rows.append((float(gain_key), block[radio]))
        for gain_key, block in FINE["fine"].items():
            rows.append((float(json.loads(gain_key)[radio]), block[radio]))
        rows.sort(key=lambda item: item[0])
        merged = {}
        for gain, data in rows:
            merged.setdefault(gain, []).extend(data)
        gains = sorted(merged)
        stats = [pd_from(merged[g], dead_thresholds[radio]) for g in gains]
        curve[radio] = {"attenuation_db": gains,
                        "pd_mean": [s[0] for s in stats],
                        "pd_min": [s[1] for s in stats],
                        "pd_max": [s[2] for s in stats],
                        "n_per_point": min(len(merged[g]) for g in gains)}
    operating = {}
    for radio in RADIOS:
        chosen = META["on_gain_db"][radio]
        index = curve[radio]["attenuation_db"].index(chosen)
        operating[radio] = {"attenuation_db": chosen,
                            "pd_mean": curve[radio]["pd_mean"][index]}
    order = sorted(dead_thresholds[RADIOS[0]])
    dead_rate, parked_rate = {}, {}
    for method in order:
        d = [FINE["thresholds_dead"][r][method]["effective_rate"] for r in RADIOS]
        p = [FINE["leak"]["parked_fire_rate"][r][method] for r in RADIOS]
        dead_rate[method] = float(np.mean(d))
        parked_rate[method] = float(np.mean(p))
    return render("fig_calibration", {
        "title": "Calibrating the injection: where detection is partial, and proof that a parked TX is silent",
        "pd_curve": curve, "operating_point": operating,
        "leak": {"order": order, "dead_rate": dead_rate,
                 "parked_rate": parked_rate, "nominal_rate": 0.01,
                 "n": len(FINE["leak"]["dead"][RADIOS[0]])}})


# ------------------------------------------------------------------------ D1
def d1():
    levels = []
    for key, lv in sorted(RES["levels"].items(), key=lambda kv: float(kv[0])):
        per = {}
        solvable = 0
        for method in RES["methods"]:
            pooled = lv["occupancy"]["methods"][method]["pooled"]
            boot = lv["bootstrap"].get(method) or {}
            if pooled.get("solvable"):
                solvable += 1
            per[method] = {"f": pooled.get("f"), "p05": boot.get("p05"),
                           "p95": boot.get("p95")}
        levels.append({"f_true": lv["f_true"], "f_realised": lv["f_realised"],
                       "cells": lv["cells"], "methods": per,
                       "solvable": solvable, "label_y": 0.0})
    lowest = min(v["f"] for lv in levels for v in lv["methods"].values()
                 if v["f"] is not None)
    for lv in levels:
        lv["label_y"] = lowest - 0.02
    return render("fig_d1_recovered_f", {
        "title": "D1: the estimator brackets injected occupancy at 0.30 and 0.50,\nand reads LOW at 0.15 - every algorithm below truth",
        "levels": levels, "methods": RES["methods"], "draws": 600})


# ------------------------------------------------------------------------ D2
def d2():
    points = []
    for key, lv in sorted(RES["levels"].items(), key=lambda kv: float(kv[0])):
        for method in RES["methods"]:
            pooled = lv["occupancy"]["methods"][method]["pooled"]
            if not pooled.get("solvable"):
                continue
            for side, radio in (("A", RADIOS[0]), ("B", RADIOS[1])):
                direct = lv["direct"][radio][method]["pd"]
                solved = pooled["d_a"] if side == "A" else pooled["d_b"]
                if direct is None or solved is None:
                    continue
                points.append({"side": side, "method": method,
                               "f_true": lv["f_true"], "direct": direct,
                               "solved": solved})
    bias = [p["solved"] - p["direct"] for p in points]
    median = float(np.median(bias)) if bias else 0.0
    low = sum(1 for b in bias if b < 0)
    if abs(median) <= 0.02:
        headline = ("D2: with two genuinely independent chains the solver's d "
                    "carries no detectable bias")
    else:
        headline = (f"D2: the solver's d reads "
                    f"{'low' if median < 0 else 'high'} by {abs(median):.3f} "
                    f"in {low} of {len(bias)} cases")
    return render("fig_d2_d_bias", {
        "title": headline,
        "points": points, "radio_a": NAME_A, "radio_b": NAME_B,
        "summary": {"cases": len(points),
                    "reads_low": int(sum(1 for b in bias if b < 0)),
                    "median_bias": float(np.median(bias)),
                    "worst_bias": float(max(bias, key=abs)),
                    "prior_low": 15, "prior_cases": 16, "prior_worst": 0.10}})


# ------------------------------------------------------------------------ D3
def d3():
    levels = []
    for key, lv in sorted(RES["levels"].items(), key=lambda kv: float(kv[0])):
        spread = lv["occupancy"]["f_spread"]
        sampling = spread.get("sampling") or {}
        levels.append({"f_true": lv["f_true"], "cells": lv["cells"],
                       "spread": spread.get("spread"),
                       "p05": sampling.get("p05"), "p95": sampling.get("p95")})
    worst = max((lv["spread"] or 0) for lv in levels)
    if worst >= 0.040:
        headline = ("D3: the algorithms disagree about f by as much as they do "
                    "on sky, on data where f is one number by construction")
    else:
        headline = (f"D3: on injected data the algorithms agree to {worst:.3f}, "
                    f"tighter than the 0.040 seen on sky")
    return render("fig_d3_spread", {
        "title": headline,
        "levels": levels, "draws": 200,
        "references": [{"value": 0.048, "label": "single-rig loopback (0.048)"},
                       {"value": 0.040, "label": "sky corpus (0.040)"}]})


# ------------------------------------------------------------------------ D4
def d4():
    labels = {"real": "real pairing",
              "scrambled": "scrambled (radio B from another sweep)",
              "shifted": "shifted +2 instants"}
    levels = []
    for key, lv in sorted(RES["levels"].items(), key=lambda kv: float(kv[0])):
        joins = {}
        values = [m["pooled"]["f"] for m in lv["occupancy"]["methods"].values()
                  if m["pooled"].get("solvable")]
        spread = lv["occupancy"]["f_spread"]
        joins["real"] = {
            "f_min": min(values) if values else None,
            "f_max": max(values) if values else None,
            "f_median": float(np.median(values)) if values else None,
            "solvable": len(values), "spread": spread.get("spread"),
            "spread_p05": (spread.get("sampling") or {}).get("p05"),
            "spread_p95": (spread.get("sampling") or {}).get("p95"),
            "reasons": spread.get("unsolvable") or {}}
        for control in lv["controls"]:
            name = "scrambled" if control["name"] == "scrambled" else "shifted"
            measured = control["f_spread"]
            vals = list((measured.get("values") or {}).values())
            joins[name] = {
                "f_min": min(vals) if vals else None,
                "f_max": max(vals) if vals else None,
                "f_median": float(np.median(vals)) if vals else None,
                "solvable": len(vals), "spread": measured.get("spread"),
                "spread_p05": (measured.get("sampling") or {}).get("p05"),
                "spread_p95": (measured.get("sampling") or {}).get("p95"),
                "reasons": measured.get("unsolvable") or {}}
        full = lv["verdict"]["verdict"]
        short = {"CONSISTENT": "consistent", "VACUOUS": "vacuous",
                 "ALGORITHMS DISAGREE": "algorithms disagree",
                 "NOT VALIDATED": "not validated"}
        key = full.split(" \u2014 ")[0].strip()
        levels.append({"f_true": lv["f_true"], "f_realised": lv["f_realised"],
                       "cells": lv["cells"], "joins": joins,
                       "verdict": full,
                       "verdict_short": short.get(key, key.lower())})
    return render("fig_d4_controls", {
        "title": "D4: the negative controls destroy f exactly as they should - and the agreement check still cannot see it",
        "levels": levels, "joins": ["real", "scrambled", "shifted"],
        "join_labels": labels})


# ------------------------------------------------------------------------ D5
def d5():
    points = []
    for method, rows in RES["joint_null"]["methods"].items():
        for row in rows:
            points.append({"method": method, **row})
    summary = RES["joint_null"].get("summary", {})
    return render("fig_d5_joint_null", {
        "title": "D5: the joint null really is the product of the marginals",
        "points": points, "n": RES["joint_null"]["instants"],
        "cases": summary.get("cases", len(points)),
        "tested": summary.get("tested", len(points)),
        "min_fisher_p": summary.get("min_fisher_p"),
        "consistent": summary.get(
            "consistent", int(sum(1 for p in points if p["consistent"])))})


if __name__ == "__main__":
    wanted = sys.argv[1:] or ["calibration", "d1", "d2", "d3", "d4", "d5"]
    for name in wanted:
        print(f"--- {name}")
        globals()[name]()
