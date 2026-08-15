"""A3 figure: does the detector order survive a change of probe length and rate?

Eight detectors on one axis would be eight identities; the story is whether the
GROUPS hold, so the leading group at the 20 ms / 5 MS/s baseline is one colour
and the trailing group another.  Two hues, used in fixed order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F                                        # noqa: E402

SRC = HERE.parent / "a3_arms.json"
A2 = HERE.parent / "a2_ranking.json"
FIGJ = HERE / "a2-common-false-alarm-ranking.json"
OUT = HERE / "a3-ranking-across-arms"

#: preferred left-to-right order of arms; missing ones are skipped
ARM_ORDER = ["baseline", "80ms-1.25MSs", "80ms-2.5MSs", "160ms-5MSs", "640ms-2.5MSs"]
ARM_LABEL = {"baseline": "20 ms\n5 MS/s",
             "80ms-1.25MSs": "80 ms\n1.25 MS/s *",
             "80ms-2.5MSs": "80 ms\n2.5 MS/s",
             "160ms-5MSs": "160 ms\n5 MS/s",
             "640ms-2.5MSs": "640 ms\n2.5 MS/s"}


def main():
    F.setup()
    import matplotlib.pyplot as plt

    data = json.loads(SRC.read_text())
    lead = set(json.loads(FIGJ.read_text())["leading_group"])
    series = {"baseline": data["baseline_20ms_5MSs"]["snr50_db"]}
    resolved = {}
    for name, arm in data["arms"].items():
        series[name] = arm["snr50_db"]
        resolved[name] = arm["pairs_resolved_family_wise"]
    arms = [a for a in ARM_ORDER if a in series]
    methods = list(data["baseline_20ms_5MSs"]["snr50_db"])
    methods.sort(key=lambda m: series["baseline"][m])
    x = np.arange(len(arms))

    fig = plt.figure(figsize=(13.6, 7.4))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.22, 1.0], wspace=0.22,
                            left=0.072, right=0.885, top=0.755, bottom=0.245)
    axa = fig.add_subplot(grid[0, 0])
    axb = fig.add_subplot(grid[0, 1])

    def colour(m):
        return F.S1 if m in lead else F.MUTED

    # -- panel A: absolute sensitivity -------------------------------------
    for m in methods:
        ys = [series[a].get(m, np.nan) for a in arms]
        axa.plot(x, ys, "-o", color=colour(m), lw=1.7, markersize=6,
                 alpha=0.95 if m in lead else 0.75,
                 markeredgecolor=F.SURFACE, markeredgewidth=1.2, zorder=3)
    axa.set_xticks(x)
    axa.set_xticklabels([ARM_LABEL.get(a, a) for a in arms], fontsize=9.2)
    axa.set_ylabel("SNR at P$_d$ = 0.5 (dB)")
    axa.set_title("Sensitivity by arm", loc="left")
    axa.invert_yaxis()
    axa.text(0.012, 0.028, "↓ more sensitive", transform=axa.transAxes,
             fontsize=9.4, color=F.MUTED)

    # -- panel B: rank ------------------------------------------------------
    for m in methods:
        ranks = []
        for a in arms:
            order = sorted(methods, key=lambda k: series[a].get(k, np.inf))
            ranks.append(order.index(m) + 1)
        axb.plot(x, ranks, "-o", color=colour(m), lw=1.7, markersize=6,
                 alpha=0.95 if m in lead else 0.75,
                 markeredgecolor=F.SURFACE, markeredgewidth=1.2, zorder=3)
        axb.annotate(m, xy=(x[-1], ranks[-1]), xytext=(6, 0),
                     textcoords="offset points", va="center", fontsize=9.2,
                     color=F.INK2)
    axb.set_xticks(x)
    axb.set_xticklabels(
        [f"{ARM_LABEL.get(a, a)}\n{'17' if a == 'baseline' else resolved[a]}/28"
         for a in arms], fontsize=9.2)
    axb.set_yticks(range(1, len(methods) + 1))
    axb.set_ylabel("rank by SNR at P$_d$=0.5 (1 = most sensitive)")
    axb.set_title("Rank order, arm by arm   (bottom row: pairs resolved family-wise)",
                  loc="left", fontsize=11.2)
    axb.invert_yaxis()
    axb.set_xlim(-0.25, len(arms) - 0.55)

    handles = [plt.Line2D([], [], color=F.S1, marker="o", lw=1.7,
                          markeredgecolor=F.SURFACE,
                          label="leading group at the 20 ms / 5 MS/s baseline"),
               plt.Line2D([], [], color=F.MUTED, marker="o", lw=1.7,
                          markeredgecolor=F.SURFACE,
                          label="trailing group at the baseline")]
    fig.legend(handles=handles, loc="lower center", ncol=2,
               bbox_to_anchor=(0.5, 0.115), frameon=False, columnspacing=2.4)

    # The headline is COMPUTED, not asserted.
    trail = [m for m in methods if m not in lead]
    resolving = [a for a in arms if a == "baseline" or resolved[a] >= 5]
    holds = [a for a in resolving
             if set(sorted(series[a], key=lambda m: series[a][m])[:len(lead)]) == lead]
    worst_rate = min(arms, key=lambda a: max(series[a].values()))
    ranks_g32 = [sorted(methods, key=lambda k: series[a][k]).index("glrt-32") + 1
                 for a in resolving]
    dead = [a for a in arms if a != "baseline" and resolved[a] < 5]
    agree_dead = (max((data["arms"][a].get("decision_agreement", {}).get("mean") or 0)
                      for a in dead) if dead else None)
    headline = (
        f"The baseline two-group split survives in {len(holds)} of {len(resolving)} arms that resolve anything; "
        f"within the leading group nothing is stable -\n"
        f"glrt-32 sits at rank {'/'.join(str(r) for r in ranks_g32)} across them. "
        + (f"In {len(dead)} arm(s) nothing resolves at all: the eight converge, agreeing on "
           f"{agree_dead:.0%} of per-cell decisions.\n" if dead else "\n")
        + f"1.25 MS/s is markedly worse as expected - the 1.875 MHz pilot cannot fit, "
          f"costing {np.mean(list(series['80ms-1.25MSs'].values())) - np.mean(list(series['80ms-2.5MSs'].values())):.1f} dB "
          f"on average vs 2.5 MS/s at 80 ms")
    fig.suptitle(headline, fontsize=11.8, fontweight="bold", color=F.INK,
                 x=0.008, ha="left", y=0.985)
    F.finish(fig, [axa, axb],
             "* the 1.875 MHz pilot allocation does not fit inside a 1.25 MS/s receiver. SNR here is per-sample and "
             "in-band, so absolute values across arms of\ndifferent rate also carry the change in noise bandwidth; "
             "the within-arm ORDER is the comparison that does not.\n" + F.CAVEAT_TWO,
             caveat_y=0.008, axis="y")
    fig.savefig(OUT.with_suffix(".png"))
    print("wrote", OUT.with_suffix(".png"))

    payload = {"figure": "a3-ranking-across-arms",
               "note": F.CAVEAT_TWO.replace("\n", " "),
               "arms": arms,
               "snr50_by_arm": {a: series[a] for a in arms},
               "rank_by_arm": {a: sorted(methods, key=lambda k: series[a].get(k, np.inf))
                               for a in arms},
               "leading_group_baseline": sorted(lead),
               "arms_where_baseline_split_holds": holds,
               "arms_resolving": resolving,
               "glrt32_rank_in_resolving_arms": dict(zip(resolving, ranks_g32)),
               "arms_resolving_nothing": dead,
               "pairs_resolved_family_wise": resolved}
    OUT.with_suffix(".json").write_text(json.dumps(payload, indent=1))
    print("wrote", OUT.with_suffix(".json"))


if __name__ == "__main__":
    main()
