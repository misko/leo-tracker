"""A1 figure: does P(AB|T=1) = dA.dB once the two chains stop sharing a clock?"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F                                        # noqa: E402

SRC = HERE.parent / (sys.argv[1] if len(sys.argv) > 1
                     else "a1_occupied_independence.json")
ONE = HERE.parent / "a1_one_radio_ci.json"
OUT = HERE / (sys.argv[2] if len(sys.argv) > 2
              else "a1-occupied-cell-independence")
RATE = "0.05"


def main():
    F.setup()
    import matplotlib.pyplot as plt

    data = json.loads(SRC.read_text())
    one = json.loads(ONE.read_text()) if ONE.exists() else None
    block = data["per_rate"][RATE]
    methods = list(block["methods"])
    # order by the two-radio occupied excess, most negative first
    methods.sort(key=lambda m: block["methods"][m]["occupied"]["excess"])
    y = np.arange(len(methods))[::-1]

    n_occ = data["cells"]["occupied"]
    n_emp = data["cells"]["empty_target"]

    fig = plt.figure(figsize=(13.6, 7.2))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.28, 1.0], wspace=0.30,
                            left=0.135, right=0.988, top=0.735, bottom=0.215)
    axa = fig.add_subplot(grid[0, 0])
    axb = fig.add_subplot(grid[0, 1])

    # -- panel A: occupied cells -------------------------------------------
    axa.axvline(0, color=F.AXIS, lw=1.2, zorder=1)
    if one:
        for i, m in enumerate(methods):
            o = one["methods"][m]["occupied"]
            axa.plot(o["ci95"], [y[i] + 0.19] * 2, "-", color=F.S2, lw=2.4,
                     alpha=0.32, solid_capstyle="round", zorder=2)
        axa.plot([one["methods"][m]["occupied"]["excess"] for m in methods],
                 y + 0.19, "o", color=F.S2, markersize=8, linestyle="none",
                 markeredgecolor=F.SURFACE, markeredgewidth=1.5, zorder=3,
                 label=f"ONE radio, RX1 vs RX2 (shared clock) — OCCUPIED, "
                       f"n={one['methods'][methods[0]]['occupied']['cells']}")
    for i, m in enumerate(methods):
        c = block["methods"][m]["occupied"]
        axa.plot(c["ci95"], [y[i] - 0.19] * 2, "-", color=F.S1, lw=2.6,
                 alpha=0.34, solid_capstyle="round", zorder=2)
    axa.plot([block["methods"][m]["occupied"]["excess"] for m in methods],
             y - 0.19, "o", color=F.S1, markersize=9, linestyle="none",
             markeredgecolor=F.SURFACE, markeredgewidth=1.5, zorder=3,
             label=f"TWO independent radios — OCCUPIED, n={n_occ}")
    axa.set_yticks(y)
    axa.set_yticklabels(methods)
    axa.set_xlabel("P(AB | T=1) − P(A | T=1)·P(B | T=1)      excess coincidence")
    axa.set_title("OCCUPIED cells (T=1): the assumption under test", loc="left")
    axa.set_ylim(-0.6, len(methods) - 0.4)

    # -- panel B: empty-cell control ---------------------------------------
    axb.axvline(0, color=F.AXIS, lw=1.2, zorder=1)
    for i, m in enumerate(methods):
        c = block["methods"][m]["empty"]
        axb.plot(c["ci95"], [y[i]] * 2, "-", color=F.S3, lw=2.6, alpha=0.34,
                 solid_capstyle="round", zorder=2)
    axb.plot([block["methods"][m]["empty"]["excess"] for m in methods], y, "o",
             color=F.S3, markersize=9, linestyle="none",
             markeredgecolor=F.SURFACE, markeredgewidth=1.5, zorder=3,
             label=f"TWO independent radios — EMPTY, n={n_emp}")
    axb.set_yticks(y)
    axb.set_yticklabels([])
    axb.set_xlabel("P(AB | T=0) − P(A | T=0)·P(B | T=0)")
    axb.set_title("EMPTY cells (T=0): the control", loc="left")
    axb.set_ylim(-0.6, len(methods) - 0.4)

    handles = [h for ax in (axa, axb) for h in ax.get_legend_handles_labels()[0]]
    labels = [l for ax in (axa, axb) for l in ax.get_legend_handles_labels()[1]]
    fig.legend(handles, labels, loc="lower center", ncol=3,
               bbox_to_anchor=(0.5, 0.086), frameon=False, handletextpad=0.5,
               columnspacing=2.2)

    mean_occ = block["occupied_excess_mean"]
    lo = min(block["methods"][m]["occupied"]["ci95"][0] for m in methods)
    hi = max(block["methods"][m]["occupied"]["ci95"][1] for m in methods)
    cover = block["occupied_ci_covering_one_radio_prior"]
    sign = ("all eight positive" if all(
        block["methods"][m]["occupied"]["excess"] > 0 for m in methods)
        else "mixed in sign")
    nsig = block["occupied_permutation_significant_bonferroni"]
    one_cover = (one or {}).get("occupied_intervals_covering_zero")
    fig.suptitle(
        f"Conditional independence HOLDS on occupied cells: with {n_occ} cells on two independent radios the excess is\n"
        f"{mean_occ:+.3f} ({sign}), {nsig}/8 significant after correction, and {8 - cover}/8 intervals EXCLUDE the "
        f"single-radio prior.\nThat prior was never established either - {one_cover}/8 of its own intervals covered "
        f"zero at n=111. P$_d$ ≈ {block['mean_pd'][data['radios'][0]]:.2f}/"
        f"{block['mean_pd'][data['radios'][1]]:.2f} at {float(RATE):.0%} per-cell false alarm",
        fontsize=12.2, fontweight="bold", color=F.INK, x=0.008, ha="left", y=0.985)
    F.finish(fig, [axa, axb], F.CAVEAT_TWO, caveat_y=0.010, axis="x")
    fig.savefig(OUT.with_suffix(".png"))
    print("wrote", OUT.with_suffix(".png"))

    payload = {
        "figure": "a1-occupied-cell-independence",
        "note": F.CAVEAT_TWO.replace("\n", " "),
        "per_cell_false_alarm": float(RATE),
        "occupied_cells": n_occ, "empty_cells": n_emp,
        "two_radio_occupied": {m: block["methods"][m]["occupied"] for m in methods},
        "two_radio_empty": {m: block["methods"][m]["empty"] for m in methods},
        "one_radio_prior": (None if not one else
                            {m: one["methods"][m]["occupied"] for m in methods}),
        "intervals_covering_one_radio_prior": cover,
        "permutation_significant_bonferroni": nsig,
        "excess_ci_span": [lo, hi]}
    OUT.with_suffix(".json").write_text(json.dumps(payload, indent=1))
    print("wrote", OUT.with_suffix(".json"))


if __name__ == "__main__":
    main()
