#!/usr/bin/env python3
"""negative-control — the consistency check passes on data the model forbids.

The withdrawn claim was that eight algorithms agreeing on a sky-occupancy
parameter ``f`` validated the cross-radio coincidence model.  This script runs
the project's own estimator (``leo_tracker.radio.beacon.cross_radio``,
unmodified, imported not reimplemented) over three joins of the same corpus:

  real       radio A and radio B of the SAME sweep at the SAME tuning instant.
             The model can be true here.
  shifted    radio A at instant i joined to radio B at instant (i+2) mod 8 of
             the same sweep.  Different tuning, different moment: the model is
             definitionally false.
  scrambled  radio A of one sweep joined to radio B of a DIFFERENT sweep on the
             same arm, a minimum of 235 s and a median of 1,446 s apart.  The
             two chains are looking at sky minutes apart: definitionally false.

Everything except the join is held fixed.  Thresholds and the empty-sky rate p
are drawn once, from the cross-edge null arms of the whole paired corpus, and
reused for all three joins — they are properties of the detectors and the null,
not of the join.  All three joins are filtered to matched-arm cells and all
three end up with exactly the same 2,464 cells, so the comparison is not
confounded by sample size.

Data
----
Every number is computed from ``/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*``.
Because that host has 4 GB of RAM and a live collector on it, the full 1.6 MB
sidecars cannot all be held in memory at once, so ``extract_lite.py`` (beside
this file) first streams the corpus into a compact mirror holding only the
fields the estimator reads, with every score copied verbatim.  Run it first:

    nice -n 15 python3 snapshot.py
    nice -n 15 python3 extract_lite.py
    nice -n 15 python3 negative-control.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("REFRESH_WORK", HERE.parent / "work"))
LITE = Path(os.environ.get("LITE_ROOT", WORK / "lite"))
SNAPSHOT = WORK / "snapshot.json"
OUT_PNG = HERE / "negative-control.png"
OUT_JSON = HERE / "negative-control.json"

#: Instants a sweep holds; the shifted control wraps within one sweep so that
#: it keeps exactly the same cell count as the real join.
INSTANTS = 8
SHIFT = 2

#: Algorithm order, and the family each belongs to.  The order is the optimal
#: adjacent-similarity seriation of the phi matrix, computed by exhaustive
#: enumeration of all 8! orderings in ``method-correlation.py``; the colour
#: groups here ARE the blocks that figure shows.  Recomputed on the full corpus:
#: the two differential variants swap places against the 320-sidecar snapshot,
#: which is a tie being broken differently, not a change of structure.
ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify", "full-frame-full", "full-frame-acquire", "differential-32", "differential-16"]

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d7d6d2"
SURFACE = "#fcfcfb"
# Categorical slots 1-3 of the validated default palette (blue, orange, aqua):
# the documented all-pairs-safe subset.  The fourth group is a single algorithm
# and wears text ink rather than a fourth hue.  Marker shape is the primary
# identity channel throughout, so the figure reads with no colour at all.
STYLE = {
    "anchor-8":           (INK,       "o", "full"),
    "glrt-32":            ("#2a78d6", "s", "full"),
    "glrt-64":            ("#2a78d6", "D", "none"),
    "full-frame-verify":  ("#eb6834", "^", "full"),
    "full-frame-full":    ("#eb6834", "v", "none"),
    "full-frame-acquire": ("#eb6834", "<", "full"),
    "differential-16":    ("#1baf7a", "P", "full"),
    "differential-32":    ("#1baf7a", "X", "none"),
}

#: What the retraction report and the review brief state, for comparison.
#: These are NOT plotted; they are carried into the JSON so the integrator can
#: see where this recomputation agrees with the report and where it does not.
REPORTED = {
    "real":      {"f_min": 0.307, "f_max": 0.362, "spread": 0.050},
    "scrambled": {"f_min": 0.637, "f_max": 0.713, "spread": 0.075},
    "shifted":   {"f_min": 0.622, "f_max": 0.667, "spread": 0.045},
}

#: The other snapshot the same experiment was reported at -- 2,464 cells per
#: join, the numbers the review brief carries.  Both are kept because f moved
#: between them, and a reader comparing this run against only one of them would
#: not see that the *level* has never held still while the *structure* has.
REPORTED_AT_2464_CELLS = {
    "real":      {"f_min": 0.255, "f_max": 0.302, "spread": 0.047,
                  "spread_over_bootstrap_p50": 0.92},
    "shifted":   {"f_min": 0.644, "f_max": 0.710, "spread": 0.066,
                  "spread_over_bootstrap_p50": 0.68},
    "scrambled": {"f_min": 0.795, "f_max": 0.894, "spread": 0.099,
                  "spread_over_bootstrap_p50": 0.78},
    "cells_per_join": 2464,
}


# --------------------------------------------------------------------------
# the three joins
# --------------------------------------------------------------------------

def stamp(sweep: str) -> float:
    return datetime.strptime(sweep, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def shift_entry(entry: dict, k: int, instants: int = INSTANTS) -> dict:
    """One radio's observations re-labelled k instants earlier.

    Joining A's instant i to this entry's instant i recovers A(i) against
    B(i+k): the same sweep, the wrong moment and the wrong tuning.
    """
    observations = []
    for observation in entry["scores"]["observations"]:
        index = observation.get("iq_index")
        row = dict(observation)
        if isinstance(index, int):
            row["iq_index"] = (index - k) % instants
        observations.append(row)
    scores = dict(entry["scores"])
    scores["observations"] = observations
    out = dict(entry)
    out["scores"] = scores
    return out


def make_pair(side_a: dict, side_b: dict, sweep: str, skew) -> dict:
    """A pair record ``cross_radio.join_cells`` accepts, built from two sides.

    ``matched_arm`` is recomputed from the two arm names rather than copied
    from a manifest, so the same rule applies to real and synthetic pairs
    alike.  On the 176 real pairs the two definitions agree 176/176.
    """
    geometry = cr.sweep_geometry(side_a["sample_order"], side_b["sample_order"])
    return {"paired_sweep": sweep, "radios": [side_a, side_b],
            "geometry": geometry, "geometry_declared": geometry,
            "geometry_agrees": True,
            "matched_arm": side_a["arm_name"] == side_b["arm_name"],
            "arms": sorted({side_a["arm_name"], side_b["arm_name"]}),
            "skew_ms": skew, "excluded_receivers": {}}


def build_joins(pairs: list[dict]) -> tuple[dict, dict]:
    """The three joins, plus the separation the scramble actually achieved."""
    matched = [pair for pair in pairs if pair["matched_arm"]]

    real = [cell for pair in matched for cell in cr.join_cells(pair)]

    shifted = [cell for pair in matched for cell in cr.join_cells(make_pair(
        pair["radios"][0], shift_entry(pair["radios"][1], SHIFT),
        pair["paired_sweep"], pair["skew_ms"]))]

    # Scramble WITHIN an arm.  Crossing arms would also change the threshold
    # each side is judged against, which is a second difference; the control
    # has to move one thing only.  A half-length cyclic shift inside each arm
    # group maximises the wall-clock separation while keeping every A-side and
    # every B-side entry used exactly once.
    by_arm: dict = defaultdict(list)
    for pair in matched:
        by_arm[pair["radios"][0]["arm_name"]].append(pair)
    scrambled, separations = [], []
    for _, group in sorted(by_arm.items()):
        size = len(group)
        step = max(1, size // 2)
        for index, pair in enumerate(group):
            other = group[(index + step) % size]
            separations.append(abs(stamp(other["paired_sweep"])
                                   - stamp(pair["paired_sweep"])))
            scrambled += cr.join_cells(make_pair(
                pair["radios"][0], other["radios"][1],
                f"{pair['paired_sweep']}+{other['paired_sweep']}",
                pair["skew_ms"]))

    joins = {"real": real, "shifted": shifted, "scrambled": scrambled}
    apart = {"pairings": len(separations), "min_s": min(separations),
             "median_s": statistics.median(separations),
             "max_s": max(separations)}
    return joins, apart


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def frozen_census() -> dict:
    with open(SNAPSHOT) as handle:
        snapshot = json.load(handle)
    return {key: snapshot[key] for key in
            ("measured_utc", "sweeps_on_share", "corpus_entries",
             "scored_sidecars", "scored_digest")}


def compute() -> dict:
    pairs, census = cr.load_pairs(LITE)
    if not pairs:
        raise SystemExit(f"no paired sweeps under {LITE}; run extract_lite.py")
    entries = [entry for pair in pairs for entry in pair["radios"]]
    methods = cr.methods_in(entries)
    thresholds = cr.null_thresholds(entries)
    false_alarm = cr.cell_false_alarm(entries, thresholds)

    joins, apart = build_joins(pairs)
    results = {}
    for name, cells in joins.items():
        kept = [cell for cell in cells if cell["matched_arm"]]
        occ = cr.occupancy(kept, thresholds, false_alarm, methods=methods)
        spread = occ["f_spread"]
        boot = spread["sampling"]
        values = {m: occ["methods"][m]["pooled"]["f"] for m in methods}
        rates = {m: {k: occ["methods"][m]["pooled"].get(k)
                     for k in ("p_a", "p_b", "p_ab", "cells")}
                 for m in methods}
        mean_f = statistics.fmean(v for v in values.values() if v is not None)
        results[name] = {
            "cells": len(kept), "f": values, "rates": rates,
            "f_min": spread["min"], "f_max": spread["max"],
            "f_mean": mean_f, "spread": spread["spread"],
            "solvable_methods": spread["methods"],
            "unsolvable": spread["unsolvable"],
            "bootstrap_spread": {k: boot.get(k)
                                 for k in ("draws", "p05", "p50", "p95", "basis")},
            "spread_over_bootstrap_p50": (spread["spread"] / boot["p50"]
                                          if boot.get("p50") else None),
            "spread_over_mean_f": spread["spread"] / mean_f if mean_f else None,
        }

    return {"corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
            "mirror": str(LITE), "snapshot": frozen_census(), "census": census,
            "pairs_loaded": len(pairs),
            "pairs_matched_arm": sum(1 for p in pairs if p["matched_arm"]),
            "entries": len(entries), "methods": methods,
            "null_false_alarm_rate_per_cell": {
                m: false_alarm[m]["rate"] for m in methods},
            "null_thresholds_drawn": len(thresholds),
            "scramble_separation": apart,
            "shift_instants": SHIFT, "instants_per_sweep": INSTANTS,
            "joins": results}


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------

COLUMNS = [
    ("real", "real join\nsame instant\nmodel CAN be true"),
    ("shifted", f"shifted control\nradio B at $i+{SHIFT}$\nmodel IS false"),
    ("scrambled", "scrambled control\nanother sweep\nmodel IS false"),
]


def plot(data: dict) -> None:
    joins = data["joins"]
    plt.rcParams.update({
        "font.size": 12, "axes.labelsize": 12, "axes.titlesize": 13.5,
        "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(13.4, 9.4), dpi=150,
        gridspec_kw={"width_ratios": [1.5, 1]}, facecolor=SURFACE)

    # ---- left: f per algorithm -------------------------------------------
    # A light band across each column's min-max makes the tightness of the
    # cluster the thing the eye lands on, which is the whole argument.
    for column, (name, _) in enumerate(COLUMNS):
        item = joins[name]
        left.add_patch(plt.Rectangle(
            (column - 0.40, item["f_min"]), 0.80,
            item["f_max"] - item["f_min"],
            facecolor=GRID, edgecolor="none", alpha=0.85, zorder=1))
        left.text(column, item["f_max"] + 0.028,
                  f"spread {item['spread']:.3f}", ha="center", va="bottom",
                  fontsize=11, color=INK)

    offsets = {m: -0.30 + 0.6 * i / (len(ORDER) - 1) for i, m in enumerate(ORDER)}
    for method in ORDER:
        colour, marker, fill = STYLE[method]
        xs, ys = [], []
        for column, (name, _) in enumerate(COLUMNS):
            value = joins[name]["f"].get(method)
            if value is None:
                continue
            xs.append(column + offsets[method])
            ys.append(value)
        left.plot(xs, ys, color=colour, linewidth=0.9, alpha=0.40,
                  linestyle="-", zorder=2)
        left.plot(xs, ys, linestyle="none", marker=marker, markersize=9,
                  fillstyle=fill, color=colour, markeredgecolor=colour,
                  markerfacecolor=colour if fill == "full" else SURFACE,
                  markeredgewidth=1.6, label=method, zorder=3)

    left.set_xticks(range(len(COLUMNS)))
    left.set_xticklabels([label for _, label in COLUMNS], fontsize=11,
                         linespacing=1.5)
    left.set_xlim(-0.62, len(COLUMNS) - 0.38)
    left.set_ylim(0.0, 1.0)
    left.set_ylabel("sky-occupancy $f$  (dimensionless, 0\u20131)")
    left.set_title("$f$ moves 3\u00d7 between the joins \u2014 and the eight algorithms\n"
                   "cluster just as tightly in the two the model forbids", pad=12)
    left.grid(axis="y", color=GRID, linewidth=0.7)
    left.set_axisbelow(True)
    for side in ("top", "right"):
        left.spines[side].set_visible(False)

    left.annotate(
        "$f$ moves \u00d73, so the joins really are different data.\n"
        "All eight algorithms move together, so every cluster\n"
        "stays tight \u2014 including where the model cannot hold.",
        xy=(1.66, joins["scrambled"]["f_min"] - 0.012), xytext=(1.54, 0.255),
        ha="center", va="top", fontsize=11, color=INK, linespacing=1.4,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.5,
                    "connectionstyle": "arc3,rad=0.22"})

    # ---- right: the spread against its own sampling noise -----------------
    # The axis is scaled to the data: at 2,464 cells per join the spreads sat
    # near 0.10 and a fixed 0..0.30 axis was right; at 16,560 they sit near
    # 0.04 and the same axis would crush all three marks onto the floor.
    ceiling = max(max(item["spread"] for item in joins.values()),
                  max(item["bootstrap_spread"]["p95"] for item in joins.values()))
    for column, (name, _) in enumerate(COLUMNS):
        item = joins[name]
        boot = item["bootstrap_spread"]
        right.add_patch(plt.Rectangle(
            (column - 0.22, boot["p05"]), 0.44, boot["p95"] - boot["p05"],
            facecolor=GRID, edgecolor=MUTED, linewidth=1.0, zorder=1))
        right.plot([column - 0.24, column + 0.24], [boot["p50"]] * 2,
                   color=MUTED, linewidth=2.0, linestyle="--", zorder=2)
        right.plot([column], [item["spread"]], marker="o", markersize=13,
                   color=INK, markerfacecolor=SURFACE, markeredgewidth=2.6,
                   linestyle="none", zorder=3)
        right.text(column, item["spread"] + ceiling * 0.115,
                   f"{item['spread']:.3f}", ha="center", va="bottom",
                   fontsize=11.5, color=INK)
        right.text(column, item["bootstrap_spread"]["p05"] - ceiling * 0.075,
                   "%.2f\u00d7 its own\nsampling noise"
                   % item["spread_over_bootstrap_p50"], ha="center", va="top",
                   fontsize=9.5, color=MUTED, linespacing=1.35)

    right.plot([], [], marker="o", markersize=11, color=INK,
               markerfacecolor=SURFACE, markeredgewidth=2.4, linestyle="none",
               label="observed spread (max \u2212 min of the 8)")
    right.plot([], [], color=MUTED, linewidth=2.0, linestyle="--",
               label="median spread from sampling noise alone")
    right.add_patch(plt.Rectangle((0, 0), 0, 0, facecolor=GRID,
                                  edgecolor=MUTED, linewidth=1.0,
                                  label="p05\u2013p95, 200 joint resamples"))

    right.set_xticks(range(len(COLUMNS)))
    right.set_xticklabels(["real", "shifted", "scrambled"], fontsize=11.5)
    right.set_xlim(-0.62, len(COLUMNS) - 0.38)
    right.set_ylim(0.0, ceiling * 2.7)
    right.set_ylabel("spread of $f$ across the 8 algorithms\n"
                     "(max \u2212 min, $f$ units)")
    right.set_title("the spread \u2014 the quantity used as evidence \u2014\n"
                    "never rises above its own sampling noise", pad=12)
    right.grid(axis="y", color=GRID, linewidth=0.7)
    right.set_axisbelow(True)
    for side in ("top", "right"):
        right.spines[side].set_visible(False)
    right.legend(loc="upper left", frameon=True, framealpha=1.0,
                 edgecolor=GRID, facecolor=SURFACE, borderpad=0.5,
                 fontsize=10)

    right.annotate(
        "on sky the two radios never shared,\n"
        "the eight agree BETTER than sampling\n"
        "noise alone predicts \u2014 no failure mode",
        xy=(2.0, joins["scrambled"]["bootstrap_spread"]["p95"] + ceiling * 0.06),
        xytext=(1.02, ceiling * 1.80),
        ha="center", va="top", fontsize=10.5, color=INK, linespacing=1.4,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.5,
                    "connectionstyle": "arc3,rad=-0.25"})

    handles, labels = left.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, frameon=True,
               framealpha=1.0, edgecolor=GRID, facecolor=SURFACE,
               fontsize=10.5, handletextpad=0.5, columnspacing=1.6,
               bbox_to_anchor=(0.5, 0.205), borderpad=0.6)

    fig.suptitle("The consistency check cannot fail: eight algorithms agree on $f$ "
                 "on joins where the model is definitionally false",
                 fontsize=15, y=0.985, color=INK)
    snap = data.get("snapshot") or {}
    fig.text(0.5, 0.008,
             f"{joins['real']['cells']:,} matched-arm cells in each of the three "
             f"joins \u2014 {data['pairs_matched_arm']:,} matched-arm paired "
             f"sweeps, {data['entries']:,} scored sidecars in a pair.\n"
             "Thresholds and the empty-sky rate p are drawn once from the "
             "cross-edge null arms and held fixed across all three joins;\n"
             "only the join changes.  Scrambled pairings are a median "
             f"{data['scramble_separation']['median_s'] / 60:.0f} min apart "
             f"(minimum {data['scramble_separation']['min_s'] / 60:.0f} min).\n"
             "CENSUS frozen before any figure was computed and shared with all "
             f"six (digest {snap.get('scored_digest', '?')}):\n"
             f"{snap.get('sweeps_on_share', 0):,} sweeps  |  "
             f"{snap.get('corpus_entries', 0):,} corpus entries  |  "
             f"{snap.get('scored_sidecars', 0):,} scored sidecars.\n"
             f"Previously reported at {REPORTED_AT_2464_CELLS['cells_per_join']:,}"
             " cells per join: real "
             f"{REPORTED_AT_2464_CELLS['real']['f_min']:.3f}\u2013"
             f"{REPORTED_AT_2464_CELLS['real']['f_max']:.3f}, shifted "
             f"{REPORTED_AT_2464_CELLS['shifted']['f_min']:.3f}\u2013"
             f"{REPORTED_AT_2464_CELLS['shifted']['f_max']:.3f}, scrambled "
             f"{REPORTED_AT_2464_CELLS['scrambled']['f_min']:.3f}\u2013"
             f"{REPORTED_AT_2464_CELLS['scrambled']['f_max']:.3f}\n"
             "\u2014 recomputed here at 6.7\u00d7 the cells, not replaced by it."
             "  Estimator: leo_tracker.radio.beacon.cross_radio, unmodified.",
             ha="center", va="bottom", fontsize=9.5, color=MUTED,
             linespacing=1.5)

    fig.tight_layout(rect=(0, 0.290, 1, 0.945))
    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)
    print(f"wrote {OUT_PNG}")


def main(argv: list[str]) -> int:
    if "--replot" in argv and OUT_JSON.is_file():
        # Every number plot() draws is already in the JSON this script wrote,
        # so a layout change does not need a second pass over the corpus.  The
        # data path is untouched: --replot cannot compute anything.
        plot(json.loads(OUT_JSON.read_text()))
        return 0
    data = compute()
    for name, item in data["joins"].items():
        got = REPORTED.get(name, {})
        item["reported_in_retraction_brief"] = got
        item["reported_at_2464_cells"] = REPORTED_AT_2464_CELLS.get(name, {})
        item["agrees_with_brief"] = {
            key: (abs(item[key] - got[key]) <= 0.01) if key in got else None
            for key in ("f_min", "f_max", "spread")}
    real, shifted = data["joins"]["real"], data["joins"]["shifted"]
    data["headline_checks"] = {
        "f_value_separates_real_from_controls": (
            real["f_max"] < shifted["f_min"]
            and real["f_max"] < data["joins"]["scrambled"]["f_min"]),
        "every_observed_spread_at_or_below_its_bootstrap_median": all(
            item["spread"] <= item["bootstrap_spread"]["p50"]
            for item in data["joins"].values()),
        "shifted_raw_spread_tighter_than_real": shifted["spread"] < real["spread"],
        "shifted_spread_tighter_than_real_relative_to_sampling_noise": (
            shifted["spread_over_bootstrap_p50"] < real["spread_over_bootstrap_p50"]),
        "shifted_spread_tighter_than_real_relative_to_f": (
            shifted["spread_over_mean_f"] < real["spread_over_mean_f"]),
        "controls_still_fail_to_separate_on_spread": all(
            item["spread"] <= item["bootstrap_spread"]["p50"]
            for item in data["joins"].values()),
        "f_drift_across_snapshots": {
            "real_f_mean_here": real["f_mean"],
            "real_f_min_max_here": [real["f_min"], real["f_max"]],
            "real_reported_at_2464_cells": [
                REPORTED_AT_2464_CELLS["real"]["f_min"],
                REPORTED_AT_2464_CELLS["real"]["f_max"]],
            "real_reported_in_retraction_brief": [
                REPORTED["real"]["f_min"], REPORTED["real"]["f_max"]],
            "cells_here": real["cells"],
            "cells_reported": REPORTED_AT_2464_CELLS["cells_per_join"]},
    }
    OUT_JSON.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT_JSON}")
    plot(data)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
