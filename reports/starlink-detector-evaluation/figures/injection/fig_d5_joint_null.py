"""D5: is the joint empty-cell rate really p_A x p_B?

Regenerates fig_d5_joint_null.png from fig_d5_joint_null.json.
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_d5_joint_null.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.0, 5.4))
points = D["points"]

# -- panel A: measured joint rate against the model's prediction -------
pos = [p for p in points if p["expected"] > 0 and p["p_ab"] > 0]
zero = [p for p in points if p["p_ab"] == 0 and p["expected"] > 0]
xs = [p["expected"] for p in points if p["expected"] > 0]
ys = [p["p_ab"] for p in pos]
top = max(xs + ys) * 2.2
bottom = min(xs + ys) * 0.4
edge = np.array([bottom, top])
ax.plot(edge, edge, color=F.CRITICAL, linestyle="--", linewidth=1.6, zorder=1,
        label="P(AB) = P(A)P(B)  (the model's assumption)")
ax.scatter([p["expected"] for p in pos], [p["p_ab"] for p in pos],
           s=54, color=F.S1, edgecolor=F.SURFACE, linewidth=1.0, zorder=3,
           label=f"measured, {len(pos)} of {len(points)} cases")
if zero:
    ax.scatter([p["expected"] for p in zero], [bottom * 1.25] * len(zero),
               s=54, color=F.S1, alpha=0.4, marker="v",
               edgecolor=F.SURFACE, linewidth=1.0, zorder=3,
               label=f"P(AB) = 0 observed ({len(zero)} cases, plotted at floor)")
ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xlim(bottom, top)
ax.set_ylim(bottom, top)
ax.set_xlabel("P(A) x P(B)  -  independent prediction")
ax.set_ylabel("P(AB) measured, both radios silent\nat the same instant")
ax.set_title(f"Joint null, n = {D['n']} silent instants", loc="left")
ax.legend(loc="lower right", fontsize=8.8)

# -- panel B: Fisher exact p-values ------------------------------------
rates = sorted({p["target_rate"] for p in points}, reverse=True)
bx.axhline(0.05, color=F.CRITICAL, linestyle="--", linewidth=1.6, zorder=1)
bx.text(len(rates) - 0.5, 0.05, " reject below", fontsize=9, color=F.CRITICAL,
        va="bottom", ha="right")
for i, rate in enumerate(rates):
    group = [p for p in points if p["target_rate"] == rate
             and p["fisher_p"] is not None]
    if not group:
        continue
    spread = np.linspace(-0.24, 0.24, len(group))
    bx.scatter([i + s for s in spread], [p["fisher_p"] for p in group],
               s=42, color=F.S1, edgecolor=F.SURFACE, linewidth=0.8, zorder=3)
bx.set_yscale("log")
bx.set_xticks(np.arange(len(rates)))
bx.set_xticklabels([f"{r:.0%}" for r in rates])
bx.set_xlabel("false-alarm rate the threshold was set for\n"
              "(one dot per algorithm, 8 per setting)")
bx.set_ylabel("Fisher exact p-value for independence\n(2x2 joint table)")
bx.set_title(f"{D['consistent']} of {D['cases']} cases consistent "
             f"with independence", loc="left")

fig.suptitle(D["title"], fontsize=13.5, fontweight="bold", color=F.INK,
             x=0.008, ha="left", y=0.985)
F.finish(fig, [ax, bx])
ax.grid(True, axis="both", alpha=1.0)
bx.grid(True, axis="both", alpha=1.0)
fig.tight_layout(rect=[0, 0.10, 1, 0.925])
fig.savefig(HERE / "fig_d5_joint_null.png")
print("wrote fig_d5_joint_null.png")
