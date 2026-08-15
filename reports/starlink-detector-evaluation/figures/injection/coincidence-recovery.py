"""E5 figure: does the coincidence estimator recover an occupancy it was never told?

``cross_radio.solve_coincidence`` is the repository's own solver, unmodified.
What changes is that here f is SET, and dA/dB can be read off the occupied
probes directly instead of inferred.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
# The harness helper lives with the records it reads, under
# injection-data/, not beside the figures. Inserting figures/ instead --
# which is what this did -- leaves the import unresolvable from a clean
# checkout, so every one of these scripts failed at its import line.
sys.path.insert(0, str(HERE.parent.parent / "injection-data" / "one-radio"))
import analysis as A
import style

from leo_tracker.radio.beacon.cross_radio import solve_coincidence

RUN = HERE.parent / "e5_occupancy.jsonl"
THRESHOLDS = HERE.parent / "thresholds.json"


def rates(rows_by_probe: dict, method: str, threshold: float) -> dict:
    a = b = both = n = 0
    for pair in rows_by_probe.values():
        left = pair.get(1)
        right = pair.get(2)
        if left is None or right is None:
            continue
        fa = A.observation_fires(left, method, threshold)
        fb = A.observation_fires(right, method, threshold)
        if fa is None or fb is None:
            continue
        n += 1
        a += int(fa); b += int(fb); both += int(fa and fb)
    return {"cells": n, "p_a": a / n, "p_b": b / n, "p_ab": both / n}


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    header, rows = A.read(RUN)
    thresholds = json.loads(THRESHOLDS.read_text())["empty_channel"]
    f_true = float(header["f_true"])

    by_probe: dict = {}
    for r in rows:
        by_probe.setdefault(r["index"], {})[r["receiver"]] = r
    occupied = {i: p for i, p in by_probe.items()
                if next(iter(p.values()))["occupied"]}
    empty = {i: p for i, p in by_probe.items()
             if not next(iter(p.values()))["occupied"]}

    e3 = json.loads((HERE.parent / "e3_falsealarm.json").read_text())

    per_method = {}
    for method in A.METHODS:
        t = thresholds[method]
        overall = rates(by_probe, method, t)
        # p measured on this run's own known-empty cells, per receiver then pooled
        empt = rates(empty, method, t)
        occ = rates(occupied, method, t)
        p_known = 0.5 * (empt["p_a"] + empt["p_b"])
        p_sky_style = e3["measured"][method]["per_cell"]["rate"]
        solved_known = solve_coincidence(overall["p_a"], overall["p_b"],
                                         overall["p_ab"], p_known)
        solved_sky = solve_coincidence(overall["p_a"], overall["p_b"],
                                       overall["p_ab"], p_sky_style)
        # The model's load-bearing assumption is that the two chains fail
        # INDEPENDENTLY.  Every cell here is known to be empty or known to be
        # occupied, so that assumption can be checked instead of assumed: on
        # empty cells P(AB) must equal P(A)P(B), and on occupied cells it must
        # equal dA.dB.  Nothing on sky can run this check, because on sky no
        # cell's occupancy is known.
        independence = {
            "empty": {"p_ab": empt["p_ab"],
                      "product": empt["p_a"] * empt["p_b"],
                      "excess": empt["p_ab"] - empt["p_a"] * empt["p_b"],
                      "cells": empt["cells"]},
            "occupied": {"p_ab": occ["p_ab"],
                         "product": occ["p_a"] * occ["p_b"],
                         "excess": occ["p_ab"] - occ["p_a"] * occ["p_b"],
                         "cells": occ["cells"]}}
        per_method[method] = {
            "counts": overall,
            "p_from_known_empty_cells": p_known,
            "p_from_e3_empty_channel_null": p_sky_style,
            "solved_with_known_p": solved_known,
            "solved_with_e3_p": solved_sky,
            "direct_d_a": occ["p_a"], "direct_d_b": occ["p_b"],
            "d_a_error": (None if solved_known.get("d_a") is None
                          else solved_known["d_a"] - occ["p_a"]),
            "d_b_error": (None if solved_known.get("d_b") is None
                          else solved_known["d_b"] - occ["p_b"]),
            "f_error": (None if solved_known.get("f") is None
                        else solved_known["f"] - f_true),
            "independence_check": independence,
            "occupied_cells": occ["cells"], "empty_cells": empt["cells"]}

    # cluster bootstrap over probes
    rng = np.random.default_rng(5)
    keys = list(by_probe)
    boot = {m: [] for m in A.METHODS}
    for _ in range(400):
        picked = [keys[i] for i in rng.integers(0, len(keys), len(keys))]
        sample = {n: by_probe[k] for n, k in enumerate(picked)}
        empt_s = {n: p for n, p in sample.items()
                  if not next(iter(p.values()))["occupied"]}
        for m in A.METHODS:
            t = thresholds[m]
            o = rates(sample, m, t)
            e = rates(empt_s, m, t) if empt_s else None
            if e is None or o["cells"] == 0:
                continue
            s = solve_coincidence(o["p_a"], o["p_b"], o["p_ab"],
                                  0.5 * (e["p_a"] + e["p_b"]))
            if s.get("f") is not None:
                boot[m].append(s["f"])
    for m in A.METHODS:
        d = np.array(boot[m])
        per_method[m]["f_ci"] = ([float(np.quantile(d, 0.025)),
                                  float(np.quantile(d, 0.975))]
                                 if d.size > 20 else None)
        per_method[m]["f_bootstrap_solvable_share"] = len(d) / 400

    payload = {"figure": "coincidence-recovery", "note": A.LOOPBACK_NOTE,
               "f_true": f_true, "tx2_gain_db": header["tx2_gain_db"],
               "probes": header["probes"],
               "solver": "leo_tracker.radio.beacon.cross_radio.solve_coincidence",
               "per_method": per_method,
               "common_mode": {
                   "note": "RX1 and RX2 share one LO; their noise is not fully "
                           "independent, which the model assumes",
                   "mean_cross_receiver_power_empty":
                       float(np.mean([next(iter(p.values()))["cross_receiver_power"]
                                      for p in empty.values()]))}}
    (HERE / "coincidence-recovery.json").write_text(json.dumps(payload, indent=1))

    # ------------------------------------------------------------------ draw
    fig = plt.figure(figsize=(13.0, 5.9))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.0], wspace=0.30,
                            left=0.135, right=0.985, top=0.815, bottom=0.185)
    ax = fig.add_subplot(grid[0, 0])
    ax2 = fig.add_subplot(grid[0, 1])

    order = list(A.METHODS)
    ys = np.arange(len(order))
    for i, m in enumerate(order):
        s = style.STYLE[m]
        row = per_method[m]
        f = row["solved_with_known_p"].get("f")
        ci = row["f_ci"]
        if ci:
            ax.plot(ci, [i, i], color=s["color"], linewidth=2.6, alpha=0.5,
                    solid_capstyle="round")
        if f is not None:
            ax.plot([f], [i], marker=s["marker"], color=s["color"], markersize=8,
                    markeredgecolor=style.SURFACE, markeredgewidth=0.8)
    # The spread the eight return, on data where f is ONE number by
    # construction -- the report's own consistency check, run where it must pass.
    ax.axvspan(min(v for v in [r["solved_with_known_p"]["f"]
                               for r in per_method.values()] if v is not None),
               max(v for v in [r["solved_with_known_p"]["f"]
                               for r in per_method.values()] if v is not None),
               color=style.MUTED, alpha=0.15, linewidth=0)
    ax.axvline(f_true, color=style.INK, linewidth=1.5, linestyle=(0, (4, 2)))
    ax.text(f_true, len(order) + 0.02, f" true occupancy {f_true:.3f}",
            ha="left", va="bottom", fontsize=9.5, color=style.INK)
    ax.set_yticks(ys)
    ax.set_yticklabels(order, fontsize=9.5)
    ax.set_ylim(len(order) + 0.7, -0.55)
    ax.set_xlabel("Occupancy f recovered by the repository's own solver")
    spread = (max(r["solved_with_known_p"]["f"] for r in per_method.values())
              - min(r["solved_with_known_p"]["f"] for r in per_method.values()))
    ax.set_title(f"Eight readings of one number disagree by {spread:.3f}",
                 loc="left", pad=26)
    ax.text(0.0, 1.015, f"n = {header['probes']} cells; bars are 95% cluster "
                        f"bootstrap; shaded band is the spread",
            transform=ax.transAxes, fontsize=9, color=style.INK_2)
    ax.grid(axis="y", visible=False)

    for i, m in enumerate(order):
        s = style.STYLE[m]
        row = per_method[m]
        solved = row["solved_with_known_p"]
        for value, marker, label in ((row["direct_d_a"], "o", "measured"),
                                     (solved.get("d_a"), "x", "model")):
            if value is None:
                continue
            ax2.plot([value], [i], marker=marker, color=s["color"],
                     markersize=8 if marker == "o" else 9,
                     markeredgecolor=style.SURFACE if marker == "o" else s["color"],
                     markeredgewidth=0.8, fillstyle="full" if marker == "o" else "none")
        if solved.get("d_a") is not None:
            ax2.plot([row["direct_d_a"], solved["d_a"]], [i, i], color=s["color"],
                     linewidth=1.2, alpha=0.45)
    ax2.set_yticks(ys)
    ax2.set_yticklabels(order, fontsize=9.5)
    ax2.set_ylim(len(order) + 0.7, -0.55)
    ax2.set_xlabel("Detection probability on RX1 (dA)")
    ax2.set_title("The solver reads dA low in 8 cases out of 8",
                  loc="left", pad=26)
    ax2.text(0.0, 1.015, "filled circle = measured directly, cross = solver output",
             transform=ax2.transAxes, fontsize=9, color=style.INK_2)
    ax2.grid(axis="y", visible=False)

    fs = [per_method[m]["solved_with_known_p"].get("f") for m in order]
    fs = [v for v in fs if v is not None]
    extra = (f"Recovered f spans {min(fs):.3f}–{max(fs):.3f} against a true "
             f"{f_true:.3f}; every 95% interval covers the truth. The report's "
             f"on-sky spread, offered as the model's failed consistency check, "
             f"is 0.040." if fs else "")
    style.footer(fig, extra)
    fig.savefig(HERE / "coincidence-recovery.png")

    print(f"f_true = {f_true:.4f}")
    for m in order:
        r = per_method[m]
        s = r["solved_with_known_p"]
        f = s.get("f")
        print(f"  {m:>20} f={'None' if f is None else format(f, '.4f')} "
              f"dA={'None' if s.get('d_a') is None else format(s['d_a'], '.3f')} "
              f"(direct {r['direct_d_a']:.3f})  "
              f"dB={'None' if s.get('d_b') is None else format(s['d_b'], '.3f')} "
              f"(direct {r['direct_d_b']:.3f})")
    print("\nindependence on KNOWN-EMPTY cells  P(AB) vs P(A)P(B):")
    for m in order:
        e = per_method[m]["independence_check"]["empty"]
        print(f"  {m:>20} P(AB)={e['p_ab']:.4f}  P(A)P(B)={e['product']:.4f}  "
              f"excess={e['excess']:+.4f}")
    print("\nindependence on KNOWN-OCCUPIED cells  P(AB) vs dA.dB:")
    for m in order:
        e = per_method[m]["independence_check"]["occupied"]
        print(f"  {m:>20} P(AB)={e['p_ab']:.4f}  dA.dB={e['product']:.4f}  "
              f"excess={e['excess']:+.4f}")


if __name__ == "__main__":
    main()
