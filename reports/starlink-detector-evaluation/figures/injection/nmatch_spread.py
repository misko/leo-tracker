#!/usr/bin/env python3
"""nmatch_spread - the sky f-spread once n is matched to the injected runs.

Reads only nmatch_spread.json beside it.  Palette: the report's own figstyle
slots (validated categorical 1-3), used in fixed order, never cycled; identity
carries on marker shape and direct labels as well as hue.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

HERE = Path(__file__).resolve().parent
D = json.loads((HERE / "nmatch_spread.json").read_text())

SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"

plt.rcParams.update({
    "figure.dpi": 150, "savefig.dpi": 150,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
    "font.size": 10.5,
    "axes.titlesize": 11.5, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "axes.labelsize": 10.5, "axes.labelcolor": INK2,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "xtick.labelsize": 9.5, "ytick.labelsize": 9.5,
    "grid.color": GRID, "grid.linewidth": 0.8,
    "legend.frameon": False, "legend.fontsize": 9.5,
    "lines.linewidth": 2.0, "lines.markersize": 8,
})

sky = D["sky"]
n500 = sky["n500_sweep"]
full = sky["full_sweep_bootstrap"]
obs = sky["observed_full_spread"]
pub = D["injected"]["as_published"]
arch = D["injected"]["as_archived"]

fig = plt.figure(figsize=(13.6, 7.6))
gs = fig.add_gridspec(2, 2, width_ratios=[1.32, 1.0], height_ratios=[1.15, 1.0],
                      hspace=0.42, wspace=0.24,
                      left=0.060, right=0.985, top=0.775, bottom=0.145)
ax_a = fig.add_subplot(gs[0, 0])
ax_b = fig.add_subplot(gs[1, 0], sharex=ax_a)
ax_c = fig.add_subplot(gs[:, 1])

XLO, XHI = 0.0, 0.175
bins = np.linspace(XLO, XHI, 71)


def band(ax, lo, hi, color):
    ax.axvspan(lo, hi, color=color, alpha=0.10, lw=0, zorder=0)


# ---------------------------------------------------------------- panel A
v = np.array(n500["values"])
ax_a.hist(v, bins=bins, color=S1, alpha=0.55, edgecolor=SURFACE, linewidth=0.6,
          zorder=2)
band(ax_a, n500["p05"], n500["p95"], S1)
ax_a.axvline(n500["p50"], color=S1, lw=2.0, zorder=3)
top = ax_a.get_ylim()[1]
ax_a.set_ylim(0, top * 1.34)
top = ax_a.get_ylim()[1]

ax_a.annotate(f"median {n500['p50']:.4f}", xy=(n500["p50"], top * 0.60),
              xytext=(4, 0), textcoords="offset points", color=S1,
              fontsize=9.5, fontweight="bold", va="center")
ax_a.text(n500["p05"], top * 0.94, f"p05 {n500['p05']:.4f}", color=INK2,
          fontsize=9, ha="right", va="top")
ax_a.text(n500["p95"], top * 0.94, f"p95 {n500['p95']:.4f}", color=INK2,
          fontsize=9, ha="left", va="top")

row_pub, row_arch = top * 0.78, top * 0.62
for row, rows, marker, fill in ((row_pub, pub, "D", S2),
                                (row_arch, arch, "o", SURFACE)):
    for r in sorted(rows, key=lambda x: x["spread"]):
        ax_a.plot([r["spread"]], [row], marker=marker, markersize=9,
                  markerfacecolor=fill, markeredgecolor=S2,
                  markeredgewidth=1.8, zorder=5, clip_on=False)
        ax_a.vlines(r["spread"], 0, row, color=S2, lw=1.0, ls=(0, (2, 2)),
                    alpha=0.75, zorder=4)
LEVEL_DY = {0.5: 10, 0.3: 22, 0.15: 10}
for r in pub:
    ax_a.annotate(f"f={r['f_true']:g}", xy=(r["spread"], row_pub),
                  xytext=(0, LEVEL_DY[r["f_true"]]), textcoords="offset points",
                  ha="center", color=INK2, fontsize=8.4, zorder=6)
ARCH_DY = {0.15: 10, 0.5: 10, 0.3: 22}
for r in arch:
    ax_a.annotate(f"f={r['f_true']:g}", xy=(r["spread"], row_arch),
                  xytext=(0, ARCH_DY[r["f_true"]]), textcoords="offset points",
                  ha="center", color=INK2, fontsize=8.4, zorder=6)

ax_a.set_title("Sky, subsampled by sweep to 500 cells — 3,000 draws", loc="left")
ax_a.set_ylabel("draws")

# ---------------------------------------------------------------- panel B
w = np.array(full["values"])
ax_b.hist(w, bins=bins, color=S3, alpha=0.60, edgecolor=SURFACE, linewidth=0.6,
          zorder=2)
band(ax_b, full["p05"], full["p95"], S3)
topb = ax_b.get_ylim()[1] * 1.30
ax_b.set_ylim(0, topb)
ax_b.plot([obs], [topb * 0.55], marker="v", markersize=10, color=INK, zorder=6,
          clip_on=False)
ax_b.vlines(obs, 0, topb * 0.55, color=INK, lw=1.6, zorder=5)
ax_b.annotate(f"observed {obs:.4f}\n(percentile "
              f"{sky['observed_percentile_in_own_n']:.0f} of its own n)",
              xy=(obs, topb * 0.60), xytext=(8, 0), textcoords="offset points",
              color=INK, fontsize=9.2, fontweight="bold", va="center")
ax_b.text(full["p05"], topb * 0.95, f"p05 {full['p05']:.4f}", color=INK2,
          fontsize=9, ha="right", va="top")
ax_b.text(full["p95"], topb * 0.95, f"p95 {full['p95']:.4f}", color=INK2,
          fontsize=9, ha="left", va="top")
ax_b.set_title(f"Sky at its own size — {sky['cells']:,} cells, "
               f"{sky['sweeps']:,} sweeps resampled", loc="left")
ax_b.set_xlabel("across-algorithm f-spread  (max − min over the eight detectors)")
ax_b.set_ylabel("draws")
ax_b.set_xlim(XLO, XHI)

# ---------------------------------------------------------------- panel C
curve = sky["n_curve"]
ns = np.array([r["n"] for r in curve], dtype=float)
p50 = np.array([r["p50"] for r in curve])
p05 = np.array([r["p05"] for r in curve])
p95 = np.array([r["p95"] for r in curve])
ax_c.fill_between(ns, p05, p95, color=S1, alpha=0.16, lw=0, zorder=1)
ax_c.plot(ns, p50, color=S1, marker="o", markersize=7, zorder=3,
          markeredgecolor=SURFACE, markeredgewidth=1.4)
ax_c.set_xscale("log")
ax_c.set_xlim(100, 26000)
ax_c.set_ylim(0, 0.20)
ax_c.axvline(500, color=AXIS, lw=1.0, ls=(0, (3, 3)), zorder=0)
ax_c.text(500, 0.196, " n = 500", color=MUTED, fontsize=9, va="top")

for r in pub:
    ax_c.plot([500], [r["spread"]], marker="D", markersize=9, zorder=5,
              markerfacecolor=S2, markeredgecolor=S2)
for r in arch:
    ax_c.plot([500], [r["spread"]], marker="o", markersize=9, zorder=5,
              markerfacecolor=SURFACE, markeredgecolor=S2, markeredgewidth=1.8)
ax_c.plot([sky["cells"]], [obs], marker="v", markersize=11, color=INK, zorder=6)
ax_c.annotate(f"sky, full corpus\n{obs:.4f}", xy=(sky["cells"], obs),
              xytext=(-10, 16), textcoords="offset points", ha="right",
              color=INK, fontsize=9.2, fontweight="bold")
ax_c.text(150, 0.183, "sky median at each n,\nwith its p05–p95 band",
          color=S1, fontsize=9.4, fontweight="bold", va="top", linespacing=1.35)
ax_c.set_title("Spread shrinks with n — the whole of the original gap", loc="left")
ax_c.set_xlabel("cells in the join (log)")
ax_c.set_ylabel("across-algorithm f-spread")
ax_c.set_xticks([128, 256, 500, 1000, 2000, 4000, 8000, 16560])
ax_c.set_xticklabels(["128", "256", "500", "1k", "2k", "4k", "8k", "16.6k"])

handles = [
    Line2D([], [], color=S1, lw=6, alpha=0.55, label="sky, sweep-resampled (p05–p95 band)"),
    Line2D([], [], color=S3, lw=6, alpha=0.60, label="sky at full size, sweep bootstrap"),
    Line2D([], [], color=S2, marker="D", ls="none", markersize=9,
           markerfacecolor=S2, label="injected, 500 cells — as the report quotes"),
    Line2D([], [], color=S2, marker="o", ls="none", markersize=9,
           markerfacecolor=SURFACE, markeredgewidth=1.8,
           label="injected, 500 cells — recomputed from the archived run"),
    Line2D([], [], color=INK, marker="v", ls="none", markersize=9,
           label="sky, full corpus (16,560 cells)"),
]
fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.055, 0.876),
           ncol=3, columnspacing=2.2, handletextpad=0.7, fontsize=9.2)

fig.suptitle("Match n first: the sky join's own spread at 500 cells is 0.066, not 0.040",
             x=0.060, ha="left", y=0.982, fontsize=14.5, fontweight="bold", color=INK)
fig.text(0.060, 0.940,
         "All three injected values the report quotes — 0.0569 / 0.0509 / 0.0460 — fall INSIDE the sky's own 500-cell distribution, "
         "below its median (percentiles 33 / 22 / 14).",
         ha="left", fontsize=10.2, color=INK)
fig.text(0.060, 0.908,
         "Resampling unit is the SWEEP (16 cells each): cells inside a sweep share the moment, the sky state, both radios and the arm.",
         ha="left", fontsize=9.5, color=INK2)

for ax in (ax_a, ax_b, ax_c):
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", alpha=1.0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

fig.text(0.060, 0.022,
         "Sky: 1,035 matched-arm synchronised sweeps from /mnt/qnap01/…/corpus/sync-*, joined, thresholded and solved by "
         "leo_tracker.radio.beacon.cross_radio\n(null_thresholds, cell_false_alarm, join_cells, observation_fires, "
         "solve_coincidence) — the estimator is the repository's, only the resampling unit is new.\n"
         "Injected: two cabled loopbacks, 500 cells per occupancy level. The three values the report quotes reproduce "
         "results_dry.json (selftest.py's SYNTHETIC scores) exactly, not runs.tar.gz.",
         ha="left", fontsize=8.0, color=MUTED, linespacing=1.5)

out = HERE / "nmatch_spread.png"
fig.savefig(out)
print("wrote", out)
