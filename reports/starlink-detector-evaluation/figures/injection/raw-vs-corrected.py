#!/usr/bin/env python3
"""raw-vs-corrected: the cliff is sharpest on the corrected axis, not manufactured by it.

THE HYPOTHESIS UNDER TEST.  The coarse banks are built with ``center_hz=0``
(``survey_scoring._banks`` -> ``fast_scan.build_bank``), so they search RAW
offset about each receiver's own LO: +/-300 kHz for the deployed bank A,
+/-700 kHz for the candidate bank E.  The cliff figure, however, plots
``abs(cfo - receiver_centers_hz[receiver])`` -- a BIAS-CORRECTED axis.  If
detections were confined to the deployed +/-300 kHz on the raw axis by
construction, then correcting each port by a different amount would smear that
one edge into an apparent cliff near 350-400 kHz.

WHAT THIS FIGURE SHOWS INSTEAD.
  * lnb-b and lnb-d carry ``receiver_centers_hz == 0.0`` EXACTLY, so for them
    the two columns are the same array.  They show the cliff at full depth.
    The hypothesis has no leverage on the ports that carry two thirds of the
    cliff bin.
  * lnb-c (+604,159.8 Hz) lands on top of lnb-b and lnb-d in the CORRECTED
    column and 250 kHz away from them in the RAW column.  Correcting is what
    makes the three agree.
  * lnb-a's recorded centre is +1,170.0 Hz, but its measured rx0-rx1 mismatch
    is +568,436 Hz (n=1,685, ``work/mismatch.py``, lnb_calibration's own
    estimator).  Its "corrected" axis is therefore a raw axis, which is why it
    sits half a megahertz to the right in BOTH columns.  That is a stale
    calibration, not evidence about the cliff.
  * Row 3 unfolds the abs().  The live region is ONE-SIDED: -300..0 kHz on the
    corrected axis for every correctly-calibrated port.  The "cliff at 350-400
    kHz" is the folded far edge of a ~300 kHz window centred near -150 kHz.

Inputs: work/cliff-corpus.npz (work/extract_cliff.py, frozen snapshot
ec5505611f5f1fdf, 2,292 paired entries / 363,004 scored points -- the same
census the report's cfo-cliff figure used).

Run:  PYTHONPATH=/home/satpi01/leo-tracker/src nice -n 15 python3 raw-vs-corrected.py
"""
from __future__ import annotations

import collections
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.survey_comparison import (             # noqa: E402
    DEFAULT_FALSE_ALARM_RATE, threshold_from)
from leo_tracker.radio.beacon import survey_scoring                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(os.path.dirname(HERE), "work")
CACHE = os.path.join(WORK, "cliff-corpus.npz")
NAME = "raw-vs-corrected"

METHOD = "differential-32"
RATES = (2.5e6, 5.0e6, 10.0e6)          # the three rates the cliff is reported at
STEP = 50.0                              # kHz, uniform bins
ABS_EDGES = np.arange(0.0, 900.0, STEP)
SIGNED_EDGES = np.arange(-800.0, 850.0, STEP)
MIN_N = 100
REPORT_CLIFF = (350.0, 400.0)

DEPLOYED_SPAN = survey_scoring.COARSE_CONFIGS["A"]["offset_span_hz"] / 1e3
CANDIDATE_SPAN = survey_scoring.COARSE_CONFIGS["E"]["offset_span_hz"] / 1e3

# dataviz reference palette, categorical slots 1-4, light mode.  Marker and
# dash carry the same identity, so the figure reads without colour; every line
# is also directly labelled, which is the relief rule for the low-contrast slots.
PORTS = ("lnb-b", "lnb-d", "lnb-c", "lnb-a")
STYLE = {
    "lnb-b": {"color": "#2a78d6", "marker": "o", "ls": "-"},
    "lnb-d": {"color": "#eb6834", "marker": "s", "ls": "--"},
    "lnb-c": {"color": "#1baf7a", "marker": "^", "ls": "-."},
    "lnb-a": {"color": "#eda100", "marker": "D", "ls": ":"},
}
INK, MUTED, FAINT, GRID = "#0b0b0b", "#52514e", "#8a8983", "#e2e1dd"
BAND = "#f0efec"
SURFACE = "#fcfcfb"


# ---------------------------------------------------------------- data ----
def prepare():
    data = np.load(CACHE)
    label, rate, probe = data["label"], data["rate"], data["probe_ms"]
    target, cfo, bias = data["is_target"], data["cfo_hz"], data["bias_hz"]
    score = data[f"score::{METHOD}"]

    # cross_radio.null_thresholds' population rule; the threshold itself is the
    # repository's survey_comparison.threshold_from, not a local quantile.
    pops = collections.defaultdict(list)
    null = (~target) & ~np.isnan(score)
    for key, value in zip(zip(rate[null].tolist(), probe[null].tolist()),
                          score[null].tolist()):
        pops[key].append(value)
    table = {k: threshold_from(v, false_alarm_rate=DEFAULT_FALSE_ALARM_RATE)
             for k, v in pops.items()}
    thresh = np.array([(table.get((r, p)) or {}).get("threshold", np.nan)
                       for r, p in zip(rate.tolist(), probe.tolist())], float)

    keep = target & ~np.isnan(cfo) & ~np.isnan(score) & np.isin(rate, RATES)
    return {
        "label": label[keep], "fired": (score > thresh)[keep],
        "raw_abs": np.abs(cfo[keep]) / 1e3,
        "cor_abs": np.abs(cfo[keep] - bias[keep]) / 1e3,
        "raw_signed": cfo[keep] / 1e3,
        "cor_signed": (cfo[keep] - bias[keep]) / 1e3,
        "centre": {port: float(np.median(bias[keep & (label == port)]))
                   for port in PORTS},
        "thresholds": {f"{k[0]}|{k[1]}": v for k, v in table.items()},
        "n_points": int(keep.sum()),
        "snapshot_digest": str(data["snapshot_digest"]),
        "entries": int(data["entries"]),
    }


def curve(state, axis_key, port, edges):
    axis, rows = state[axis_key], []
    at_port = state["label"] == port
    for low, high in zip(edges[:-1], edges[1:]):
        inside = at_port & (axis >= low) & (axis < high)
        n = int(inside.sum())
        rows.append({"low": float(low), "high": float(high), "mid": float((low + high) / 2),
                     "n": n, "fired": int(state["fired"][inside].sum()),
                     "pct": (100.0 * float(state["fired"][inside].sum()) / n)
                            if n else None,
                     "plotted": n >= MIN_N})
    return rows


# ---------------------------------------------------------------- draw ----
def draw_rate(ax, state, axis_key, edges, title, subtitle):
    ax.set_facecolor(SURFACE)
    if edges[0] >= 0:
        ax.axvspan(*REPORT_CLIFF, color=BAND, zorder=0)
        ax.text(np.mean(REPORT_CLIFF), 99, "report's\ncliff bin", ha="center",
                va="top", fontsize=7.0, color=MUTED, linespacing=1.15)
    for port in PORTS:
        rows = curve(state, axis_key, port, edges)
        x = [r["mid"] for r in rows if r["plotted"]]
        y = [r["pct"] for r in rows if r["plotted"]]
        style = STYLE[port]
        ax.plot(x, y, color=style["color"], marker=style["marker"], ls=style["ls"],
                lw=1.9, ms=4.4, mec=SURFACE, mew=0.7, label=port, zorder=3,
                clip_on=True)
        if x:
            peak = int(np.argmax(y))
            ax.annotate(port, (x[peak], y[peak]), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=8.0, weight="bold",
                        color=style["color"], zorder=5)
    ax.set_title(title, fontsize=10.0, weight="bold", color=INK, pad=12, loc="left")
    ax.text(0.0, 1.012, subtitle, transform=ax.transAxes, fontsize=7.6,
            color=MUTED, va="bottom")
    ax.set_ylim(-3, 104)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_ylabel("detection rate (%)", fontsize=8.4, color=MUTED)
    ax.grid(True, color=GRID, lw=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(FAINT)
    ax.tick_params(labelsize=8.0, colors=MUTED)


def note(ax, text, y=0.985):
    ax.text(0.982, y, text, transform=ax.transAxes, ha="right", va="top",
            fontsize=7.9, color=INK, linespacing=1.3, zorder=6,
            bbox=dict(boxstyle="round,pad=0.38", fc="#ffffff", ec=GRID, lw=0.9))


def mark_banks_raw(ax):
    """On the raw axis a bank edge is one number for every port."""
    for span, name, dash in ((DEPLOYED_SPAN, "deployed bank A  +/-300", (4, 2)),
                             (CANDIDATE_SPAN, "candidate bank E  +/-700", (1, 2))):
        ax.axvline(span, color=MUTED, lw=1.2, dashes=dash, zorder=2)
        ax.text(span - 8, 26, name, rotation=90, ha="right", va="bottom",
                fontsize=7.2, color=MUTED)


def mark_banks_corrected(ax, state):
    """On the corrected axis one bank edge becomes two per port, and they scatter."""
    for port in PORTS:
        centre = state["centre"][port] / 1e3
        for span in (DEPLOYED_SPAN, CANDIDATE_SPAN):
            for sign in (+1, -1):
                position = abs(sign * span - centre)
                if position > 880:
                    continue
                ax.plot([position, position], [-3, 6.0], color=STYLE[port]["color"],
                        lw=1.8, solid_capstyle="butt", zorder=4)
    ax.text(0.635, 0.085,
            "coloured ticks: where those SAME two bank edges land\n"
            "once each port is corrected -- they scatter",
            transform=ax.transAxes, ha="center", va="bottom", fontsize=7.2,
            color=MUTED, linespacing=1.25)


def draw_counts(ax, state, axis_key, edges, xlabel):
    ax.set_facecolor(SURFACE)
    width = STEP / (len(PORTS) + 1.2)
    for index, port in enumerate(PORTS):
        rows = curve(state, axis_key, port, edges)
        offset = (index - (len(PORTS) - 1) / 2) * width
        ax.bar([r["mid"] + offset for r in rows], [r["n"] for r in rows],
               width=width * 0.84, color=STYLE[port]["color"], label=port,
               linewidth=0.7, edgecolor=SURFACE, zorder=3)
    ax.axhline(MIN_N, color=MUTED, lw=1.0, dashes=(3, 2), zorder=4)
    ax.text(edges[0], MIN_N * 0.40, f"n={MIN_N} plotting floor", ha="left",
            va="bottom", fontsize=7.0, color=MUTED)
    ax.set_yscale("log")
    ax.set_ylim(25, 25000)
    ax.set_ylabel("n per bin", fontsize=8.4, color=MUTED)
    ax.set_xlabel(xlabel, fontsize=8.6, color=MUTED)
    ax.grid(True, axis="y", color=GRID, lw=0.7, zorder=0)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(FAINT)
    ax.tick_params(labelsize=8.0, colors=MUTED)


def main() -> int:
    state = prepare()
    figure = plt.figure(figsize=(9.9, 12.9), facecolor=SURFACE)
    grid = figure.add_gridspec(3, 2, height_ratios=[1.22, 0.60, 1.05],
                               hspace=0.46, wspace=0.21,
                               left=0.077, right=0.985, top=0.845, bottom=0.108)

    ax_raw = figure.add_subplot(grid[0, 0])
    ax_cor = figure.add_subplot(grid[0, 1], sharey=ax_raw)
    draw_rate(ax_raw, state, "raw_abs", ABS_EDGES,
              "RAW  |cfo|  -- what the banks search",
              "center_hz = 0, so this is offset about each port's own LO")
    draw_rate(ax_cor, state, "cor_abs", ABS_EDGES,
              "CORRECTED  |cfo - centre|  -- what the cliff plots",
              "lnb-b and lnb-d have centre 0.0 exactly: same array as the left")
    mark_banks_raw(ax_raw)
    mark_banks_corrected(ax_cor, state)
    note(ax_raw, "NO cliff at 350-400 here:\nlnb-c and lnb-a peak BEYOND it")
    note(ax_cor, "b, c, d fall together at 350-400.\nlnb-a does not: its centre is stale")

    draw_counts(figure.add_subplot(grid[1, 0]), state, "raw_abs", ABS_EDGES,
                "raw offset |cfo|  (kHz)")
    draw_counts(figure.add_subplot(grid[1, 1]), state, "cor_abs", ABS_EDGES,
                "corrected offset |cfo - centre|  (kHz)")

    ax_sraw = figure.add_subplot(grid[2, 0])
    ax_scor = figure.add_subplot(grid[2, 1], sharey=ax_sraw)
    draw_rate(ax_sraw, state, "raw_signed", SIGNED_EDGES,
              "SIGNED raw offset", "the same points, abs() removed")
    draw_rate(ax_scor, state, "cor_signed", SIGNED_EDGES,
              "SIGNED corrected  -- what the abs axis hides",
              "the live region is ONE-SIDED, not a symmetric +/-350 kHz tolerance")
    for ax in (ax_sraw, ax_scor):
        ax.set_xlabel("signed offset (kHz)", fontsize=8.6, color=MUTED)
        ax.axvline(0.0, color=FAINT, lw=1.0)
        ax.set_xticks([-800, -400, 0, 400, 800])
    ax_scor.axvspan(-300, 0, color=BAND, zorder=0)
    ax_scor.text(-780, 101, "live window\nb, c, d\n-300 .. 0 kHz", ha="left",
                 va="top", fontsize=7.6, color=MUTED, linespacing=1.25)
    ax_scor.annotate("", xy=(385, 24), xytext=(-110, 24),
                     arrowprops=dict(arrowstyle="->", color=STYLE["lnb-a"]["color"],
                                     lw=1.6, shrinkA=2, shrinkB=2))
    ax_scor.text(140, 27, "+534 kHz: lnb-a's stale centre", ha="center",
                 fontsize=7.4, color=STYLE["lnb-a"]["color"], weight="bold")

    handles = [Line2D([], [], color=STYLE[p]["color"], marker=STYLE[p]["marker"],
                      ls=STYLE[p]["ls"], lw=1.9, ms=5.0,
                      label=f"{p}  {state['centre'][p]/1e3:+.1f} kHz")
               for p in PORTS]
    figure.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.068, 0.914),
                  ncol=4, frameon=False, fontsize=8.6, handlelength=2.6,
                  columnspacing=1.6)

    figure.text(0.077, 0.990,
                "The sky cliff is NOT made by the bias-corrected axis.",
                fontsize=14.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.077, 0.9705,
                "It survives at full depth on the two ports whose bias is exactly zero",
                fontsize=14.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.077, 0.9495,
                "differential-32, cross-edge-null threshold at 1% false alarm, target "
                "arm, 2.5 / 5.0 / 10.0 MS/s.",
                fontsize=8.6, color=MUTED, ha="left", va="top")
    figure.text(0.077, 0.9355,
                f"{state['n_points']:,} scored points from {state['entries']:,} paired "
                f"entries; frozen snapshot {state['snapshot_digest']}.",
                fontsize=8.6, color=MUTED, ha="left", va="top")
    figure.text(0.077, 0.012,
                "Bank spans come from survey_scoring.COARSE_CONFIGS.  Every sidecar in "
                "this corpus records candidate_coarse = \"E\", so the confinement is the\n"
                "+/-700 kHz bank, not the +/-300 kHz one: 28.6% of the points plotted here "
                "sit beyond 350 kHz RAW (38.5% corpus-wide) and points reach 813.5 kHz.\n"
                "lnb-a is plotted, "
                "not excluded --\nits recorded centre is +1,170.0 Hz while its measured "
                "rx0-rx1 mismatch is +568,436 Hz (n=1,685), so its correction is stale "
                "by 567 kHz.",
                fontsize=7.8, color=MUTED, ha="left", va="bottom", linespacing=1.45)

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150,
                   facecolor=SURFACE)

    payload = {
        "figure": NAME,
        "method": METHOD, "rates_hz": list(RATES),
        "false_alarm_rate": DEFAULT_FALSE_ALARM_RATE,
        "bin_step_khz": STEP, "min_bin_n": MIN_N,
        "deployed_span_khz": DEPLOYED_SPAN, "candidate_span_khz": CANDIDATE_SPAN,
        "coarse_configs": {k: {"shape": list(v["shape"]),
                               "offset_span_hz": v["offset_span_hz"]}
                           for k, v in survey_scoring.COARSE_CONFIGS.items()},
        "candidate_coarse": survey_scoring.CANDIDATE_COARSE,
        "receiver_centers_hz": state["centre"],
        "snapshot_digest": state["snapshot_digest"],
        "paired_entries": state["entries"], "scored_points_plotted": state["n_points"],
        "thresholds": state["thresholds"],
        "curves": {},
    }
    for axis_key, edges in (("raw_abs", ABS_EDGES), ("cor_abs", ABS_EDGES),
                            ("raw_signed", SIGNED_EDGES),
                            ("cor_signed", SIGNED_EDGES)):
        payload["curves"][axis_key] = {port: curve(state, axis_key, port, edges)
                                       for port in PORTS}
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=1)
    print(f"wrote {NAME}.png and {NAME}.json in {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
