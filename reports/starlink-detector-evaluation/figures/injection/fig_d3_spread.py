"""D3: how far apart the eight algorithms put an f that is one number by construction.

Regenerates fig_d3_spread.png from fig_d3_spread.json.
"""
import json
import sys
from pathlib import Path

import textwrap

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_d3_spread.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(9.8, 5.6))

levels = D["levels"]
pos = np.arange(len(levels))

for reference in D["references"]:
    ax.axhline(reference["value"], color=F.MUTED, linestyle=":", linewidth=1.5)
    ax.text(0.008, reference["value"], reference["label"], fontsize=9,
            color=F.INK2, va="bottom", ha="left",
            transform=ax.get_yaxis_transform())

for i, lv in enumerate(levels):
    lo, hi = lv["p05"], lv["p95"]
    if lo is not None:
        ax.plot([i, i], [lo, hi], color=F.S1, linewidth=9, alpha=0.22,
                solid_capstyle="round", zorder=2)
    ax.plot([i], [lv["spread"]], marker="o", markersize=13, color=F.S1,
            markeredgecolor=F.SURFACE, markeredgewidth=2, zorder=4)
    ax.annotate(f"{lv['spread']:.3f}", (i, lv["spread"]),
                textcoords="offset points", xytext=(17, 0), fontsize=11,
                color=F.INK, fontweight="bold", va="center",
                bbox=dict(boxstyle="round,pad=0.22", facecolor=F.SURFACE,
                          edgecolor="none"))

ax.plot([], [], marker="o", markersize=11, color=F.S1, linestyle="none",
        label="observed spread  (max - min of f over the 8 algorithms)")
ax.plot([], [], linewidth=9, alpha=0.22, color=F.S1,
        label=f"5th-95th percentile of the spread under {D['draws']} joint cell resamples")

ax.set_xticks(pos)
ax.set_xticklabels([f"f = {lv['f_true']:g}\nn = {lv['cells']} instants" for lv in levels])
ax.set_xlim(-0.62, len(levels) - 0.38)
ax.set_ylim(bottom=0)
ax.set_xlabel("injected occupancy level")
ax.set_ylabel("across-algorithm spread in f\n(dimensionless; f is a fraction)")
ax.set_title(textwrap.fill(D["title"], 74), loc="left")
ax.legend(loc="lower right", fontsize=9.2)
F.finish(fig, ax)
fig.tight_layout(rect=[0, 0.095, 1, 1])
fig.savefig(HERE / "fig_d3_spread.png")
print("wrote fig_d3_spread.png")
