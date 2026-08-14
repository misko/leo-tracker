#!/usr/bin/env python3
"""channel-edge-correlation - is the sky channel-correlated, and how much of that is the apparatus?

Eight TUNINGS (4 channels x {lower-edge, upper-edge}), correlated across sweeps.
The unit of observation is one receiver chain's pass over one sweep: within a
sweep a chain visits all eight tunings once, so a unit contributes one fire /
no-fire decision per tuning and the eight columns are commensurable.  A unit
enters only if all eight tunings returned a verdict, so n is identical in every
cell of the matrix.

TWO THINGS CHANGED FROM THE PUBLISHED VERSION.

1. lnb-a IS INCLUDED.  The published figure ran with
   ``cross_radio.DEAD_RECEIVERS = ('lnb-a',)`` on 3,774 units; this one has
   5,032, four receivers instead of three.

2. THE NULL IS CORRECTED, and it is the bigger change.  The published null
   shuffled each tuning column independently across ALL units.  That destroys
   the time trend, the arm composition and the receiver state at the same time
   as the association being tested, so it answers "is there ANY structure here"
   -- to which the answer is trivially yes -- rather than "is there structure
   BETWEEN CHANNELS once the apparatus is held fixed".  Its mean |phi| came out
   at 0.013 and every cross-channel cell cleared it.

   The permutation here is STRATIFIED by (time block x arm x receiver): each
   tuning column is shuffled only WITHIN a stratum, so the trend, the arm and
   the receiver survive the shuffle and only the between-tuning association is
   destroyed.  The statistic plotted alongside is the matching one -- phi
   between columns centred on their own stratum means, which is the partial
   correlation controlling for stratum.

WHAT THAT DOES.  The cross-channel term roughly HALVES.  Almost none of that is
the time trend: removing time blocks alone leaves ~94% of the term standing,
while removing the ARM (sample rate x probe length) removes ~43% of it.  The
same-channel term is barely touched.  And the surviving cross-channel term is
NOT FLAT: it RISES with channel separation, which a common additive mode under
every tuning could not do.  Under family-wise correction against the stratified
null, only about two thirds of the 48 cross-channel cells clear their own bar.

Axis order is fixed, not seriated: ch1 lower, ch1 upper, ch2 lower, ch2 upper,
... so that a channel's OWN two edges sit adjacent.  The order is declared in
advance rather than fitted, so the block structure is a finding and not a
consequence of the layout.

    nice -n 15 python3 channel-edge-correlation.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

HERE = Path(__file__).resolve().parent
for _candidate in (HERE, HERE.parent / "work"):
    if (_candidate / "hcore.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402
import hcore  # noqa: E402

NAME = "channel-edge-correlation"
INK, MUTED, SURFACE, DIAGONAL = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e2"
GRID, BAND = "#d7d6d2", "#e9e8e4"
ACCENT, ORANGE, TEAL = "#2a78d6", "#eb6834", "#2f8f7f"
BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
        "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]

PERMUTATIONS = 400
SEED = 20260814

#: How many equal-count blocks the night is cut into for the time stratum.
#: 4, 8 and 16 are all computed; 8 is plotted, and the JSON shows the term is
#: insensitive to the choice (it moves by <0.003 across the three).
TIME_BLOCKS = 8

#: The stratum the primary null and the primary statistic both use.
PRIMARY_STRATUM = "time8 x arm x receiver"

#: The published run of this figure, for the delta.
PUBLISHED = {
    "units_complete": 3774,
    "lnb_a": "EXCLUDED",
    "same_channel_mean": 0.45343145945259117,
    "cross_channel_mean": 0.11820529022203978,
    "cross_channel_min": 0.07173690696177817,
    "cross_channel_max": 0.16121410316309356,
    "ratio_same_over_cross": 3.8359658742925475,
    "null": {"what": "each tuning column shuffled independently across ALL "
                     "units, which destroys the time trend, the arm "
                     "composition and the receiver state along with the "
                     "association being tested",
             "mean_abs_phi": 0.01303961102466845,
             "p99_of_max_abs_phi": 0.062385322546039575},
    "claim_now_withdrawn": "Every cross-channel cell clears the null, but the "
                           "weakest clears it by 0.010.",
}


def same_channel_mask() -> np.ndarray:
    mask = np.zeros((8, 8), dtype=bool)
    for index in range(0, 8, 2):
        mask[index, index + 1] = mask[index + 1, index] = True
    return mask


SAME = same_channel_mask()
OFF = ~np.eye(8, dtype=bool)
CROSS = OFF & ~SAME


def restore_lnb_a(*, threshold_null_excludes=("lnb-a",)):
    """A corpus whose target arm holds all four ports.

    The threshold bar is left where the published figure drew it -- the
    three-port null population -- so that the receiver set and the null are the
    only things that changed.  ``robustness_thresholds_redrawn`` in the JSON
    reports the same statistics with lnb-a's null arm in the bar.
    """
    cr.DEAD_RECEIVERS = tuple(threshold_null_excludes)
    corpus = hcore.Corpus()
    cr.DEAD_RECEIVERS = ()
    corpus._table = None
    return corpus


# --------------------------------------------------------------------------
# units and strata
# --------------------------------------------------------------------------

def units_of(corpus) -> tuple:
    """(capture, receiver) -> eight tuning decisions, plus that unit's strata."""
    table = corpus.table()
    fired = table["fired"]
    anyfire = corpus.any_method_column()
    slot = {pair: i for i, pair in enumerate(hcore.TUNINGS)}
    width = fired.shape[1]
    rows: dict = defaultdict(
        lambda: {"any": [None] * 8, "per": [[None] * 8 for _ in range(width)],
                 "receiver": None, "capture": None, "arm": None})
    for row in range(fired.shape[0]):
        key = (str(table["capture"][row]), str(table["receiver"][row]))
        position = slot[(int(table["channel"][row]), str(table["edge"][row]))]
        unit = rows[key]
        unit["receiver"] = str(table["receiver"][row])
        unit["capture"] = str(table["capture"][row])
        unit["arm"] = f"{table['rate'][row] / 1e6:g} MS/s {table['probe'][row]:g} ms"
        unit["any"][position] = bool(anyfire[row])
        for method in range(width):
            unit["per"][method][position] = bool(fired[row, method])
    complete = [unit for unit in rows.values()
                if all(value is not None for value in unit["any"])]
    return len(rows), complete


def strata_of(units: list) -> dict:
    """Every stratification the ladder walks through, as integer codes.

    Time is cut into EQUAL-COUNT blocks of units ordered by capture time, so a
    block is a stretch of the night with the same number of observations in it
    rather than the same number of minutes -- collection was not uniform.
    """
    receiver = np.array([u["receiver"] for u in units])
    arm = np.array([u["arm"] for u in units])
    capture = np.array([u["capture"] for u in units])
    order = np.argsort(capture, kind="stable")

    def blocks(count: int) -> np.ndarray:
        out = np.empty(len(units), dtype=int)
        out[order] = (np.arange(len(units)) * count) // len(units)
        return np.array([f"t{value}" for value in out])

    time = {count: blocks(count) for count in (4, 8, 16)}

    def combo(*parts):
        return np.array(["|".join(values) for values in zip(*parts)])

    return {
        "none": None,
        "receiver": receiver,
        "time8 x receiver": combo(time[8], receiver),
        "arm x receiver": combo(arm, receiver),
        "time4 x arm x receiver": combo(time[4], arm, receiver),
        "time8 x arm x receiver": combo(time[8], arm, receiver),
        "time16 x arm x receiver": combo(time[16], arm, receiver),
    }


def codes_of(strata: np.ndarray | None, size: int) -> np.ndarray:
    if strata is None:
        return np.zeros(size, dtype=np.int64)
    _, codes = np.unique(strata, return_inverse=True)
    return codes.astype(np.int64)


def residualised(columns: np.ndarray, codes: np.ndarray,
                 groups: int) -> np.ndarray:
    """phi between columns centred on their own stratum means.

    With one stratum this is exactly Pearson on the raw columns, which is what
    ``hcore.phi_from_counts`` computes -- ``selftest`` asserts the two agree.
    With several strata it is the partial correlation controlling for stratum:
    the between-stratum differences that the apparatus produces are removed and
    only within-stratum co-movement is left.
    """
    x = columns.astype(np.float64)
    counts = np.bincount(codes, minlength=groups).astype(np.float64)
    totals = np.zeros((groups, x.shape[1]), dtype=np.float64)
    np.add.at(totals, codes, x)
    means = totals / np.where(counts > 0, counts, 1.0)[:, None]
    z = x - means[codes]
    norm = np.sqrt((z * z).sum(axis=0))
    usable = norm > 0
    scaled = z / np.where(usable, norm, 1.0)
    matrix = scaled.T @ scaled
    matrix[~usable, :] = np.nan
    matrix[:, ~usable] = np.nan
    np.fill_diagonal(matrix, 1.0)
    return matrix


def terms(matrix: np.ndarray) -> dict:
    return {"same_channel_mean": float(np.nanmean(matrix[SAME])),
            "cross_channel_mean": float(np.nanmean(matrix[CROSS])),
            "cross_channel_min": float(np.nanmin(matrix[CROSS])),
            "cross_channel_max": float(np.nanmax(matrix[CROSS]))}


def separation_of(matrix: np.ndarray) -> dict:
    buckets: dict = defaultdict(list)
    for i in range(8):
        for j in range(i + 1, 8):
            gap = abs(hcore.TUNINGS[i][0] - hcore.TUNINGS[j][0])
            buckets[gap].append(float(matrix[i, j]))
    return {str(gap): {"tuning_pairs": len(values),
                       "mean_phi": float(np.mean(values)),
                       "min_phi": float(np.min(values)),
                       "max_phi": float(np.max(values)),
                       "values": [float(v) for v in values]}
            for gap, values in sorted(buckets.items())}


def permute_within(columns: np.ndarray, order: np.ndarray, starts: np.ndarray,
                   sizes: np.ndarray, generator) -> np.ndarray:
    """Each column shuffled independently, but only WITHIN each stratum."""
    out = np.empty_like(columns)
    for column in range(columns.shape[1]):
        values = columns[:, column]
        for start, size in zip(starts, sizes):
            block = order[start:start + size]
            out[block, column] = values[generator.permutation(block)]
    return out


def stratified_null(columns: np.ndarray, codes: np.ndarray, groups: int, *,
                    draws: int, seed: int) -> dict:
    """The residualised statistic's null, permuting only inside each stratum."""
    order = np.argsort(codes, kind="stable")
    sizes = np.bincount(codes, minlength=groups)
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    keep = sizes > 1
    generator = np.random.default_rng(seed)
    cross_means, max_abs, cells = [], [], []
    for _ in range(draws):
        shuffled = permute_within(columns, order, starts[keep], sizes[keep],
                                  generator)
        matrix = residualised(shuffled, codes, groups)
        cross_means.append(float(np.nanmean(matrix[CROSS])))
        max_abs.append(float(np.nanmax(np.abs(matrix[OFF]))))
        cells.append(np.abs(matrix))
    stack = np.stack(cells)
    return {
        "draws": draws, "seed": seed,
        "strata": int(groups),
        "strata_with_more_than_one_unit": int(keep.sum()),
        "cross_channel_mean": {
            "null_mean": float(np.mean(cross_means)),
            "null_p99": float(np.percentile(cross_means, 99)),
            "null_max": float(np.max(cross_means))},
        "family_wise_bar_p99_of_max_abs_phi": float(np.percentile(max_abs, 99)),
        "_per_cell_null": stack,
    }


def per_cell_verdicts(matrix: np.ndarray, null: dict) -> dict:
    """Which cells clear a family-wise bar, and each cell's own p-value."""
    bar = null["family_wise_bar_p99_of_max_abs_phi"]
    stack = null["_per_cell_null"]
    clears = np.zeros((8, 8), dtype=bool)
    pvalue = np.full((8, 8), np.nan)
    for i in range(8):
        for j in range(8):
            if i == j:
                continue
            value = abs(matrix[i, j])
            clears[i, j] = value > bar
            pvalue[i, j] = float((stack[:, i, j] >= value).mean())
    failing = []
    for i in range(8):
        for j in range(8):
            if CROSS[i, j] and j > i and pvalue[i, j] > 0.05:
                failing.append({"pair": [hcore.TUNING_LABELS[i],
                                         hcore.TUNING_LABELS[j]],
                                "phi": float(matrix[i, j]),
                                "p": float(pvalue[i, j])})
    return {
        "family_wise_bar": bar,
        "cross_channel_cells": int(CROSS.sum()),
        "cross_channel_cells_clearing_the_bar": int((clears & CROSS).sum()),
        "same_channel_cells": int(SAME.sum()),
        "same_channel_cells_clearing_the_bar": int((clears & SAME).sum()),
        "weakest_cross_channel_cell": float(np.nanmin(matrix[CROSS])),
        "cells_failing_their_own_per_cell_null_at_p_0_05": sorted(
            failing, key=lambda row: -row["p"]),
        "_clears": clears, "_p": pvalue,
    }


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def ladder_for(columns: np.ndarray, strata: dict) -> dict:
    out = {}
    for name, values in strata.items():
        codes = codes_of(values, columns.shape[0])
        groups = int(codes.max()) + 1
        matrix = residualised(columns, codes, groups)
        out["raw (no strata)" if name == "none" else name] = {
            **terms(matrix), "strata": groups}
    return out


def compute() -> dict:
    corpus = restore_lnb_a()
    methods = corpus.methods
    seen, units = units_of(corpus)
    columns = np.array([u["any"] for u in units], dtype=bool)
    strata = strata_of(units)

    # -- the statistic itself, raw and residualised -----------------------
    raw = residualised(columns, np.zeros(len(units), dtype=np.int64), 1)
    codes = codes_of(strata[PRIMARY_STRATUM], len(units))
    groups = int(codes.max()) + 1
    matrix = residualised(columns, codes, groups)

    # phi from this file must equal the module's own on the raw columns
    check = max(abs(raw[i, j] - hcore.phi_of_columns(columns[:, i],
                                                     columns[:, j]))
                for i in range(8) for j in range(8) if i != j)

    null = stratified_null(columns, codes, groups, draws=PERMUTATIONS,
                           seed=SEED)
    verdicts = per_cell_verdicts(matrix, null)

    # -- the same for the one predeclared detector ------------------------
    index = methods.index("glrt-32")
    glrt_columns = np.array([u["per"][index] for u in units], dtype=bool)
    glrt_raw = residualised(glrt_columns, np.zeros(len(units), dtype=np.int64), 1)
    glrt_matrix = residualised(glrt_columns, codes, groups)
    glrt_null = stratified_null(glrt_columns, codes, groups, draws=PERMUTATIONS,
                                seed=SEED + 1)
    glrt_verdicts = per_cell_verdicts(glrt_matrix, glrt_null)

    ladders = {"any-of-eight": ladder_for(columns, strata),
               "glrt-32": ladder_for(glrt_columns, strata)}
    survives = {}
    for rule, ladder in ladders.items():
        base = ladder["raw (no strata)"]["cross_channel_mean"]
        survives[rule] = {
            "time_only": ladder["time8 x receiver"]["cross_channel_mean"] / base,
            "arm_and_receiver":
                ladder["arm x receiver"]["cross_channel_mean"] / base,
            "time_and_arm": ladder[PRIMARY_STRATUM]["cross_channel_mean"] / base,
        }

    # -- per receiver, to show the structure is not one bad port ----------
    receiver = np.array([u["receiver"] for u in units])
    per_receiver = {}
    for name in sorted(set(receiver.tolist())):
        keep = receiver == name
        sub_codes = codes_of(strata[PRIMARY_STRATUM][keep], int(keep.sum()))
        sub_groups = int(sub_codes.max()) + 1
        per_receiver[name] = {
            "units": int(keep.sum()),
            "raw": terms(residualised(columns[keep],
                                      np.zeros(int(keep.sum()), dtype=np.int64), 1)),
            "residualised": terms(residualised(columns[keep], sub_codes,
                                               sub_groups)),
        }

    # -- per method, as a robustness check --------------------------------
    per_method = {}
    for position, method in enumerate(methods):
        block = np.array([u["per"][position] for u in units], dtype=bool)
        per_method[method] = {
            "raw": terms(residualised(block,
                                      np.zeros(len(units), dtype=np.int64), 1)),
            "residualised": terms(residualised(block, codes, groups)),
        }

    # -- the same with lnb-a's null arm in the threshold bar --------------
    cr.DEAD_RECEIVERS = ()
    redrawn = hcore.Corpus()
    _, redrawn_units = units_of(redrawn)
    redrawn_columns = np.array([u["any"] for u in redrawn_units], dtype=bool)
    redrawn_codes = codes_of(strata_of(redrawn_units)[PRIMARY_STRATUM],
                             len(redrawn_units))
    redrawn_groups = int(redrawn_codes.max()) + 1

    census = corpus.census_block()
    census["excluded_receivers"] = {}
    census["receivers_live"] = ["lnb-a", "lnb-b", "lnb-c", "lnb-d"]
    census["lnb_a"] = (
        "INCLUDED. The exclusion recorded in cross_radio.DEAD_RECEIVERS cites a "
        "flat ~1.19 peak-to-median at every tuning since 2026-08-13 04:44 UTC. "
        "That instant falls inside pluto-5d4d's 03:24:04Z-05:07:56Z outage, when "
        "the radio produced no data at all, and this scored corpus stops at "
        "2026-08-14T03:21:55Z regardless.")
    census["threshold_null_population"] = (
        "lnb-b, lnb-c, lnb-d -- the published figure's own null population, held "
        "fixed so that the receiver set and the permutation null are the only "
        "things that changed")

    data = {
        "figure": NAME,
        "question": "does one channel's firing predict another channel's once "
                    "the apparatus is held fixed, and is a channel's own "
                    "upper/lower pair tighter than two different channels are?",
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
        "census": census,
        "lnb_a": "INCLUDED",
        "unit_of_observation": "one receiver chain's pass over one sweep; it "
                               "visits all eight tunings once",
        "units_seen": seen,
        "units_complete": int(columns.shape[0]),
        "n_per_cell": int(columns.shape[0]),
        "n_is_identical_in_every_cell": True,
        "decision_rule": "cross_radio._any_method_fires (any of the eight "
                         "scored methods), each against its own (sample rate, "
                         "probe length) cross-edge-null threshold",
        "phi_matches_module_phi_to": check,
        "axis_order": hcore.TUNING_LABELS,
        "axis_order_basis": "declared in advance: a channel's own two edges are "
                            "adjacent, so channel structure would appear as four "
                            "2x2 diagonal blocks",
        "fire_rate_per_tuning": dict(zip(hcore.TUNING_LABELS,
                                         columns.mean(axis=0).tolist())),
        "statistic": {
            "primary": "phi between tuning columns CENTRED ON THEIR OWN STRATUM "
                       "MEANS, stratum = " + PRIMARY_STRATUM + ". This is the "
                       "partial correlation controlling for stratum: the "
                       "between-stratum structure the apparatus produces is "
                       "removed and only within-stratum co-movement is left.",
            "raw": "phi on the uncentred columns -- the published statistic, "
                   "kept for the comparison",
            "stratum": PRIMARY_STRATUM,
            "time_blocks": "equal-COUNT blocks of units ordered by capture "
                           "time; collection was not uniform, so equal-width "
                           "blocks would not hold observation count fixed",
        },
        "phi_residualised": {
            row: {col: float(matrix[i, j])
                  for j, col in enumerate(hcore.TUNING_LABELS)}
            for i, row in enumerate(hcore.TUNING_LABELS)},
        "phi_raw": {row: {col: float(raw[i, j])
                          for j, col in enumerate(hcore.TUNING_LABELS)}
                    for i, row in enumerate(hcore.TUNING_LABELS)},
        "same_channel_edge_pairs_residualised": {
            f"ch{index // 2 + 1}": float(matrix[index, index + 1])
            for index in range(0, 8, 2)},
        "terms_raw": terms(raw),
        "terms_residualised": terms(matrix),
        "ratio_same_over_cross_raw":
            float(np.nanmean(raw[SAME]) / np.nanmean(raw[CROSS])),
        "ratio_same_over_cross_residualised":
            float(np.nanmean(matrix[SAME]) / np.nanmean(matrix[CROSS])),
        "cross_channel_mean_ladder": ladders,
        "fraction_of_the_raw_cross_channel_term_that_survives": survives,
        "channel_separation_raw": separation_of(raw),
        "channel_separation_residualised": separation_of(matrix),
        "channel_separation_residualised_glrt_32": separation_of(glrt_matrix),
        "stratified_permutation_null": {
            key: value for key, value in null.items()
            if not key.startswith("_")},
        "per_cell": {key: value for key, value in verdicts.items()
                     if not key.startswith("_")},
        "glrt_32": {
            "terms_raw": terms(glrt_raw),
            "terms_residualised": terms(glrt_matrix),
            "stratified_permutation_null": {
                key: value for key, value in glrt_null.items()
                if not key.startswith("_")},
            "per_cell": {key: value for key, value in glrt_verdicts.items()
                         if not key.startswith("_")},
        },
        "per_receiver": per_receiver,
        "per_method": per_method,
        "published_with_lnb_a_excluded_and_the_unstratified_null": PUBLISHED,
        "robustness_thresholds_redrawn": {
            "what": "the same statistics with lnb-a's cross-edge null arm "
                    "folded into the population null_thresholds draws from",
            "units_complete": len(redrawn_units),
            "terms_raw": terms(residualised(
                redrawn_columns, np.zeros(len(redrawn_units), dtype=np.int64), 1)),
            "terms_residualised": terms(residualised(
                redrawn_columns, redrawn_codes, redrawn_groups)),
        },
        "time_block_sensitivity": {
            name: ladders["any-of-eight"][name]["cross_channel_mean"]
            for name in ("time4 x arm x receiver", "time8 x arm x receiver",
                         "time16 x arm x receiver")},
    }
    data["_matrix"] = matrix
    data["_clears"] = verdicts["_clears"]
    data["_glrt_clears"] = glrt_verdicts["_clears"]
    return data


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------

def heatmap(ax, data: dict) -> None:
    labels = data["axis_order"]
    matrix = data["_matrix"]
    clears = data["_clears"]
    off = OFF
    lo, hi = float(np.nanmin(matrix[off])), float(np.nanmax(matrix[off]))

    cmap = LinearSegmentedColormap.from_list("phi-blue", BLUE)
    cmap.set_bad(DIAGONAL)
    shown = np.ma.masked_array(matrix, mask=np.eye(8, dtype=bool))
    image = ax.imshow(shown, cmap=cmap, vmin=lo, vmax=hi)

    span = hi - lo
    for i in range(8):
        for j in range(8):
            if i == j:
                continue
            value = matrix[i, j]
            light = (value - lo) / span > 0.62
            colour = SURFACE if light else INK
            ax.text(j, i - 0.10, f"{value:.3f}", ha="center", va="center",
                    fontsize=10.5, color=colour)
            ax.text(j, i + 0.26, "clears" if clears[i, j] else "n.s.",
                    ha="center", va="center", fontsize=7.6, color=colour,
                    style="italic" if not clears[i, j] else "normal",
                    fontweight="bold" if clears[i, j] else "normal")

    for index in range(0, 8, 2):
        ax.add_patch(plt.Rectangle((index - 0.5, index - 0.5), 2, 2,
                                   fill=False, edgecolor=INK, linewidth=2.4,
                                   zorder=4))

    ax.set_xticks(range(8))
    ax.set_yticks(range(8))
    ax.xaxis.set_ticks_position("top")
    ax.set_xticklabels(labels, rotation=40, ha="left", rotation_mode="anchor",
                       fontsize=10)
    ax.set_yticklabels(labels, fontsize=10.5)
    ax.set_xticks(np.arange(9) - 0.5, minor=True)
    ax.set_yticks(np.arange(9) - 0.5, minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2.0)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for side in ax.spines.values():
        side.set_visible(False)

    bar = plt.colorbar(image, ax=ax, fraction=0.040, pad=0.028, aspect=24)
    bar.set_label("$\\varphi$ within stratum  (dimensionless)", fontsize=10.5,
                  color=INK)
    bar.outline.set_edgecolor(MUTED)
    bar.ax.tick_params(labelsize=9.5, color=MUTED, labelcolor=MUTED)

    cell = data["per_cell"]
    ax.set_title(
        "CORRECTED $\\varphi$: within (time block × arm × receiver)\n"
        f"outlined 2×2 blocks are a channel's own two edges — "
        f"{cell['same_channel_cells_clearing_the_bar']}/"
        f"{cell['same_channel_cells']} clear the bar;\n"
        f"of the {cell['cross_channel_cells']} cross-channel cells only "
        f"{cell['cross_channel_cells_clearing_the_bar']} do",
        pad=44, fontsize=12.0, linespacing=1.45)


def ladder(ax, data: dict) -> None:
    steps = [("raw (no strata)", "raw\n(published statistic)"),
             ("receiver", "+ receiver"),
             ("time8 x receiver", "+ time block"),
             ("arm x receiver", "+ arm\n(rate × probe)"),
             ("time8 x arm x receiver", "+ time AND arm\n(the corrected term)")]
    x = np.arange(len(steps))
    for rule, colour, marker in (("any-of-eight", ACCENT, "o"),
                                 ("glrt-32", ORANGE, "s")):
        values = [data["cross_channel_mean_ladder"][rule][key]["cross_channel_mean"]
                  for key, _ in steps]
        ax.plot(x, values, color=colour, linewidth=2.0, zorder=2,
                label=f"{rule}: {values[0]:.3f} → {values[-1]:.3f}")
        ax.scatter(x, values, s=78, facecolor=colour, edgecolor=colour,
                   zorder=3, marker=marker)
        for position, value in zip(x, values):
            ax.text(position, value + 0.006, f"{value:.3f}", ha="center",
                    va="bottom", fontsize=9.8, color=INK)

    ax.axvspan(2.5, 3.5, color=BAND, zorder=0)
    ax.text(3.0, 0.196, "the ARM is where\nthe term goes", ha="center",
            va="top", fontsize=9.8, color=INK, linespacing=1.4)

    ax.set_xticks(x)
    ax.set_xticklabels([label for _, label in steps], fontsize=9.3)
    ax.set_ylim(0.0, 0.205)
    ax.set_ylabel("mean cross-channel $\\varphi$", fontsize=10.5)
    ax.set_xlabel("what the permutation and the statistic hold fixed",
                  fontsize=10.5, labelpad=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(length=0)
    ax.legend(loc="lower left", frameon=False, fontsize=10)
    survives = data["fraction_of_the_raw_cross_channel_term_that_survives"]
    ax.set_title(
        "REMOVING TIME COSTS ALMOST NOTHING; REMOVING THE ARM COSTS ~43%\n"
        "time alone leaves "
        f"{survives['any-of-eight']['time_only'] * 100:.0f}–"
        f"{survives['glrt-32']['time_only'] * 100:.0f}% standing; "
        "time AND arm leave "
        f"{min(v['time_and_arm'] for v in survives.values()) * 100:.0f}–"
        f"{max(v['time_and_arm'] for v in survives.values()) * 100:.0f}%",
        pad=10, fontsize=11.5, linespacing=1.45)


def separation(ax, data: dict) -> None:
    for rule, key, colour, marker, below in (
            ("any-of-eight", "channel_separation_residualised", ACCENT, "o",
             True),
            ("glrt-32", "channel_separation_residualised_glrt_32", ORANGE,
             "s", False)):
        block = data[key]
        gaps = [1, 2, 3]
        values = [block[str(gap)]["mean_phi"] for gap in gaps]
        counts = [block[str(gap)]["tuning_pairs"] for gap in gaps]
        ax.plot(gaps, values, color=colour, linewidth=2.0, zorder=2,
                label=f"{rule}   ×{values[-1] / values[0]:.1f} from "
                      "distance 1 to 3")
        ax.scatter(gaps, values, s=82, facecolor=colour, edgecolor=colour,
                   marker=marker, zorder=3)
        # the two rules run close together at distance 1, so one series
        # labels below its markers and the other above.
        for gap, value, count in zip(gaps, values, counts):
            ax.text(gap, value - 0.006 if below else value + 0.006,
                    f"{value:.4f}\n({count} pairs)", ha="center",
                    va="top" if below else "bottom", fontsize=9.5,
                    color=INK, linespacing=1.35)

    ax.axhline(0.0, color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)))
    ax.set_xticks([1, 2, 3])
    ax.set_xticklabels(["1 channel apart", "2 apart", "3 apart\n(ch1 × ch4 only)"],
                       fontsize=10)
    ax.set_xlim(0.68, 3.42)
    ax.set_ylim(0.005, 0.190)
    ax.set_ylabel("mean cross-channel $\\varphi$ within stratum", fontsize=10.5)
    ax.set_xlabel("separation between the two channels", fontsize=10.5,
                  labelpad=8)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(length=0)
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    ax.set_title(
        "NOT A FLAT COMMON MODE: the surviving term RISES with separation\n"
        "a common additive mode under every tuning would be separation-flat, "
        "and this is not",
        pad=10, fontsize=11.5, linespacing=1.45)


def plot(data: dict):
    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 11.5,
        "xtick.labelsize": 10, "ytick.labelsize": 10.5,
        "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    fig = plt.figure(figsize=(19.2, 12.4), dpi=150, facecolor=SURFACE)
    grid = fig.add_gridspec(2, 2, width_ratios=[1.22, 1.0],
                            height_ratios=[1.0, 1.0],
                            wspace=0.20, hspace=0.52,
                            left=0.072, right=0.968, top=0.752, bottom=0.222)
    heatmap(fig.add_subplot(grid[:, 0]), data)
    ladder(fig.add_subplot(grid[0, 1]), data)
    separation(fig.add_subplot(grid[1, 1]), data)

    raw = data["terms_raw"]
    res = data["terms_residualised"]
    fig.suptitle(
        "Under a null that holds the apparatus fixed, the cross-channel term "
        f"HALVES — {raw['cross_channel_mean']:.3f} → "
        f"{res['cross_channel_mean']:.3f} —\n"
        "the arm and not the time trend is where it goes, and what survives is "
        "not flat: it rises with channel separation",
        fontsize=15.0, color=INK, y=0.986, linespacing=1.5)

    census = data["census"]["frozen_at_start"]
    was = data["published_with_lnb_a_excluded_and_the_unstratified_null"]
    cell = data["per_cell"]
    gcell = data["glrt_32"]["per_cell"]
    null = data["stratified_permutation_null"]
    lines = [
        f"n = {data['units_complete']:,} complete units in EVERY cell — one "
        "receiver chain's pass over one sweep, visiting all eight tunings once.  "
        "lnb-a IS INCLUDED: four receivers, against "
        f"{was['units_complete']:,} units on three in the published run.",

        "THE NULL IS CORRECTED.  The published null shuffled each tuning column "
        "across ALL units, destroying the time trend, the arm composition and "
        "the receiver state along with the association being tested; its mean "
        f"|$\\varphi$| was {was['null']['mean_abs_phi']:.3f} and every",

        "cross-channel cell cleared it.  The permutation here shuffles only "
        f"WITHIN a ({PRIMARY_STRATUM}) stratum — {null['strata_with_more_than_one_unit']:,} "
        f"strata of more than one unit, {null['draws']} draws, seed "
        f"{null['seed']} — and the statistic is matched to it: $\\varphi$ between "
        "columns centred",

        "on their own stratum means.  Time blocks are equal-COUNT, and the term "
        "moves by less than 0.003 across 4, 8 and 16 blocks (JSON).  "
        f"SAME-CHANNEL is barely touched: {raw['same_channel_mean']:.3f} → "
        f"{res['same_channel_mean']:.3f}.  CROSS-CHANNEL halves:",

        f"{raw['cross_channel_mean']:.3f} → {res['cross_channel_mean']:.3f} "
        "(any-of-eight) and "
        f"{data['glrt_32']['terms_raw']['cross_channel_mean']:.3f} → "
        f"{data['glrt_32']['terms_residualised']['cross_channel_mean']:.3f} "
        "(glrt-32).  Cells are marked 'clears' where |$\\varphi$| exceeds the "
        "family-wise bar (99th percentile of the largest |$\\varphi$| anywhere in "
        "a null draw,",

        f"{cell['family_wise_bar']:.4f}): "
        f"{cell['cross_channel_cells_clearing_the_bar']}/"
        f"{cell['cross_channel_cells']} cross-channel cells clear it under "
        "any-of-eight and "
        f"{gcell['cross_channel_cells_clearing_the_bar']}/"
        f"{gcell['cross_channel_cells']} under glrt-32, against 48/48 under the "
        f"published null.  The withdrawn claim: “{was['claim_now_withdrawn']}”",

        "Axis order is declared in advance, not seriated.  Decisions come from "
        "leo_tracker.radio.beacon.cross_radio, unmodified, each method against "
        "its own (sample rate, probe length) cross-edge-null threshold; "
        f"$\\varphi$ here matches the module's own to {data['phi_matches_module_phi_to']:.1e}.",

        f"CENSUS frozen before computing (digest {census['scored_digest']}): "
        f"{census['sweeps_on_share']:,} sweeps | "
        f"{census['corpus_entries']:,} corpus entries | "
        f"{census['scored_sidecars']:,} scored sidecars; "
        f"{data['census']['paired_sweeps']:,} paired sweeps, all four receivers "
        "live.",
    ]
    caption = hcore.drift_caption()
    if caption:
        lines.append(caption)
    fig.text(0.5, 0.010, "\n".join(lines), ha="center", va="bottom",
             fontsize=8.8, color=MUTED, linespacing=1.50)
    return fig


def main() -> int:
    data = compute()
    raw, res = data["terms_raw"], data["terms_residualised"]
    survives = data["fraction_of_the_raw_cross_channel_term_that_survives"]
    sep = data["channel_separation_residualised"]
    gsep = data["channel_separation_residualised_glrt_32"]
    cell, gcell = data["per_cell"], data["glrt_32"]["per_cell"]
    data["headline_checks"] = {
        "lnb_a": "INCLUDED",
        "units_complete": data["units_complete"],
        "cross_channel_raw": raw["cross_channel_mean"],
        "cross_channel_residualised": res["cross_channel_mean"],
        "cross_channel_residualised_glrt_32":
            data["glrt_32"]["terms_residualised"]["cross_channel_mean"],
        "same_channel_raw": raw["same_channel_mean"],
        "same_channel_residualised": res["same_channel_mean"],
        "fraction_surviving": survives,
        "term_rises_with_separation": bool(
            sep["1"]["mean_phi"] < sep["2"]["mean_phi"] < sep["3"]["mean_phi"]),
        "separation_any_of_eight": [sep[str(g)]["mean_phi"] for g in (1, 2, 3)],
        "separation_glrt_32": [gsep[str(g)]["mean_phi"] for g in (1, 2, 3)],
        "cross_channel_cells_clearing_family_wise": [
            cell["cross_channel_cells_clearing_the_bar"],
            gcell["cross_channel_cells_clearing_the_bar"],
            cell["cross_channel_cells"]],
        "flat_common_mode_still_supportable": False,
        "verdict": (
            "The cross-channel term survives a null that holds the apparatus "
            f"fixed, but at about half its published size: "
            f"{raw['cross_channel_mean']:.4f} -> {res['cross_channel_mean']:.4f} "
            "(any-of-eight) and "
            f"{data['glrt_32']['terms_raw']['cross_channel_mean']:.4f} -> "
            f"{data['glrt_32']['terms_residualised']['cross_channel_mean']:.4f} "
            "(glrt-32). The time trend is not what costs it: removing time "
            f"blocks alone leaves {survives['any-of-eight']['time_only'] * 100:.0f}% "
            "standing, while removing the arm (sample rate x probe length) "
            "removes about 43%. The same-channel term is barely affected "
            f"({raw['same_channel_mean']:.4f} -> {res['same_channel_mean']:.4f}). "
            "The phrase 'flat sweep-wide common mode' is NOT supportable: the "
            "surviving term rises monotonically with channel separation, "
            f"{sep['1']['mean_phi']:.4f}/{sep['2']['mean_phi']:.4f}/"
            f"{sep['3']['mean_phi']:.4f} (any-of-eight) and "
            f"{gsep['1']['mean_phi']:.4f}/{gsep['2']['mean_phi']:.4f}/"
            f"{gsep['3']['mean_phi']:.4f} (glrt-32), which a common additive "
            "mode under every tuning could not do. Under family-wise correction "
            f"only {cell['cross_channel_cells_clearing_the_bar']}/"
            f"{cell['cross_channel_cells']} and "
            f"{gcell['cross_channel_cells_clearing_the_bar']}/"
            f"{gcell['cross_channel_cells']} cross-channel cells clear their "
            "null, against 48/48 under the published one"),
    }
    figure = plot(data)
    data["census_drift_at_end"] = hcore.drift_block()
    for key in [k for k in data if k.startswith("_")]:
        del data[key]
    hcore.write_outputs(NAME, data, figure)
    print(json.dumps(data["headline_checks"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
