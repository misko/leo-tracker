#!/usr/bin/env python3
"""tle-residual: the -150 kHz against an external clock.

Residual = measured bias-corrected carrier offset - TLE-predicted Doppler, per
sky detection, on the 878 narrow sky sweeps of 2026-08-12/13 (the only sky
captures carrying a probe UTC and an rf_center_hz; the sync-* sweeps section 12
binned carry neither).  Geometry from orbit.association.catalog_doppler against
the archived catalogue store; thresholds from cross_radio.null_thresholds.

Reads tle-residual.json + tlework/residual-axes.npz, written by
tle-residual-stats.py.  Nothing here is hand-entered except MARK_KHZ, which is
the report's own claimed centre, plotted so disagreement with it is visible.

Run: PYTHONPATH=/home/satpi01/leo-tracker/src nice -n 15 python3 tle-residual.py
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

ROOT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/groupb"
NAME = "tle-residual"
MARK_KHZ = -150.0          # the centre REPORT section 12 reports; not a fit
ELEV_FLOOR = 40.0
LNB_LO_HZ = 9.75e9

# Same identity assignment as the report's figures/injection/raw-vs-corrected.py
# (dataviz reference palette, categorical slots 1-4, light mode), so the two
# figures can be read side by side.  Marker and dash carry identity too.
PORTS = ("lnb-b", "lnb-d", "lnb-c", "lnb-a")
STYLE = {
    "lnb-b": {"color": "#2a78d6", "marker": "o"},
    "lnb-d": {"color": "#eb6834", "marker": "s"},
    "lnb-c": {"color": "#1baf7a", "marker": "^"},
    "lnb-a": {"color": "#eda100", "marker": "D"},
}
INK, MUTED, FAINT, GRID = "#0b0b0b", "#52514e", "#8a8983", "#e2e1dd"
SURFACE, BAND, DOPPLER = "#fcfcfb", "#f0efec", "#d8d6d1"
EPOCH_LS = {"gen1": "-", "gen2": "--"}
EPOCH_NAME = {"gen1": "before the 08-13 LNB swap", "gen2": "after it"}


def frame(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(FAINT)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=7.5, length=3, width=0.8)
    ax.grid(True, color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)


def density(values, edges):
    counts, _ = np.histogram(values, bins=edges)
    width = np.diff(edges)
    total = counts.sum()
    return counts / (total * width) if total else counts.astype(float)


def main():
    stats = json.load(open(os.path.join(ROOT, "figures", "tle-residual.json")))
    sync = json.load(open(os.path.join(ROOT, "figures", "tle-residual-synccheck.json")))
    axes_data = np.load(os.path.join(ROOT, "tlework", "residual-axes.npz"),
                        allow_pickle=False)
    resid = axes_data["resid_maxel"] / 1e3
    # Refined points only for the shapes: a point claimed by a coarse proposer
    # sits on a 25 kHz grid tooth and would draw spikes that are the bank, not
    # the sky.  The centres quoted everywhere are the all-point figures from
    # tle-residual.json; the refined-only centres are within 10 kHz of them.
    fired = axes_data["fired"].astype(bool) & axes_data["refined"].astype(bool)
    label = axes_data["label"]
    gen = axes_data["gen"]
    utc = axes_data["utc"]

    pool = axes_data["pool_doppler_sample"] / 1e3
    pool_elev = axes_data["pool_elev_sample"]
    pool = pool[pool_elev >= ELEV_FLOOR]

    sidecar = {"mark_khz": MARK_KHZ, "panels": {}}

    fig = plt.figure(figsize=(13.6, 8.6), dpi=170, facecolor="white")
    grid = fig.add_gridspec(2, 12, height_ratios=[1.0, 0.92],
                            left=0.052, right=0.988, top=0.795, bottom=0.078,
                            wspace=2.1, hspace=0.52)

    fig.text(0.052, 0.966,
             "The −150 kHz is not Doppler and not one tuning error: it is each "
             "LNB's own local oscillator",
             fontsize=14.6, color=INK, fontweight="bold")
    fig.text(0.052, 0.931,
             "Residual = measured bias-corrected carrier offset − SGP4/TLE-predicted Doppler, "
             "per sky detection.  878 narrow sky sweeps, 2026-08-12/13, differential-32 at a 1% "
             "cross-edge-null false alarm.",
             fontsize=8.8, color=MUTED)
    fig.text(0.052, 0.907,
             "The predicted Doppler population is symmetric about zero, so the residual centre "
             "is the measured centre — and it differs by 150 kHz between two radios watching the "
             "same sky at the same instant.",
             fontsize=8.8, color=MUTED)

    # ---------------------------------------------------- row 1: residuals --
    edges = np.arange(-450, 460, 25.0)
    mids = (edges[:-1] + edges[1:]) / 2
    doppler_density = density(pool, edges)
    curves = {}
    for port in PORTS:
        for generation in ("gen1", "gen2"):
            mask = fired & (label == port) & (gen == generation) & np.isfinite(resid)
            if mask.sum() >= 200:
                curves[(port, generation)] = (density(resid[mask], edges) * 1e3,
                                              int(mask.sum()))
    ceiling = 1.06 * max(float(v[0].max()) for v in curves.values())
    for column, port in enumerate(PORTS):
        ax = fig.add_subplot(grid[0, column * 3:(column + 1) * 3])
        frame(ax)
        ax.fill_between(mids, doppler_density * 1e3, color=DOPPLER, zorder=1,
                        step="mid", linewidth=0)
        ax.axvline(0, color=MUTED, linewidth=1.0, zorder=2)
        ax.axvline(MARK_KHZ, color=INK, linewidth=1.0, linestyle=(0, (4, 3)), zorder=2)
        rows = {}
        for generation in ("gen1", "gen2"):
            entry = stats["by_port"].get(f"{port}|{generation}")
            curve = curves.get((port, generation))
            if entry is None or curve is None:
                continue
            ax.plot(mids, curve[0], color=STYLE[port]["color"], linewidth=1.9,
                    linestyle=EPOCH_LS[generation], zorder=4, solid_capstyle="round")
            centre = entry["residual_vs_maxel"]["mean_khz"]
            ax.plot([centre], [0.0], marker=STYLE[port]["marker"], markersize=6.5,
                    color=STYLE[port]["color"], markeredgecolor="white",
                    markeredgewidth=1.0, clip_on=False, zorder=6)
            rows[generation] = {"n": curve[1], "centre_khz": centre,
                                "ci95_khz": entry["residual_vs_maxel"]["mean_ci95_khz"]}
        ax.set_xlim(-450, 450)
        ax.set_ylim(0, ceiling)
        ax.set_yticks([])
        ax.set_xticks([-400, -200, 0, 200, 400])
        ax.set_title(f"{port}   ({stats['by_port'].get(port + '|gen1', {}).get('radio') or ''})",
                     fontsize=10.5, color=INK, loc="left", pad=5,
                     fontweight="bold")
        for line, (generation, value) in enumerate(sorted(rows.items())):
            ax.text(0.985, 0.955 - 0.085 * line,
                    f"{'before swap' if generation == 'gen1' else 'after swap'}"
                    f"   {value['centre_khz']:+.0f} kHz",
                    transform=ax.transAxes, fontsize=7.6,
                    color=STYLE[port]["color"], va="top", ha="right",
                    fontweight="bold", zorder=8,
                    bbox={"facecolor": SURFACE, "edgecolor": "none",
                          "pad": 1.6, "alpha": 0.92})
        if column == 0:
            ax.set_ylabel("density of detections", fontsize=8, color=MUTED)
        ax.set_xlabel("residual  (kHz)", fontsize=8, color=MUTED)
        sidecar["panels"].setdefault("residual_by_port", {})[port] = rows

    # ------------------------------------------- row 2a: constant vs IF -----
    ax = fig.add_subplot(grid[1, 0:4])
    frame(ax)
    # Each receiver-epoch is drawn relative to its OWN centre, so the seven
    # curves share an origin and the only thing left on the y axis is how the
    # offset moves across the eight tunings.
    plotted = {}
    for key, entry in sorted(stats["by_tuning"].items()):
        port, generation = key.split("|")
        if key == "lnb-a|gen2":
            continue                      # port outside its own search grid
        style = STYLE[port]
        base = float(np.mean([r["mean_khz"] for r in entry["rows"]]))
        for edge, marker_fill in (("lower", True), ("upper", False)):
            rows = [r for r in entry["rows"] if r["region"].startswith(edge)]
            if not rows:
                continue
            x = [r["if_mhz"] / 1e3 for r in rows]
            y = [r["mean_khz"] - base for r in rows]
            ax.plot(x, y, color=style["color"], linewidth=1.3,
                    linestyle=EPOCH_LS[generation], zorder=3, alpha=0.9)
            ax.plot(x, y, linestyle="none", marker=style["marker"], markersize=4.8,
                    color=style["color"] if marker_fill else SURFACE,
                    markeredgecolor=style["color"], markeredgewidth=1.2, zorder=4)
        plotted[key] = {"constant_khz": entry["constant_khz"],
                        "if_slope_ppm": entry["if_slope_ppm"],
                        "if_term_over_span_khz": entry["if_term_over_span_khz"],
                        "edge_half_step_khz": entry["edge_half_step_khz"],
                        "fit_residual_rms_khz": entry["fit_residual_rms_khz"]}
    # What a Pluto-side fractional error big enough to BE the -150 kHz would
    # look like: proportional to the IF, so it would add another -153 kHz across
    # the 2.02x span.  No port moves by more than 26 kHz.
    span = np.array([0.9597, 1.9403])
    ax.plot(span, MARK_KHZ * (span / span[0] - 1.0), color=INK, linewidth=1.4,
            linestyle=(0, (1, 2)), zorder=5)
    ax.annotate("a tuner or reference error\nlarge enough to BE the −150 kHz\n"
                "would run down this line",
                xy=(1.80, MARK_KHZ * (1.80 / span[0] - 1.0)), xytext=(1.16, -122),
                fontsize=7.2, color=INK, ha="left", va="top", linespacing=1.25,
                arrowprops={"arrowstyle": "-", "color": FAINT, "linewidth": 0.8})
    ax.axhline(0, color=MUTED, linewidth=1.0, zorder=2)
    ax.text(0.965, 50, "filled = lower edge,  open = upper edge:\n"
            "a +33 kHz step, the same on every receiver",
            fontsize=7.0, color=MUTED, ha="left", va="top", linespacing=1.25)
    ax.set_xlabel("Pluto IF centre  (GHz)   —   a 2.02× lever", fontsize=8, color=MUTED)
    ax.set_ylabel("centre − that receiver's own centre  (kHz)", fontsize=8, color=MUTED)
    ax.set_title("Constant in hertz, not proportional to the tuning",
                 fontsize=9.8, color=INK, loc="left", pad=6, fontweight="bold")
    ax.set_ylim(-170, 55)
    ax.set_xlim(0.90, 2.01)
    sidecar["panels"]["tuning_decomposition"] = plotted

    # --------------------------------------- row 2b: centres with CIs -------
    ax = fig.add_subplot(grid[1, 4:8])
    frame(ax)
    order = [(p, g) for g in ("gen1", "gen2") for p in PORTS
             if f"{p}|{g}" in stats["by_port"]]
    caterpillar = {}
    for row, (port, generation) in enumerate(order):
        entry = stats["by_port"][f"{port}|{generation}"]
        value = entry["residual_vs_maxel"]["mean_khz"]
        low, high = entry["residual_vs_maxel"]["mean_ci95_khz"]
        broken = (port == "lnb-a" and generation == "gen2")
        ax.plot([low, high], [row, row], color=STYLE[port]["color"], linewidth=2.4,
                solid_capstyle="round", alpha=0.35 if broken else 1.0, zorder=3)
        ax.plot([value], [row], marker=STYLE[port]["marker"], markersize=7.0,
                color=STYLE[port]["color"] if generation == "gen1" else SURFACE,
                markeredgecolor=STYLE[port]["color"], markeredgewidth=1.4,
                alpha=0.35 if broken else 1.0, zorder=4)
        ax.text(high + 8, row, f"{value:+.0f}", fontsize=7.6, ha="left",
                va="center", color=MUTED if broken else INK, fontweight="bold")
        caterpillar[f"{port}|{generation}"] = {"mean_khz": value, "ci95_khz": [low, high]}
    ax.axvline(MARK_KHZ, color=INK, linewidth=1.0, linestyle=(0, (4, 3)), zorder=2)
    ax.axvline(0, color=MUTED, linewidth=1.0, zorder=2)
    ax.text(MARK_KHZ - 8, len(order) - 0.55, "−150 kHz ", fontsize=7.4, color=INK,
            ha="right", va="center")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([f"{p}  {'before' if g == 'gen1' else 'after'}"
                        for p, g in order], fontsize=7.8)
    ax.set_ylim(-0.9, len(order) - 0.25)
    ax.set_xlim(-330, 130)
    ax.set_xlabel("residual centre, 95% sweep-clustered CI  (kHz)",
                  fontsize=8, color=MUTED)
    ax.set_title("Seven live receiver-epochs, seven different centres",
                 fontsize=9.8, color=INK, loc="left", pad=6, fontweight="bold")
    ax.text(0.988, 0.90, "faint: lnb-a after the swap — outside\n"
            "its own search grid, 0.2–5% fire rate,\nnot interpretable",
            transform=ax.transAxes, fontsize=6.9, color=MUTED, ha="right",
            va="top", linespacing=1.35)
    sidecar["panels"]["centres"] = caterpillar

    # ----------------------------------------- row 2c: hourly stability -----
    ax = fig.add_subplot(grid[1, 8:12])
    frame(ax)
    origin = float(np.nanmin(utc))
    hourly = {}
    for key, entry in sorted(stats["diagnostics"]["hourly"].items()):
        port, generation = key.split("|")
        if key == "lnb-a|gen2":
            continue
        hours = [(dt.datetime.fromisoformat(r["utc"]).timestamp() - origin) / 3600.0
                 for r in entry["hours"]]
        values = [r["residual_mean_khz"] for r in entry["hours"]]
        ax.plot(hours, values, color=STYLE[port]["color"], linewidth=1.3,
                linestyle=EPOCH_LS[generation], marker=STYLE[port]["marker"],
                markersize=3.4, zorder=4, alpha=0.95)
        ax.plot(hours, [r["candidate_doppler_mean_khz"] for r in entry["hours"]],
                color=MUTED, linewidth=0.8, zorder=3, alpha=0.55)
        hourly[key] = {"spread_khz": entry["spread_khz"], "sd_khz": entry["sd_khz"]}
    ax.axhline(MARK_KHZ, color=INK, linewidth=0.9, linestyle=(0, (4, 3)), zorder=2)
    ax.axhline(0, color=MUTED, linewidth=1.0, zorder=2)
    swap = (dt.datetime.fromisoformat(stats["epoch_split_utc"][0].replace("Z", "+00:00"))
            .timestamp() - origin) / 3600.0
    ax.axvspan(swap, swap + 0.14, color="#c9c7c2", zorder=1)
    ax.annotate("LNB swap on pluto-19f2:\nlnb-c and lnb-d only",
                xy=(swap + 0.15, 62), xytext=(swap + 1.4, 78), fontsize=7.0,
                color=INK, ha="left", va="top", linespacing=1.25,
                arrowprops={"arrowstyle": "-", "color": FAINT, "linewidth": 0.8})
    ax.text(0.99, 0.025, "grey: mean predicted Doppler of the candidates in view",
            transform=ax.transAxes, fontsize=6.9, color=MUTED, ha="right", va="bottom")
    ax.set_xlabel("hours from 2026-08-12T16:29Z", fontsize=8, color=MUTED)
    ax.set_ylabel("hourly residual centre  (kHz)", fontsize=8, color=MUTED)
    ax.set_title("The sky turns over; the offset does not",
                 fontsize=9.8, color=INK, loc="left", pad=6, fontweight="bold")
    ax.set_ylim(-265, 100)
    sidecar["panels"]["hourly"] = hourly

    handles = [Line2D([], [], color=STYLE[p]["color"], marker=STYLE[p]["marker"],
                      markersize=5.5, linewidth=1.8, label=p) for p in PORTS]
    handles += [
        Line2D([], [], color=MUTED, linewidth=1.6, linestyle="-",
               label="before the LNB swap"),
        Line2D([], [], color=MUTED, linewidth=1.6, linestyle="--",
               label="after it"),
        Patch(facecolor=DOPPLER, edgecolor="none",
              label="TLE-predicted Doppler, catalogued satellites above 40°"),
        Line2D([], [], color=INK, linewidth=1.1, linestyle=(0, (4, 3)),
               label="−150 kHz, the centre section 12 reports"),
    ]
    fig.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.048, 0.884),
               ncol=8, frameon=False, fontsize=7.9, labelcolor=INK,
               handlelength=2.0, columnspacing=1.5, handletextpad=0.6)

    out = os.path.join(ROOT, "figures", f"{NAME}.png")
    fig.savefig(out, facecolor="white")
    plt.close(fig)

    # Each receiver's own absolute error, undoing the rx0 correction: the
    # corrected axis is anchored on rx1, so rx0's absolute figure is its
    # corrected centre plus the differential value that was subtracted from it.
    absolute = {}
    for key, entry in sorted(stats["by_tuning"].items()):
        port, generation = key.split("|")
        radio = stats["by_port"][key]["radio"]
        applied = (stats["centres_applied_hz"][f"{radio}|{generation}"]
                   if port in ("lnb-a", "lnb-c") else 0.0)
        raw = entry["constant_khz"] + applied / 1e3
        absolute[key] = {
            "radio": radio, "corrected_constant_khz": entry["constant_khz"],
            "rx0_correction_applied_khz": applied / 1e3,
            "absolute_offset_khz": raw,
            "ppm_of_lnb_lo": raw * 1e3 / LNB_LO_HZ * 1e6}
    sidecar["absolute_receiver_offsets"] = absolute
    sidecar["population"] = stats["population"]
    sidecar["identifiability"] = stats["diagnostics"]["assignment_identifiability"]
    sidecar["in_view_doppler"] = {
        k: v for k, v in stats["diagnostics"].items() if k.startswith("in_view")}
    sidecar["sync_corpus_cross_check"] = sync["by_port"]
    json.dump(sidecar, open(os.path.join(ROOT, "figures", f"{NAME}-figure.json"), "w"),
              indent=1, sort_keys=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
