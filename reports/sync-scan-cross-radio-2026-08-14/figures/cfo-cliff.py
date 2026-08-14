#!/usr/bin/env python3
"""cfo-cliff: the detection cliff does not move with the pilot guard band.

Detection rate (differential-32, cross-edge-null threshold at 1% false alarm)
against the BIAS-CORRECTED frequency offset, one line per sample rate.  The
pilot guard is +312.5 / +1562.5 / +4062.5 kHz at 2.5 / 5.0 / 10.0 MS/s -- a 13x
range -- and the collapse sits in the same 350-400 kHz bin at all three.

Every number is computed from the frozen snapshot of
/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/ (see snapshot.py).  Nothing
here is hand-entered except REFERENCE_*, which are the reviewers' previously
reported values, plotted so any disagreement is visible.

Definitions are lifted from src/leo_tracker/radio/beacon/cross_radio.py:
  * corrected offset = abs(cfo_hz - receiver_centers_hz[receiver])
    -- exactly the quantity guard_band_curve reports as median_corrected_hz.
  * threshold        = null_thresholds(): the 99th-percentile order statistic
    of the cross-edge-null population for that (method, rate, probe_ms).
  * lnb-a is dead and is out of both populations (DEAD_RECEIVERS).
NOTE the pipeline's own guard_band_curve bins by RAW offset and carries
median_corrected_hz only as a descriptive statistic inside a raw bin; binning BY
the corrected offset, which is what this figure needs, is done here.

TWO DEFECTS THE ADVERSARIAL REVIEW FOUND IN THE PREVIOUS VERSION, both fixed
here rather than carried forward:

 1. PORT COMPOSITION.  At the 2,464-cell snapshot the 2.5 MS/s cliff bin was
    86% lnb-c (103 of 120 points), so the crux datum leaned on the one port
    carrying a +604 kHz LO bias.  Rows 2 and 3 of this figure now disaggregate
    every bin by port: row 2 is the per-port detection rate, row 3 is the
    stacked per-port count.  At the full corpus the 2.5 MS/s cliff bin is
    *still* one-port (552 of 616 = 90% lnb-c, worse than before), but 5.0 and
    10 MS/s now carry all three ports through the cliff at n in the hundreds
    each, and all three fall together.  Both facts are on the figure.

 2. RAW-AXIS GUARDS ON A CORRECTED AXIS.  The guard bounds |cfo| inside the
    captured band; it is not a bound on |cfo - LO centre|, which is what the
    x axis plots.  Drawing it as a full-height vertical invited reading it as a
    threshold on the plotted quantity.  The guards are now short axis-anchored
    ticks in neutral ink, labelled RAW-AXIS, and an inset shows what the raw
    axis actually does at 2.5 MS/s: detection RISES through the guard.

Usage:
    nice -n 15 python3 snapshot.py      # freeze the corpus census
    nice -n 15 python3 extract_lite.py  # verbatim compact mirror
    nice -n 15 python3 extract.py       # -> cfo-port-corpus.npz
    nice -n 15 python3 cfo-cliff.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("REFRESH_WORK", os.path.join(os.path.dirname(HERE), "work"))
CACHE = os.path.join(WORK, "cfo-port-corpus.npz")
SNAPSHOT = os.path.join(WORK, "snapshot.json")
NAME = "cfo-cliff"

METHOD = "differential-32"
FALSE_ALARM_RATE = 0.01
DEAD_RECEIVERS = ("lnb-a",)
PILOT_BAND_HZ = 1_875_000.0
MIN_BIN_N = 20                      # thinner than this is not plotted at all

#: Bin edges in kHz.  The first nine bins (0..500) are the reviewers' bins, kept
#: exactly so their numbers are directly comparable; the rest extend the axis
#: out to the largest offset the corpus contains, so the reader can see that the
#: rate is already on the floor long before the 5.0 and 10 MS/s guards.
EDGES_KHZ = np.array([0, 50, 100, 150, 200, 250, 300, 350, 400,
                      500, 600, 700, 850, 1000, 1450], float)
N_REFERENCE_BINS = 9
CLIFF_BIN = 7                       # 350-400 kHz
BEFORE_BIN = 6                      # 300-350 kHz

#: What the reviewers reported with differential-32.  Hand-entered ON PURPOSE
#: and ONLY to be plotted against the corpus, never to stand in for it.
REFERENCE_RATE = {
    2.5e6: [10.6, 25.3, 36.4, 45.3, 39.0, 40.0, 26.7, 0.0, 1.2],
    5.0e6: [17.0, 33.5, 43.6, 55.9, 31.0, 19.7, 12.1, 2.0, 1.9],
    10.0e6: [24.1, 49.0, 50.6, 60.4, 38.9, 28.6, 10.8, 1.6, 1.8],
}
REFERENCE_N = {5.0e6: [481, 355, 937, 422, 554, 239, 605, 247, 309]}
#: The port composition the review measured in the 2.5 MS/s cliff bin.
REFERENCE_CLIFF_COMPOSITION = {"rate_hz": 2.5e6, "bin_khz": [350, 400],
                               "n": 120, "lnb_c": 103, "lnb_c_pct": 85.8}

RATES = [2.5e6, 5.0e6, 10.0e6]
PORTS = ["lnb-b", "lnb-c", "lnb-d"]
# dataviz reference palette, categorical slots 1-3: the documented all-pairs
# safe subset.  Marker and linestyle carry the same identity, so the figure
# reads without colour.
STYLE = {
    2.5e6: {"color": "#2a78d6", "marker": "o", "ls": "-", "label": "2.5 MS/s"},
    5.0e6: {"color": "#eb6834", "marker": "s", "ls": "--", "label": "5.0 MS/s"},
    10.0e6: {"color": "#1baf7a", "marker": "^", "ls": "-.", "label": "10.0 MS/s"},
}
PORT_STYLE = {
    "lnb-b": {"color": "#2a78d6", "marker": "o", "ls": "-"},
    "lnb-c": {"color": "#eb6834", "marker": "s", "ls": "--"},
    "lnb-d": {"color": "#1baf7a", "marker": "^", "ls": "-."},
}

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
CLIFF_INK, CLIFF_FILL = "#4a3aa7", "#efeef8"


def pilot_guard_khz(rate: float) -> float:
    return (float(rate) / 2.0 - PILOT_BAND_HZ / 2.0) / 1e3


def _at(ordered: np.ndarray, fraction: float) -> float:
    """survey_comparison._at: a plain order statistic, no interpolation."""
    if ordered.size == 0:
        return float("nan")
    return float(ordered[min(ordered.size - 1,
                             max(0, int(round(fraction * (ordered.size - 1)))))])


def load():
    if not os.path.isfile(CACHE):
        subprocess.run([sys.executable, os.path.join(HERE, "extract.py")], check=True)
    return np.load(CACHE)


def census() -> dict:
    with open(SNAPSHOT) as handle:
        frozen = json.load(handle)
    return {key: frozen[key] for key in
            ("measured_utc", "sweeps_on_share", "corpus_entries",
             "scored_sidecars", "scored_digest")}


def prepare(data):
    label, rate = data["label"], data["rate"]
    probe, target = data["probe_ms"], data["is_target"]
    cfo, bias, score = data["cfo_hz"], data["bias_hz"], data["score"]
    live = ~np.isin(label, DEAD_RECEIVERS)

    # null_thresholds(): cross-edge null, live ports only, per (rate, probe_ms)
    thresholds = {}
    for key in sorted(set(zip(rate.tolist(), probe.tolist()))):
        null = (rate == key[0]) & (probe == key[1]) & (~target) & live & ~np.isnan(score)
        thresholds[key] = _at(np.sort(score[null]), 1.0 - FALSE_ALARM_RATE)
    threshold = np.array([thresholds[(r, p)] for r, p in zip(rate, probe)])

    keep = target & live & ~np.isnan(cfo) & ~np.isnan(score)
    return {"rate": rate[keep],
            "label": label[keep],
            "corrected_khz": np.abs(cfo[keep] - bias[keep]) / 1e3,
            "raw_khz": np.abs(cfo[keep]) / 1e3,
            "fired": (score[keep] > threshold[keep]),
            "thresholds": thresholds}


def curves(prepared, axis_key="corrected_khz", port=None):
    """Detection rate per bin per rate, optionally restricted to one port."""
    out = {}
    x = prepared[axis_key]
    for value in RATES:
        rows = []
        at_rate = prepared["rate"] == value
        if port is not None:
            at_rate = at_rate & (prepared["label"] == port)
        for low, high in zip(EDGES_KHZ[:-1], EDGES_KHZ[1:]):
            inside = at_rate & (x >= low) & (x < high)
            count = int(inside.sum())
            hits = int(prepared["fired"][inside].sum())
            rows.append({"low_khz": low, "high_khz": high, "mid_khz": (low + high) / 2,
                         "n": count, "fired": hits,
                         "rate_pct": (100.0 * hits / count) if count else None,
                         "plotted": count >= MIN_BIN_N})
        out[value] = rows
    return out


def composition(prepared) -> dict:
    """Who is in each bin.  This is the defect the review named; it is now data."""
    out = {}
    for value in RATES:
        per_bin = []
        for index, (low, high) in enumerate(zip(EDGES_KHZ[:-1], EDGES_KHZ[1:])):
            inside = ((prepared["rate"] == value)
                      & (prepared["corrected_khz"] >= low)
                      & (prepared["corrected_khz"] < high))
            total = int(inside.sum())
            row = {"bin": index, "low_khz": low, "high_khz": high, "n": total,
                   "ports": {}}
            for port in PORTS:
                mask = inside & (prepared["label"] == port)
                count = int(mask.sum())
                row["ports"][port] = {
                    "n": count,
                    "share_pct": (100.0 * count / total) if total else None,
                    "rate_pct": (100.0 * float(prepared["fired"][mask].sum()) / count)
                                if count else None}
            largest = max(row["ports"].items(),
                          key=lambda item: item[1]["n"]) if total else (None, None)
            row["dominant_port"] = largest[0]
            row["dominant_share_pct"] = (largest[1]["share_pct"]
                                         if largest[1] else None)
            row["single_port_bin"] = bool(total and largest[1]["share_pct"] >= 80.0)
            per_bin.append(row)
        out[value] = per_bin
    return out


def cliff_by_probe(data, prepared) -> dict:
    """The cliff, disaggregated to every (rate, probe length) cell.

    Pooling probe lengths inside one rate is not safe -- the 0-50 kHz bin reads
    very differently at 80 ms and at 640 ms -- so the headline has to survive
    the disaggregation to mean anything.
    """
    label, rate = data["label"], data["rate"]
    probe, target = data["probe_ms"], data["is_target"]
    cfo, bias, score = data["cfo_hz"], data["bias_hz"], data["score"]
    live = ~np.isin(label, DEAD_RECEIVERS)
    threshold = np.array([prepared["thresholds"][(r, p)]
                          for r, p in zip(rate, probe)])
    keep = target & live & ~np.isnan(cfo) & ~np.isnan(score)
    corrected = np.abs(cfo - bias) / 1e3
    fired = score > threshold
    out = {}
    for value in RATES:
        for length in sorted(set(probe[rate == value].tolist())):
            cell = keep & (rate == value) & (probe == length)
            row = {}
            for name, (low, high) in (("before_300_350", (300, 350)),
                                      ("after_350_400", (350, 400))):
                inside = cell & (corrected >= low) & (corrected < high)
                count = int(inside.sum())
                row[name] = {"n": count,
                             "rate_pct": (100.0 * int(fired[inside].sum()) / count)
                                         if count else None}
            before = row["before_300_350"]["rate_pct"]
            after = row["after_350_400"]["rate_pct"]
            row["collapses"] = (before is not None and after is not None
                                and after < before / 3.0)
            out[f"{value / 1e6:g} MS/s / {length:g} ms"] = row
    return out


def raw_axis_through_the_guard(prepared) -> dict:
    """What the guard verticals looked like on the axis they belong to.

    The review's second finding.  On the RAW axis at 2.5 MS/s, detection does
    not fall through the +312.5 kHz guard, it rises into it -- so a guard drawn
    on the corrected axis was not merely misplaced, it pointed the wrong way.
    """
    out = {}
    for value in RATES:
        guard = pilot_guard_khz(value)
        at_rate = prepared["rate"] == value
        rows = []
        for low, high in zip(EDGES_KHZ[:-1], EDGES_KHZ[1:]):
            inside = at_rate & (prepared["raw_khz"] >= low) & (prepared["raw_khz"] < high)
            count = int(inside.sum())
            rows.append({"low_khz": low, "high_khz": high, "n": count,
                         "rate_pct": (100.0 * float(prepared["fired"][inside].sum())
                                      / count) if count else None,
                         "plotted": count >= MIN_BIN_N})
        below = [row for row in rows if row["high_khz"] <= guard and row["plotted"]]
        above = [row for row in rows if row["low_khz"] >= guard and row["plotted"]]
        out[f"{value / 1e6:g} MS/s"] = {
            "guard_khz": guard, "bins": rows,
            "last_bin_below_guard_pct": below[-1]["rate_pct"] if below else None,
            "first_bin_above_guard_pct": above[0]["rate_pct"] if above else None,
            "guard_inside_the_data": bool(below and above),
            "verdict": ("rises through its guard"
                        if below and above
                        and above[0]["rate_pct"] > below[-1]["rate_pct"]
                        else "falls through its guard" if below and above
                        else "guard is beyond the furthest candidate in the "
                             "corpus, so no bin sits above it"),
            "rises_through_its_guard": bool(
                below and above and above[0]["rate_pct"] > below[-1]["rate_pct"]),
        }
    return out


# --------------------------------------------------------------------------
# figure
# --------------------------------------------------------------------------

def collapse_bin(rows: list[dict], floor_fraction: float = 8.0) -> int | None:
    """The first bin after which detection never recovers.

    "The cliff" is neither the lowest bin nor the steepest fractional drop.
    Past the cliff every bin is on the floor, so the lowest of them is picked
    by rounding noise (2.5 MS/s reads 0.0% at 500-600 and 1.3% at 400-500), and
    a fractional drop of 1.3% -> 0.0% scores 100% while the real collapse of
    35.4% -> 2.6% scores 93%.  Both answered the wrong question in earlier
    drafts of this check.

    What the figure claims is a *sustained* collapse, so that is what is
    tested: the first bin from which the rate stays below an eighth of that
    rate's own peak for every remaining plotted bin.
    """
    plotted = [(index, row) for index, row in enumerate(rows) if row["plotted"]]
    if not plotted:
        return None
    peak = max(row["rate_pct"] for _, row in plotted)
    floor = peak / floor_fraction
    for position, (index, row) in enumerate(plotted):
        if all(later["rate_pct"] <= floor for _, later in plotted[position:]):
            return index
    return None


def bin_labels() -> list[str]:
    return [f"{int(low)}\u2013{int(high)}"
            for low, high in zip(EDGES_KHZ[:-1], EDGES_KHZ[1:])]


def main() -> None:
    data = load()
    prepared = prepare(data)
    table = curves(prepared)
    by_port = {port: curves(prepared, port=port) for port in PORTS}
    comp = composition(prepared)
    raw = raw_axis_through_the_guard(prepared)
    frozen = census()
    reach = {value: float(prepared["corrected_khz"][prepared["rate"] == value].max())
             for value in RATES}
    furthest = max(reach.values())
    counts = data["census_keys"], data["census_vals"]
    pairs_joined = int(dict(zip(counts[0].tolist(),
                                counts[1].tolist()))["pairs_joined"])
    plotted_points = int(prepared["rate"].size)

    plt.rcParams.update({"font.size": 12, "axes.titlesize": 15,
                         "axes.labelsize": 13, "xtick.labelsize": 12,
                         "ytick.labelsize": 12, "legend.fontsize": 11,
                         "axes.grid": True, "grid.alpha": 0.25,
                         "grid.linewidth": 0.6, "axes.edgecolor": "#8a8a86",
                         "figure.facecolor": "white", "axes.facecolor": "white"})

    figure = plt.figure(figsize=(13.6, 17.2))
    outer = figure.add_gridspec(3, 1, height_ratios=[2.72, 1.20, 1.14],
                                hspace=0.30, left=0.079, right=0.988,
                                top=0.922, bottom=0.163)
    head = outer[0].subgridspec(3, 1, height_ratios=[2.55, 0.98, 0.30],
                               hspace=0.09)
    top = figure.add_subplot(head[0])
    bottom = figure.add_subplot(head[1], sharex=top)
    rail = figure.add_subplot(head[2], sharex=top)
    mid_row = outer[1].subgridspec(1, 3, wspace=0.10)
    low_row = outer[2].subgridspec(1, 3, wspace=0.10)
    port_axes = [figure.add_subplot(mid_row[i]) for i in range(3)]
    stack_axes = [figure.add_subplot(low_row[i]) for i in range(3)]

    # ---- the cliff, marked before anything is drawn over it ---------------
    for axes in (top, bottom):
        axes.axvspan(350, 400, color=CLIFF_INK, alpha=0.20, zorder=0, lw=0)
        axes.axvline(350, color=CLIFF_INK, lw=1.0, alpha=0.55, zorder=1)
        axes.axvline(400, color=CLIFF_INK, lw=1.0, alpha=0.55, zorder=1)

    for value in RATES:
        style, rows = STYLE[value], table[value]
        x = np.array([row["mid_khz"] for row in rows])
        y = np.array([row["rate_pct"] if row["plotted"] else np.nan for row in rows])
        n = np.array([row["n"] if row["plotted"] else np.nan for row in rows])
        top.plot(x, y, color=style["color"], marker=style["marker"],
                 linestyle=style["ls"], linewidth=2.0, markersize=8,
                 markeredgecolor="white", markeredgewidth=1.0,
                 label=style["label"], zorder=4)
        bottom.plot(x, n, color=style["color"], marker=style["marker"],
                    linestyle=style["ls"], linewidth=1.7, markersize=6.5,
                    markeredgecolor="white", markeredgewidth=0.8, zorder=4)

    # ---- FIX 2: the guards, demoted to RAW-AXIS ticks in the count panel ---
    # They are not a bound on the quantity the detection panel plots.  A
    # full-height vertical through the curves read as one, so they are now
    # short ticks stood on the floor of the panel below, in neutral ink and
    # labelled RAW-AXIS, where they can be located but not misread.
    for value in RATES:
        guard = pilot_guard_khz(value)
        rail.plot([guard, guard], [0.06, 0.94], color=MUTED, lw=2.0,
                  solid_capstyle="butt", zorder=6)

    # the 13x spread between the outermost guards, on the ticks themselves
    rail.annotate("", xy=(312.5, 0.50), xytext=(4062.5, 0.50),
                  arrowprops=dict(arrowstyle="<|-|>", color=MUTED, lw=1.5),
                  zorder=6)
    rail.text(1127, 0.62, "13\u00d7 apart", ha="center", va="bottom",
              fontsize=10.0, color=MUTED, zorder=6)
    rail.text(20, 0.50,
              "PILOT GUARD, RAW axis \u2014 read off the ticks below, "
              "not off the panels above",
              ha="left", va="center", fontsize=10.0, color=MUTED,
              style="italic", zorder=6)
    rail.set_ylim(0, 1.0)
    rail.set_yticks([])
    rail.grid(False)
    rail.set_facecolor("#f6f6f4")
    for side in ("top", "right", "left"):
        rail.spines[side].set_visible(False)

    # reviewers' reported values, so a disagreement is on the figure
    for value in RATES:
        mids = [(EDGES_KHZ[i] + EDGES_KHZ[i + 1]) / 2 for i in range(N_REFERENCE_BINS)]
        top.plot(mids, REFERENCE_RATE[value], linestyle="none", marker="x",
                 markersize=7, markeredgewidth=1.6, color="#52514e",
                 alpha=0.85, zorder=5)

    # ---- the load-bearing annotation --------------------------------------
    drop = "\n".join(
        f"  {STYLE[v]['label']:>9s}  {table[v][BEFORE_BIN]['rate_pct']:4.1f}%"
        f" -> {table[v][CLIFF_BIN]['rate_pct']:.1f}%" for v in RATES)
    top.annotate("THE CLIFF\nevery rate collapses in the\nsame 350-400 kHz bin:\n"
                 f"{drop}",
                 xy=(392, 5.0), xytext=(430, 54.0), fontsize=11,
                 color="#241d52", ha="left", va="center", linespacing=1.45,
                 arrowprops=dict(arrowstyle="-|>", color=CLIFF_INK, lw=2.2,
                                 shrinkA=8, shrinkB=3,
                                 connectionstyle="arc3,rad=0.25"),
                 bbox=dict(boxstyle="round,pad=0.45", fc=CLIFF_FILL,
                           ec=CLIFF_INK, lw=1.6, alpha=0.97), zorder=7)

    # where the corpus actually stops
    for axes in (top, bottom):
        axes.axvspan(furthest, 6400, color="#0b0b0b", alpha=0.07, lw=0, zorder=1)
    top.text(6100, 86, f"no candidate anywhere in the corpus exceeds "
                       f"{furthest / 1e3:.2f} MHz,\n"
                       f"so the 5.0 and 10 MS/s guards are never reached\n"
                       f"and leave no feature at all",
             ha="right", va="top", fontsize=10.5, color=MUTED, style="italic",
             zorder=6)

    top.set_xscale("log")
    top.set_xlim(18, 6400)
    top.set_ylim(0, 88)
    top.set_yticks([0, 10, 20, 30, 40, 50, 60, 70])
    top.set_ylabel("detection rate, differential-32 (%)")
    top.set_title("The detection cliff stays at 350\u2013400 kHz while the pilot "
                  "guard moves 13\u00d7\n"
                  "so the guard band does not set it \u2014 and what does is "
                  "still unidentified",
                  fontweight="bold", pad=12)
    handles = [Line2D([], [], color=STYLE[v]["color"], marker=STYLE[v]["marker"],
                      linestyle=STYLE[v]["ls"], linewidth=2.0, markersize=8,
                      label=f"{STYLE[v]['label']}  (raw-axis guard "
                            f"{pilot_guard_khz(v):,.1f} kHz)")
               for v in RATES]
    handles.append(Line2D([], [], color="#52514e", marker="x", linestyle="none",
                          markersize=7, markeredgewidth=1.6,
                          label="previously reported values (320 scored sidecars)"))
    top.legend(handles=handles, loc="upper left", framealpha=0.95, ncol=1,
               borderpad=0.6)

    # ---- FIX 2, second half: the raw axis, in an inset --------------------
    inset = top.inset_axes([0.665, 0.115, 0.300, 0.345])
    guard25 = pilot_guard_khz(2.5e6)
    raw25 = raw["2.5 MS/s"]["bins"]
    xs = [row["low_khz"] + (row["high_khz"] - row["low_khz"]) / 2 for row in raw25]
    ys = [row["rate_pct"] if row["plotted"] else np.nan for row in raw25]
    inset.axvline(guard25, color="#2a78d6", lw=2.0, ls="--", zorder=2)
    inset.plot(xs, ys, color="#2a78d6", marker="o", ms=5.0, lw=1.8,
               markeredgecolor="white", markeredgewidth=0.7, zorder=3)
    inset.set_xlim(0, 620)
    inset.set_ylim(0, 62)
    inset.set_title("the RAW axis at 2.5 MS/s:\ndetection RISES through the guard",
                    fontsize=9.5, color=INK, pad=4)
    inset.set_xlabel("raw |cfo| (kHz)", fontsize=8.5, labelpad=1)
    inset.set_ylabel("det. %", fontsize=8.5, labelpad=1)
    inset.tick_params(labelsize=8)
    inset.text(guard25 + 12, 5, "guard\n312.5", fontsize=8, color="#2a78d6",
               ha="left", va="bottom", fontweight="bold")
    inset.grid(alpha=0.22, lw=0.5)
    for side in ("top", "right"):
        inset.spines[side].set_visible(False)
    inset.set_facecolor("#fbfbfa")

    bottom.set_yscale("log")
    bottom.set_ylim(60, 26000)
    bottom.set_ylabel("candidate points\nper bin (n)")
    rail.set_xlabel("bias-corrected frequency offset  |cfo \u2212 receiver centre|"
                    "  (kHz, log scale)")
    bottom.axhline(150, color=MUTED, linestyle=":", linewidth=1.3)
    bottom.text(19, 143, "n = 150: below this line, read the bin with care",
                fontsize=10.0, color=MUTED, va="top")

    ticks = [20, 50, 100, 200, 312.5, 500, 1000, 1562.5, 4062.5]
    for axes in (top, bottom, rail):
        axes.set_xticks(ticks)
        axes.set_xticklabels([f"{v:,.1f}".rstrip("0").rstrip(".") for v in ticks])
        axes.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
        axes.tick_params(axis="x", which="minor", length=2)
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
    plt.setp(top.get_xticklabels(), visible=False)
    plt.setp(bottom.get_xticklabels(), visible=False)

    # ---- FIX 1: per-port detection rate, one panel per rate ---------------
    labels = bin_labels()
    index = np.arange(len(labels))
    show = list(range(len(labels)))
    for column, value in enumerate(RATES):
        axes = port_axes[column]
        axes.axvspan(CLIFF_BIN - 0.5, CLIFF_BIN + 0.5, color=CLIFF_INK,
                     alpha=0.18, lw=0, zorder=0)
        for port in PORTS:
            rows = by_port[port][value]
            y = [row["rate_pct"] if row["plotted"] else np.nan for row in rows]
            style = PORT_STYLE[port]
            axes.plot(index, y, color=style["color"], marker=style["marker"],
                      linestyle=style["ls"], linewidth=1.8, markersize=6.0,
                      markeredgecolor="white", markeredgewidth=0.8,
                      label=port, zorder=3)
        axes.set_xticks(show)
        axes.set_xticklabels([labels[i] for i in show], rotation=68, ha="right",
                             fontsize=7.6)
        axes.set_ylim(0, 105)
        axes.set_xlim(-0.6, len(labels) - 0.4)
        axes.set_title(f"{STYLE[value]['label']} \u2014 per port",
                       fontsize=12, color=INK, pad=6)
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
        if column == 0:
            axes.set_ylabel("detection rate\nper port (%)", fontsize=11)
            axes.legend(loc="upper right", fontsize=9, framealpha=0.95,
                        borderpad=0.4, handlelength=2.0)
        else:
            axes.tick_params(labelleft=False)
        cliff = comp[value][CLIFF_BIN]
        note = ("ONE-PORT BIN\n%s is %.0f%% of it\n(%d of %d points)"
                % (cliff["dominant_port"], cliff["dominant_share_pct"],
                   cliff["ports"][cliff["dominant_port"]]["n"], cliff["n"])
                ) if cliff["single_port_bin"] else (
                "all three ports present\n%s\nand all three fall"
                % " / ".join("%s n=%d" % (p, cliff["ports"][p]["n"])
                             for p in PORTS))
        axes.text(0.50, 0.965, note, transform=axes.transAxes, fontsize=8.8,
                  ha="center", va="top", linespacing=1.35,
                  color="#8a1c1c" if cliff["single_port_bin"] else "#12603f",
                  bbox=dict(boxstyle="round,pad=0.32", fc="#fdeeee"
                            if cliff["single_port_bin"] else "#e9f7f0",
                            ec="#8a1c1c" if cliff["single_port_bin"]
                            else "#12603f", lw=1.1, alpha=0.95), zorder=6)

    # ---- FIX 1, second half: stacked per-port counts ----------------------
    stack_top = max(comp[v][i]["n"] for v in RATES for i in range(len(labels)))
    for column, value in enumerate(RATES):
        axes = stack_axes[column]
        axes.axvspan(CLIFF_BIN - 0.5, CLIFF_BIN + 0.5, color=CLIFF_INK,
                     alpha=0.18, lw=0, zorder=0)
        floor = np.zeros(len(labels))
        for port in PORTS:
            heights = np.array([comp[value][i]["ports"][port]["n"]
                                for i in range(len(labels))], float)
            axes.bar(index, heights, bottom=floor, width=0.78,
                     color=PORT_STYLE[port]["color"], edgecolor="white",
                     linewidth=0.6, label=port, zorder=3)
            floor += heights
        for i in range(len(labels)):
            total = comp[value][i]["n"]
            if total:
                axes.text(i, total + stack_top * 0.015, f"{total:,}",
                          ha="center", va="bottom", fontsize=7.0, color=MUTED,
                          rotation=90, zorder=5)
        axes.set_xticks(show)
        axes.set_xticklabels([labels[i] for i in show], rotation=68, ha="right",
                             fontsize=7.6)
        axes.set_xlim(-0.6, len(labels) - 0.4)
        axes.set_ylim(0, stack_top * 1.24)
        axes.set_xlabel("bias-corrected offset bin (kHz)", fontsize=10)
        axes.set_title(f"{STYLE[value]['label']} \u2014 who is in each bin",
                       fontsize=12, color=INK, pad=6)
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
        if column == 0:
            axes.set_ylabel("candidate points\nstacked by port (n)", fontsize=11)
            axes.legend(loc="upper right", fontsize=9, framealpha=0.95,
                        borderpad=0.4)
        else:
            axes.tick_params(labelleft=False)

    figure.text(0.5, 0.994,
                "Rows 2\u20133 answer the review: is the cliff bin one port or "
                "three?  At 5.0 and 10 MS/s all three ports are present in the "
                "hundreds and all three fall together.\n"
                "At 2.5 MS/s the cliff bin is still %.0f%% lnb-c \u2014 the "
                "biased port \u2014 so that rate's cliff datum is a one-port "
                "reading and must not be quoted as a three-port result."
                % comp[2.5e6][CLIFF_BIN]["dominant_share_pct"],
                ha="center", va="top", fontsize=11, color="#8a1c1c",
                style="italic", linespacing=1.5)

    figure.text(0.5, 0.104,
                "CENSUS, frozen before any figure was computed (snapshot.py, "
                "digest %s):  %s sweeps on the scan share  |  %s corpus entries  |  "
                "%s scored sidecars,\n"
                "of which %s join into %s paired sweeps, giving %s live target "
                "candidate points plotted here.   Previous snapshot, for scale: "
                "320 sidecars / 176 pairs / 2,464 cells.\n"
                "differential-32, fired at the cross-edge-null 1%% false-alarm "
                "threshold for its own (sample rate, probe length).  lnb-a excluded "
                "as dead (DEAD_RECEIVERS).  Bins with n < %d are not plotted.\n"
                "Grey \u00d7 are the values reported at the 320-sidecar snapshot, "
                "plotted beside the recomputation rather than instead of it.\n"
                "The pilot guard (rate/2 \u2212 pilot band/2) bounds |cfo| inside "
                "the captured band; it is NOT a bound on |cfo \u2212 LO centre|, "
                "which is what these panels plot, so it is drawn in its own lane "
                "and the inset\nshows what the raw axis actually does: at "
                "2.5 MS/s detection RISES through the guard "
                "(%.1f%% \u2192 %.1f%%) rather than falling."
                % (frozen["scored_digest"],
                   f"{frozen['sweeps_on_share']:,}",
                   f"{frozen['corpus_entries']:,}",
                   f"{frozen['scored_sidecars']:,}",
                   f"{int(data['entries']):,}",
                   f"{pairs_joined:,}",
                   f"{plotted_points:,}", MIN_BIN_N,
                   raw["2.5 MS/s"]["last_bin_below_guard_pct"],
                   raw["2.5 MS/s"]["first_bin_above_guard_pct"]),
                ha="center", va="top", fontsize=9.5, color=MUTED, style="italic",
                linespacing=1.6)

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=140,
                   facecolor="white")

    # ---- the plotted values, exactly ---------------------------------------
    payload = {
        "figure": NAME,
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/",
        "snapshot": frozen,
        "census": {
            "sweeps_on_share": frozen["sweeps_on_share"],
            "corpus_entries": frozen["corpus_entries"],
            "scored_sidecars": frozen["scored_sidecars"],
            "scored_sidecars_in_a_pair": int(data["entries"]),
            "paired_sweeps": pairs_joined,
            "candidate_points_read": int(data["cfo_hz"].size),
            "live_target_points_plotted": plotted_points,
            "previous_snapshot_for_comparison": {
                "paired_sweeps": 176, "scored_sidecars": 320,
                "cells": 2464}},
        "method": METHOD,
        "false_alarm_rate": FALSE_ALARM_RATE,
        "excluded_receivers": list(DEAD_RECEIVERS),
        "x_axis": "abs(cfo_hz - receiver_centers_hz[receiver]) -- the pipeline's "
                  "bias-corrected offset (cross_radio.median_corrected_hz), binned "
                  "by that quantity rather than by raw offset",
        "min_bin_n_plotted": MIN_BIN_N,
        "bin_edges_khz": EDGES_KHZ.tolist(),
        "reviewer_bin_count": N_REFERENCE_BINS,
        "thresholds": {f"{int(r)}Hz/{p}ms": t
                       for (r, p), t in sorted(prepared["thresholds"].items())},
        "guard_khz": {f"{v / 1e6:g} MS/s": pilot_guard_khz(v) for v in RATES},
        "guard_spread": pilot_guard_khz(10e6) / pilot_guard_khz(2.5e6),
        "guard_axis_note":
            "The pilot guard is a RAW-axis quantity: rate/2 - PILOT_BAND_HZ/2 "
            "bounds |cfo| inside the captured band, not |cfo - LO centre|. The "
            "previous figure drew it as a full-height vertical on the corrected "
            "axis, which invited reading it as a bound on the plotted quantity. "
            "It is now an axis-anchored tick labelled RAW-AXIS, and "
            "raw_axis_through_the_guard below carries what the raw axis "
            "actually does.",
        "raw_axis_through_the_guard": raw,
        "max_corrected_offset_khz": reach,
        "series": {f"{v / 1e6:g} MS/s": table[v] for v in RATES},
        "series_by_port": {f"{v / 1e6:g} MS/s": {port: by_port[port][v]
                                                 for port in PORTS}
                           for v in RATES},
        "bin_port_composition": {f"{v / 1e6:g} MS/s": comp[v] for v in RATES},
        "cliff_bin_composition": {
            f"{v / 1e6:g} MS/s": comp[v][CLIFF_BIN] for v in RATES},
        "cliff_bin_composition_note":
            "The review found the 2.5 MS/s cliff bin was 86%% lnb-c (103 of "
            "120). At the full corpus it is %.0f%% lnb-c (%d of %d) -- the "
            "concentration got worse, not better, so that rate's cliff is "
            "still a one-port reading. 5.0 MS/s (%d/%d/%d for b/c/d) and 10 "
            "MS/s (%d/%d/%d) now carry all three ports through the cliff and "
            "all three fall."
            % (comp[2.5e6][CLIFF_BIN]["dominant_share_pct"],
               comp[2.5e6][CLIFF_BIN]["ports"]["lnb-c"]["n"],
               comp[2.5e6][CLIFF_BIN]["n"],
               *[comp[5.0e6][CLIFF_BIN]["ports"][p]["n"] for p in PORTS],
               *[comp[10.0e6][CLIFF_BIN]["ports"][p]["n"] for p in PORTS]),
        "reference_reported": {f"{v / 1e6:g} MS/s": REFERENCE_RATE[v] for v in RATES},
        "reference_reported_n": {f"{v / 1e6:g} MS/s": REFERENCE_N[v] for v in REFERENCE_N},
        "reference_cliff_composition": REFERENCE_CLIFF_COMPOSITION,
        "reference_vs_corpus_delta_pct": {
            f"{v / 1e6:g} MS/s": [
                None if table[v][i]["rate_pct"] is None
                else round(table[v][i]["rate_pct"] - REFERENCE_RATE[v][i], 1)
                for i in range(N_REFERENCE_BINS)]
            for v in RATES},
        "cliff_robustness_by_probe_ms": cliff_by_probe(data, prepared),
        "cliff_robustness_note":
            "Every (rate, probe length) cell is checked for a collapse into "
            "the 350-400 kHz bin, so the cliff is not an artefact of pooling "
            "probe lengths inside a rate.",
        "headline_checks": {
            "cliff_bin_is_350_400_at_every_rate": all(
                collapse_bin(table[v]) == CLIFF_BIN for v in RATES),
            "collapse_bin_per_rate": {
                f"{v / 1e6:g} MS/s": bin_labels()[collapse_bin(table[v])]
                for v in RATES},
            "collapse_bin_definition":
                "first bin from which the rate stays below an eighth of that "
                "rate's own peak for every remaining plotted bin",
            "every_rate_collapses_at_350_400": all(
                table[v][CLIFF_BIN]["rate_pct"] < table[v][BEFORE_BIN]["rate_pct"] / 3
                for v in RATES),
            "cliff_bin_single_port": {f"{v / 1e6:g} MS/s":
                                      comp[v][CLIFF_BIN]["single_port_bin"]
                                      for v in RATES},
            "raw_axis_through_its_own_guard": {k: v["verdict"]
                                               for k, v in raw.items()},
        },
    }
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=2)
    print(json.dumps(payload["headline_checks"], indent=2))
    print(json.dumps({k: {"n": v["n"], "dominant": v["dominant_port"],
                          "share": v["dominant_share_pct"]}
                      for k, v in payload["cliff_bin_composition"].items()},
                     indent=2))
    print("max corrected offset (kHz):", reach)


if __name__ == "__main__":
    main()
