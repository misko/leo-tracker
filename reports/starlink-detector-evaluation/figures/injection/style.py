"""Shared figure styling: validated categorical palette + secondary encoding.

Palette is the dataviz reference instance, unmodified and in its documented
order.  Re-validated here in Python (no node on this host): worst adjacent
normal-vision dE 19.6 (floor 15, pass), protan 10.6 / deutan 10.7 (target 8,
pass), tritan 6.8 -- inside the 6-8 warn band, which is legal only with
secondary encoding, so every series also carries its own marker shape and
dash pattern, and curves are direct-labelled.  Three slots sit under 3:1 on the
light surface, so the relief rule applies and direct labels are mandatory
rather than decorative.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e3e2de"

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*"]
DASHES = [(None, None), (5, 1.5), (1.5, 1.5), (7, 2, 1.5, 2),
          (3, 1.5), (None, None), (5, 1.5), (1.5, 1.5)]

#: The eight, in the order the report lists them, so colour follows the entity
#: everywhere and never its rank in the current chart.
METHODS = ["anchor-8", "differential-16", "differential-32", "glrt-32",
           "glrt-64", "full-frame-full", "full-frame-acquire",
           "full-frame-verify"]
STYLE = {name: {"color": SERIES[i], "marker": MARKERS[i], "dashes": DASHES[i]}
         for i, name in enumerate(METHODS)}

LOOPBACK = ("CABLED LOOPBACK — TX2 → SMA tee → 2×30 dB → RX1, RX2. "
            "Tests the detectors and the digital pipeline; NOT the LNBs, the antenna, or real sky.")


def apply() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "savefig.dpi": 150, "figure.dpi": 150,
        "font.size": 11, "axes.titlesize": 13, "axes.labelsize": 11,
        "axes.edgecolor": GRID, "axes.labelcolor": INK, "text.color": INK,
        "xtick.color": INK_2, "ytick.color": INK_2,
        "xtick.labelsize": 10, "ytick.labelsize": 10,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "legend.fontsize": 9,
        "lines.linewidth": 2.0, "lines.markersize": 5.5,
    })


def line(ax, x, y, name, **kw):
    s = STYLE[name]
    handle, = ax.plot(x, y, color=s["color"], marker=s["marker"],
                      label=name, **kw)
    if s["dashes"][0] is not None:
        handle.set_dashes(list(s["dashes"]))
    return handle


def footer(fig, extra: str = "") -> None:
    text = LOOPBACK + (("  " + extra) if extra else "")
    fig.text(0.5, 0.012, text, ha="center", va="bottom", fontsize=8.5,
             color=INK_2, wrap=True)
