"""A2 figure: the ranking once every detector pays the same per-cell cost."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F                                        # noqa: E402

RANK = HERE.parent / "a2_ranking.json"
POINT = HERE.parent / "a2_point_estimates.json"
OUT = HERE / "a2-common-false-alarm-ranking"

SHORT = {"full-frame-verify": "full-frame-verify", "full-frame-full": "full-frame-full",
         "glrt-32": "glrt-32", "full-frame-acquire": "full-frame-acquire",
         "glrt-64": "glrt-64", "anchor-8": "anchor-8",
         "differential-32": "differential-32", "differential-16": "differential-16"}


def main():
    F.setup()
    import matplotlib.pyplot as plt

    rank = json.loads(RANK.read_text())
    point = json.loads(POINT.read_text())
    old, new = rank["old"], rank["new"]
    order = new["ranking_best_first"]                 # best first
    y = np.arange(len(order))[::-1]                   # best at top

    fa_old = point["per_cell_rates"]["roc_tx_off_rungs"]["old"]
    fa_new = point["per_cell_rates"]["roc_tx_off_rungs"]["new"]
    cells = point["per_cell_rates"]["roc_tx_off_rungs"]["cells"]

    # the leading group = methods no family-wise pair separates from the leader
    lead = [m for m in order
            if not any(v["family_wise_resolved"]
                       for k, v in new["pairs"].items()
                       if m in k.split(" - ") and order[0] in k.split(" - "))
            or m == order[0]]

    rung_cells = min(e["cells"] for e in point["_rungs"]["new"]
                     if e["transmitting"])
    n_rungs = sum(1 for e in point["_rungs"]["new"]
                  if e["transmitting"] and e["above_estimator_floor"])

    fig = plt.figure(figsize=(13.6, 6.9))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.42], wspace=0.30,
                            left=0.115, right=0.988, top=0.775, bottom=0.215)
    axa = fig.add_subplot(grid[0, 0])
    axb = fig.add_subplot(grid[0, 1])

    # -- panel A: the operating cost, before and after --------------------
    axa.plot([fa_old[m] * 100 for m in order], y, "o", color=F.MUTED,
             markersize=8, label="_a",
             markeredgecolor=F.SURFACE, markeredgewidth=1.5, linestyle="none")
    axa.plot([fa_new[m] * 100 for m in order], y, "o", color=F.S1,
             markersize=8, label="_b",
             markeredgecolor=F.SURFACE, markeredgewidth=1.5, linestyle="none")
    for i, m in enumerate(order):
        axa.annotate("", xy=(fa_new[m] * 100, y[i]), xytext=(fa_old[m] * 100, y[i]),
                     arrowprops=dict(arrowstyle="-", color=F.GRID, lw=1.6))
    axa.axvline(5.0, color=F.S1, lw=1.0, ls=":", alpha=0.7)
    axa.set_yticks(y)
    axa.set_yticklabels([SHORT[m] for m in order])
    axa.set_xlabel("measured false alarm rate per cell (%)")
    axa.set_title(f"Equal operational cost\n(genuinely empty input, n={cells} cells)",
                  loc="left")
    axa.set_xlim(0, 11.4)
    axa.set_ylim(-0.6, len(order) - 0.4)

    # -- panel B: SNR at Pd=0.5, old vs new -------------------------------
    for i, m in enumerate(order):
        lo, hi = new["snr50_ci"][m]
        axb.plot([lo, hi], [y[i], y[i]], "-", color=F.S1, lw=2.4, alpha=0.30,
                 solid_capstyle="round")
    axb.plot([old["snr50_db"][m] for m in order], y, "o", color=F.MUTED,
             markersize=8, markeredgecolor=F.SURFACE, markeredgewidth=1.5,
             linestyle="none", label="published: 1% per candidate point (unequal per-cell cost)")
    axb.plot([new["snr50_db"][m] for m in order], y, "o", color=F.S1,
             markersize=9, markeredgecolor=F.SURFACE, markeredgewidth=1.5,
             linestyle="none", label="recalibrated: 5% per cell (equal cost), 95% CI")

    span = [new["snr50_db"][m] for m in lead]
    axb.axhspan(min(y[:len(lead)]) - 0.45, max(y[:len(lead)]) + 0.45,
                color=F.S1, alpha=0.055, zorder=0)

    axb.set_yticks(y)
    axb.set_yticklabels([SHORT[m] for m in order])
    axb.set_xlabel("SNR at P$_d$ = 0.5 (dB)   ← more sensitive")
    axb.set_title(f"SNR at P$_d$=0.5  ({rung_cells} cells/rung, {n_rungs} rungs)",
                  loc="left")
    axb.set_ylim(-0.6, len(order) - 0.4)
    axb.invert_xaxis()
    axb.text(0.015, 1 - (len(lead) - 0.42) / (len(order) - 0.2),
             f"leading group: {len(lead)} detectors within "
             f"{max(span) - min(span):.2f} dB,\nno pair separated family-wise",
             transform=axb.transAxes, fontsize=9.4, color=F.S1,
             va="bottom", ha="left")

    handles, labels = axb.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2,
               bbox_to_anchor=(0.5, 0.088), frameon=False, handletextpad=0.5,
               columnspacing=2.4)

    fig.suptitle("Charging every detector the same per-cell false alarm dissolves the head of the ranking: "
                 "glrt-32 falls from 1st to 3rd,\nand five detectors tie within 0.25 dB - "
                 f"{new['pairs_resolved_family_wise']}/28 pairs survive a family-wise correction "
                 f"(the published 21/28 carried none)",
                 fontsize=12.4, fontweight="bold", color=F.INK, x=0.008, ha="left", y=0.975)
    F.finish(fig, [axa, axb], F.CAVEAT_ONE, caveat_y=0.010, axis="x")
    fig.savefig(OUT.with_suffix(".png"))
    print("wrote", OUT.with_suffix(".png"))

    payload = {
        "figure": "a2-common-false-alarm-ranking",
        "note": F.CAVEAT_ONE.replace("\n", " "),
        "ranking_old_best_first": old["ranking_best_first"],
        "ranking_new_best_first": new["ranking_best_first"],
        "snr50_old_db": old["snr50_db"], "snr50_new_db": new["snr50_db"],
        "snr50_new_ci": new["snr50_ci"],
        "per_cell_false_alarm_old": fa_old, "per_cell_false_alarm_new": fa_new,
        "leading_group": lead,
        "leading_group_spread_db": float(max(span) - min(span)),
        "pairs_resolved_marginal_old": old["pairs_resolved_marginal"],
        "pairs_resolved_family_wise_old": old["pairs_resolved_family_wise"],
        "pairs_resolved_marginal_new": new["pairs_resolved_marginal"],
        "pairs_resolved_family_wise_new": new["pairs_resolved_family_wise"],
        "partial_order_edges_new": new["partial_order_edges"]}
    OUT.with_suffix(".json").write_text(json.dumps(payload, indent=1))
    print("wrote", OUT.with_suffix(".json"))


if __name__ == "__main__":
    main()
