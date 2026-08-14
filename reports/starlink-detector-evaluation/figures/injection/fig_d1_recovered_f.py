"""D1: does the coincidence estimator recover the occupancy that was injected?

Regenerates fig_d1_recovered_f.png from fig_d1_recovered_f.json.
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_d1_recovered_f.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(9.2, 5.4))

levels = D["levels"]
xs = [lv["f_realised"] for lv in levels]
span = max(xs) - min(xs)
pad = 0.09 * span if span else 0.1

line = np.array([min(xs) - pad, max(xs) + pad])
ax.plot(line, line, color=F.CRITICAL, linestyle="--", linewidth=1.6,
        zorder=1, label="truth (injected occupancy)")

jitter = np.linspace(-0.0125, 0.0125, len(D["methods"]))
for lv in levels:
    for j, method in enumerate(D["methods"]):
        block = lv["methods"].get(method)
        if not block or block.get("f") is None:
            continue
        x = lv["f_realised"] + jitter[j]
        lo, hi = block.get("p05"), block.get("p95")
        if lo is not None and hi is not None:
            ax.plot([x, x], [lo, hi], color=F.S1, linewidth=1.5, alpha=0.55,
                    zorder=2, solid_capstyle="round")
        ax.plot([x], [block["f"]], marker="o", markersize=7, color=F.S1,
                markeredgecolor=F.SURFACE, markeredgewidth=1.2, zorder=3)
    ax.annotate(f"n = {lv['cells']} instants\n{lv['solvable']}/8 solvable",
                (lv["f_realised"], lv["label_y"]), ha="center", va="top",
                fontsize=9, color=F.INK2)

ax.plot([], [], marker="o", markersize=7, color=F.S1, linestyle="none",
        label=f"recovered f, one dot per algorithm ({len(D['methods'])} of them)\n"
              f"whisker = 5th-95th percentile, {D['draws']} cell resamples")

ax.set_xlabel("injected occupancy f (fraction of instants carrying a pilot frame)")
ax.set_ylabel("occupancy f recovered by the\ncoincidence estimator")
ax.set_xticks(xs)
ax.set_xticklabels([f"{lv['f_true']:g}\n(realised {lv['f_realised']:.3f})"
                    for lv in levels])
ax.set_xlim(line[0], line[1])
bottom = min([lv["label_y"] for lv in levels])
ax.set_ylim(bottom=bottom - 0.055)
ax.set_title(D["title"], loc="left")
ax.legend(loc="upper left", fontsize=9.4)
F.finish(fig, ax)
ax.grid(True, axis="both", alpha=1.0)
fig.tight_layout(rect=[0, 0.095, 1, 1])
fig.savefig(HERE / "fig_d1_recovered_f.png")
print("wrote fig_d1_recovered_f.png")
