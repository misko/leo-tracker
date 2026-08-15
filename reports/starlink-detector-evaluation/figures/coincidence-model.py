#!/usr/bin/env python3
"""coincidence-model: the idea meant to stand in for injection, and what it returns.

LEFT is a SCHEMATIC and carries no data: two chains that share only the sky,
and the three-equation model that follows from that.  For sky-occupancy f,
per-chain detection d, per-cell false-alarm p::

    P(A)  = f.dA + (1-f).p
    P(B)  = f.dB + (1-f).p
    P(AB) = f.dA.dB + (1-f).p^2

Three equations, three unknowns, so d comes out without a known input ever
existing.  P(A), P(B) and P(AB) are counted on the corpus; p is measured on the
cross-edge null arm; f, dA and dB are SOLVED.  EVERY d ON THIS FIGURE IS A
MODEL OUTPUT AND NOT A MEASUREMENT, and the figure says so on itself.

RIGHT is real: every f, dA and dB is solved by the repository's own
``cross_radio.solve_coincidence`` from counts on cells built by
``cross_radio.join_cells``, per algorithm, per geometry and per receiver pair.

WHAT CHANGED FROM THE PUBLISHED VERSION.

lnb-a IS INCLUDED.  The published figure ran with
``cross_radio.DEAD_RECEIVERS = ('lnb-a',)``, which left exactly two receiver
pairs in every joined cell -- lnb-c|lnb-b and lnb-d|lnb-b -- so "chain B" was
one physical port and every number on the panel rested on it.  With lnb-a
restored each tuning instant yields FOUR cross-radio pairings, the matched-arm
cell count doubles from 18,192 to 36,384, and chain B is two ports rather than
one.

Restoring lnb-a moves the threshold population and the empty-sky rate p as well
as the cells, and it has to: p is "how often a chain fires on a null cell", and
a p measured without lnb-a's null arm would be the wrong chain's empty-sky rate
for half the cells.  So this figure redraws both.  The report's own thresholds
are carried in the JSON as ``robustness_published_thresholds``.

The source is also different, and deliberately so.  The published run read
``cache/firerate-coincidence.npz``, built from a freeze (2,544 sidecars, digest
8cec1405bfbca027) whose cell table no longer exists on disk.  This run builds
its cells with ``cross_radio.join_cells`` from the heatmaps cache instead, on
the 2,547-sidecar freeze the rest of this figure set uses, so all four figures
now rest on ONE population.  The published values are carried alongside for the
comparison.

Usage:  nice -n 15 python3 coincidence-model.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = Path(__file__).resolve().parent
#: ``extract_heatmaps`` sits beside this file when the figure directory is
#: self-contained, and in ../work beside the frozen snapshot when regenerated.
for _candidate in (HERE, HERE.parent / "work"):
    if (_candidate / "extract_heatmaps.py").is_file():
        PIPELINE = _candidate
        sys.path.insert(0, str(_candidate))
        break
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402
from _pipeline import load as _load_pipeline  # noqa: E402
ex = _load_pipeline("extract_heatmaps")

OUT_PNG = HERE / "coincidence-model.png"
OUT_JSON = HERE / "coincidence-model.json"
SNAPSHOT = PIPELINE / "heatmaps-pipeline-snapshot.json"
DRIFT = PIPELINE / "heatmaps-pipeline-drift.json"

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"
BAND, SKY = "#eceae5", "#e3ecf7"
BLUE, ORANGE, TEAL = "#2a78d6", "#eb6834", "#2f8f7f"

#: Seriated order used by every figure in the 2026-08-14 cross-radio set, so a
#: reader carries one row order between them.
ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
         "full-frame-full", "full-frame-acquire", "differential-32",
         "differential-16"]

#: Operator-supplied hardware facts.  WATER means liquid water was found on that
#: port's bias-tee SMA pins.  Chain A is always pluto-19f2 and chain B always
#: pluto-5d4d, because ``load_pairs`` orders a pair's radios by radio_id -- so
#: chain A is always a WET port and chain B always a DRY one, and dA against dB
#: cannot be read as a water effect.
HARDWARE = {
    "lnb-a": {"model": "X", "water": "dry", "radio": "pluto-5d4d", "port": "rx0"},
    "lnb-b": {"model": "Y", "water": "dry", "radio": "pluto-5d4d", "port": "rx1"},
    "lnb-c": {"model": "X", "water": "wet", "radio": "pluto-19f2", "port": "rx0"},
    "lnb-d": {"model": "X", "water": "wet", "radio": "pluto-19f2", "port": "rx1"},
}

CONFOUND = (
    "WATER IS CONFOUNDED WITH RADIO. Chain A is always pluto-19f2 (lnb-c or "
    "lnb-d, both WET) and chain B always pluto-5d4d (lnb-a or lnb-b, both DRY), "
    "because the join orders a pair's radios by radio_id. Any gap between dA and "
    "dB is therefore a radio-and-water difference at once, and this corpus holds "
    "no dry port on 19f2 and no wet port on 5d4d to break the tie.")

#: The published run of this figure, on the freeze whose cell table is gone.
#: Carried so the population change is visible rather than quietly absorbed.
PUBLISHED = {
    "population": {"scored_sidecars": 2544, "scored_digest": "8cec1405bfbca027",
                   "paired_sweeps": 1257, "matched_arm_sweeps": 1136,
                   "matched_arm_cells": 18176,
                   "receiver_pairs": ["lnb-c|lnb-b", "lnb-d|lnb-b"],
                   "lnb_a": "EXCLUDED from target AND null"},
    "opposite_edge": {
        "anchor-8": {"f": 0.346, "d_a": 0.810, "d_b": 0.752},
        "glrt-32": {"f": 0.344, "d_a": 0.814, "d_b": 0.766},
        "full-frame-full": {"f": 0.388, "d_a": 0.778, "d_b": 0.728}},
}

D_IS_A_MODEL_OUTPUT = (
    "MODEL OUTPUT, NOT A MEASUREMENT. Nothing was injected into this corpus, so "
    "no d here has ever been checked against a known input. d is what the three "
    "equations return once f is eliminated; if the assumption they rest on -- "
    "that the two chains fail independently given the sky -- is wrong, so is "
    "every d beside it.")


def drift() -> dict:
    if DRIFT.is_file():
        return json.loads(DRIFT.read_text())
    return {"checked": False, "note": "no end-of-run re-measurement available"}


# --------------------------------------------------------------------------
# cells
# --------------------------------------------------------------------------

def build(*, dead_receivers=()) -> dict:
    """Matched-arm target cells, thresholds and p, at one exclusion setting.

    Every step is the repository's own: ``null_thresholds`` for the bar,
    ``cell_false_alarm`` for p, ``join_cells`` for the cells and
    ``observation_fires`` for each verdict.  Nothing is reimplemented here.
    """
    cr.DEAD_RECEIVERS = tuple(dead_receivers)
    cache = ex.Cache()
    saved = cr.DEAD_RECEIVERS
    cr.DEAD_RECEIVERS = ()        # pairs() reads it only for bookkeeping
    pairs, load_census = cache.pairs()
    cr.DEAD_RECEIVERS = saved

    entries = [entry for pair in pairs for entry in pair["radios"]]
    methods = cr.methods_in(entries)
    thresholds = cr.null_thresholds(entries)
    false_alarm = cr.cell_false_alarm(entries, thresholds)

    matched = [pair for pair in pairs if pair["matched_arm"]]
    cells = [cell for pair in matched for cell in cr.join_cells(pair)]

    # Freeze one verdict per (cell, method) so every slice below rests on the
    # same decision; ``verify`` checks the frozen counts against cr._rates.
    left = np.full((len(cells), len(methods)), -1, dtype=np.int8)
    right = np.full((len(cells), len(methods)), -1, dtype=np.int8)
    for row, cell in enumerate(cells):
        for column, method in enumerate(methods):
            a = cr.observation_fires(
                cell["a"]["observation"], method,
                cr._threshold_for(thresholds, method, cell["a"]["key"]))
            b = cr.observation_fires(
                cell["b"]["observation"], method,
                cr._threshold_for(thresholds, method, cell["b"]["key"]))
            if a is not None:
                left[row, column] = int(a)
            if b is not None:
                right[row, column] = int(b)

    return {
        "methods": methods, "thresholds": thresholds,
        "false_alarm": false_alarm, "cells": cells,
        "left": left, "right": right,
        "geometry": np.array([c["geometry"] for c in cells]),
        "receiver_pair": np.array([c["receiver_pair"] for c in cells]),
        "sweep": np.array([c["paired_sweep"] for c in cells]),
        "pairs": pairs, "matched": matched, "entries": entries,
        "load_census": load_census,
        "dead_receivers": list(saved),
    }


def verify(model: dict) -> dict:
    """The frozen counts must equal ``cross_radio._rates`` exactly."""
    mismatches = []
    for index, method in enumerate(model["methods"]):
        truth = cr._rates(model["cells"], method, model["thresholds"])
        ok = (model["left"][:, index] >= 0) & (model["right"][:, index] >= 0)
        a = model["left"][:, index][ok] == 1
        b = model["right"][:, index][ok] == 1
        mine = {"cells": int(ok.sum()), "fired_a": int(a.sum()),
                "fired_b": int(b.sum()), "both": int((a & b).sum())}
        if mine != truth:
            mismatches.append({"method": method, "cross_radio": truth,
                               "frozen": mine})
    return {"methods_checked": len(model["methods"]),
            "cells": len(model["cells"]),
            "mismatches": mismatches, "exact": not mismatches}


def estimate(model: dict, index: int, keep, p: float) -> dict:
    """Counts on one slice, handed to the repository's estimator."""
    left, right = model["left"][:, index], model["right"][:, index]
    usable = keep & (left >= 0) & (right >= 0)
    n = int(usable.sum())
    if not n:
        return {"cells": 0, "solvable": False, "reason": "no usable cells",
                "f": None, "d_a": None, "d_b": None}
    a, b = left[usable] == 1, right[usable] == 1
    solved = cr.solve_coincidence(float(a.mean()), float(b.mean()),
                                  float((a & b).mean()), p)
    return {**solved, "cells": n, "fired_a": int(a.sum()),
            "fired_b": int(b.sum()), "both": int((a & b).sum())}


def spread_of(per_method: dict, methods: list, name: str) -> dict:
    values = {m: per_method[m][name]["f"] for m in methods
              if per_method[m][name].get("solvable")}
    return {
        "cells": max((per_method[m][name]["cells"] for m in methods), default=0),
        "methods_solved": len(values),
        "f_min": min(values.values()) if values else None,
        "f_max": max(values.values()) if values else None,
        "f_spread": (max(values.values()) - min(values.values())
                     if values else None),
        "f_argmin": min(values, key=values.get) if values else None,
        "f_argmax": max(values, key=values.get) if values else None,
        "d_min": min(min(per_method[m][name]["d_a"], per_method[m][name]["d_b"])
                     for m in values) if values else None,
        "d_max": max(max(per_method[m][name]["d_a"], per_method[m][name]["d_b"])
                     for m in values) if values else None,
    }


def solve_all(model: dict) -> tuple:
    methods = model["methods"]
    geometry, receiver_pair = model["geometry"], model["receiver_pair"]
    geometries = sorted(set(geometry.tolist()))
    receiver_pairs = sorted(set(receiver_pair.tolist()))

    slices = {"pooled": np.ones(len(geometry), dtype=bool)}
    for name in geometries:
        slices[name] = geometry == name
    for name in receiver_pairs:
        slices[name] = receiver_pair == name

    per_method: dict = {}
    for index, method in enumerate(methods):
        p = (model["false_alarm"].get(method) or {}).get("rate")
        per_method[method] = {
            "false_alarm_rate_p": p,
            "null_fires": (model["false_alarm"].get(method) or {}).get("count"),
            "null_observations":
                (model["false_alarm"].get(method) or {}).get("cells"),
        }
        for name, keep in slices.items():
            per_method[method][name] = estimate(model, index, keep, p)

    spread = {name: spread_of(per_method, methods, name) for name in slices}
    return per_method, spread, geometries, receiver_pairs


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def compute() -> dict:
    model = build(dead_receivers=())          # lnb-a live in target, null, join
    checked = verify(model)
    per_method, spread, geometries, receiver_pairs = solve_all(model)
    methods = model["methods"]

    # -- the same solve on the published three-port threshold / p bar -----
    other = build(dead_receivers=("lnb-a",))
    other_per_method, other_spread, _, other_pairs = solve_all(other)

    snapshot = json.loads(SNAPSHOT.read_text())
    census = {key: snapshot[key] for key in
              ("measured_utc", "sweeps_on_share", "corpus_entries",
               "scored_sidecars", "scored_digest")}
    census["paired_sweeps"] = len(model["pairs"])
    census["matched_arm_sweeps"] = len(model["matched"])
    census["scored_sidecars_in_a_pair"] = len(model["entries"])
    census["matched_arm_cells"] = len(model["cells"])
    census["null_observations"] = max(
        (v["cells"] for v in model["false_alarm"].values()), default=0)
    census["load_pairs_census"] = model["load_census"]
    census["excluded_receivers"] = {}
    census["receivers_live"] = ["lnb-a", "lnb-b", "lnb-c", "lnb-d"]
    census["lnb_a"] = (
        "INCLUDED in target AND null. The exclusion recorded in "
        "cross_radio.DEAD_RECEIVERS cites a flat ~1.19 peak-to-median at every "
        "tuning since 2026-08-13 04:44 UTC. That instant falls inside "
        "pluto-5d4d's 03:24:04Z-05:07:56Z outage, when the radio produced no "
        "data at all, and this scored corpus stops at 2026-08-14T03:21:55Z "
        "regardless.")
    census["cells_per_receiver_pair"] = {
        name: int((model["receiver_pair"] == name).sum())
        for name in receiver_pairs}

    verification = {}
    for method, reported in PUBLISHED["opposite_edge"].items():
        got = per_method[method].get("opposite-edge", {})
        verification[method] = {
            "published_with_lnb_a_excluded": reported,
            "recomputed_with_lnb_a_included": {
                key: got.get(key) for key in ("f", "d_a", "d_b")},
            "delta": {key: (None if got.get(key) is None
                            else got[key] - reported[key])
                      for key in ("f", "d_a", "d_b")},
        }

    return {
        "figure": "coincidence-model",
        "lnb_a": "INCLUDED",
        "model": {"P(A)": "f*dA + (1-f)*p", "P(B)": "f*dB + (1-f)*p",
                  "P(AB)": "f*dA*dB + (1-f)*p^2",
                  "solver": "leo_tracker.radio.beacon.cross_radio."
                            "solve_coincidence",
                  "cells_from": "leo_tracker.radio.beacon.cross_radio.join_cells",
                  "thresholds_from": "leo_tracker.radio.beacon.cross_radio."
                                     "null_thresholds",
                  "p_from": "leo_tracker.radio.beacon.cross_radio."
                            "cell_false_alarm",
                  "counted": ["P(A)", "P(B)", "P(AB)"],
                  "measured": ["p (cross-edge null arm, per cell)"],
                  "solved": ["f", "dA", "dB"],
                  "status_of_d": D_IS_A_MODEL_OUTPUT},
        "invariance_check": "f is a property of the sky, so every algorithm "
                            "must return the same f. The spread across the "
                            "eight is the model's own consistency check.",
        "chain_identity": {
            "chain_A": "pluto-19f2 — lnb-c or lnb-d, both WET",
            "chain_B": "pluto-5d4d — lnb-a or lnb-b, both DRY",
            "why": "load_pairs orders a pair's radios by radio_id, and "
                   "pluto-19f2 sorts before pluto-5d4d",
            "not_identified": CONFOUND},
        "hardware": HARDWARE,
        "source": {
            "cells": "built here with cross_radio.join_cells from the heatmaps "
                     "cache, not read from cache/firerate-coincidence.npz",
            "why": "the published run's cell table was built from a different "
                   "freeze (2,544 sidecars, digest 8cec1405bfbca027) whose "
                   "cells.json.gz no longer exists on disk. Rebuilding on the "
                   "2,547-sidecar freeze the other three figures use puts the "
                   "whole set on one population.",
            "frozen_verdicts_match_cross_radio_rates": checked},
        "census": census,
        "census_recheck_at_end_of_run": drift(),
        "methods": methods,
        "row_order": ORDER,
        "geometries": geometries,
        "receiver_pairs": receiver_pairs,
        "per_method": per_method,
        "spread": spread,
        "published_with_lnb_a_excluded": PUBLISHED,
        "verification_against_the_published_run": verification,
        "robustness_lnb_a_excluded_everywhere": {
            "what": "the published exclusion setting reproduced on THIS freeze: "
                    "lnb-a out of the target arm, the null arm and the join, so "
                    "the cells fall back to the two receiver pairs the published "
                    "figure had. It is the like-for-like control on the source "
                    "change -- it isolates how much of the movement below is the "
                    "receiver set and how much is the different freeze.",
            "matched_arm_cells": len(other["cells"]),
            "receiver_pairs": other_pairs,
            "frozen_verdicts_match_cross_radio_rates": verify(other),
            "per_method": {m: {"false_alarm_rate_p":
                               other_per_method[m]["false_alarm_rate_p"],
                               "pooled": {k: other_per_method[m]["pooled"].get(k)
                                          for k in ("f", "d_a", "d_b", "cells")}}
                           for m in methods},
            "spread_pooled": other_spread["pooled"],
        },
    }


# --------------------------------------------------------------------------
# plot: left panel is a schematic and says so
# --------------------------------------------------------------------------

def box(ax, xy, w, h, text, *, face, edge, fontsize=10.5, weight="normal",
        colour=INK):
    ax.add_patch(FancyBboxPatch(
        (xy[0] - w / 2, xy[1] - h / 2), w, h,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.3, facecolor=face, edgecolor=edge, zorder=2))
    ax.text(xy[0], xy[1], text, ha="center", va="center", fontsize=fontsize,
            color=colour, zorder=3, linespacing=1.35, fontweight=weight)


def arrow(ax, start, end, *, colour=INK, style="-|>", rad=0.0, width=1.5):
    ax.add_patch(FancyArrowPatch(
        start, end, arrowstyle=style, mutation_scale=13, linewidth=width,
        color=colour, shrinkA=2, shrinkB=2, zorder=2,
        connectionstyle=f"arc3,rad={rad}"))


def schematic(ax) -> None:
    """The model as a picture.  Carries no data and says so at the top."""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.992,
            "S C H E M A T I C   —   N O   D A T A   O N   T H I S   P A N E L",
            ha="center", va="top", fontsize=9.5, color=MUTED, fontweight="bold")

    box(ax, (0.5, 0.905), 0.80, 0.098,
        "ONE SKY CELL\none channel edge, one instant\n"
        "occupied with probability $f$",
        face=SKY, edge=BLUE, fontsize=10.5)

    arrow(ax, (0.34, 0.854), (0.215, 0.792), colour=BLUE)
    arrow(ax, (0.66, 0.854), (0.785, 0.792), colour=ORANGE)
    ax.text(0.212, 0.828, "detects\nw.p. $d_A$", ha="right", va="center",
            fontsize=10, color=BLUE, linespacing=1.3)
    ax.text(0.788, 0.828, "detects\nw.p. $d_B$", ha="left", va="center",
            fontsize=10, color=ORANGE, linespacing=1.3)

    box(ax, (0.21, 0.727), 0.40, 0.126,
        "CHAIN A\npluto-19f2\nlnb-c or lnb-d — both WET\nown Pluto, own USB bus",
        face=SURFACE, edge=BLUE, fontsize=9.0)
    box(ax, (0.79, 0.727), 0.40, 0.126,
        "CHAIN B\npluto-5d4d\nlnb-a or lnb-b — both DRY\nown Pluto, own USB bus",
        face=SURFACE, edge=ORANGE, fontsize=9.0)
    ax.annotate("", xy=(0.417, 0.727), xytext=(0.583, 0.727),
                arrowprops={"arrowstyle": "<->", "color": MUTED,
                            "linewidth": 1.2})
    ax.text(0.5, 0.658, "they share ONLY the sky", ha="center", va="top",
            fontsize=9.5, color=MUTED, style="italic")

    arrow(ax, (0.20, 0.668), (0.20, 0.612), colour=MUTED, width=1.1)
    arrow(ax, (0.80, 0.668), (0.80, 0.612), colour=MUTED, width=1.1)
    box(ax, (0.5, 0.573), 0.94, 0.072,
        "empty cell: EITHER chain still fires with probability $p$\n"
        "$p$ is MEASURED on the cross-edge null arm, per cell",
        face=BAND, edge=MUTED, fontsize=10)

    arrow(ax, (0.5, 0.537), (0.5, 0.503), colour=INK)

    ax.add_patch(FancyBboxPatch(
        (0.045, 0.228), 0.91, 0.267,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.6, facecolor=SURFACE, edgecolor=INK, zorder=2))
    ax.text(0.5, 0.480, "what the two chains are then seen to do",
            ha="center", va="top", fontsize=9.5, color=MUTED, zorder=3)
    for row, (label, expression) in enumerate((
            ("$P(A)$", r"$= f\,d_A + (1-f)\,p$"),
            ("$P(B)$", r"$= f\,d_B + (1-f)\,p$"),
            ("$P(AB)$", r"$= f\,d_A d_B + (1-f)\,p^{2}$"))):
        y = 0.427 - row * 0.060
        ax.text(0.335, y, label, ha="right", va="center", fontsize=13,
                color=INK, zorder=3)
        ax.text(0.360, y, expression, ha="left", va="center", fontsize=13,
                color=INK, zorder=3)
    ax.text(0.5, 0.248,
            "COUNTED on the corpus: $P(A)$, $P(B)$, $P(AB)$      MEASURED: $p$",
            ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

    arrow(ax, (0.5, 0.228), (0.5, 0.194), colour=INK)
    box(ax, (0.5, 0.144), 0.94, 0.092,
        "3 equations, 3 unknowns  →  $f$, $d_A$, $d_B$ all SOLVED\n"
        "a detection probability with no known input anywhere: the\n"
        "substitute for the injection this corpus never had",
        face=SKY, edge=INK, fontsize=10.5, weight="bold")

    ax.text(0.5, 0.074,
            "EVERY $d$ IS A MODEL OUTPUT, NOT A MEASUREMENT.",
            ha="center", va="top", fontsize=10, color=INK, fontweight="bold")
    ax.text(0.5, 0.046,
            "Injection measures $d$ against a signal you put there; this INFERS "
            "$d$ from an\nassumption that the two chains fail independently.  If "
            "that assumption is wrong,\nso is every $d$ on the right-hand panel.",
            ha="center", va="top", fontsize=9, color=MUTED, linespacing=1.45,
            style="italic")


MARKS = [("f", "o", INK, "sky-occupancy $f$  (SOLVED)"),
         ("d_a", "^", BLUE, "detection $d_A$ — MODEL OUTPUT"),
         ("d_b", "v", ORANGE, "detection $d_B$ — MODEL OUTPUT")]
GEOMETRY_FILL = {"opposite-edge": "full", "same-edge": "none"}
TEXT_TOP = -0.70
TEXT_FLOOR = -3.05


def recovered(ax, data: dict) -> None:
    """f, dA and dB per algorithm and per geometry, as solved on the corpus."""
    per_method = data["per_method"]
    geometries = [g for g in ("opposite-edge", "same-edge")
                  if g in data["geometries"]]
    rows = [m for m in ORDER if m in data["methods"]]
    y_of = {method: len(rows) - 1 - index for index, method in enumerate(rows)}

    for index, method in enumerate(rows):
        if index % 2 == 0:
            ax.axhspan(y_of[method] - 0.5, y_of[method] + 0.5,
                       color=BAND, alpha=0.5, zorder=0)

    band = data["spread"]["opposite-edge"]
    ax.axvspan(band["f_min"], band["f_max"], color=BLUE, alpha=0.14, zorder=1)
    for edge in (band["f_min"], band["f_max"]):
        ax.axvline(edge, color=BLUE, linewidth=1.0, alpha=0.55, zorder=1)

    for geometry in geometries:
        fill = GEOMETRY_FILL[geometry]
        offset = 0.17 if geometry == "opposite-edge" else -0.17
        for method in rows:
            solved = per_method[method][geometry]
            if not solved.get("solvable"):
                continue
            for key, marker, colour, _ in MARKS:
                ax.plot(solved[key], y_of[method] + offset, marker=marker,
                        markersize=8.5, linestyle="none", color=colour,
                        markeredgecolor=colour, markeredgewidth=1.5,
                        markerfacecolor=colour if fill == "full" else SURFACE,
                        zorder=3)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(list(reversed(rows)), fontsize=10.5)
    ax.set_ylim(TEXT_FLOOR, len(rows) - 0.35)
    ax.set_xlim(0.0, 1.0)
    ax.set_xlabel("probability   (dimensionless, 0–1)")
    ax.set_xticks(np.arange(0.0, 1.01, 0.1))
    ax.grid(axis="x", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.axhline(TEXT_TOP + 0.12, color=GRID, linewidth=1.0, zorder=1)

    pooled = data["spread"]["pooled"]
    ax.set_title("SOLVED FROM THE REAL CORPUS, lnb-a INCLUDED —\n"
                 "the one sky parameter still does not come out as one number\n"
                 f"n = {pooled['cells']:,} joined cells",
                 pad=10, fontsize=11.5, linespacing=1.45)

    ax.annotate(
        "$f$ is a property of the SKY. All eight read\n"
        "the same sky at the same instant, so all\n"
        "eight must return the SAME $f$. On opposite-\n"
        f"edge cells (band) they return {band['f_min']:.3f}–{band['f_max']:.3f},\n"
        f"spread {band['f_spread']:.3f}: the model's OWN check, unmet.",
        xy=((band["f_min"] + band["f_max"]) / 2, TEXT_TOP + 0.16),
        xytext=(0.020, TEXT_TOP - 0.18), textcoords="data",
        ha="left", va="top", fontsize=9.5, color=INK, linespacing=1.45,
        arrowprops={"arrowstyle": "->", "color": BLUE, "linewidth": 1.4,
                    "connectionstyle": "arc3,rad=0.20"})

    d_lo, d_hi = band["d_min"], band["d_max"]
    ax.annotate(
        "every $d$ on this panel is a MODEL\n"
        "OUTPUT, never a measured quantity\n"
        f"({d_lo:.2f}–{d_hi:.2f}). Nothing was injected, so\n"
        "no $d$ here has ever been checked\n"
        "against a known input.",
        xy=(0.5 * (d_lo + d_hi), TEXT_TOP + 0.16),
        xytext=(0.980, TEXT_TOP - 0.18), textcoords="data",
        ha="right", va="top", fontsize=9.5, color=INK, linespacing=1.45,
        arrowprops={"arrowstyle": "->", "color": MUTED, "linewidth": 1.4,
                    "connectionstyle": "arc3,rad=-0.20"})

    quantity = [plt.Line2D([], [], linestyle="none", marker=marker, color=colour,
                           markersize=8.5, markeredgecolor=colour,
                           markeredgewidth=1.5, label=label)
                for _, marker, colour, label in MARKS]
    geometry_keys = [
        plt.Line2D([], [], linestyle="none", marker="o", color=MUTED,
                   markersize=8.5, markerfacecolor=MUTED, markeredgecolor=MUTED,
                   label="filled = opposite-edge"),
        plt.Line2D([], [], linestyle="none", marker="o", color=MUTED,
                   markersize=8.5, markerfacecolor=SURFACE,
                   markeredgecolor=MUTED, markeredgewidth=1.5,
                   label="open = same-edge"),
        plt.Line2D([], [], linestyle="none", marker="none", label=" ")]
    handles = [item for pair in zip(quantity, geometry_keys) for item in pair]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.098),
              ncol=3, frameon=False, fontsize=9.5, handletextpad=0.5,
              columnspacing=2.0, labelspacing=0.5)


def by_receiver_pair(ax, data: dict) -> None:
    """f per algorithm per receiver pair — four pairs now, not two."""
    per_method = data["per_method"]
    rows = [m for m in ORDER if m in data["methods"]]
    pairs = data["receiver_pairs"]
    y_of = {method: len(rows) - 1 - index for index, method in enumerate(rows)}
    colours = {"lnb-c|lnb-a": BLUE, "lnb-c|lnb-b": TEAL,
               "lnb-d|lnb-a": ORANGE, "lnb-d|lnb-b": MUTED}
    markers = {"lnb-c|lnb-a": "o", "lnb-c|lnb-b": "s",
               "lnb-d|lnb-a": "D", "lnb-d|lnb-b": "^"}
    offsets = np.linspace(0.24, -0.24, len(pairs))

    for index, method in enumerate(rows):
        if index % 2 == 0:
            ax.axhspan(y_of[method] - 0.5, y_of[method] + 0.5, color=BAND,
                       alpha=0.5, zorder=0)
    for name, offset in zip(pairs, offsets):
        published = name in PUBLISHED["population"]["receiver_pairs"]
        for method in rows:
            solved = per_method[method][name]
            if not solved.get("solvable"):
                continue
            ax.plot(solved["f"], y_of[method] + offset,
                    marker=markers.get(name, "o"), markersize=7.5,
                    linestyle="none", color=colours.get(name, INK),
                    markeredgecolor=colours.get(name, INK), markeredgewidth=1.5,
                    markerfacecolor=colours.get(name, INK) if published
                    else SURFACE, zorder=3)

    handles = [plt.Line2D([], [], linestyle="none", marker=markers.get(n, "o"),
                          color=colours.get(n, INK), markersize=7.5,
                          markeredgecolor=colours.get(n, INK),
                          markeredgewidth=1.5,
                          markerfacecolor=colours.get(n, INK)
                          if n in PUBLISHED["population"]["receiver_pairs"]
                          else SURFACE,
                          label=n + ("" if n in
                                     PUBLISHED["population"]["receiver_pairs"]
                                     else "   (new: lnb-a)"))
               for n in pairs]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.098),
              ncol=2, frameon=False, fontsize=9.5, handletextpad=0.5,
              columnspacing=1.6, labelspacing=0.45)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([])
    ax.set_ylim(TEXT_FLOOR, len(rows) - 0.35)
    ax.set_xlim(0.245, 0.455)
    ax.set_xticks([0.25, 0.30, 0.35, 0.40, 0.45])
    ax.set_xlabel("sky-occupancy $f$   (dimensionless, 0–1)")
    ax.grid(axis="x", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.axhline(TEXT_TOP + 0.12, color=GRID, linewidth=1.0, zorder=1)
    per_pair = {n: data["census"]["cells_per_receiver_pair"][n] for n in pairs}
    lo = min(data["spread"][n]["f_min"] for n in pairs)
    hi = max(data["spread"][n]["f_max"] for n in pairs)
    ax.text(0.250, TEXT_TOP - 0.18,
            "$f$ is meant to be ONE SKY, so it cannot\n"
            "depend on which two ports look at it.\n"
            f"Across the four pairs it runs {lo:.3f}–{hi:.3f},\n"
            "wider than the spread across the eight\n"
            "algorithms. The two pairs the published\n"
            "figure had (filled) are the two highest.",
            ha="left", va="top", fontsize=9.5, color=INK, linespacing=1.45)
    ax.set_title("ONE SKY, FOUR RECEIVER PAIRS —\n"
                 "and $f$ depends on which pair of ports you ask\n"
                 f"n = {min(per_pair.values()):,} cells per pair",
                 pad=10, fontsize=11.5, linespacing=1.45)


def plot(data: dict):
    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 11.5, "axes.titlesize": 12.0,
        "xtick.labelsize": 10, "ytick.labelsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    fig = plt.figure(figsize=(17.4, 11.4), dpi=150, facecolor=SURFACE)
    grid = fig.add_gridspec(1, 3, width_ratios=[0.92, 1.16, 0.80], wspace=0.30,
                            left=0.030, right=0.982, top=0.812, bottom=0.290)
    schematic(fig.add_subplot(grid[0]))
    recovered(fig.add_subplot(grid[1]), data)
    by_receiver_pair(fig.add_subplot(grid[2]), data)

    fig.suptitle(
        "WITH NOTHING INJECTED, TWO CHAINS THAT SHARE ONLY THE SKY ARE THE "
        "SUBSTITUTE: THREE EQUATIONS RECOVER $d$ —\n"
        "IF THE ONE SKY THEY ASSUME IS REALLY THERE.  EVERY $d$ BELOW IS A "
        "MODEL OUTPUT, NOT A MEASUREMENT.",
        fontsize=14.0, fontweight="bold", color=INK, y=0.985, linespacing=1.45)

    census = data["census"]
    pooled = data["spread"]["pooled"]
    was = PUBLISHED["population"]
    lines = [
        f"n = {pooled['cells']:,} joined matched-arm cells "
        f"({data['spread']['opposite-edge']['cells']:,} opposite-edge, "
        f"{data['spread']['same-edge']['cells']:,} same-edge) from "
        f"{census['matched_arm_sweeps']:,} matched-arm sweeps of "
        f"{census['paired_sweeps']:,} paired sweeps — "
        f"{len(data['receiver_pairs'])} receiver pairs, "
        f"{min(census['cells_per_receiver_pair'].values()):,} cells each.  "
        "lnb-a IS INCLUDED in target AND null.",

        f"The published run excluded it and had {was['matched_arm_cells']:,} "
        f"cells in only two receiver pairs ({', '.join(was['receiver_pairs'])}), "
        "so chain B was one physical port.  $p$ is measured per method on "
        f"{census['null_observations']:,} live cross-edge null observations; it "
        "moves when",

        "lnb-a returns, and it has to — a $p$ without lnb-a's null arm is the "
        "wrong chain's empty-sky rate for half these cells.  WATER IS CONFOUNDED "
        "WITH RADIO: chain A is always pluto-19f2 (lnb-c or lnb-d, both WET) and "
        "chain B always",

        "pluto-5d4d (lnb-a or lnb-b, both DRY), because the join orders a pair's "
        "radios by radio_id, so any gap between $d_A$ and $d_B$ is a "
        "radio-and-water difference at once and this corpus has no dry port on "
        "19f2 or wet port on 5d4d to break the tie.",

        "Cells from cross_radio.join_cells; thresholds from null_thresholds; $p$ "
        "from cell_false_alarm; $f$, $d_A$, $d_B$ from solve_coincidence — all "
        "unmodified.  The frozen verdicts behind every count were checked against "
        "cross_radio._rates ("
        + ("exact" if data["source"]["frozen_verdicts_match_cross_radio_rates"]["exact"]
           else "MISMATCH — see JSON") + ").",

        f"Corpus frozen {census['measured_utc']} at "
        f"{census['scored_sidecars']:,} scored sidecars (digest "
        f"{census['scored_digest']}) of {census['corpus_entries']:,} entries, "
        f"{census['sweeps_on_share']:,} sweeps on the share.  The published run "
        f"used a different freeze ({was['scored_sidecars']:,} sidecars, digest "
        f"{was['scored_digest']})",

        "whose cell table no longer exists on disk, so this figure is rebuilt on "
        "the freeze the rest of the set uses; the published values are carried in "
        "the JSON for comparison.",
    ]
    block = data["census_recheck_at_end_of_run"]
    if block.get("delta"):
        lines.append(
            "DRIFT while computing: scored sidecars "
            f"{block['frozen_at_start']['scored_sidecars']:,} → "
            f"{block['measured_at_end']['scored_sidecars']:,} "
            f"({block['delta']['scored_sidecars']:+,}), "
            f"{block['scored_removed']} removed, {block['sweeps_added']} new "
            "sweeps (collection paused).  This figure uses the FROZEN list only.")
    fig.text(0.5, 0.012, "\n".join(lines), ha="center", va="bottom",
             fontsize=8.6, color=MUTED, linespacing=1.52)

    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    data = compute()
    plot(data)
    OUT_JSON.write_text(json.dumps(data, indent=1, sort_keys=False,
                                   default=str) + "\n")
    print("wrote", OUT_PNG, "and", OUT_JSON)
    for name in ("opposite-edge", "same-edge", "pooled"):
        print(f"-- {name}")
        for method in ORDER:
            got = data["per_method"][method][name]
            print(f"   {method:>19}  n {got['cells']:>6}  f {got['f']:.4f}"
                  f"  dA {got['d_a']:.4f}  dB {got['d_b']:.4f}")
        print("   spread", json.dumps(data["spread"][name]))
    for name in data["receiver_pairs"]:
        block = data["spread"][name]
        print(f"-- {name}: f {block['f_min']:.4f}-{block['f_max']:.4f} "
              f"on {block['cells']:,} cells")
    print("verify", json.dumps(data["verification_against_the_published_run"],
                               indent=1))


if __name__ == "__main__":
    main()
