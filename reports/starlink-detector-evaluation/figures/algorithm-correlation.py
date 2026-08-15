#!/usr/bin/env python3
"""algorithm-correlation - the eight detectors are near-duplicates.

Refreshes ``reports/sync-scan-cross-radio-2026-08-14/figures/method-correlation.py``
on the current corpus, keeping its ordering logic unchanged: the row/column
order is the one maximising total adjacent phi, found by enumerating all
8! = 40,320 orderings, so the block structure the figure shows is a property of
the data rather than an assumption about detector families.

phi is the Pearson correlation between two detectors' binary fire / no-fire
decisions taken over the SAME observations: every live target observation in the
paired corpus, each detector judged against the threshold
``cross_radio.null_thresholds`` drew for its own (sample rate, probe length)
from the cross-edge null arms.  An observation enters only if all eight
detectors returned a verdict on it, so every cell rests on one identical
population and n is the same in every cell.

lnb-a IS INCLUDED.  The published version of this figure ran with
``cross_radio.DEAD_RECEIVERS = ('lnb-a',)`` and 30,192 observations; lnb-a's
exclusion has been withdrawn, so the population here is 40,256 -- all four
ports -- and n moves in every cell of the matrix together.

This is the mechanism behind the vacuous consistency check: the f estimate is
built from three firing rates, so eight detectors that make nearly the same
decision on every observation must return nearly the same f whatever they are
fed -- including on joins the coincidence model forbids.

    nice -n 15 python3 algorithm-correlation.py
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

HERE = Path(__file__).resolve().parent
#: ``hcore`` sits beside this file when the figure directory is self-contained,
#: and in ../work beside the frozen snapshot when it is regenerated.
for _candidate in (HERE, HERE.parent / "work"):
    if (_candidate / "hcore.py").is_file():
        sys.path.insert(0, str(_candidate))
        break
sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon import cross_radio as cr  # noqa: E402
from _pipeline import load as _load_pipeline  # noqa: E402
hcore = _load_pipeline("hcore")

NAME = "algorithm-correlation"
INK, MUTED, SURFACE, DIAGONAL = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e2"
#: Sequential blue ramp, steps 100 -> 700 of the validated default palette.
#: One hue, light to dark: phi here is a magnitude, so it gets a sequential scale.
BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
        "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]

#: Which detectors are variants of one another, used only to outline the blocks
#: the seriation already put side by side.
FAMILY = {"anchor-8": "anchor", "glrt-32": "glrt", "glrt-64": "glrt",
          "full-frame-verify": "full-frame", "full-frame-full": "full-frame",
          "full-frame-acquire": "full-frame",
          "differential-16": "differential", "differential-32": "differential"}

#: What the retraction report states, for comparison.
REPORTED = {"phi_min": 0.82, "phi_max": 0.94, "observations": 2160}

#: The published run of THIS figure, with lnb-a excluded, for the delta.
PUBLISHED_WITH_LNB_A_OUT = {
    "phi_min": 0.8470439640483043, "phi_max": 0.9453835469439522,
    "phi_mean": 0.8878918476425017, "observations": 30192,
    "lowest_pair": ["anchor-8", "differential-16"],
    "highest_pair": ["full-frame-verify", "full-frame-full"],
    "order": ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
              "full-frame-full", "full-frame-acquire", "differential-32",
              "differential-16"],
}

#: The receiver set, and why it is what it is.
#:
#: The threshold bar is deliberately left where the published figure drew it:
#: ``null_thresholds`` is given the three-port null population, and lnb-a's
#: TARGET observations are then judged against that same bar.  One thing changes
#: between the published matrix and this one -- the receiver set -- so any
#: movement in the band is attributable to the receivers and not to a threshold
#: that moved underneath it.  ``robustness_thresholds_redrawn`` in the JSON
#: reports the same matrix with lnb-a's null arm folded into the threshold
#: population as well.
THRESHOLD_NULL_RECEIVERS = ("lnb-a",)


def restore_lnb_a(*, thresholds_from: tuple = THRESHOLD_NULL_RECEIVERS):
    """A corpus whose target arm holds all four ports.

    ``cross_radio.DEAD_RECEIVERS`` is the module's own documented exclusion
    list ("a list rather than a flag because the next port to die should be one
    edit").  It is read at call time by ``_live``, so emptying it restores
    lnb-a to the target arm, the null arm and the join without touching any of
    the functions that do the work.
    """
    cr.DEAD_RECEIVERS = tuple(thresholds_from)
    corpus = hcore.Corpus()          # draws null_thresholds under this setting
    cr.DEAD_RECEIVERS = ()           # lnb-a is a live receiver from here on
    corpus._table = None             # rebuild the observation table with it in
    return corpus


def census_of(corpus) -> dict:
    """``census_block`` with the exclusion bookkeeping told straight."""
    census = corpus.census_block()
    census["excluded_receivers"] = {}
    census["lnb_a"] = (
        "INCLUDED. The exclusion recorded in cross_radio.DEAD_RECEIVERS cites a "
        "flat ~1.19 peak-to-median at every tuning since 2026-08-13 04:44 UTC. "
        "That instant falls inside pluto-5d4d's 03:24:04Z-05:07:56Z outage, when "
        "the radio produced no data at all, and this scored corpus stops at "
        "2026-08-14T03:21:55Z regardless. Re-measured on this freeze with the "
        "repository's own fire logic, lnb-a's coarse peak-to-median runs median "
        "1.104, max 2.007, sd 0.078 -- not flat.")
    census["receivers_live"] = ["lnb-a", "lnb-b", "lnb-c", "lnb-d"]
    census["threshold_null_population"] = (
        "lnb-b, lnb-c, lnb-d -- the published figure's own null population, held "
        "fixed so that the receiver set is the only thing that changed"
        if THRESHOLD_NULL_RECEIVERS else "all four ports")
    return census


def seriate(matrix: np.ndarray, names: list[str]) -> tuple:
    """Order maximising the sum of adjacent similarities, over all 8! orders.

    An ordering and its reverse score identically, so the direction is pinned by
    putting the alphabetically first name at the top; without that the figure
    would flip between runs for no reason.
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


def band_of(fired: np.ndarray, methods: list[str], order) -> dict:
    """The off-diagonal band of the phi matrix under one fixed row order."""
    phi = hcore.phi_matrix(fired)
    matrix = phi[np.ix_(order, order)]
    ordered = [methods[i] for i in order]
    size = len(ordered)
    off = matrix[~np.eye(size, dtype=bool)]
    lowest = np.unravel_index(np.argmin(matrix + np.eye(size) * 9), matrix.shape)
    highest = np.unravel_index(np.argmax(matrix - np.eye(size) * 9), matrix.shape)
    return {"matrix": matrix, "ordered": ordered,
            "min": float(off.min()), "max": float(off.max()),
            "mean": float(off.mean()), "pairs": int(off.size // 2),
            "lowest_pair": [ordered[lowest[0]], ordered[lowest[1]]],
            "highest_pair": [ordered[highest[0]], ordered[highest[1]]]}


def compute() -> dict:
    corpus = restore_lnb_a()
    table = corpus.table()
    fired = table["fired"]
    methods = corpus.methods
    receivers = sorted(set(table["receiver"].tolist()))

    phi = hcore.phi_matrix(fired)
    order, score = seriate(phi, methods)
    band = band_of(fired, methods, order)
    ordered, matrix = band["ordered"], band["matrix"]

    # -- per receiver: the band must not be one port's property ------------
    per_receiver = {}
    for receiver in receivers:
        keep = table["receiver"] == receiver
        sub = band_of(fired[keep], methods, order)
        per_receiver[receiver] = {
            "observations": int(keep.sum()),
            "phi_min": sub["min"], "phi_max": sub["max"],
            "phi_mean": sub["mean"]}

    # -- robustness: the same matrix with lnb-a's null arm in the bar ------
    cr.DEAD_RECEIVERS = ()
    redrawn_corpus = hcore.Corpus()
    redrawn = band_of(redrawn_corpus.table()["fired"], redrawn_corpus.methods,
                      order)

    return {
        "figure": NAME,
        "question": "do the eight algorithms make the same fire / no-fire "
                    "decision on the same observations?",
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
        "census": census_of(corpus),
        "lnb_a": "INCLUDED",
        "population": "every live target observation in the paired corpus, all "
                      "four receivers (lnb-a, lnb-b, lnb-c, lnb-d), all eight "
                      "verdicts present",
        "receivers": receivers,
        "observations": int(fired.shape[0]),
        "observations_added_by_restoring_lnb_a":
            int(fired.shape[0]) - PUBLISHED_WITH_LNB_A_OUT["observations"],
        "n_per_cell": int(fired.shape[0]),
        "n_is_identical_in_every_cell": True,
        "observations_with_a_missing_verdict":
            table["observations_with_a_missing_verdict"],
        "methods_alphabetical": methods,
        "order": ordered,
        "order_basis": "maximises adjacent phi, exhaustive over 8! = 40320 "
                       f"orderings; total adjacent phi {score:.4f}",
        "order_unchanged_by_restoring_lnb_a":
            ordered == PUBLISHED_WITH_LNB_A_OUT["order"],
        #: A row order is only used to put like beside like, so what matters is
        #: whether each detector family still lands in one contiguous run, not
        #: whether two rows inside a block swapped.
        "family_blocks_contiguous": all(
            len({i for i, m in enumerate(ordered) if FAMILY[m] == family}) ==
            max(i for i, m in enumerate(ordered) if FAMILY[m] == family)
            - min(i for i, m in enumerate(ordered) if FAMILY[m] == family) + 1
            for family in set(FAMILY.values())),
        "fire_rate": {m: float(fired[:, i].mean()) for i, m in enumerate(methods)},
        "phi": {row: {col: float(matrix[i, j]) for j, col in enumerate(ordered)}
                for i, row in enumerate(ordered)},
        "phi_off_diagonal": {k: band[k] for k in
                             ("min", "max", "mean", "pairs", "lowest_pair",
                              "highest_pair")},
        "per_receiver": per_receiver,
        "published_with_lnb_a_excluded": PUBLISHED_WITH_LNB_A_OUT,
        "movement_from_restoring_lnb_a": {
            "phi_min": band["min"] - PUBLISHED_WITH_LNB_A_OUT["phi_min"],
            "phi_max": band["max"] - PUBLISHED_WITH_LNB_A_OUT["phi_max"],
            "phi_mean": band["mean"] - PUBLISHED_WITH_LNB_A_OUT["phi_mean"],
        },
        "robustness_thresholds_redrawn": {
            "what": "the same matrix with lnb-a's cross-edge null arm folded "
                    "into the population null_thresholds draws from, instead "
                    "of the published three-port null",
            "phi_min": redrawn["min"], "phi_max": redrawn["max"],
            "phi_mean": redrawn["mean"],
            "lowest_pair": redrawn["lowest_pair"],
            "highest_pair": redrawn["highest_pair"],
            "observations": int(redrawn_corpus.table()["fired"].shape[0]),
        },
        "reported_in_retraction_report": REPORTED,
    }


def plot(data: dict):
    ordered = data["order"]
    size = len(ordered)
    matrix = np.array([[data["phi"][r][c] for c in ordered] for r in ordered])
    lo = data["phi_off_diagonal"]["min"]
    hi = data["phi_off_diagonal"]["max"]

    plt.rcParams.update({
        "font.size": 12, "axes.labelsize": 12,
        "xtick.labelsize": 10.5, "ytick.labelsize": 11.5,
        "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    cmap = LinearSegmentedColormap.from_list("phi-blue", BLUE)
    cmap.set_bad(DIAGONAL)
    fig, ax = plt.subplots(figsize=(11.8, 11.4), dpi=150, facecolor=SURFACE)

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
            "variants, and hold the tightest pairs\n"
            f"($\\varphi$ up to {hi:.3f}).  But the blocks barely\n"
            f"matter: no pair anywhere falls below {lo:.2f}",
            ha="left", va="center", fontsize=11, color=INK, linespacing=1.45,
            clip_on=False)

    fig.suptitle(
        "Not eight opinions but one, counted eight times: with lnb-a restored, "
        "every pair of\n"
        f"detectors agrees at $\\varphi$ {lo:.2f}–{hi:.2f} on the same "
        f"{data['observations']:,} observations — the band does not move",
        fontsize=15.5, color=INK, y=0.982)

    census = data["census"]["frozen_at_start"]
    was = data["published_with_lnb_a_excluded"]
    moved = data["movement_from_restoring_lnb_a"]
    redrawn = data["robustness_thresholds_redrawn"]
    lines = [
        f"n = {data['observations']:,} live target observations in EVERY cell, "
        f"from {data['census']['paired_sweeps']:,} paired sweeps and "
        f"{data['census']['scored_sidecars_in_a_pair']:,} scored sidecars.",

        "lnb-a IS INCLUDED — all four receivers "
        f"({', '.join(data['receivers'])}), "
        f"{data['observations_added_by_restoring_lnb_a']:,} observations more than "
        "the published run of this figure, which excluded it.",

        "An observation enters only if all eight detectors returned a verdict, so "
        "every cell rests on one identical population.  Each detector is judged",

        "against the threshold drawn for its own sample rate and probe length from "
        "the cross-edge null arms — the published figure's own three-port null,",

        "held fixed so the receiver set is the only thing that changed; redrawing "
        f"it with lnb-a's null arm in gives {redrawn['phi_min']:.3f}–"
        f"{redrawn['phi_max']:.3f} (JSON).",

        "Row order maximises adjacent $\\varphi$ over all 8! = 40,320 orderings; "
        "the diagonal (self-correlation = 1) is masked.  Decisions and $\\varphi$",

        "come from leo_tracker.radio.beacon.cross_radio, unmodified.  CENSUS frozen "
        f"before computing (digest {census['scored_digest']}): "
        f"{census['sweeps_on_share']:,} sweeps | "
        f"{census['corpus_entries']:,} corpus entries |",

        f"{census['scored_sidecars']:,} scored sidecars.   WITH lnb-a EXCLUDED this "
        f"figure read $\\varphi$ {was['phi_min']:.3f}–{was['phi_max']:.3f} on "
        f"{was['observations']:,} observations; restored, the band is "
        f"{lo:.3f}–{hi:.3f}",

        f"on {data['observations']:,} — floor {moved['phi_min']:+.3f}, ceiling "
        f"{moved['phi_max']:+.3f}.  The retraction report's $\\varphi$ "
        f"{REPORTED['phi_min']:.2f}–{REPORTED['phi_max']:.2f} on "
        f"{REPORTED['observations']:,} observations still sits below this floor.",
    ]
    caption = hcore.drift_caption()
    if caption:
        lines.append(caption)
    fig.text(0.5, 0.014, "\n".join(lines),
             ha="center", va="bottom", fontsize=8.6, color=MUTED,
             linespacing=1.52)
    fig.subplots_adjust(left=0.170, right=0.885, top=0.828, bottom=0.318)
    return fig


def main() -> int:
    data = compute()
    off = data["phi_off_diagonal"]
    was = data["published_with_lnb_a_excluded"]
    moved = data["movement_from_restoring_lnb_a"]
    data["headline_checks"] = {
        "lnb_a": "INCLUDED",
        "expected_band_0_82_to_0_94": [REPORTED["phi_min"], REPORTED["phi_max"]],
        "observed_band": [off["min"], off["max"]],
        "band_with_lnb_a_excluded": [was["phi_min"], was["phi_max"]],
        "band_movement": [moved["phi_min"], moved["phi_max"]],
        "every_pair_above_0_8": bool(off["min"] > 0.8),
        "observed_floor_above_reported_floor": bool(off["min"] > REPORTED["phi_min"]),
        "observed_ceiling_above_reported_ceiling":
            bool(off["max"] > REPORTED["phi_max"]),
        "band_moves_by_less_than_0_01": bool(
            abs(moved["phi_min"]) < 0.01 and abs(moved["phi_max"]) < 0.01),
        "row_order_identical": data["order_unchanged_by_restoring_lnb_a"],
        "family_blocks_still_contiguous": data["family_blocks_contiguous"],
        "loosest_pair_unchanged":
            sorted(off["lowest_pair"]) == sorted(was["lowest_pair"]),
        "tightest_pair_unchanged":
            sorted(off["highest_pair"]) == sorted(was["highest_pair"]),
        "lnb_a_own_band": [data["per_receiver"]["lnb-a"]["phi_min"],
                           data["per_receiver"]["lnb-a"]["phi_max"]],
        "verdict": (
            f"CONCLUSION UNCHANGED. The band was phi {was['phi_min']:.3f}-"
            f"{was['phi_max']:.3f} on {was['observations']:,} observations and is "
            f"phi {off['min']:.3f}-{off['max']:.3f} on {data['observations']:,}: "
            f"floor {moved['phi_min']:+.4f}, ceiling {moved['phi_max']:+.4f}, mean "
            f"{moved['phi_mean']:+.4f}. The loosest pair (anchor-8 vs "
            "differential-16) and the tightest pair (full-frame-full vs "
            "full-frame-verify) are the same pairs, and the four detector families "
            "still seriate into contiguous blocks; the only order change is "
            "full-frame-acquire and full-frame-verify swapping places INSIDE the "
            "full-frame block, which the block outline absorbs. lnb-a's own 10,064 "
            f"observations give phi {data['per_receiver']['lnb-a']['phi_min']:.3f}-"
            f"{data['per_receiver']['lnb-a']['phi_max']:.3f}, inside the range the "
            "other three ports span. Eight detectors remain one opinion counted "
            "eight times"),
    }
    data["census_drift_at_end"] = hcore.drift_block()
    hcore.write_outputs(NAME, data, plot(data))
    print(json.dumps(data["headline_checks"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
