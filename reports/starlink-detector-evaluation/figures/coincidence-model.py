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
cross-edge null arm; f, dA and dB are SOLVED.

RIGHT is real: every f, dA and dB there is solved by the repository's own
``cross_radio.solve_coincidence`` from counts on the joined matched-arm cells,
per algorithm and per geometry, out of ../cache/firerate-coincidence.npz.

f is a property of the SKY.  The eight algorithms read one sky, so one f: that
invariance is the model's own consistency check and the figure states it, marks
the observed spread, and says plainly that every d is a model output.

Usage:  python3 coincidence-model.py
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

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.cross_radio import solve_coincidence  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache" / "firerate-coincidence.npz"
OUT_PNG = HERE / "coincidence-model.png"
OUT_JSON = HERE / "coincidence-model.json"

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"
BAND, SKY = "#eceae5", "#e3ecf7"
BLUE, ORANGE = "#2a78d6", "#eb6834"

#: Seriated order used by every figure in the 2026-08-14 cross-radio set, so a
#: reader carries one row order between them.
ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
         "full-frame-full", "full-frame-acquire", "differential-32",
         "differential-16"]

#: The authoritative full-corpus run's opposite-edge values, carried into the
#: JSON so the drift between that population and this one is visible rather
#: than quietly absorbed.  NOT plotted.
REPORTED_OPPOSITE_EDGE = {
    "anchor-8": {"f": 0.346, "d_a": 0.810, "d_b": 0.752},
    "glrt-32": {"f": 0.344, "d_a": 0.814, "d_b": 0.766},
    "full-frame-full": {"f": 0.388, "d_a": 0.778, "d_b": 0.728},
}


#: Scoring runs while these figures are built, so the census is frozen once at
#: the start (snapshot.py) and re-measured at the end.  The re-measurement is
#: reported here rather than hidden: the figures use the FROZEN list, so a
#: sidecar scored mid-run cannot move one figure and not the other.
DRIFT = (HERE.parent / "firerate" / "work" / "drift.json")


def drift() -> dict:
    import json as _json
    if DRIFT.is_file():
        return _json.loads(DRIFT.read_text())
    return {"checked": False,
            "note": "no end-of-run re-measurement available"}


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def estimate(dec_a, dec_b, keep, p: float) -> dict:
    """Counts on one slice, handed to the repository's estimator."""
    usable = keep & (dec_a >= 0) & (dec_b >= 0)
    n = int(usable.sum())
    if not n:
        return {"cells": 0, "solvable": False, "reason": "no usable cells",
                "f": None, "d_a": None, "d_b": None}
    a, b = dec_a[usable] == 1, dec_b[usable] == 1
    solved = solve_coincidence(float(a.mean()), float(b.mean()),
                               float((a & b).mean()), p)
    return {**solved, "cells": n, "fired_a": int(a.sum()),
            "fired_b": int(b.sum()), "both": int((a & b).sum())}


def compute() -> dict:
    blob = np.load(CACHE, allow_pickle=False)
    methods = [str(m) for m in blob["methods"]]
    census = json.loads(str(blob["census"]))
    geometry = np.array([str(g) for g in blob["geometry"]])
    geometries = sorted(set(geometry.tolist()))

    slices = {"pooled": np.ones(len(geometry), dtype=bool)}
    for name in geometries:
        slices[name] = geometry == name

    per_method: dict = {}
    for index, method in enumerate(methods):
        p = float(blob["fa_count"][index]) / float(blob["fa_cells"][index])
        per_method[method] = {
            "false_alarm_rate_p": p,
            "null_fires": int(blob["fa_count"][index]),
            "null_observations": int(blob["fa_cells"][index]),
        }
        for name, keep in slices.items():
            per_method[method][name] = estimate(
                blob["dec_a"][:, index], blob["dec_b"][:, index], keep, p)

    spread = {}
    for name in slices:
        values = {m: per_method[m][name]["f"] for m in methods
                  if per_method[m][name].get("solvable")}
        spread[name] = {
            "cells": max((per_method[m][name]["cells"] for m in methods),
                         default=0),
            "methods_solved": len(values),
            "f_min": min(values.values()) if values else None,
            "f_max": max(values.values()) if values else None,
            "f_spread": (max(values.values()) - min(values.values())
                         if values else None),
            "f_argmin": min(values, key=values.get) if values else None,
            "f_argmax": max(values, key=values.get) if values else None,
            "d_min": min(min(per_method[m][name]["d_a"],
                             per_method[m][name]["d_b"])
                         for m in values) if values else None,
            "d_max": max(max(per_method[m][name]["d_a"],
                             per_method[m][name]["d_b"])
                         for m in values) if values else None,
        }

    verify = {}
    for method, reported in REPORTED_OPPOSITE_EDGE.items():
        got = per_method[method].get("opposite-edge", {})
        verify[method] = {
            "reported": reported,
            "recomputed": {key: got.get(key) for key in ("f", "d_a", "d_b")},
            "delta": {key: (None if got.get(key) is None
                            else got[key] - reported[key])
                      for key in ("f", "d_a", "d_b")},
        }

    return {
        "figure": "coincidence-model",
        "model": {"P(A)": "f*dA + (1-f)*p", "P(B)": "f*dB + (1-f)*p",
                  "P(AB)": "f*dA*dB + (1-f)*p^2",
                  "solver": "leo_tracker.radio.beacon.cross_radio."
                            "solve_coincidence",
                  "counted": ["P(A)", "P(B)", "P(AB)"],
                  "measured": ["p (cross-edge null arm, per cell)"],
                  "solved": ["f", "dA", "dB"],
                  "status_of_d": "MODEL OUTPUT. There is no injection in this "
                                 "corpus; d is never measured against a known "
                                 "input."},
        "invariance_check": "f is a property of the sky, so every algorithm "
                            "must return the same f. The spread across the "
                            "eight is the model's own consistency check.",
        "cache": str(CACHE),
        "census": census,
        "census_recheck_at_end_of_run": drift(),
        "methods": methods,
        "row_order": ORDER,
        "geometries": geometries,
        "per_method": per_method,
        "spread": spread,
        "verification_against_authoritative_run": verify,
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
    ax.text(0.5, 0.985,
            "S C H E M A T I C   —   N O   D A T A   O N   T H I S   P A N E L",
            ha="center", va="top", fontsize=9.5, color=MUTED, fontweight="bold")

    box(ax, (0.5, 0.888), 0.80, 0.105,
        "ONE SKY CELL\none channel edge, one instant\n"
        "occupied with probability $f$",
        face=SKY, edge=BLUE, fontsize=10.5)

    arrow(ax, (0.34, 0.833), (0.215, 0.762), colour=BLUE)
    arrow(ax, (0.66, 0.833), (0.785, 0.762), colour=ORANGE)
    ax.text(0.215, 0.803, "detects\nw.p. $d_A$", ha="right", va="center",
            fontsize=10, color=BLUE, linespacing=1.3)
    ax.text(0.785, 0.803, "detects\nw.p. $d_B$", ha="left", va="center",
            fontsize=10, color=ORANGE, linespacing=1.3)

    box(ax, (0.20, 0.700), 0.36, 0.115,
        "CHAIN A\nlnb-c / lnb-d\nown Pluto, own USB bus",
        face=SURFACE, edge=BLUE, fontsize=10)
    box(ax, (0.80, 0.700), 0.36, 0.115,
        "CHAIN B\nlnb-b\nown Pluto, own USB bus",
        face=SURFACE, edge=ORANGE, fontsize=10)
    ax.annotate("", xy=(0.388, 0.700), xytext=(0.612, 0.700),
                arrowprops={"arrowstyle": "<->", "color": MUTED,
                            "linewidth": 1.2})
    ax.text(0.5, 0.628, "they share ONLY the sky", ha="center", va="top",
            fontsize=9.5, color=MUTED, style="italic")

    arrow(ax, (0.20, 0.6425), (0.20, 0.586), colour=MUTED, width=1.1)
    arrow(ax, (0.80, 0.6425), (0.80, 0.586), colour=MUTED, width=1.1)
    box(ax, (0.5, 0.545), 0.92, 0.078,
        "empty cell: EITHER chain still fires with probability $p$\n"
        "$p$ is MEASURED on the cross-edge null arm, per cell",
        face=BAND, edge=MUTED, fontsize=10)

    arrow(ax, (0.5, 0.506), (0.5, 0.470), colour=INK)

    ax.add_patch(FancyBboxPatch(
        (0.045, 0.185), 0.91, 0.277,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.6, facecolor=SURFACE, edgecolor=INK, zorder=2))
    ax.text(0.5, 0.446, "what the two chains are then seen to do",
            ha="center", va="top", fontsize=9.5, color=MUTED, zorder=3)
    for row, (label, expression) in enumerate((
            ("$P(A)$", r"$= f\,d_A + (1-f)\,p$"),
            ("$P(B)$", r"$= f\,d_B + (1-f)\,p$"),
            ("$P(AB)$", r"$= f\,d_A d_B + (1-f)\,p^{2}$"))):
        y = 0.390 - row * 0.063
        ax.text(0.335, y, label, ha="right", va="center", fontsize=13,
                color=INK, zorder=3)
        ax.text(0.360, y, expression, ha="left", va="center", fontsize=13,
                color=INK, zorder=3)
    ax.text(0.5, 0.207,
            "COUNTED on the corpus: $P(A)$, $P(B)$, $P(AB)$      "
            "MEASURED: $p$",
            ha="center", va="center", fontsize=9, color=MUTED, zorder=3)

    arrow(ax, (0.5, 0.185), (0.5, 0.150), colour=INK)
    box(ax, (0.5, 0.100), 0.94, 0.098,
        "3 equations, 3 unknowns  →  $f$, $d_A$, $d_B$ all SOLVED\n"
        "a detection probability with no known input anywhere: the\n"
        "substitute for the injection this corpus never had",
        face=SKY, edge=INK, fontsize=10.5, weight="bold")

    ax.text(0.5, 0.033,
            "A substitute, not an equal. Injection measures $d$ against a signal you\n"
            "put there; this INFERS $d$ from an assumption that the two chains fail\n"
            "independently. If that assumption is wrong, so is every $d$ beside it.",
            ha="center", va="top", fontsize=9, color=MUTED, linespacing=1.45,
            style="italic")


MARKS = [("f", "o", INK, "sky-occupancy $f$"),
         ("d_a", "^", BLUE, "detection $d_A$ (chain A)"),
         ("d_b", "v", ORANGE, "detection $d_B$ (chain B)")]
GEOMETRY_FILL = {"opposite-edge": "full", "same-edge": "none"}

#: Rows sit at y = 0, 1, ... 7; everything below TEXT_TOP is annotation space,
#: so the two callouts never land on a marker.
TEXT_TOP = -0.70
TEXT_FLOOR = -2.95


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
    ax.axvspan(band["f_min"], band["f_max"], ymin=0.0, ymax=1.0,
               color=BLUE, alpha=0.14, zorder=1)
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

    ax.set_title("SOLVED FROM THE REAL CORPUS — and the one sky parameter\n"
                 "does not come out as one number", pad=9, fontsize=12.5)

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
    # Interleaved so that matplotlib's column-major fill puts the three
    # quantities on one row and the two geometry keys on the next.
    handles = [item for pair in zip(quantity, geometry_keys) for item in pair]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.115),
              ncol=3, frameon=False, fontsize=9.5, handletextpad=0.5,
              columnspacing=2.2, labelspacing=0.5)


def plot(data: dict) -> None:
    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 11.5, "axes.titlesize": 12.5,
        "xtick.labelsize": 10, "ytick.labelsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(13.2, 9.2), dpi=150,
        gridspec_kw={"width_ratios": [1.0, 1.10], "wspace": 0.30},
        facecolor=SURFACE)
    fig.subplots_adjust(left=0.012, right=0.988, top=0.820, bottom=0.205)

    schematic(left)
    recovered(right, data)

    fig.suptitle("WITH NOTHING INJECTED, TWO CHAINS THAT SHARE ONLY THE SKY ARE THE SUBSTITUTE:\n"
                 "THREE EQUATIONS RECOVER $d$ — IF THE ONE SKY THEY ASSUME IS REALLY THERE",
                 fontsize=14.5, fontweight="bold", color=INK, y=0.988,
                 linespacing=1.45)

    census = data["census"]
    pooled = data["spread"]["pooled"]
    for y, line in (
        (0.065, f"n = {pooled['cells']:,} joined matched-arm cells "
                f"({data['spread']['opposite-edge']['cells']:,} opposite-edge, "
                f"{data['spread']['same-edge']['cells']:,} same-edge) from "
                f"{census['matched_arm_sweeps']:,} matched-arm sweeps of "
                f"{census['paired_sweeps']:,} paired sweeps."),
        (0.040, f"$p$ is measured per method on {census['null_observations']:,} live "
                "cross-edge null observations.  lnb-a excluded from target AND null "
                "(dead port, flat ~1.19 at every tuning).  "
                "$f$, $d_A$, $d_B$ from cross_radio.solve_coincidence."),
        (0.015, f"Corpus frozen {census['measured_utc']} at "
                f"{census['scored_sidecars']:,} scored sidecars "
                f"(digest {census['scored_digest']}) of {census['corpus_entries']:,} "
                f"entries, {census['sweeps_on_share']:,} sweeps on the share; "
                "scoring still running, drift reported with the figure."),
    ):
        fig.text(0.5, y, line, ha="center", va="bottom", fontsize=8.5,
                 color=MUTED)

    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    data = compute()
    plot(data)
    OUT_JSON.write_text(json.dumps(data, indent=1, sort_keys=False) + "\n")
    print("wrote", OUT_PNG, "and", OUT_JSON)
    for geometry in ("opposite-edge", "same-edge", "pooled"):
        print(f"-- {geometry}")
        for method in ORDER:
            got = data["per_method"][method][geometry]
            print(f"   {method:>19}  n {got['cells']:>6}  f {got['f']:.4f}"
                  f"  dA {got['d_a']:.4f}  dB {got['d_b']:.4f}")
        print("   spread", json.dumps(data["spread"][geometry]))
    print("verify", json.dumps(data["verification_against_authoritative_run"],
                               indent=1))


if __name__ == "__main__":
    main()
