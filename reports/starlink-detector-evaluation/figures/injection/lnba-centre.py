#!/usr/bin/env python3
"""lnba-centre: lnb-a's recorded centre is wrong by 566 kHz, and the fix lands.

LEFT  -- each port's signed live window on the centre currently recorded in
         lnb-calibration.json: lnb-a +1,170.0 Hz, lnb-b 0, lnb-c +604,159.8 Hz,
         lnb-d 0.  Three ports sit on a window at -300..0 kHz; lnb-a sits half a
         megahertz away at +250..+550 kHz.
RIGHT -- the same plot with lnb-a moved onto its MEASURED centre, +567,402 Hz
         (corpus paired-instant rx0-rx1, 2,585 instants over 931 sweeps).  Its
         window lands on the other three.

Points are REFINED only -- no coarse proposer claimed them, so the offset is a
continuous estimate rather than a grid tooth.  Tooth points sit at 0, +/-116.67,
+/-233.33, +/-300, +/-350, +/-466.67 and +/-700 kHz and quantise the axis; the
report's own cliff analysis showed they inflate the bins.  All-point curves are
carried in the JSON sidecar.

Detection is differential-32 above the cross-edge-null threshold at a 1% false
alarm rate, per (sample rate, probe length), exactly as cross_radio.null_thresholds
computes it.

Inputs: ../analysis2.json  (analyse2.py, from the paired sync-* corpus,
2026-08-14T00:03Z..05:46Z, 3,194 entries / 505,297 scored points)

Run: nice -n 15 python3 lnba-centre.py
"""
from __future__ import annotations

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "lnba-centre"

# Reference categorical palette, light mode, slots 1-4 in fixed order.  This is
# the documented adjacent-pairlist configuration (worst adjacent CVD dE 9.1,
# normal-vision 19.6).  Slot 4 sits below 3:1 on the light surface, so the
# relief rule applies and every series is direct-labelled as well as legended;
# each also carries a distinct marker, so identity is never colour alone.
STYLE = {
    "lnb-a": {"color": "#2a78d6", "marker": "o", "z": 6},
    "lnb-b": {"color": "#eb6834", "marker": "s", "z": 4},
    "lnb-c": {"color": "#1baf7a", "marker": "^", "z": 3},
    "lnb-d": {"color": "#eda100", "marker": "D", "z": 2},
}
PORTS = ("lnb-a", "lnb-b", "lnb-c", "lnb-d")
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
SECOND = "#52514e"
MUTED = "#84837c"
HAIR = "#dedcd6"
MIN_N = 30
MEASURED = 567402.1
RECORDED = {"lnb-a": 1170.0, "lnb-b": 0.0, "lnb-c": 604159.8, "lnb-d": 0.0}


def series(rows):
    mid = np.array([(r["low_khz"] + r["high_khz"]) / 2 for r in rows])
    pct = np.array([np.nan if r["pct"] is None or r["n"] < MIN_N else r["pct"]
                    for r in rows])
    n = np.array([r["n"] for r in rows], float)
    return mid, pct, n


def main() -> None:
    data = json.load(open(os.path.join(os.path.dirname(HERE), "analysis2.json")))
    ports = data["m2_ports"]

    before = {p: ports[p]["rows_refined_corrected"] for p in PORTS}
    after = dict(before)
    after["lnb-a"] = data["m3"]["rows_refined"]
    centres_after = dict(RECORDED, **{"lnb-a": MEASURED})

    figure, axes = plt.subplots(
        2, 2, figsize=(10.4, 7.8), dpi=150, sharex=True,
        gridspec_kw={"height_ratios": [2.4, 1.0], "hspace": 0.14,
                     "wspace": 0.105, "left": 0.082, "right": 0.988,
                     "top": 0.760, "bottom": 0.140})
    figure.patch.set_facecolor(SURFACE)

    panels = (("BEFORE", "lnb-a on its recorded centre  +1,170 Hz",
               before, RECORDED),
              ("AFTER", "lnb-a on its measured centre  +567,402 Hz",
               after, centres_after))

    for column, (tag, heading, rows_by_port, centres) in enumerate(panels):
        top, bottom = axes[0][column], axes[1][column]
        for rail in (top, bottom):
            rail.set_facecolor(SURFACE)
            rail.grid(True, which="major", color=HAIR, lw=0.6, ls="-")
            rail.set_axisbelow(True)
            for side in ("top", "right"):
                rail.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                rail.spines[side].set_color(HAIR)
                rail.spines[side].set_linewidth(0.8)
            rail.tick_params(colors=SECOND, labelsize=10.5, length=3, width=0.8)
            # The window every correctly-centred port shares.
            rail.axvspan(-300, 0, color="#2a78d6", alpha=0.05, lw=0, zorder=0)
            rail.axvline(0, color=HAIR, lw=0.9, zorder=1)

        for port in PORTS:
            style = STYLE[port]
            mid, pct, n = series(rows_by_port[port])
            good = np.isfinite(pct)
            top.plot(mid[good], pct[good], color=style["color"], lw=2.0,
                     marker=style["marker"], ms=5.0, mew=0.9, mec=SURFACE,
                     zorder=style["z"], solid_capstyle="round")
            bottom.plot(mid, n, color=style["color"], lw=2.0,
                        marker=style["marker"], ms=4.0, mew=0.8, mec=SURFACE,
                        zorder=style["z"], solid_capstyle="round")

        # Selective direct label: only the port that moves.  The other three
        # share one peak and would collide; the legend and the per-series
        # markers carry their identity, so colour is never the only channel.
        mid, pct, _ = series(rows_by_port["lnb-a"])
        peak = int(np.nanargmax(pct))
        top.annotate("lnb-a", (mid[peak], pct[peak]),
                     textcoords="offset points", xytext=(0, 12),
                     ha="center", fontsize=11.5, weight="bold",
                     color=STYLE["lnb-a"]["color"], zorder=10)
        if column == 0:
            top.annotate("lnb-b / lnb-c / lnb-d", (-150, 71),
                         textcoords="offset points", xytext=(0, 13),
                         ha="center", fontsize=10.0, weight="bold",
                         color=SECOND, zorder=10)

        top.text(0.018, 0.955, tag, transform=top.transAxes, fontsize=12.0,
                 weight="bold", color=INK, ha="left", va="top")
        top.text(0.018, 0.893, heading, transform=top.transAxes, fontsize=9.8,
                 color=SECOND, ha="left", va="top")
        top.set_ylim(-3, 95)
        bottom.set_ylim(0, 8800)
        bottom.set_xlim(-840, 840)
        bottom.set_xticks(np.arange(-800, 801, 400))
        bottom.set_xticks(np.arange(-800, 801, 200), minor=True)
        bottom.set_xlabel("signed corrected offset:  cfo − receiver centre  (kHz)",
                          fontsize=11.0, color=SECOND, labelpad=6)
        if column == 0:
            top.set_ylabel("detection rate  (% of points)", fontsize=11.0,
                           color=SECOND, labelpad=6)
            bottom.set_ylabel("points per bin", fontsize=11.0,
                              color=SECOND, labelpad=6)
        else:
            top.tick_params(labelleft=False)
            bottom.tick_params(labelleft=False)

    axes[0][0].set_yticks(np.arange(0, 91, 20))
    for column in (0, 1):
        axes[1][column].set_yticks([0, 4000, 8000])

    handles = [Line2D([], [], color=STYLE[p]["color"], marker=STYLE[p]["marker"],
                      lw=2.0, ms=6.0, mec=SURFACE, mew=0.9,
                      label=f"{p}   centre {centres_after[p]/1e3:+.1f} kHz")
               for p in PORTS]
    figure.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.076, 0.816),
                  ncol=4, frameon=False, fontsize=10.2, handlelength=2.0,
                  columnspacing=1.4, handletextpad=0.5, labelcolor=INK)

    figure.text(0.076, 0.988,
                "lnb-a's recorded centre is wrong by 566 kHz.",
                fontsize=16.0, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.076, 0.945,
                "Corrected, its detection window lands on the other three ports.",
                fontsize=16.0, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.076, 0.900,
                "Live window (≥10% detection): lnb-a sits at +250…+550 kHz before, "
                "−300…0 kHz after — where every other port already is.",
                fontsize=10.4, color=SECOND, ha="left", va="top")
    figure.text(0.076, 0.868,
                "differential-32 above the cross-edge-null threshold at 1% false alarm.  "
                "Refined points only, no coarse-grid teeth.  3,194 paired entries, "
                "2026-08-14 UTC.",
                fontsize=9.2, color=MUTED, ha="left", va="top")
    figure.text(0.076, 0.052,
                "Measured centre +567,402 Hz: rx0−rx1 at 2,585 instants where both ports "
                "fired, over 931 sweeps.  95% CI ±150 Hz by sweep-clustered",
                fontsize=8.8, color=MUTED, ha="left", va="top")
    figure.text(0.076, 0.028,
                "bootstrap; ±4 kHz once method choice is included, calibrated on pluto-19f2 "
                "where the centre is known.  Top row omits bins with n < 30.",
                fontsize=8.8, color=MUTED, ha="left", va="top")

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150, facecolor=SURFACE)

    payload = {
        "figure": NAME,
        "finding": ("lnb-a's recorded receiver centre (+1,170.0 Hz) is wrong by "
                    "566,232 Hz; on the measured centre (+567,402 Hz) its signed "
                    "live window moves from +250..+550 kHz onto -300..0 kHz, "
                    "which is where lnb-b, lnb-c and lnb-d already sit."),
        "method": "differential-32",
        "threshold": "cross-edge-null, 1% false alarm, per (sample_rate_hz, probe_ms)",
        "points": "refined only (no coarse proposer claimed the point)",
        "bin_khz": 50,
        "min_bin_n": MIN_N,
        "measured_centre_hz": MEASURED,
        "recorded_centres_hz": RECORDED,
        "centres_after_hz": centres_after,
        "corpus_span_utc": data["corpus_span_utc"],
        "windows": {
            "before": {p: data["m2_ports"][p]["window_refined_corrected"]
                       for p in PORTS},
            "after": dict({p: data["m2_ports"][p]["window_refined_corrected"]
                           for p in PORTS}, **{"lnb-a": data["m3"]["window_refined"]}),
        },
        "curves_refined": {"before": before, "after": after},
        "curves_all_points": {
            "before": {p: data["m2_ports"][p]["rows_all_corrected"] for p in PORTS},
            "after": dict({p: data["m2_ports"][p]["rows_all_corrected"]
                           for p in PORTS}, **{"lnb-a": data["m3"]["rows_all"]}),
        },
        "measurement": data["m1b"],
        "residual_after_correction": data["m3_residual"],
    }
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=1)
    print("wrote", os.path.join(HERE, f"{NAME}.png"))


if __name__ == "__main__":
    main()
