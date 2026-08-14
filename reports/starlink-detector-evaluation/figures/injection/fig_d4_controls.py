"""D4: the report's own negative controls, on data where they MUST destroy f.

Regenerates fig_d4_controls.png from fig_d4_controls.json.
"""
import json
import sys
import textwrap
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_d4_controls.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.2, 5.5))

joins = D["joins"]
colours = {"real": F.S1, "scrambled": F.S2, "shifted": F.S3}
short = {"real": "real pairing", "scrambled": "scrambled sweep",
         "shifted": "shifted +2 instants"}
levels = D["levels"]
pos = np.arange(len(levels))
width = 0.25

# -- panel A: does f move? --------------------------------------------
for j, join in enumerate(joins):
    offset = (j - 1) * width
    for i, lv in enumerate(levels):
        block = lv["joins"][join]
        x = i + offset
        if block["f_min"] is None:
            ax.text(x, 0.02, "no\nfit", ha="center", va="bottom", fontsize=9,
                    color=F.MUTED, fontweight="bold")
            continue
        ax.plot([x, x], [block["f_min"], block["f_max"]], color=colours[join],
                linewidth=8, alpha=0.30, solid_capstyle="round", zorder=2)
        ax.plot([x], [block["f_median"]], marker="o", markersize=8,
                color=colours[join], markeredgecolor=F.SURFACE,
                markeredgewidth=1.4, zorder=4)

for i, lv in enumerate(levels):
    ax.plot([i - 0.45, i + 0.45], [lv["f_realised"]] * 2, color=F.CRITICAL,
            linestyle="--", linewidth=1.7, zorder=5)

ax.set_xticks(pos)
ax.set_xticklabels([f"f = {lv['f_true']:g}\nn = {lv['cells']}" for lv in levels])
ax.set_ylabel("occupancy f returned by the estimator")
ax.set_xlabel("injected occupancy level")
ax.set_ylim(bottom=0)
ax.set_title("Breaking the pairing destroys f", loc="left")

# -- panel B: does the agreement check notice? -------------------------
for j, join in enumerate(joins):
    offset = (j - 1) * width
    for i, lv in enumerate(levels):
        block = lv["joins"][join]
        x = i + offset
        if block["spread"] is None:
            bx.text(x, 0.004, "no\nfit", ha="center", va="bottom", fontsize=9,
                    color=F.MUTED, fontweight="bold")
            continue
        if block["spread_p05"] is not None:
            bx.plot([x, x], [block["spread_p05"], block["spread_p95"]],
                    color=colours[join], linewidth=8, alpha=0.30,
                    solid_capstyle="round", zorder=2)
        bx.plot([x], [block["spread"]], marker="o", markersize=8,
                color=colours[join], markeredgecolor=F.SURFACE,
                markeredgewidth=1.4, zorder=4)
bx.set_xticks(pos)
bx.set_xticklabels([f"f = {lv['f_true']:g}\nverdict: {lv['verdict_short']}"
                    for lv in levels])
bx.set_ylabel("across-algorithm spread in f")
bx.set_xlabel("injected occupancy level")
bx.set_ylim(bottom=0)
bx.set_title("...and the check has nothing left to compare", loc="left")

# one shared legend, above both panels, so neither sits on data
handles = [plt.Line2D([], [], marker="o", markersize=8, linestyle="none",
                      color=colours[j], label=short[j]) for j in joins]
handles.append(plt.Line2D([], [], color=F.CRITICAL, linestyle="--",
                          linewidth=1.7, label="truth (injected occupancy)"))
handles.append(plt.Line2D([], [], color=F.MUTED, linewidth=8, alpha=0.30,
                          label="bar = range over the 8 algorithms"))
fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.05, 0.895),
           ncol=3, fontsize=9.2, columnspacing=1.6, handletextpad=0.6)

fig.suptitle(textwrap.fill(D["title"], 96), fontsize=13.0, fontweight="bold",
             color=F.INK, x=0.008, ha="left", y=0.988)
F.finish(fig, [ax, bx])
fig.tight_layout(rect=[0, 0.10, 1, 0.855])
fig.savefig(HERE / "fig_d4_controls.png")
print("wrote fig_d4_controls.png")
