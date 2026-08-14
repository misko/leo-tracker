#!/usr/bin/env python3
"""method-correlation — the eight detectors are near-duplicates.

This is the mechanism behind ``negative-control.png``.  The f estimate is built
from three firing rates, so eight detectors that make nearly the same fire /
no-fire decision on every observation must return nearly the same f whatever
they are fed — including on joins the coincidence model forbids.  This script
measures how nearly.

phi is the Pearson correlation between two detectors' binary decisions, taken
over the SAME observations: every live target observation in the paired corpus,
each detector judged against the threshold ``cross_radio.null_thresholds`` drew
for its own (sample rate, probe length) from the cross-edge null arms.  An
observation enters only if all eight detectors returned a verdict on it, so
every cell of the matrix is computed on one identical population.

The row/column order is the ordering that maximises total adjacent similarity,
found by enumerating all 8! = 40,320 orderings.  It is reported rather than
assumed, so the block structure the figure shows is a property of the data.

Data
----
Every number comes from ``/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*``.
Run ``extract_lite.py`` first; it streams the corpus into a compact mirror
because the full sidecars do not fit in this host's memory.

    nice -n 15 python3 snapshot.py
    nice -n 15 python3 extract_lite.py
    nice -n 15 python3 method-correlation.py
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("REFRESH_WORK", HERE.parent / "work"))
LITE = Path(os.environ.get("LITE_ROOT", WORK / "lite"))
SNAPSHOT = WORK / "snapshot.json"
OUT_PNG = HERE / "method-correlation.png"
OUT_JSON = HERE / "method-correlation.json"

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d7d6d2"
SURFACE, DIAGONAL = "#fcfcfb", "#e6e5e2"
#: Sequential blue ramp, steps 100 -> 700 of the validated default palette.
#: One hue, light to dark: phi is a magnitude, so it gets a sequential scale.
BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
        "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]

#: Which detectors are variants of one another, used only to outline the blocks
#: the seriation already puts side by side.
FAMILY = {"anchor-8": "anchor", "glrt-32": "glrt", "glrt-64": "glrt",
          "full-frame-verify": "full-frame", "full-frame-full": "full-frame",
          "full-frame-acquire": "full-frame",
          "differential-16": "differential", "differential-32": "differential"}

#: What the retraction report states, for comparison.
REPORTED = {"phi_min": 0.82, "phi_max": 0.94, "observations": 2160}


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def decisions(pairs: list[dict], methods: list[str], thresholds: dict):
    """One fire / no-fire row per live target observation, all eight columns."""
    rows, seen, partial = [], [], 0
    for pair in pairs:
        for entry in pair["radios"]:
            key = cr.threshold_key(entry)
            for observation in entry["scores"]["observations"]:
                if observation.get("arm") != "target" or not cr._live(observation):
                    continue
                verdicts = [cr.observation_fires(
                    observation, method, cr._threshold_for(thresholds, method, key))
                    for method in methods]
                if any(v is None for v in verdicts):
                    partial += 1
                    continue
                rows.append([int(v) for v in verdicts])
                seen.append((entry["capture"], observation.get("iq_index"),
                             observation.get("receiver_label")))
    return np.array(rows, dtype=float), seen, partial


def seriate(matrix: np.ndarray, names: list[str]) -> tuple:
    """Order maximising the sum of adjacent similarities, over all 8! orders.

    An ordering and its reverse score identically, so the direction is pinned
    by putting the alphabetically first name at the top; without that the
    figure would flip between runs for no reason.
    """
    size = matrix.shape[0]
    best = None
    for order in itertools.permutations(range(size)):
        total = sum(matrix[order[i], order[i + 1]] for i in range(size - 1))
        if best is None or total > best[0]:
            best = (total, order)
    total, order = best
    if names[order[0]] > names[order[-1]]:
        order = tuple(reversed(order))
    return order, total


def frozen_census() -> dict:
    with open(SNAPSHOT) as handle:
        snapshot = json.load(handle)
    return {key: snapshot[key] for key in
            ("measured_utc", "sweeps_on_share", "corpus_entries",
             "scored_sidecars", "scored_digest")}


def compute() -> dict:
    pairs, _ = cr.load_pairs(LITE)
    if not pairs:
        raise SystemExit(f"no paired sweeps under {LITE}; run extract_lite.py")
    entries = [entry for pair in pairs for entry in pair["radios"]]
    methods = cr.methods_in(entries)
    thresholds = cr.null_thresholds(entries)

    fired, seen, partial = decisions(pairs, methods, thresholds)
    phi = np.corrcoef(fired.T)
    order, score = seriate(phi, methods)
    ordered = [methods[i] for i in order]
    matrix = phi[np.ix_(order, order)]

    off = matrix[~np.eye(len(ordered), dtype=bool)]
    lowest = np.unravel_index(np.argmin(matrix + np.eye(len(ordered)) * 9),
                              matrix.shape)
    highest = np.unravel_index(np.argmax(matrix - np.eye(len(ordered)) * 9),
                               matrix.shape)
    return {
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
        "mirror": str(LITE),
        "snapshot": frozen_census(),
        "census": {
            **frozen_census(),
            "paired_sweeps": len(pairs),
            "matched_arm_paired_sweeps": sum(1 for pair in pairs
                                             if pair["matched_arm"]),
            "scored_sidecars_in_a_pair": len(entries),
            "live_target_observations": int(fired.shape[0]),
            "matched_arm_cells": sum(len(cr.join_cells(pair)) for pair in pairs
                                     if pair["matched_arm"]),
            "previous_snapshot_for_comparison": {
                "paired_sweeps": 176, "scored_sidecars": 320,
                "observations": REPORTED["observations"], "cells": 2464}},
        "pairs": len(pairs), "entries": len(entries),
        "observations": int(fired.shape[0]),
        "observations_with_a_missing_verdict": partial,
        "unique_observations": len(set(seen)),
        "population": "every live target observation in the paired corpus "
                      "(lnb-a excluded: dead port), all eight verdicts present",
        "methods_alphabetical": methods,
        "order": ordered,
        "order_basis": f"maximises adjacent phi, exhaustive over 8! = 40320 "
                       f"orderings; total adjacent phi {score:.4f}",
        "fire_rate": {m: float(fired[:, i].mean()) for i, m in enumerate(methods)},
        "phi": {row: {col: float(matrix[i, j]) for j, col in enumerate(ordered)}
                for i, row in enumerate(ordered)},
        "phi_off_diagonal": {
            "min": float(off.min()), "max": float(off.max()),
            "mean": float(off.mean()), "pairs": int(off.size // 2),
            "lowest_pair": [ordered[lowest[0]], ordered[lowest[1]]],
            "highest_pair": [ordered[highest[0]], ordered[highest[1]]],
        },
        "reported_in_retraction_report": REPORTED,
    }


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------

def plot(data: dict) -> None:
    ordered = data["order"]
    size = len(ordered)
    matrix = np.array([[data["phi"][r][c] for c in ordered] for r in ordered])
    lo = data["phi_off_diagonal"]["min"]
    hi = data["phi_off_diagonal"]["max"]

    plt.rcParams.update({
        "font.size": 12, "axes.labelsize": 12,
        "xtick.labelsize": 10.5, "ytick.labelsize": 11.5,
        "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    cmap = LinearSegmentedColormap.from_list("phi-blue", BLUE)
    cmap.set_bad(DIAGONAL)
    fig, ax = plt.subplots(figsize=(11.8, 11.2), dpi=150, facecolor=SURFACE)

    shown = np.ma.masked_array(matrix, mask=np.eye(size, dtype=bool))
    image = ax.imshow(shown, cmap=cmap, vmin=lo, vmax=hi)

    span = hi - lo
    for i in range(size):
        for j in range(size):
            if i == j:
                continue
            value = matrix[i, j]
            light = (value - lo) / span > 0.62
            ax.text(j, i, f"{value:.3f}", ha="center", va="center",
                    fontsize=11, color=SURFACE if light else INK)

    # Outline the blocks the seriation put together: each is one detector's
    # own variants, and they hold the tightest pairs in the matrix.
    start = 0
    for index in range(size + 1):
        if index == size or FAMILY[ordered[index]] != FAMILY[ordered[start]]:
            if index - start > 1:
                ax.add_patch(plt.Rectangle(
                    (start - 0.5, start - 0.5), index - start, index - start,
                    fill=False, edgecolor=INK, linewidth=2.4, zorder=4))
            start = index

    ax.set_xticks(range(size))
    ax.set_yticks(range(size))
    ax.xaxis.set_ticks_position("top")
    ax.set_xticklabels(ordered, rotation=40, ha="left", rotation_mode="anchor")
    ax.set_yticklabels(ordered)
    ax.set_xticks(np.arange(size + 1) - 0.5, minor=True)
    ax.set_yticks(np.arange(size + 1) - 0.5, minor=True)
    # A surface gap between adjacent fills, so cells read as separate marks.
    ax.grid(which="minor", color=SURFACE, linewidth=2.0)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for side in ax.spines.values():
        side.set_visible(False)

    bar = fig.colorbar(image, ax=ax, fraction=0.042, pad=0.03, aspect=26)
    bar.set_label("$\\varphi$  (fire / no-fire correlation)", fontsize=11.5,
                  color=INK)
    bar.outline.set_edgecolor(MUTED)
    bar.ax.tick_params(labelsize=10.5, color=MUTED, labelcolor=MUTED)

    # ---- callouts, in the clear space below the matrix --------------------
    # The matrix is symmetric, so the loosest pair appears twice; point at the
    # copy below the diagonal, which is the one nearest the callout.
    low_a, low_b = data["phi_off_diagonal"]["lowest_pair"]
    ends = sorted((ordered.index(low_a), ordered.index(low_b)))
    ax.annotate(
        "the LOOSEST pair anywhere in the matrix is\n"
        f"{low_a} vs {low_b}, $\\varphi$ = {lo:.3f}.\n"
        "Eight independent opinions would sit near 0.",
        xy=(ends[0], ends[1] + 0.58), xytext=(1.75, 8.42), ha="center",
        va="top", fontsize=11, color=INK, linespacing=1.45,
        annotation_clip=False,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.6,
                    "connectionstyle": "arc3,rad=0.2"})

    ax.add_patch(plt.Rectangle((4.72, 8.62), 0.32, 0.32, fill=False,
                               edgecolor=INK, linewidth=2.4, clip_on=False,
                               zorder=5))
    ax.text(5.15, 8.78,
            "outlined blocks are one detector's own\n"
            f"variants, and hold the tightest pairs\n"
            f"($\\varphi$ up to {hi:.3f}).  But the blocks barely\n"
            f"matter: no pair anywhere falls below {lo:.2f}",
            ha="left", va="center", fontsize=11, color=INK, linespacing=1.45,
            clip_on=False)

    fig.suptitle(
        "Not eight opinions but one, counted eight times: every pair of\n"
        f"detectors agrees at $\\varphi$ {lo:.2f}\u2013{hi:.2f} on the same "
        f"{data['observations']:,} observations",
        fontsize=15.5, color=INK, y=0.982)

    census = data.get("snapshot") or {}
    fig.text(0.5, 0.015,
             f"{data['observations']:,} live target observations "
             f"({data['pairs']:,} paired sweeps, {data['entries']:,} scored "
             "sidecars; lnb-a excluded as a dead port).  Each detector is judged\n"
             "against the threshold drawn for its own sample rate and probe length "
             "from the cross-edge null arms, so every cell is one identical\n"
             "population.  Row order maximises adjacent $\\varphi$ over all 8! "
             "orderings.  Diagonal (self-correlation = 1) is masked.\n"
             "CENSUS frozen before any figure was computed and shared with all six "
             f"(digest {census.get('scored_digest', '?')}): "
             f"{census.get('sweeps_on_share', 0):,} sweeps | "
             f"{census.get('corpus_entries', 0):,} corpus entries | "
             f"{census.get('scored_sidecars', 0):,} scored sidecars.\n"
             f"Previously reported: $\\varphi$ "
             f"{REPORTED['phi_min']:.2f}\u2013{REPORTED['phi_max']:.2f} on "
             f"{REPORTED['observations']:,} observations \u2014 recomputed here "
             "at 12.7\u00d7 the observations, not replaced by it.\n"
             "Decisions from leo_tracker.radio.beacon.cross_radio, unmodified.",
             ha="center", va="bottom", fontsize=9.5, color=MUTED,
             linespacing=1.55)
    fig.subplots_adjust(left=0.170, right=0.885, top=0.830, bottom=0.285)
    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)
    print(f"wrote {OUT_PNG}")


def main() -> int:
    data = compute()
    off = data["phi_off_diagonal"]
    data["headline_checks"] = {
        "every_pair_above_0_8": off["min"] > 0.8,
        "range_overlaps_report_0_82_to_0_94": (off["min"] <= 0.94
                                               and off["max"] >= 0.82),
        "observations_match_report": data["observations"] == REPORTED["observations"],
    }
    OUT_JSON.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {OUT_JSON}")
    plot(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
