"""Shared figure style: the dataviz reference palette, unchanged.

Copied from reports/starlink-detector-evaluation/figures/injection/figstyle.py so
these figures match the published ones.  Light surface only (PNGs on a light
page).  Categorical slots are used in fixed order and never cycled; no figure
here puts more than three categorical hues in play at once.  Where eight
algorithms appear they are ONE colour with emphasis, because the story is one
number, not eight identities.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt   # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

S1 = "#2a78d6"     # blue
S2 = "#eb6834"     # orange
S3 = "#1baf7a"     # aqua
CRITICAL = "#d03b3b"
GOOD = "#0ca30c"

#: A1/A3 run on two radios; A2 re-analyses the single-radio ladder.
CAVEAT_TWO = (
    "CABLED LOOPBACK on TWO INDEPENDENT RADIOS (separate oscillators, clocks and noise; no RF path between them),\n"
    "each TX2 -> splitter -> 2x30 dB -> RX1. Tests the DETECTORS and the digital pipeline - not LNBs, antennas or sky.\n"
    "Carrier offset is near zero by construction: inside each radio TX and RX share one reference.")
CAVEAT_ONE = (
    "CABLED LOOPBACK on ONE RADIO (TX2 -> splitter -> 2x30 dB -> RX1,RX2). Tests the DETECTORS and the digital\n"
    "pipeline - not LNBs, antennas or sky. Carrier offset is near zero by construction: TX and RX share one reference.")


def setup():
    plt.rcParams.update({
        "figure.dpi": 150, "savefig.dpi": 150,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans"],
        "font.size": 11,
        "axes.titlesize": 12.5, "axes.titleweight": "bold",
        "axes.titlecolor": INK, "axes.labelsize": 11, "axes.labelcolor": INK2,
        "axes.edgecolor": AXIS, "axes.linewidth": 0.8,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
        "xtick.labelsize": 10, "ytick.labelsize": 10,
        "grid.color": GRID, "grid.linewidth": 0.8,
        "legend.frameon": False, "legend.fontsize": 10,
        "lines.linewidth": 2.0, "lines.markersize": 8,
    })


def finish(fig, ax_list, caveat: str, caveat_y: float = 0.012, axis="y"):
    for ax in (ax_list if isinstance(ax_list, (list, tuple)) else [ax_list]):
        ax.set_axisbelow(True)
        ax.grid(True, axis=axis, alpha=1.0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.text(0.5, caveat_y, caveat, ha="center", va="bottom",
             fontsize=7.6, color=MUTED, linespacing=1.35)
