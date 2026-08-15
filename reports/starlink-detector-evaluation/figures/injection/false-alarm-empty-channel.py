"""E3 figure: what the detectors do on a channel that is genuinely empty.

Left: the false-alarm rate when the threshold is drawn from the empty channel
itself, per point and per cell, against the nominal 1% and against the
5.47-6.74% per cell the report measures on sky.

Right: the same empty channel, judged by thresholds drawn the way the pipeline
draws them.  The repository builds two different cross-edge nulls and they do
not behave alike; this is the panel that says which.
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

E3 = HERE.parent / "e3_falsealarm.json"
E3B = HERE.parent / "e3b_summary.json"


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    data = json.loads(E3.read_text())
    extra = json.loads(E3B.read_text()) if E3B.exists() else None

    methods = list(A.METHODS)
    ys = np.arange(len(methods))

    fig = plt.figure(figsize=(13.6, 6.4))
    grid = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.12], wspace=0.32,
                            left=0.135, right=0.985, top=0.835, bottom=0.185)
    ax = fig.add_subplot(grid[0, 0])
    ax2 = fig.add_subplot(grid[0, 1])

    sky = list(A.SKY_NULL_RATE.values())
    ax.axvspan(min(sky) * 100, max(sky) * 100, color=style.MUTED, alpha=0.18,
               linewidth=0)
    ax.axvline(1.0, color=style.INK, linewidth=1.3, linestyle=(0, (4, 2)))
    # Both reference labels sit along the bottom of the plot body: at the top
    # they run into the subtitle, and this axis has spare room below the last
    # detector row.
    ax.text(1.0, len(methods) + 0.02, " nominal\n 1% per point", ha="left",
            va="bottom", fontsize=8.5, color=style.INK)
    ax.text(np.mean(sky) * 100, len(methods) + 0.02,
            "sky cross-edge null\n5.47–6.74% per cell", ha="center",
            va="bottom", fontsize=8.5, color=style.INK_2)

    for i, m in enumerate(methods):
        s = style.STYLE[m]
        row = data["measured"][m]
        for key, offset, filled in (("per_point", -0.19, False),
                                    ("per_cell", 0.19, True)):
            r = row[key]
            ax.plot([r["lo"] * 100, r["hi"] * 100], [i + offset] * 2,
                    color=s["color"], linewidth=2.2, alpha=0.5,
                    solid_capstyle="round")
            ax.plot([r["rate"] * 100], [i + offset], marker=s["marker"],
                    color=s["color"], markersize=7.5,
                    markerfacecolor=s["color"] if filled else style.SURFACE,
                    markeredgecolor=s["color"], markeredgewidth=1.3)
    ax.set_yticks(ys)
    ax.set_yticklabels(methods, fontsize=9.5)
    ax.set_ylim(len(methods) + 0.62, -0.55)
    ax.set_xlabel("False-alarm rate on an empty channel (%)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Calibrated on empty input, the thresholds hold", loc="left",
                 pad=26)
    pop = data["population"]
    ax.text(0.0, 1.015,
            f"hollow = per point, filled = per cell ({pop['points_per_cell_mean']:.1f} "
            f"points/cell); n = {pop['evaluation_observations']} held-out cells",
            transform=ax.transAxes, fontsize=9, color=style.INK_2)

    # -- right panel: the two null constructions ----------------------------
    labels = ["threshold from the\nempty channel itself",
              "threshold from the conditioned\ncross-edge score\n(opposite template, "
              "target-selected points)"]
    series = [[data["measured"][m]["per_cell"]["rate"] * 100 for m in methods],
              [data["measured"][m]["per_cell_cross_edge_threshold"]["rate"] * 100
               for m in methods]]
    if extra:
        labels.append("threshold from the separate\ncross-edge NULL ARM\n"
                      "(opposite edge as its own target)")
        series.append([extra["per_cell_at_null_arm_threshold"][m]["rate"] * 100
                       for m in methods])

    height = 0.8 / len(series)
    hatches = [None, "///", ".."]
    greys = ["#4a3aa7", "#e34948", "#eb6834"]
    for k, (label, values) in enumerate(zip(labels, series)):
        ax2.barh(ys + (k - (len(series) - 1) / 2) * height, values,
                 height=height * 0.86, color=greys[k], alpha=0.85,
                 label=label, hatch=hatches[k], edgecolor=style.SURFACE,
                 linewidth=1.0)
    ax2.axvline(np.mean(sky) * 100, color=style.INK, linewidth=1.2,
                linestyle=(0, (4, 2)))
    ax2.text(np.mean(sky) * 100 + 1.0, len(methods) + 0.05, "sky null ~6%",
             fontsize=8.5, color=style.INK, va="bottom")
    ax2.set_yticks(ys)
    ax2.set_yticklabels(methods, fontsize=9.5)
    ax2.set_ylim(len(methods) + 0.62, -0.55)
    ax2.set_xlabel("Per-cell false-alarm rate on the SAME empty channel (%)")
    ax2.grid(axis="y", visible=False)
    ax2.legend(loc="upper right", fontsize=8.2, labelspacing=1.0,
               borderpad=0.7, framealpha=0.94, frameon=True,
               facecolor=style.SURFACE, edgecolor=style.GRID)
    worst = max(series[1])
    ax2.set_title("The null the report uses is sound; the other one is not",
                  loc="left", pad=26)
    ax2.text(0.0, 1.015,
             "same probes, same detectors — only the null population changes",
             transform=ax2.transAxes, fontsize=9, color=style.INK_2)

    style.footer(fig,
                 f"Per-point rate realised on held-out empty input: "
                 f"{min(r['per_point']['rate'] for r in data['measured'].values())*100:.2f}"
                 f"–{max(r['per_point']['rate'] for r in data['measured'].values())*100:.2f}% "
                 f"against a requested 1%.")
    fig.savefig(HERE / "false-alarm-empty-channel.png")
    print("per-cell at empty threshold:",
          {m: round(data["measured"][m]["per_cell"]["rate"] * 100, 2) for m in methods})
    print("per-cell at conditioned cross-edge threshold:",
          {m: round(data["measured"][m]["per_cell_cross_edge_threshold"]["rate"] * 100, 2)
           for m in methods})


if __name__ == "__main__":
    main()
