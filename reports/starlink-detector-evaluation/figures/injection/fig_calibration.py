"""Detection probability against TX attenuation, and the leak control.

Regenerates fig_calibration.png from fig_calibration.json.
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import figstyle as F

D = json.loads((HERE / "fig_calibration.json").read_text())
F.setup()
import matplotlib.pyplot as plt

fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.0, 5.2),
                             gridspec_kw={"width_ratios": [1.45, 1]})

colours = {"r183": F.S1, "r165": F.S2}
names = {"r183": ".183 (radio A)", "r165": ".165 (radio B)"}
ax.axhspan(0.5, 0.7, color=F.MUTED, alpha=0.10, linewidth=0)

for radio in ("r183", "r165"):
    curve = D["pd_curve"][radio]
    x = np.array(curve["attenuation_db"], float)
    ax.fill_between(x, curve["pd_min"], curve["pd_max"], color=colours[radio],
                    alpha=0.15, linewidth=0)
    pick = D["operating_point"][radio]
    ax.plot(x, curve["pd_mean"], color=colours[radio], marker="o", markersize=5.5,
            label=f"{names[radio]}, n={curve['n_per_point']}/point\n"
                  f"operating point (diamond) {pick['attenuation_db']:g} dB, "
                  f"Pd {pick['pd_mean']:.2f}")
    ax.plot([pick["attenuation_db"]], [pick["pd_mean"]], marker="D",
            markersize=12, color=colours[radio], markeredgecolor=F.SURFACE,
            markeredgewidth=2, zorder=5)

ax.text(0.985, 0.60, "partial-detection band", fontsize=9, color=F.MUTED,
        va="center", ha="right", transform=ax.transAxes)
ax.set_xlabel("TX2 attenuation setting (dB, hardwaregain)")
ax.set_ylabel("detection probability Pd\n(mean over 8 algorithms, band = min-max)")
ax.set_ylim(-0.04, 1.08)
ax.set_title("Pd falls from 1 to 0 in about 4 dB", loc="left")
ax.legend(loc="lower right", fontsize=8.8, labelspacing=0.9,
          borderpad=0.7)

# -- leak control: only the parked rate is data; the rest are references --
labels = list(D["leak"]["order"])
pos = np.arange(len(labels))
parked = [D["leak"]["parked_rate"][k] for k in labels]
dead = float(np.mean([D["leak"]["dead_rate"][k] for k in labels]))

bx.barh(pos, parked, height=0.62, color=F.S3, zorder=3,
        label="TX parked at -89.75 dB")
bx.axvline(dead, color=F.S1, linestyle="-", linewidth=1.8, zorder=4,
           label=f"same threshold on a truly\ndead TX buffer: {dead:.3f}")
bx.axvline(D["leak"]["nominal_rate"], color=F.CRITICAL, linestyle="--",
           linewidth=1.6, zorder=4,
           label=f"nominal {D['leak']['nominal_rate']:.0%}")
bx.legend(loc="lower right", fontsize=8.6, labelspacing=0.7, borderpad=0.7)
bx.set_yticks(pos)
bx.set_yticklabels([n.replace("full-frame-", "ff-").replace("differential-", "diff-")
                    for n in labels], fontsize=9)
bx.set_xlabel(f"fire rate on empty input (n={D['leak']['n']} probes)")
bx.set_title("A parked TX reads as silent", loc="left")
bx.set_ylim(-0.7, len(labels) - 0.3)
bx.grid(True, axis="x", alpha=1.0)
bx.grid(False, axis="y")
bx.set_axisbelow(True)
for side in ("top", "right"):
    bx.spines[side].set_visible(False)

fig.suptitle(D["title"], fontsize=13.2, fontweight="bold", color=F.INK,
             x=0.008, ha="left", y=0.985)
ax.set_axisbelow(True)
ax.grid(True, axis="both", alpha=1.0)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
fig.text(0.5, 0.012, F.CAVEAT, ha="center", va="bottom", fontsize=7.6,
         color=F.MUTED, linespacing=1.35)
fig.tight_layout(rect=[0, 0.105, 1, 0.925])
fig.savefig(HERE / "fig_calibration.png")
print("wrote fig_calibration.png")
