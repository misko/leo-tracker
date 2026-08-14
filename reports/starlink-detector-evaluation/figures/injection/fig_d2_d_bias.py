"""D2: the solver's detection probability against the one measured directly.

Regenerates fig_d2_d_bias.png from fig_d2_d_bias.json.
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_d2_d_bias.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, (ax, bx) = plt.subplots(1, 2, figsize=(10.6, 5.2),
                             gridspec_kw={"width_ratios": [1.15, 1]})

colours = {"A": F.S1, "B": F.S2}
names = {"A": D["radio_a"], "B": D["radio_b"]}

lo = min([p["direct"] for p in D["points"]] + [p["solved"] for p in D["points"]])
hi = max([p["direct"] for p in D["points"]] + [p["solved"] for p in D["points"]])
pad = 0.05 * (hi - lo or 1)
edge = np.array([lo - pad, hi + pad])
ax.plot(edge, edge, color=F.CRITICAL, linestyle="--", linewidth=1.6,
        label="solver agrees with direct measurement", zorder=1)
for side in ("A", "B"):
    pts = [p for p in D["points"] if p["side"] == side]
    ax.scatter([p["direct"] for p in pts], [p["solved"] for p in pts],
               s=58, color=colours[side], edgecolor=F.SURFACE, linewidth=1.1,
               zorder=3, label=f"{names[side]}  (n={len(pts)})")
ax.set_xlabel("directly measured Pd\n(fires | a pilot frame WAS injected)")
ax.set_ylabel("detection probability d recovered\nby the coincidence solver")
ax.set_xlim(*edge)
ax.set_ylim(*edge)
ax.set_title("Solver d against ground truth", loc="left")
ax.legend(loc="lower right", fontsize=9)

# -- the bias itself ---------------------------------------------------
bias = [p["solved"] - p["direct"] for p in D["points"]]
bx.axvline(0, color=F.CRITICAL, linestyle="--", linewidth=1.6, zorder=1)
bins = np.linspace(min(bias + [-0.02]), max(bias + [0.02]), 22)
bx.hist([b for b, p in zip(bias, D["points"]) if p["side"] == "A"], bins=bins,
        color=F.S1, alpha=0.85, label=names["A"])
bx.hist([b for b, p in zip(bias, D["points"]) if p["side"] == "B"], bins=bins,
        color=F.S2, alpha=0.72, label=names["B"])
bx.set_xlabel("solver d  -  measured Pd")
bx.set_ylabel("cases (algorithm x occupancy level x radio)")
low = D["summary"]["reads_low"]
total = D["summary"]["cases"]
bx.set_title(f"Reads low in {low} of {total} cases;\n"
             f"median {D['summary']['median_bias']:+.4f}, "
             f"worst {D['summary']['worst_bias']:+.3f}", loc="left")
bx.legend(loc="center right", fontsize=9)
bx.text(0.015, 0.995,
        f"for comparison, the single-rig loopback\n(two receivers, one oscillator):\n"
        f"read low in {D['summary']['prior_low']} of {D['summary']['prior_cases']} "
        f"cases, by up to {D['summary']['prior_worst']:.2f}",
        transform=bx.transAxes, fontsize=8.4, color=F.MUTED, va="top",
        linespacing=1.45)

fig.suptitle(D["title"], fontsize=13.5, fontweight="bold", color=F.INK,
             x=0.008, ha="left", y=0.985)
F.finish(fig, [ax, bx])
ax.grid(True, axis="both", alpha=1.0)
fig.tight_layout(rect=[0, 0.09, 1, 0.93])
fig.savefig(HERE / "fig_d2_d_bias.png")
print("wrote fig_d2_d_bias.png")
