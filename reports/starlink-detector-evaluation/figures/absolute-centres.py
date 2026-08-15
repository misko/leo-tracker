#!/usr/bin/env python3
"""absolute-centres: each LNB's own local-oscillator error, and what correcting
it does to the sky window.

Palette: categorical slots 1-3 of the reference palette (blue #2a78d6, orange
#eb6834, aqua #1baf7a), the documented all-pairs-validated subset in light mode.
Aqua is below 3:1 on the light surface, so the relief rule applies and every
aqua mark carries a visible direct label.

Run: PYTHONPATH=/home/satpi01/leo-tracker/src nice -n 15 python3 absolute-centres.py
"""
from __future__ import annotations

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt      # noqa: E402
import numpy as np                   # noqa: E402

ROOT = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/abscal"
GROUPB = "/tmp/claude-1000/-home-satpi01-leo-tracker/07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/groupb"
HERE = os.path.dirname(os.path.abspath(__file__))

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8d8b85"
SURFACE, GRID = "#fcfcfb", "#e3e2dd"
PORTS = ("lnb-a", "lnb-b", "lnb-c", "lnb-d")
RX0 = {"lnb-a", "lnb-c"}
RADIO = {"lnb-a": "pluto-5d4d", "lnb-b": "pluto-5d4d",
         "lnb-c": "pluto-19f2", "lnb-d": "pluto-19f2"}
APPLIED = {("pluto-19f2", "gen1"): 434408.4, ("pluto-19f2", "gen2"): 604159.8,
           ("pluto-5d4d", "gen1"): 5154.0, ("pluto-5d4d", "gen2"): 567402.0}
EPOCH = {"gen1": ("before the 2026-08-13 LNB swap", BLUE),
         "gen2": ("after the swap", ORANGE)}


def main():
    result = json.load(open(f"{ROOT}/abscal.json"))
    methods = result["absolute_by_method"]
    plan = result["recommendation"]

    corpus = np.load(f"{GROUPB}/tlework/residual-axes.npz")
    cp = {k: corpus[k] for k in corpus.files}
    capp = np.array([APPLIED.get((r, g), 0.0) if l in RX0 else 0.0
                     for r, g, l in zip(cp["radio"], cp["gen"], cp["label"])])
    raw = cp["corrected"] + capp
    fired = cp["fired"]

    recommended = np.full(raw.size, np.nan)
    for key, entry in plan.items():
        radio, generation = key.split("|")
        for index, port in enumerate(entry["receiver_labels"]):
            recommended[(cp["label"] == port) & (cp["gen"] == generation)] = \
                entry["measured_centers_hz"][index]

    before = cp["corrected"][fired] / 1e3
    ok = fired & np.isfinite(recommended)
    solid = ok & ~((cp["label"] == "lnb-a") & (cp["gen"] == "gen2"))
    after = (raw[solid] - recommended[solid]) / 1e3

    figure = plt.figure(figsize=(9.0, 9.4), dpi=150)
    figure.patch.set_facecolor(SURFACE)
    top = figure.add_axes([0.285, 0.548, 0.685, 0.268])
    bottom = figure.add_axes([0.105, 0.075, 0.865, 0.300])

    # ------------------------------------------- panel A: per-receiver rows --
    rows, labels = [], []
    for port in PORTS:
        for generation in ("gen1", "gen2"):
            entry = methods.get(f"{port}|{generation}") or {}
            if entry.get("consensus_hz") is None:
                continue
            rows.append((port, generation, entry))
            live = (entry.get("live") or {}).get("n") or 0
            corpus_n = (entry.get("corpus") or {}).get("n") or 0
            sync_n = (entry.get("sync_corpus") or {}).get("n") or 0
            labels.append(f"{port} {'rx0' if port in RX0 else 'rx1'}  "
                          f"{'before' if generation == 'gen1' else 'after'}"
                          f"   n={live + corpus_n + sync_n:,}")
    positions = np.arange(len(rows))[::-1]

    top.set_facecolor(SURFACE)
    seen = set()
    for y, (port, generation, entry) in zip(positions, rows):
        name, colour = EPOCH[generation]
        centre = entry["consensus_hz"] / 1e3
        low, high = [v / 1e3 for v in entry["method_spread_hz"]]
        top.plot([low, high], [y, y], color=colour, linewidth=3.0,
                 solid_capstyle="round", zorder=3, alpha=0.55)
        top.plot([centre], [y], marker="o", markersize=10, color=colour,
                 markeredgecolor=SURFACE, markeredgewidth=2, zorder=5,
                 label=None if generation in seen else name)
        seen.add(generation)
        current = (APPLIED.get((RADIO[port], generation), 0.0)
                   if port in RX0 else 0.0) / 1e3
        top.plot([current], [y], marker="|", markersize=15, color=MUTED,
                 markeredgewidth=2.6, zorder=4,
                 label=None if "cur" in seen else
                       "search centre in use today")
        seen.add("cur")
        top.annotate(f"{centre:+.0f}", (max(centre, high), y),
                     textcoords="offset points", xytext=(11, -4),
                     fontsize=12, color=INK, weight="bold")

    top.axvline(0, color=INK2, linewidth=1.2, zorder=2)
    top.set_yticks(positions)
    top.set_yticklabels(labels, fontsize=10.5, color=INK)
    top.set_xlabel("absolute carrier offset of that receiver  (kHz)",
                   fontsize=11.5, color=INK2)
    top.set_xlim(-260, 720)
    top.set_ylim(-0.75, len(rows) - 0.25)
    top.grid(axis="x", color=GRID, linewidth=0.8)
    top.set_axisbelow(True)
    for side in ("top", "right", "left"):
        top.spines[side].set_visible(False)
    top.spines["bottom"].set_color(GRID)
    top.tick_params(colors=INK2, labelsize=10.5, length=0)
    top.legend(frameon=False, fontsize=9.6, labelcolor=INK2, ncol=3,
               handletextpad=0.5, borderpad=0.2, columnspacing=1.2,
               loc="upper center", bbox_to_anchor=(0.42, -0.235))
    top.set_title("Four independent constants, not one shared error",
                  fontsize=12.5, color=INK, pad=9, loc="left")

    # ------------------------------------------------ panel B: sky window ----
    bottom.set_facecolor(SURFACE)
    bins = np.arange(-400, 401, 25)
    bottom.hist(before, bins=bins, color=MUTED, alpha=0.28, zorder=2)
    bottom.hist(before, bins=bins, histtype="step", color=INK2, linewidth=1.8,
                zorder=3,
                label=f"axis in use now   n={before.size:,}")
    bottom.hist(after, bins=bins, color=AQUA, alpha=0.26, zorder=4)
    bottom.hist(after, bins=bins, histtype="step", color=AQUA, linewidth=2.3,
                zorder=5,
                label=f"corrected axis   n={after.size:,}")
    ceiling = bottom.get_ylim()[1] * 1.34
    bottom.set_ylim(0, ceiling)
    for value, colour, text, align, dx in (
            (before.mean(), INK2, f"centroid {before.mean():+.0f} kHz", "right", -9),
            (after.mean(), AQUA, f"centroid {after.mean():+.1f} kHz", "left", 9)):
        bottom.plot([value, value], [0, ceiling * 0.70], color=colour,
                    linewidth=1.9, linestyle=(0, (4, 3)), zorder=6)
        bottom.annotate(text, (value, ceiling * 0.72), ha=align, va="bottom",
                        fontsize=11.5, color=INK, weight="bold",
                        textcoords="offset points", xytext=(dx, 0))
    bottom.axvline(0, color=INK2, linewidth=1.0, zorder=1)
    bottom.set_xlabel("carrier offset of a sky detection  (kHz)",
                      fontsize=11.5, color=INK2)
    bottom.set_ylabel("detections", fontsize=11.5, color=INK2)
    bottom.set_xlim(-400, 400)
    bottom.grid(axis="y", color=GRID, linewidth=0.8)
    bottom.set_axisbelow(True)
    for side in ("top", "right"):
        bottom.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        bottom.spines[side].set_color(GRID)
    bottom.tick_params(colors=INK2, labelsize=10.5)
    bottom.legend(frameon=False, fontsize=10, loc="upper right",
                  labelcolor=INK2, borderpad=0.2)
    bottom.set_title("Correcting each receiver separately moves the sky window "
                     "onto zero", fontsize=12, color=INK, pad=9, loc="left")

    figure.suptitle("The -150 kHz sky window is each LNB's own oscillator "
                    "error", fontsize=15.5, color=INK, x=0.045, ha="left",
                    y=0.985, weight="bold")
    figure.text(0.045, 0.938,
                "878 narrow sky sweeps (2026-08-12/13), 3,182 live narrow "
                "reports, the 2026-08-14 sync corpus.\nBars span the independent "
                "populations that measured each receiver, the dot is their mean.  "
                f"The\nresidual {after.mean():+.1f} kHz is the calibration's "
                "accuracy floor, not a leftover tuning error.",
                fontsize=9.4, color=MUTED, ha="left", va="top", linespacing=1.5)

    figure.savefig(f"{HERE}/absolute-centres.png", dpi=150, facecolor=SURFACE)
    payload = {
        "panel_a": {f"{port}|{g}": {
            "consensus_hz": (methods.get(f"{port}|{g}") or {}).get("consensus_hz"),
            "method_spread_hz": (methods.get(f"{port}|{g}") or {}).get("method_spread_hz"),
            "n_populations": (methods.get(f"{port}|{g}") or {}).get("n_populations"),
            "populations_hz": (methods.get(f"{port}|{g}") or {}).get("populations_hz"),
            "currently_applied_hz": (APPLIED.get((RADIO[port], g), 0.0)
                                     if port in RX0 else 0.0)}
            for port in PORTS for g in ("gen1", "gen2")},
        "panel_b": {
            "n_before": int(before.size), "n_after": int(after.size),
            "before_centroid_khz": float(before.mean()),
            "before_median_khz": float(np.median(before)),
            "before_fraction_negative": float((before < 0).mean()),
            "after_centroid_khz": float(after.mean()),
            "after_median_khz": float(np.median(after)),
            "after_fraction_negative": float((after < 0).mean()),
            "excluded": "lnb-a|gen2, whose corpus detections are censored at "
                        "the survey bank edge and are not a usable window",
        },
        "recommendation": plan,
        "palette": {"blue": BLUE, "orange": ORANGE, "aqua": AQUA,
                    "note": "reference palette slots 1-3, light mode, "
                            "all-pairs validated; aqua carries direct labels"},
    }
    json.dump(payload, open(f"{HERE}/absolute-centres.json", "w"),
              indent=1, sort_keys=True)
    print("before", before.mean(), "after", after.mean(),
          "neg before", (before < 0).mean(), "neg after", (after < 0).mean())
    print("wrote absolute-centres.png / .json")


if __name__ == "__main__":
    sys.exit(main())
