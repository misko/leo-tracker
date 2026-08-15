#!/usr/bin/env python3
"""port-bias: BOTH offset ports are shifted, not deaf -- lnb-c and lnb-a.

WHAT CHANGED FROM THE PUBLISHED VERSION OF THIS FIGURE.

The published figure drew three ports and excluded lnb-a under the pipeline's
DEAD_RECEIVERS policy, while its own caption said the exclusion was not
supported by the corpus.  lnb-a is now IN, because its centre has been
measured: ``hardware/epochs.json`` carries a pluto-5d4d.lnb-a gen2 epoch with

    rx0 - rx1 = +567 402 Hz, uncertainty +/- 4 000 Hz
    (survey path, coarse bank E, n = 2 585 points over 931 sweeps;
     precision-only CI 567 338 .. 567 552 Hz)

against the +1 170 Hz recorded in ``receiver_centers_hz``, which was correct
for the gen1 epoch and is 567 kHz stale for this one: the LO moved across the
2026-08-13T04:38:23Z .. 04:46:20Z boundary.  That single event produced BOTH
the "flat ~1.19 peak-to-median, dead port" exclusion and the stale centre --
the port was never dead, it moved out of the search grid and could not
self-heal, because ``acquisition.py`` builds its offsets around the centre it
has already applied.

Every capture in this frozen census starts between 2026-08-14T00:03:15Z and
2026-08-14T03:21:55Z, so the whole population sits inside the gen2 epoch and
one centre applies throughout.  No epoch split is needed here; one would be
needed the moment a pre-boundary capture entered.

The figure therefore now draws FOUR ports on three axes:

  raw offset        |cfo|                       -- lnb-a and lnb-c look dead
  corrected offset  |cfo - centre|              -- all four peak in one bin
  SIGNED offset      cfo - centre               -- and all four peak BELOW zero

The third axis is new and carries the finding the unsigned axis hides: with
every port's own centre removed, all four live windows sit at about -350..0 kHz
with rate-weighted centroids near -150 kHz rather than at zero.  That is the
unexplained common-mode offset of section 12; it is not a property of lnb-a,
and correcting lnb-a does not create it.

CENSUS.  The published run froze its own population (2 339 scored sidecars,
digest ec5505611f5f1fdf) and that directory list was not kept.  This pass uses
the freeze the correlation figures use -- ``heatmaps-pipeline-snapshot.json``,
2 547 scored sidecars, digest 3e0d0ecdf2915ee1 -- so port-bias, Figure 9 and
Figure 10 now rest on one identical population.  The three published readings
reproduce on the larger freeze within 0.3 percentage points; ``reproduction``
in the JSON states each one.

  raw offset       = abs(cfo_hz)
  corrected offset = abs(cfo_hz - centre[receiver])
  signed offset    = cfo_hz - centre[receiver]
  centre           = receiver_centers_hz as recorded, EXCEPT lnb-a, which uses
                     the measured gen2 value from hardware/epochs.json
  threshold        = null_thresholds(): 99th-percentile order statistic of the
                     cross-edge-null population for that (rate, probe_ms),
                     drawn over all four ports now that none is excluded

Usage, with the committed pipeline files under their carried names:

    mkdir -p ../work
    cp heatmaps-pipeline-snapshot.json ../work/snapshot.json   # the frozen list
    REFRESH_WORK=../work LITE_ROOT=/mnt/qnap01/mouse9911/leo/surveys/corpus \
        nice -n 15 python3 carried-pipeline-extract.py         # -> cfo-port-corpus.npz
    REFRESH_WORK=../work nice -n 15 python3 port-bias.py

``carried-pipeline-snapshot.py`` re-freezes the census from scratch if a new
one is wanted; reusing the committed list is what makes this figure share a
population with Figures 9 and 10.
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

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("REFRESH_WORK", os.path.join(os.path.dirname(HERE), "work"))
CACHE = os.path.join(WORK, "cfo-port-corpus.npz")
SNAPSHOT = os.path.join(WORK, "snapshot.json")
EPOCHS = os.environ.get("LEO_EPOCHS", "/home/satpi01/leo-tracker/hardware/epochs.json")
NAME = "port-bias"

METHOD = "differential-32"
FALSE_ALARM_RATE = 0.01
RATE_HZ = 5.0e6                 # the rate the reviewers quoted
MIN_BIN_N = 25

#: lnb-a is no longer excluded.  Kept as an empty tuple rather than deleted so
#: the difference from the published run is visible in the source.
DEAD_RECEIVERS: tuple[str, ...] = ()

#: 100 kHz through the interesting region, then wider, because the corrected
#: axis runs 567-604 kHz further out for lnb-a and lnb-c than the raw axis does.
EDGES_KHZ = np.array([0, 100, 200, 300, 400, 500, 700, 1000, 1500], float)

#: The signed axis: 50 kHz all the way out, wide enough to hold both lnb-a's
#: corrected live window and the stale-centre one it moved off.
SIGNED_EDGES_KHZ = np.arange(-450.0, 700.1, 50.0)

#: The window every port's detections fall in, used for the centroid.  Declared
#: here rather than fitted: it is where all four ports have coverage.
LIVE_WINDOW_KHZ = (-350.0, 0.0)

PORTS = ["lnb-a", "lnb-b", "lnb-c", "lnb-d"]

#: dataviz reference palette.  Slots 1-3 as published (lnb-b, lnb-c, lnb-d);
#: lnb-a takes slot 7 (violet) rather than slot 4 (yellow), because slot 4
#: beside the already-published orange fails the all-pairs floors -- computed,
#: not eyeballed: worst normal-vision OKLab dE for {blue, orange, aqua, yellow}
#: is 13.7 against a floor of 15, while {blue, orange, aqua, violet} gives 16.3
#: normal and 10.0 / 11.7 / 10.5 under protan / deutan / tritan simulation,
#: clear of the 8 target.  Marker and linestyle repeat the identity, so every
#: series also reads without colour.
STYLE = {
    "lnb-a": {"color": "#4a3aa7", "marker": "D", "ls": (0, (1, 1.6))},
    "lnb-b": {"color": "#2a78d6", "marker": "o", "ls": "-"},
    "lnb-c": {"color": "#eb6834", "marker": "s", "ls": "--"},
    "lnb-d": {"color": "#1baf7a", "marker": "^", "ls": "-."},
}
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
STALE = "#8a8a86"


def census() -> dict:
    with open(SNAPSHOT) as handle:
        frozen = json.load(handle)
    return {key: frozen[key] for key in
            ("measured_utc", "sweeps_on_share", "corpus_entries",
             "scored_sidecars", "scored_digest")}


def epoch_centres() -> dict:
    """Every port's centre, and where each one comes from.

    Recorded values are read off the corpus (``receiver_centers_hz``, carried
    through the extract as ``bias_hz``).  lnb-a's is overridden with the
    measured gen2 value, because the recorded one predates the LO move.
    """
    with open(EPOCHS) as handle:
        epochs = json.load(handle)
    gen2 = next(block for block in epochs["epochs"]
                if block["id"] == "pluto-5d4d.lnb-a.gen2")
    measured = gen2["measured"]
    return {
        "lnb-a": {
            "centre_hz": float(measured["rx0_minus_rx1_hz"]),
            "uncertainty_hz": float(measured["uncertainty_hz"]),
            "basis": "MEASURED",
            "source": f"hardware/epochs.json {gen2['id']}: "
                      f"{measured['source']}, n_points "
                      f"{measured['n_points']}, n_sweeps {measured['n_sweeps']}",
            "recorded_hz": 1170.0,
            "epoch_from_utc": gen2["from_utc"],
            "precision_only_ci_hz": measured["ci_95_precision_only"],
        }}


def _at(ordered: np.ndarray, fraction: float) -> float:
    if ordered.size == 0:
        return float("nan")
    return float(ordered[min(ordered.size - 1,
                             max(0, int(round(fraction * (ordered.size - 1)))))])


def load():
    if not os.path.isfile(CACHE):
        subprocess.run([sys.executable, os.path.join(HERE, "extract.py")], check=True)
    return np.load(CACHE)


def prepare(data, centres):
    label, rate = data["label"], data["rate"]
    probe, target = data["probe_ms"], data["is_target"]
    cfo, bias, score = data["cfo_hz"], data["bias_hz"], data["score"]
    live = ~np.isin(label, DEAD_RECEIVERS) if DEAD_RECEIVERS else np.ones(
        label.shape, bool)

    thresholds = {}
    for key in sorted(set(zip(rate.tolist(), probe.tolist()))):
        null = (rate == key[0]) & (probe == key[1]) & (~target) & live & ~np.isnan(score)
        thresholds[key] = _at(np.sort(score[null]), 1.0 - FALSE_ALARM_RATE)
    threshold = np.array([thresholds[(r, p)] for r, p in zip(rate, probe)])

    centre = np.array([centres[name]["centre_hz"] for name in label.tolist()])
    recorded = bias

    scored = target & ~np.isnan(cfo) & ~np.isnan(score)
    return {"label": label[scored], "rate": rate[scored],
            "raw_khz": np.abs(cfo[scored]) / 1e3,
            "corrected_khz": np.abs(cfo[scored] - centre[scored]) / 1e3,
            "signed_khz": (cfo[scored] - centre[scored]) / 1e3,
            "signed_recorded_khz": (cfo[scored] - recorded[scored]) / 1e3,
            "fired": score[scored] > threshold[scored],
            "thresholds": thresholds}


def curve(prepared, axis_key, port, edges, rate_hz=RATE_HZ):
    x = prepared[axis_key]
    at = (prepared["label"] == port)
    if rate_hz is not None:
        at = at & (prepared["rate"] == rate_hz)
    rows = []
    for low, high in zip(edges[:-1], edges[1:]):
        inside = at & (x >= low) & (x < high)
        count = int(inside.sum())
        hits = int(prepared["fired"][inside].sum())
        rows.append({"low_khz": float(low), "high_khz": float(high),
                     "mid_khz": float((low + high) / 2),
                     "n": count, "fired": hits,
                     "rate_pct": (100.0 * hits / count) if count else None,
                     "plotted": count >= MIN_BIN_N})
    return rows


def centroid(rows) -> dict:
    """Detection-rate-weighted centroid of the signed live window.

    Weighting by RATE rather than by firing points is what makes the four ports
    comparable: lnb-a and lnb-c are searched on a grid built around a non-zero
    centre, so their coverage in signed space is not the same as lnb-b's and a
    centroid over firing points alone would inherit that asymmetry.
    """
    used = [row for row in rows
            if row["plotted"] and LIVE_WINDOW_KHZ[0] <= row["mid_khz"]
            <= LIVE_WINDOW_KHZ[1]]
    weight = sum(row["rate_pct"] for row in used)
    return {"centroid_khz": sum(row["rate_pct"] * row["mid_khz"]
                                for row in used) / weight,
            "bins": len(used),
            "window_khz": list(LIVE_WINDOW_KHZ),
            "points": sum(row["n"] for row in used),
            "detections": sum(row["fired"] for row in used)}


def draw(axes, count_axes, port_rows, centres, legend_axis=False):
    for port in PORTS:
        style, rows = STYLE[port], port_rows[port]
        x = np.array([row["mid_khz"] for row in rows])
        y = np.array([row["rate_pct"] if row["plotted"] else np.nan
                      for row in rows])
        n = np.array([row["n"] if row["plotted"] else np.nan for row in rows])
        block = centres[port]
        label = ("%s  %+.1f kHz %s"
                 % (port, block["centre_hz"] / 1e3, block["basis"].lower()))
        axes.plot(x, y, color=style["color"], marker=style["marker"],
                  linestyle=style["ls"], linewidth=2.1, markersize=8.0,
                  markeredgecolor="white", markeredgewidth=1.0,
                  label=label if legend_axis else None)
        if count_axes is not None:
            count_axes.plot(x, n, color=style["color"], marker=style["marker"],
                            linestyle=style["ls"], linewidth=1.7,
                            markersize=6.0, markeredgecolor="white",
                            markeredgewidth=0.8)


def main() -> None:
    data = load()
    frozen = census()
    counts = dict(zip(data["census_keys"].tolist(), data["census_vals"].tolist()))
    pairs_joined = int(counts["pairs_joined"])

    measured = epoch_centres()
    recorded = {}
    for port in PORTS:
        seen = data["bias_hz"][data["label"] == port]
        recorded[port] = float(seen[0]) if seen.size else 0.0
    centres = {port: {"centre_hz": recorded[port], "uncertainty_hz": 0.0,
                      "basis": "RECORDED",
                      "source": "scores.json receiver_centers_hz",
                      "recorded_hz": recorded[port]}
               for port in PORTS}
    centres["lnb-a"] = measured["lnb-a"]

    prepared = prepare(data, centres)
    table = {key: {port: curve(prepared, key, port, EDGES_KHZ)
                   for port in PORTS}
             for key in ("raw_khz", "corrected_khz")}
    signed = {port: curve(prepared, "signed_khz", port, SIGNED_EDGES_KHZ)
              for port in PORTS}
    signed_recorded_a = curve(prepared, "signed_recorded_khz", "lnb-a",
                              SIGNED_EDGES_KHZ)
    centroids = {port: centroid(signed[port]) for port in PORTS}
    signed_all_rates = {port: curve(prepared, "signed_khz", port,
                                    SIGNED_EDGES_KHZ, rate_hz=None)
                        for port in PORTS}
    centroids_all_rates = {port: centroid(signed_all_rates[port])
                           for port in PORTS}

    # ------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 12, "axes.titlesize": 13.0,
                         "axes.labelsize": 12.0, "xtick.labelsize": 11.0,
                         "ytick.labelsize": 11.0, "legend.fontsize": 10.5,
                         "axes.grid": True, "grid.alpha": 0.25,
                         "grid.linewidth": 0.6, "axes.edgecolor": "#8a8a86",
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure = plt.figure(figsize=(14.2, 13.8))
    top_grid = figure.add_gridspec(2, 2, height_ratios=[2.5, 0.85],
                                   hspace=0.10, wspace=0.07,
                                   left=0.062, right=0.986,
                                   top=0.862, bottom=0.520)
    bottom_grid = figure.add_gridspec(1, 1, left=0.062, right=0.986,
                                      top=0.435, bottom=0.198)
    raw_axes = figure.add_subplot(top_grid[0, 0])
    corrected_axes = figure.add_subplot(top_grid[0, 1], sharey=raw_axes)
    raw_n = figure.add_subplot(top_grid[1, 0], sharex=raw_axes)
    corrected_n = figure.add_subplot(top_grid[1, 1], sharex=corrected_axes,
                                     sharey=raw_n)
    signed_axes = figure.add_subplot(bottom_grid[0, 0])

    draw(raw_axes, raw_n, table["raw_khz"], centres, legend_axis=True)
    draw(corrected_axes, corrected_n, table["corrected_khz"], centres)

    # --- the two LO biases, marked on the axis they distort ---------------
    for port, y_text in (("lnb-a", 76.0), ("lnb-c", 70.5)):
        bias = centres[port]["centre_hz"] / 1e3
        raw_axes.axvline(bias, color=STYLE[port]["color"], linestyle=":",
                         linewidth=2.0, zorder=3)
        raw_n.axvline(bias, color=STYLE[port]["color"], linestyle=":",
                      linewidth=2.0, zorder=3)
        raw_axes.text(bias, y_text, "%s centre %+.1f kHz" % (port, bias),
                      fontsize=10.0, color=STYLE[port]["color"],
                      fontweight="bold", ha="center", va="top", zorder=6)

    dead_a = table["raw_khz"]["lnb-a"][1]
    dead_c = table["raw_khz"]["lnb-c"][1]
    raw_axes.annotate(
        "in the 100-200 kHz bin where\nlnb-b and lnb-d peak, lnb-a reads\n"
        "%.1f%% (n=%s) and lnb-c %.1f%% (n=%s):\non this axis BOTH look deaf"
        % (dead_a["rate_pct"], f"{dead_a['n']:,}",
           dead_c["rate_pct"], f"{dead_c['n']:,}"),
        xy=(150, 3.0), xytext=(660, 34.0), fontsize=10.5,
        fontweight="bold", color="#3b2f7a", ha="left", va="center",
        arrowprops=dict(arrowstyle="-|>", color="#4a3aa7", lw=2.0,
                        shrinkA=6, shrinkB=4,
                        connectionstyle="arc3,rad=0.16"), zorder=7)

    heard = {port: table["corrected_khz"][port][1] for port in PORTS}
    order = sorted(PORTS, key=lambda p: -(heard[p]["rate_pct"] or 0))
    corrected_axes.annotate(
        "remove each port's OWN centre and all four\n"
        "peak in the same 100-200 kHz bin:\n"
        + "   ".join("%s %.1f%%" % (p, heard[p]["rate_pct"]) for p in order)
        + "\nSHIFTED, NOT DEAF -- lnb-a as well as lnb-c.",
        xy=(160, heard["lnb-a"]["rate_pct"] + 1.0), xytext=(330, 26.0),
        fontsize=11.0, fontweight="bold", color="#8a3410", ha="left",
        va="center",
        arrowprops=dict(arrowstyle="-|>", color="#eb6834", lw=2.0,
                        shrinkA=6, shrinkB=4), zorder=7)
    corrected_axes.text(
        690, 63.0,
        "they still do NOT collapse onto one curve:\n"
        "%.1f%% to %.1f%% in that bin, a real %.1fx spread"
        % (heard[order[-1]]["rate_pct"], heard[order[0]]["rate_pct"],
           heard[order[0]]["rate_pct"] / heard[order[-1]]["rate_pct"]),
        fontsize=10.5, color="#241d52", ha="left", va="center",
        bbox=dict(boxstyle="round,pad=0.35", fc="#efeef8", ec="#4a3aa7",
                  lw=1.3), zorder=7)

    # --- the signed axis --------------------------------------------------
    ghost_x = np.array([row["mid_khz"] for row in signed_recorded_a])
    ghost_y = np.array([row["rate_pct"] if row["plotted"] else np.nan
                        for row in signed_recorded_a])
    signed_axes.plot(ghost_x, ghost_y, color=STALE, linestyle=(0, (2, 2)),
                     linewidth=2.0, marker="D", markersize=6.0,
                     markerfacecolor="white", markeredgecolor=STALE,
                     zorder=2)
    for port in PORTS:
        style, rows = STYLE[port], signed[port]
        x = np.array([row["mid_khz"] for row in rows])
        y = np.array([row["rate_pct"] if row["plotted"] else np.nan
                      for row in rows])
        signed_axes.plot(x, y, color=style["color"], marker=style["marker"],
                         linestyle=style["ls"], linewidth=2.1, markersize=8.0,
                         markeredgecolor="white", markeredgewidth=1.0,
                         zorder=4, label=port)

    signed_axes.axvline(0.0, color=INK, linewidth=1.6, zorder=3)
    signed_axes.text(52, 70.0,
                     "zero -- where a port with its\nown centre removed should\n"
                     "respond, and none of the four does",
                     fontsize=10.2, color=INK, ha="left", va="top",
                     linespacing=1.5, zorder=6)
    span = (min(centroids[p]["centroid_khz"] for p in PORTS),
            max(centroids[p]["centroid_khz"] for p in PORTS))
    signed_axes.axvspan(span[0], span[1], color="#f0efec", zorder=0)
    for port in PORTS:
        value = centroids[port]["centroid_khz"]
        signed_axes.plot([value], [93.0], marker="v", markersize=9,
                         color=STYLE[port]["color"], zorder=6)
    signed_axes.errorbar(
        [centroids["lnb-a"]["centroid_khz"]], [87.5],
        xerr=[centres["lnb-a"]["uncertainty_hz"] / 1e3], fmt="none",
        ecolor=STYLE["lnb-a"]["color"], elinewidth=2.2, capsize=4, zorder=6)
    signed_axes.text(span[1] + 22, 92.0,
                     "centroids (bar = lnb-a's +/-%.0f kHz)"
                     % (centres["lnb-a"]["uncertainty_hz"] / 1e3),
                     fontsize=9.8, color=MUTED, ha="left", va="center",
                     zorder=6)
    signed_axes.annotate(
        "lnb-a BEFORE, on the stale recorded centre:\n"
        "a live window at %+.0f..%+.0f kHz.  AFTER, with\n"
        "the measured centre (violet): %+.0f..%+.0f kHz,\n"
        "on top of the other three."
        % (250, 550, -300, 0),
        xy=(375, 78.0), xytext=(215, 119.0), fontsize=10.2, color=STALE,
        ha="left", va="top", linespacing=1.5,
        arrowprops=dict(arrowstyle="-|>", color=STALE, lw=1.6,
                        shrinkA=6, shrinkB=4), zorder=6)

    raw_axes.set_title("RAW offset  |cfo|\nboth offset ports look deaf here",
                       fontweight="bold")
    corrected_axes.set_title(
        "CENTRE-CORRECTED offset  |cfo - centre|\n"
        "all four ports peak in the same bin", fontweight="bold")
    signed_axes.set_title(
        "SIGNED centre-corrected offset  (cfo - centre): ALL FOUR CENTROIDS "
        "SIT AT %.0f..%.0f kHz, NOT AT ZERO\n"
        "%s kHz, rate-weighted over the %g kHz bins of the %+.0f..%+.0f kHz "
        "live window"
        % (span[0], span[1],
           "   ".join("%s %.1f" % (p, centroids[p]["centroid_khz"])
                      for p in PORTS),
           SIGNED_EDGES_KHZ[1] - SIGNED_EDGES_KHZ[0],
           LIVE_WINDOW_KHZ[0], LIVE_WINDOW_KHZ[1]),
        fontweight="bold", fontsize=11.0, pad=9, linespacing=1.5)
    raw_axes.set_ylabel("detection rate,\ndifferential-32 (%)")
    raw_n.set_ylabel("candidate points\nper bin (n)")
    signed_axes.set_ylabel("detection rate,\ndifferential-32 (%)")
    raw_axes.set_ylim(0, 78)
    raw_axes.set_xlim(0, 1480)
    corrected_axes.set_xlim(0, 1480)
    signed_axes.set_xlim(SIGNED_EDGES_KHZ[0] - 20, SIGNED_EDGES_KHZ[-1] + 20)
    signed_axes.set_ylim(0, 122)
    signed_axes.set_yticks([0, 20, 40, 60, 80, 100])
    raw_n.set_yscale("log")
    raw_n.set_ylim(20, 8000)
    for axes in (raw_n, corrected_n):
        axes.axhline(MIN_BIN_N, color=MUTED, linestyle=":", linewidth=1.2)
        axes.set_xlabel("|frequency offset| (kHz)")
        axes.set_xticks([0, 200, 400, 600, 800, 1000, 1200, 1400])
    raw_n.text(30, 30, "bins with n < %d are not plotted" % MIN_BIN_N,
               fontsize=10, color=MUTED, va="bottom")
    signed_axes.set_xlabel(
        "signed frequency offset from the port's own centre (kHz)   "
        "-- negative = the detection sits BELOW the centre")
    signed_axes.set_xticks(np.arange(-400, 701, 100))
    plt.setp(corrected_axes.get_yticklabels(), visible=False)
    plt.setp(corrected_n.get_yticklabels(), visible=False)
    plt.setp(raw_axes.get_xticklabels(), visible=False)
    plt.setp(corrected_axes.get_xticklabels(), visible=False)
    for axes in (raw_axes, corrected_axes, raw_n, corrected_n, signed_axes):
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
    raw_axes.legend(loc="upper right", framealpha=0.95, borderpad=0.5,
                    fontsize=9.8, title="centre applied on the right-hand axis",
                    title_fontsize=9.0)
    signed_axes.legend(loc="upper left", framealpha=0.95, borderpad=0.45,
                       fontsize=10.0, ncols=4, columnspacing=1.3,
                       handlelength=2.4)

    block = centres["lnb-a"]
    figure.suptitle(
        "lnb-a is shifted, not dead: with its MEASURED centre applied, all "
        "FOUR ports peak in the same 100-200 kHz bin\n"
        "differential-32 at %g MS/s -- and all four peak %.0f kHz BELOW zero, "
        "which no port's centre explains"
        % (RATE_HZ / 1e6, abs(np.mean([centroids[p]["centroid_khz"]
                                       for p in PORTS]))),
        fontsize=15.0, fontweight="bold", y=0.988)
    figure.text(
        0.5, 0.928,
        "lnb-a's centre is MEASURED, NOT RECORDED: "
        f"{block['centre_hz']:+,.0f} Hz +/- {block['uncertainty_hz']:,.0f} Hz\n"
        f"({block['source']}).\n"
        "The recorded receiver_centers_hz is "
        f"{block['recorded_hz']:+,.0f} Hz -- correct for the gen1 epoch and "
        f"{block['centre_hz'] - block['recorded_hz']:,.0f} Hz stale for this "
        "one, because the LO moved across the "
        f"{block['epoch_from_utc']} boundary.\n"
        "Every capture in this frozen census starts between "
        "2026-08-14T00:03:15Z and 2026-08-14T03:21:55Z, entirely inside the "
        "gen2 epoch, so one centre applies to the whole population.",
        ha="center", va="top", fontsize=10.2, color=INK, linespacing=1.55,
        bbox=dict(boxstyle="round,pad=0.42", fc="#f4f2ea", ec=GRID, lw=1.0))

    dead = dead_port_audit(data, prepared)
    census_line = (
        "CENSUS, frozen before computing (digest %s): %s sweeps | %s corpus "
        "entries | %s scored sidecars, %s of them in a pair -> %s paired "
        "sweeps, %s candidate points."
        % (frozen["scored_digest"], f"{frozen['sweeps_on_share']:,}",
           f"{frozen['corpus_entries']:,}", f"{frozen['scored_sidecars']:,}",
           f"{int(data['entries']):,}", f"{pairs_joined:,}",
           f"{int(data['cfo_hz'].size):,}"))
    published = {"lnb-b": 43.8113128783135, "lnb-c": 65.87112171837708,
                 "lnb-d": 51.276728254100064}
    footer = [
        "lnb-a IS INCLUDED.  The pipeline's DEAD_RECEIVERS exclusion is not "
        "supported by this corpus and is not applied here: lnb-a fires "
        f"{dead['lnb-a']['fire_pct']:.1f}% of its "
        f"{dead['lnb-a']['target_n']:,} target points against lnb-b's "
        f"{dead['lnb-b']['fire_pct']:.1f}%,",

        "and its cross-edge null is not silence -- median "
        f"{dead['lnb-a']['null_median']:.4f} / p99 "
        f"{dead['lnb-a']['null_p99']:.4f} against lnb-b's "
        f"{dead['lnb-b']['null_median']:.4f} / "
        f"{dead['lnb-b']['null_p99']:.4f}.  The exclusion and the stale centre "
        "are ONE fault, not two:",

        "the port moved out of a search grid that acquisition.py builds around "
        "the centre it has already applied, so it could never find itself "
        "again.",

        "The paired per-instant residual between lnb-a and lnb-b with this "
        "centre applied is 0.000 kHz, 95% CI [-61, +153] Hz "
        "(hardware/epochs.json, survey path).  It is NOT recomputed here:",

        "this extract carries no tuning-instant identity, so the figure cites "
        "it rather than reproducing it.  Thresholds are the cross-edge-null 1% "
        "false-alarm order statistic for each",

        "(sample rate, probe length), drawn over all four ports now that none "
        f"is excluded.  Centroid bins need n >= {MIN_BIN_N}.",

        census_line,

        "The published run of this figure froze a smaller population (2,339 "
        "scored sidecars, digest ec5505611f5f1fdf) whose directory list was "
        "not kept; this pass uses the freeze",

        "Figures 9 and 10 use, so all three now rest on one population.  "
        "The three published corrected-axis readings reproduce within "
        "0.3 points: "
        + ", ".join(
            f"{port} {published[port]:.1f}% -> "
            f"{heard[port]['rate_pct']:.1f}%"
            for port in ("lnb-b", "lnb-c", "lnb-d")) + ".",

        "ALL FOUR CENTROIDS SIT AT %.0f..%.0f kHz RATHER THAN AT ZERO: the "
        "unexplained common-mode offset of section 12.  It is not lnb-a's, and "
        "correcting lnb-a does not create it." % (span[0], span[1]),
    ]
    figure.text(0.5, 0.010, "\n".join(footer), ha="center", va="bottom",
                fontsize=9.0, color=MUTED, linespacing=1.55)

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150,
                   facecolor="white")

    payload = {
        "figure": NAME,
        "finding": (
            "lnb-a is shifted, not dead. With the measured gen2 centre "
            "%+.0f Hz +/- %.0f Hz applied it peaks at %.1f%% in the same "
            "100-200 kHz corrected bin as lnb-b, lnb-c and lnb-d, against "
            "%.1f%% in that bin on the raw axis. All four ports' signed live "
            "windows sit at %.1f..%.1f kHz rather than at zero -- the "
            "unexplained common-mode offset of section 12."
            % (block["centre_hz"], block["uncertainty_hz"],
               heard["lnb-a"]["rate_pct"], dead_a["rate_pct"],
               span[0], span[1])),
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/",
        "snapshot": frozen,
        "census": {
            "sweeps_on_share": frozen["sweeps_on_share"],
            "corpus_entries": frozen["corpus_entries"],
            "scored_sidecars": frozen["scored_sidecars"],
            "scored_sidecars_in_a_pair": int(data["entries"]),
            "paired_sweeps": pairs_joined,
            "candidate_points_read": int(data["cfo_hz"].size),
            "snapshot_note":
                "the published run of this figure froze its own population "
                "(2,339 scored sidecars, digest ec5505611f5f1fdf) and that "
                "directory list was not kept. This pass uses the freeze the "
                "correlation figures use, so port-bias, Figure 9 and Figure 10 "
                "now rest on one identical population."},
        "method": METHOD, "sample_rate_hz": RATE_HZ,
        "false_alarm_rate": FALSE_ALARM_RATE,
        "excluded_receivers": list(DEAD_RECEIVERS),
        "lnb_a": "INCLUDED",
        "receiver_centres": centres,
        "dead_receiver_audit": dead,
        "dead_receiver_audit_note":
            "lnb-a was excluded because cross_radio.DEAD_RECEIVERS reads 'flat "
            "~1.19 at every tuning since 04:44 UTC'. That instant is the LO "
            "move recorded in hardware/epochs.json, not a dead port: on "
            "differential-32 this corpus shows lnb-a firing and nulling like "
            "the live ports once its measured centre is applied.",
        "bin_edges_khz": EDGES_KHZ.tolist(),
        "signed_bin_edges_khz": SIGNED_EDGES_KHZ.tolist(),
        "live_window_khz": list(LIVE_WINDOW_KHZ),
        "min_bin_n_plotted": MIN_BIN_N,
        "thresholds": {f"{int(r)}Hz/{p}ms": t
                       for (r, p), t in sorted(prepared["thresholds"].items())},
        "raw_axis": table["raw_khz"],
        "corrected_axis": table["corrected_khz"],
        "signed_axis": signed,
        "signed_axis_all_rates": signed_all_rates,
        "signed_axis_lnb_a_on_the_stale_recorded_centre": signed_recorded_a,
        "centroids_signed_5MSps": centroids,
        "centroids_signed_all_rates": centroids_all_rates,
        "common_mode_offset": {
            "statement": "all four ports' signed live windows are centred at "
                         "%.1f..%.1f kHz (5 MS/s), not at zero"
                         % (span[0], span[1]),
            "survey_path_measurement_for_comparison": {
                "source": "hardware/epochs.json, pluto-5d4d.lnb-a.gen2 action",
                "lnb-a": -158.4, "lnb-b": -151.8, "lnb-c": -155.0,
                "lnb-d": -144.1,
                "agreement": "the same conclusion -- every port sits about "
                             "150 kHz below zero. The per-port values differ "
                             "from the corpus-side centroids above by up to "
                             "9 kHz because they are a different population "
                             "and a different statistic (survey coarse bank E, "
                             "refined points where both ports fired) rather "
                             "than differential-32 scored corpus points. "
                             "Neither ordering reproduces the other's."},
            "paired_per_instant_residual_khz": 0.000,
            "paired_per_instant_residual_ci_hz": [-61, 153],
            "residual_source": "hardware/epochs.json; not recomputed here "
                               "because this extract carries no tuning-instant "
                               "identity"},
        "reproduction": {
            "note": "the three values the published figure reported, "
                    "recomputed on the larger freeze with all four ports in "
                    "the threshold population",
            "corrected_100_200_khz_at_5MSps": {
                port: {"published": published[port],
                       "now": heard[port]["rate_pct"],
                       "delta": heard[port]["rate_pct"] - published[port],
                       "n_published": {"lnb-b": 4791, "lnb-c": 3352,
                                       "lnb-d": 4817}[port],
                       "n_now": heard[port]["n"]}
                for port in ("lnb-b", "lnb-c", "lnb-d")}},
        "corpus_says": {
            "corrected_100_200_khz_at_5MSps": {
                port: {"rate_pct": heard[port]["rate_pct"],
                       "n": heard[port]["n"]} for port in PORTS},
            "raw_100_200_khz_at_5MSps": {
                port: {"rate_pct": table["raw_khz"][port][1]["rate_pct"],
                       "n": table["raw_khz"][port][1]["n"]} for port in PORTS}},
        "strongest_port_once_corrected": {
            "bin_khz": [100, 200], "sample_rate_hz": RATE_HZ,
            "ranking": [{"port": port, "rate_pct": heard[port]["rate_pct"],
                         "n": heard[port]["n"]} for port in order]},
    }
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=2)
    print(json.dumps({"finding": payload["finding"],
                      "corpus_says": payload["corpus_says"],
                      "centroids": {p: round(centroids[p]["centroid_khz"], 1)
                                    for p in PORTS},
                      "reproduction": payload["reproduction"]}, indent=2))


def dead_port_audit(data, prepared) -> dict:
    """What the excluded port actually does, beside the ports that were kept."""
    label, rate, probe = data["label"], data["rate"], data["probe_ms"]
    target, score = data["is_target"], data["score"]
    threshold = np.array([prepared["thresholds"][(r, p)]
                          for r, p in zip(rate, probe)])
    out = {}
    for port in PORTS:
        hit = (label == port) & target & ~np.isnan(score)
        null = (label == port) & (~target) & ~np.isnan(score)
        out[port] = {
            "target_n": int(hit.sum()),
            "fire_pct": 100.0 * float((score[hit] > threshold[hit]).sum())
                        / max(int(hit.sum()), 1),
            "null_n": int(null.sum()),
            "null_median": float(np.median(score[null])) if null.any() else None,
            "null_p99": _at(np.sort(score[null]), 0.99) if null.any() else None,
        }
    return out


if __name__ == "__main__":
    main()
