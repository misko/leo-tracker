"""Three orderings of the same eight detectors, one of them finally measured.

Left column: this experiment's ranking, by the SNR at which each detector
reaches 50% detection of a signal that was put there on purpose.
Middle: the coincidence model's d-ranking, a model output.
Right: the raw on-sky fire count, which the report itself says cannot rank.

Reads as a bump chart because the question is about ORDER, and because the
underlying quantities are in three incompatible units -- dB, a probability, and
a share of sky -- so plotting their values on one axis would be meaningless.
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

SOURCE = HERE / "detection-vs-snr.json"


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    data = json.loads(SOURCE.read_text())
    fits = data["fits"]
    measured = data["measured_ranking_best_first"]
    model = list(A.MODEL_D_RANKING)
    fire = list(A.FIRE_COUNT_RANKING)

    columns = [("Measured here\n(SNR at 50% detection)", measured),
               ("Model d-ranking\n(coincidence output)", model),
               ("On-sky fire count\n(most fires first)", fire)]
    rho_model = A.spearman(measured, model)
    rho_fire = A.spearman(measured, fire)
    rho_model_fire = A.spearman(model, fire)

    resolved = data["pairs_resolved"]
    total = data["pairs_total"]
    spread = fits[measured[-1]]["snr50_db"] - fits[measured[0]]["snr50_db"]

    payload = {"figure": "detector-ranking", "note": A.LOOPBACK_NOTE,
               "measured_ranking_best_first": measured,
               "model_d_ranking_best_first": model,
               "fire_count_ranking_most_first": fire,
               "snr50_db": {m: fits[m]["snr50_db"] for m in A.METHODS},
               "snr50_spread_db": spread,
               "pairs_resolved_at_95pc": resolved, "pairs_total": total,
               "spearman": {"measured_vs_model_d": rho_model,
                            "measured_vs_fire_count": rho_fire,
                            "model_d_vs_fire_count": rho_model_fire},
               "reported_spearman_model_vs_fire": -0.952}
    (HERE / "detector-ranking.json").write_text(json.dumps(payload, indent=1))

    fig, ax = plt.subplots(figsize=(11.6, 6.6))
    fig.subplots_adjust(left=0.20, right=0.80, top=0.80, bottom=0.19)
    xs = [0, 1, 2]
    for method in A.METHODS:
        s = style.STYLE[method]
        ys = [col.index(method) for _, col in columns]
        ax.plot(xs, ys, color=s["color"], linewidth=2.0, marker=s["marker"],
                markersize=8, markeredgecolor=style.SURFACE, markeredgewidth=0.9,
                alpha=0.9)
    for method in A.METHODS:
        s = style.STYLE[method]
        ax.text(-0.06, measured.index(method), method, ha="right", va="center",
                fontsize=10, color=style.INK)
        ax.text(2.06, fire.index(method), method, ha="left", va="center",
                fontsize=10, color=style.INK)

    for x, (label, _) in zip(xs, columns):
        ax.text(x, -1.62, label, ha="center", va="bottom", fontsize=10.5,
                color=style.INK)
    ax.set_xlim(-0.85, 2.85)
    ax.set_ylim(len(A.METHODS) - 0.35, -2.05)
    ax.set_xticks([])
    ax.set_yticks(range(len(A.METHODS)))
    ax.set_yticklabels([f"{i+1}" for i in range(len(A.METHODS))], fontsize=9)
    ax.set_ylabel("Rank (1 = best)")
    ax.grid(axis="x", visible=False)

    if resolved == 0:
        finding = (f"The eight detectors are indistinguishable: all within "
                   f"{spread:.2f} dB, 0 of {total} pairs resolved")
    else:
        finding = (f"The measured order matches neither published ranking "
                   f"({resolved} of {total} pairs resolved, spread {spread:.2f} dB)")
    ax.set_title(finding, loc="left", pad=46)
    ax.text(0.0, 1.075,
            f"Spearman: measured vs model d {rho_model:+.3f}   |   "
            f"measured vs fire count {rho_fire:+.3f}   |   "
            f"model d vs fire count {rho_model_fire:+.3f} (report: -0.952)",
            transform=ax.transAxes, fontsize=9.5, color=style.INK_2)
    style.footer(fig)
    fig.savefig(HERE / "detector-ranking.png")
    print("measured:", measured)
    print("rho model", round(rho_model, 3), "rho fire", round(rho_fire, 3))
    print("resolved pairs", resolved, "of", total, "spread", round(spread, 3), "dB")


if __name__ == "__main__":
    main()
