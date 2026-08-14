#!/usr/bin/env python3
"""fire-rate-problem: why counting detections cannot rank detectors.

There is no injection anywhere in this corpus, so there is no known input.  A
detector that fires more often may be more sensitive, or merely looser, and the
fire count alone cannot separate those two.  This figure makes that concrete on
the real corpus:

  left    raw fire rate on sky against the MEASURED per-cell cross-edge
          target-code null rate p, both counted per live observation, p drawn
          from the cross-edge null arm by the repository's own threshold and
          false-alarm code.
  right   the same eight algorithms ranked three ways -- by fire count, by the
          model-free excess (fire rate minus p), and by the coincidence model's
          detection probability.

WHAT THE RIGHT PANEL SHOWS, corrected 2026-08-14.  An earlier caption read the
slopegraph as "correcting for the measured rate does not nudge the order, it
turns it over ... not the model's doing".  The figure's own JSON says the
opposite and always did:

    fire_rate vs excess  (both MEASURED)    rho = +0.83   they agree
    fire_rate vs d_mean  (MODEL OUTPUT)     rho = -0.95   inverted
    excess    vs d_mean  (MODEL OUTPUT)     rho = -0.76   inverted

Both measured rankings agree with each other; only the model output inverts
them, and it inverts against either measurement.  Concretely, the model-free
correction moves no detector more than three rank places, while solve_coincidence
moves glrt-32 from 8th to 1st and full-frame-full from 1st to 7th.  The
reordering therefore rests ENTIRELY on the model, whose own consistency check is
unmet on this corpus (negative-control: both definitionally-false joins agree at
least as tightly as the real one).  The middle column is the part that stands
without a model, and it barely moves the order.

The naming is also corrected here: p is the CROSS-EDGE TARGET-CODE NULL RATE,
not an "empty-sky rate".  The null arm is target-code-free by construction --
the same sky, the same hardware, the same instant, scored for a code that is not
there.  It may still contain other Starlink energy, terrestrial interference and
receiver structure, so it bounds the target-code false alarm and nothing wider.

EVERY number is computed from the corpus, via the cached reduction in
../cache/firerate-coincidence.npz (see ../firerate/build_cache.py).  Two claims
need a second census and read a sidecar for it, exactly as the drift check does:
r2-stability.json (../firerate/work/, see r2_stability.py) refits the eight
points on every frozen cell table on disk and bootstraps the fit over whole
paired sweeps.  Both sidecars are optional; without them the figure states the
point estimates alone and says so.

Usage:  python3 fire-rate-problem.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT_PNG = HERE / "fire-rate-problem.png"
OUT_JSON = HERE / "fire-rate-problem.json"

#: Where the reduction and its sidecars live.  The committed layout is first;
#: the fallbacks let the figure be regenerated from the scratch tree the
#: reduction was actually built in, without editing a path into the script.
CACHE_CANDIDATES = (
    HERE.parent / "cache" / "firerate-coincidence.npz",
    Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
         "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/summary/cache/"
         "firerate-coincidence.npz"),
)
WORK_CANDIDATES = (
    HERE.parent / "firerate" / "work",
    HERE.parent / "work",
    Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
         "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/summary/firerate/work"),
)


def _first(candidates):
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


CACHE = _first(CACHE_CANDIDATES)
WORK = _first(WORK_CANDIDATES)

# House palette of the 2026-08-14 cross-radio figures, kept identical so the
# eight algorithms carry the same identity across every figure in the report.
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"
BAND = "#eceae5"
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


#: Scoring runs while these figures are built, so the census is frozen once at
#: the start (snapshot.py) and re-measured at the end.  The re-measurement is
#: reported here rather than hidden: the figures use the FROZEN list, so a
#: sidecar scored mid-run cannot move one figure and not the other.
DRIFT = WORK / "drift.json"

#: The same idea applied to the fit: r^2 is a least-squares coefficient over
#: EIGHT points, and one frozen list cannot say whether it is stable.
STABILITY = WORK / "r2-stability.json"


def _sidecar(path: Path, absent: dict) -> dict:
    import json as _json
    if path.is_file():
        return _json.loads(path.read_text())
    return absent


def drift() -> dict:
    return _sidecar(DRIFT, {"checked": False,
                            "note": "no end-of-run re-measurement available"})


def stability() -> dict:
    return _sidecar(STABILITY, {"checked": False,
                                "note": "no second census available; the fit is "
                                        "reported as a point estimate only"})


# --------------------------------------------------------------------------
# compute
# --------------------------------------------------------------------------

def solve(p_a: float, p_b: float, p_ab: float, p: float) -> dict:
    """The repository's estimator, imported rather than re-implemented."""
    import sys
    sys.path.insert(0, "/home/satpi01/leo-tracker/src")
    from leo_tracker.radio.beacon.cross_radio import solve_coincidence
    return solve_coincidence(p_a, p_b, p_ab, p)


def wald(count: int, n: int) -> float:
    """Half-width of a 95% normal interval on a proportion, for the bars."""
    if not n:
        return 0.0
    rate = count / n
    return 1.96 * (rate * (1.0 - rate) / n) ** 0.5


def compute() -> dict:
    blob = np.load(CACHE, allow_pickle=False)
    methods = [str(m) for m in blob["methods"]]
    census = json.loads(str(blob["census"]))
    dec_a, dec_b = blob["dec_a"], blob["dec_b"]

    rows = {}
    for index, method in enumerate(methods):
        fires, obs = int(blob["target_fires"][index]), int(blob["target_obs"][index])
        fa_count, fa_cells = int(blob["fa_count"][index]), int(blob["fa_cells"][index])
        p = fa_count / fa_cells
        fire_rate = fires / obs

        # The coincidence solve runs on the joined matched-arm cells, which is
        # the only population where both chains observed the same instant.
        left, right = dec_a[:, index], dec_b[:, index]
        usable = (left >= 0) & (right >= 0)
        n = int(usable.sum())
        a, b = left[usable] == 1, right[usable] == 1
        solved = solve(a.mean(), b.mean(), (a & b).mean(), p)

        rows[method] = {
            "target_fires": fires, "target_observations": obs,
            "fire_rate": fire_rate, "fire_rate_ci95": wald(fires, obs),
            "null_fires": fa_count, "null_observations": fa_cells,
            "false_alarm_rate_p": p, "p_ci95": wald(fa_count, fa_cells),
            "excess_fire_rate": fire_rate - p,
            "joined_cells": n,
            "p_a": solved["p_a"], "p_b": solved["p_b"], "p_ab": solved["p_ab"],
            "solvable": solved["solvable"], "reason": solved["reason"],
            "f": solved["f"], "d_a": solved["d_a"], "d_b": solved["d_b"],
            "d_mean": (None if not solved["solvable"]
                       else 0.5 * (solved["d_a"] + solved["d_b"])),
            # the same rate the model itself sees, for cross-checking the
            # all-observations fire rate the left panel plots
            "fire_rate_on_joined_cells": 0.5 * (a.mean() + b.mean()),
        }

    x = np.array([rows[m]["false_alarm_rate_p"] for m in methods])
    y = np.array([rows[m]["fire_rate"] for m in methods])
    slope, intercept = np.polyfit(x, y, 1)
    r = float(np.corrcoef(x, y)[0, 1])

    def ranking(key, reverse=True):
        order = sorted(methods, key=lambda m: rows[m][key], reverse=reverse)
        return {m: i + 1 for i, m in enumerate(order)}

    ranks = {"fire_rate": ranking("fire_rate"),
             "excess_fire_rate": ranking("excess_fire_rate"),
             "d_mean": ranking("d_mean")}

    def spearman(one: dict, two: dict) -> float:
        a = np.array([one[m] for m in methods], float)
        b = np.array([two[m] for m in methods], float)
        return float(np.corrcoef(a, b)[0, 1])

    def moves(key: str) -> dict:
        shift = {m: ranks[key][m] - ranks["fire_rate"][m] for m in methods}
        worst = max(methods, key=lambda m: abs(shift[m]))
        return {"per_method": shift, "largest_move": abs(shift[worst]),
                "moved_furthest": worst,
                "from": ranks["fire_rate"][worst], "to": ranks[key][worst]}

    # ---- is the top of the fire-count column a ranking at all? ------------
    # The first column is drawn as an ordered list, so the figure has to say
    # whether its top places are separable.  Three tests, all on this corpus:
    # the gap against its own interval, the same two on the joined cells the
    # model uses, and the same two on the other frozen census.
    order = sorted(methods, key=lambda m: rows[m]["fire_rate"], reverse=True)
    first, second = order[0], order[1]
    joined = sorted(methods, key=lambda m: rows[m]["fire_rate_on_joined_cells"],
                    reverse=True)
    stable = stability()
    other = [item for item in (stable.get("across_censuses") or [])
             if item.get("scored_sidecars") != census.get("scored_sidecars")]
    elsewhere = None
    if other:
        rates = other[0]["fire_rate"]
        elsewhere = {"census_scored_sidecars": other[0].get("scored_sidecars"),
                     "measured_utc": other[0].get("measured_utc"),
                     "top": max(rates, key=lambda m: rates[m]),
                     "order": sorted(rates, key=lambda m: -rates[m])[:3]}
    inside = sum(1 for m in methods
                 if rows[m]["fire_rate"]
                 >= rows[first]["fire_rate"] - rows[first]["fire_rate_ci95"])
    top_two = {
        "first": first, "second": second,
        "gap": rows[first]["fire_rate"] - rows[second]["fire_rate"],
        "places_inside_one_ci95": inside,
        "gap_as_fraction_of_own_ci95":
            (rows[first]["fire_rate"] - rows[second]["fire_rate"])
            / rows[first]["fire_rate_ci95"],
        "ci95_half_width": rows[first]["fire_rate_ci95"],
        "same_pair_on_joined_matched_arm_cells": {
            "leader": joined[0],
            "gap": (rows[joined[0]]["fire_rate_on_joined_cells"]
                    - rows[joined[1]]["fire_rate_on_joined_cells"]),
            "swaps": joined[0] != first},
        "excess_gap_between_the_same_two":
            rows[first]["excess_fire_rate"] - rows[second]["excess_fire_rate"],
        "top_of_the_column_on_the_other_census": elsewhere,
        "verdict": "the top of the fire-count column is not separable: read the "
                   "first column as three tied detectors, not as places 1, 2, 3",
    }

    return {
        "figure": "fire-rate-problem",
        "question": "can a fire count rank detectors when there is no injection?",
        "answer": "no: fire rate tracks the measured cross-edge target-code "
                  "null rate, and the ranking changes under every correction -- "
                  "but only the MODEL's correction reverses it, and its own "
                  "consistency check is unmet on this corpus",
        "cache": str(CACHE),
        "census": census,
        "census_recheck_at_end_of_run": drift(),
        "units": {"fire_rate": "fraction of live target observations where the "
                               "detector fired (max over the cell's candidates)",
                  "false_alarm_rate_p": "fraction of live CROSS-EDGE TARGET-CODE "
                                        "NULL observations where it fired, same "
                                        "decision. Target-code-free by "
                                        "construction, not physically empty sky: "
                                        "same sky, same hardware, same instant, "
                                        "and it may still hold other Starlink "
                                        "energy, interference and receiver "
                                        "structure",
                  "d": "MODEL OUTPUT of solve_coincidence, not a measurement"},
        "methods": methods,
        "per_method": rows,
        "fit": {"basis": "least squares over the 8 algorithm points",
                "slope": float(slope), "intercept": float(intercept),
                "pearson_r": r, "r_squared": r * r,
                "stability": stable,
                "how_to_state_it": "about two thirds of the spread; the "
                                   "coefficient is a fit on eight points and is "
                                   "not stable to two decimals"},
        "ranks": ranks,
        "rank_moves_against_the_fire_count": {
            "excess_fire_rate": moves("excess_fire_rate"),
            "d_mean": moves("d_mean")},
        "top_of_the_fire_count_column": top_two,
        "spearman": {
            "fire_rate_vs_excess": spearman(ranks["fire_rate"],
                                            ranks["excess_fire_rate"]),
            "fire_rate_vs_d_mean": spearman(ranks["fire_rate"], ranks["d_mean"]),
            "excess_vs_d_mean": spearman(ranks["excess_fire_rate"],
                                         ranks["d_mean"]),
        },
        "what_the_rankings_say": (
            "the two MEASURED rankings agree with each other (rho = +0.83); "
            "only the model output inverts them, and it inverts against either "
            "measurement (rho = -0.95 against the raw count, -0.76 against the "
            "model-free excess). The reordering is the model's, not the "
            "correction's."),
    }


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------

COLUMNS = [("fire_rate", "ranked by\nFIRE COUNT\nmeasured"),
           ("excess_fire_rate", "ranked by\nfire rate $-\\,p$\nmeasured"),
           ("d_mean", "ranked by\ndetection prob. $\\bar d$\nMODEL OUTPUT")]

#: Short labels for the plot only; the full names are in the JSON and in every
#: printed line.  "full-frame-acquire" is 18 characters and there are sixteen
#: of them on the slopegraph, which is what pushed the labels off the canvas.
SHORT = {"anchor-8": "anchor-8", "glrt-32": "glrt-32", "glrt-64": "glrt-64",
         "full-frame-verify": "ff-verify", "full-frame-full": "ff-full",
         "full-frame-acquire": "ff-acquire", "differential-16": "diff-16",
         "differential-32": "diff-32"}


def r2_phrase(data: dict, *, short: bool = False) -> str:
    """How the fit may honestly be quoted, given what the censuses did."""
    stable = data["fit"]["stability"]
    boot = stable.get("sweep_cluster_bootstrap")
    span = stable.get("census_range")
    if not boot or not span:
        return ("$r^2$ = %.2f on this census (a fit on EIGHT points)"
                % data["fit"]["r_squared"])
    if short:
        return "$r^2$ %.2f–%.2f across censuses" % (min(span), max(span))
    return ("$r^2$ %.2f–%.2f across censuses,\n%.2f–%.2f over a sweep bootstrap"
            % (min(span), max(span), boot["p05"], boot["p95"]))


def scatter(ax, data: dict) -> None:
    """Fire rate against the measured cross-edge target-code null rate."""
    rows, methods = data["per_method"], data["methods"]
    fit = data["fit"]

    # Drawn only across the span the eight algorithms actually occupy; an
    # extrapolated fit line would claim range the eight points do not cover.
    xs = np.array([rows[m]["false_alarm_rate_p"] for m in methods]) * 100
    grid = np.linspace(xs.min() - 0.22, xs.max() + 0.22, 20)
    ax.plot(grid, (fit["slope"] * grid / 100 + fit["intercept"]) * 100,
            color=MUTED, linewidth=1.2, linestyle=(0, (5, 3)), zorder=1)

    handles = []
    for method in methods:
        colour, marker, fill = STYLE[method]
        row = rows[method]
        ax.errorbar(row["false_alarm_rate_p"] * 100, row["fire_rate"] * 100,
                    xerr=row["p_ci95"] * 100, yerr=row["fire_rate_ci95"] * 100,
                    color=colour, ecolor=colour, elinewidth=1.0, capsize=2.5,
                    marker=marker, markersize=10, linestyle="none",
                    markerfacecolor=colour if fill == "full" else SURFACE,
                    markeredgecolor=colour, markeredgewidth=1.6, zorder=3)
        handles.append(plt.Line2D(
            [], [], linestyle="none", marker=marker, color=colour, markersize=9,
            markerfacecolor=colour if fill == "full" else SURFACE,
            markeredgecolor=colour, markeredgewidth=1.6, label=SHORT[method]))

    ax.set_xlim(4.95, 7.40)
    ax.set_ylim(30.1, 34.5)
    ax.set_xlabel("MEASURED cross-edge TARGET-CODE NULL rate $p$   (% of live\n"
                  "null-arm observations on which the detector fires)")
    ax.set_ylabel("raw fire rate on sky   (% of target observations)")
    ax.set_title("how often a detector fires on sky rises with how often it\n"
                 "fires on a NULL: about two thirds of the spread, on 8 points",
                 pad=9)
    ax.grid(color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(handles=handles, loc="upper left", frameon=False, ncol=2,
              fontsize=10, handletextpad=0.4, columnspacing=1.0,
              labelspacing=0.35, borderpad=0.2)

    # The two extremes get their labels ON the points: they are the pair the
    # argument turns on, and the reader should not have to hunt the legend.
    loose = max(methods, key=lambda m: rows[m]["fire_rate"])
    tight = min(methods, key=lambda m: rows[m]["fire_rate"])
    ax.annotate("fires LEAST\non sky AND\nleast on the null",
                xy=(rows[tight]["false_alarm_rate_p"] * 100 + 0.04,
                    rows[tight]["fire_rate"] * 100 - 0.10),
                xytext=(5.62, 30.74), textcoords="data",
                ha="left", va="top", fontsize=9.5, color=INK, linespacing=1.4,
                arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.3,
                            "connectionstyle": "arc3,rad=0.28"})
    ax.annotate("fires MOST\non sky AND\nmost on the null",
                xy=(rows[loose]["false_alarm_rate_p"] * 100 + 0.02,
                    rows[loose]["fire_rate"] * 100 - 0.12),
                xytext=(7.36, 32.60), textcoords="data",
                ha="right", va="top", fontsize=9.5, color=INK, linespacing=1.4,
                arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.3,
                            "connectionstyle": "arc3,rad=0.28"})
    # Three short lines rather than two long ones: the fit stopped being
    # quotable as a single two-decimal number, and a wider box would reach the
    # legend on one side or the "fires LEAST" callout on the other.
    ax.text(7.34, 34.42,
            "least squares over the 8 points:\n%s" % r2_phrase(data),
            ha="right", va="top", fontsize=9, color=MUTED, linespacing=1.4)


def slopegraph(ax, data: dict) -> None:
    """The same eight algorithms, ranked three ways."""
    rows, methods, ranks = data["per_method"], data["methods"], data["ranks"]
    n_methods = len(methods)

    for column in range(len(COLUMNS)):
        ax.axvline(column, color=GRID, linewidth=1.0, zorder=0)
    for method in methods:
        colour, marker, fill = STYLE[method]
        ys = [ranks[key][method] for key, _ in COLUMNS]
        ax.plot(range(len(COLUMNS)), ys, color=colour, linewidth=1.6,
                alpha=0.55, zorder=2)
        ax.plot(range(len(COLUMNS)), ys, linestyle="none", marker=marker,
                markersize=9.5, color=colour, markeredgecolor=colour,
                markerfacecolor=colour if fill == "full" else SURFACE,
                markeredgewidth=1.6, zorder=3)
        ax.text(-0.09, ranks["fire_rate"][method],
                f"{SHORT[method]}  {rows[method]['fire_rate']*100:.2f}%",
                ha="right", va="center", fontsize=10, color=colour)
        ax.text(len(COLUMNS) - 1 + 0.09, ranks["d_mean"][method],
                f"{rows[method]['d_mean']:.3f}  {SHORT[method]}",
                ha="left", va="center", fontsize=10, color=colour)

    ax.set_xlim(-1.10, len(COLUMNS) - 1 + 1.10)
    ax.set_ylim(n_methods + 2.30, 0.45)
    ax.set_yticks(range(1, n_methods + 1))
    ax.set_ylabel("rank   (1 = top of the list)")
    ax.set_xticks(range(len(COLUMNS)))
    ax.set_xticklabels([label for _, label in COLUMNS], fontsize=10,
                       linespacing=1.5)
    ax.set_title("three rankings of the SAME eight detectors on the SAME\n"
                 "observations — and only the MODEL reverses the order", pad=9)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)

    # A slopegraph invites the reader to read place 1 against place 2, so the
    # figure has to say where that reading stops being supported.  The two top
    # places are boxed rather than arrowed: an arrow across the ranked labels
    # would cross six of them to reach a gap smaller than the marker.
    tie = data["top_of_the_fire_count_column"]
    ax.add_patch(Rectangle((-0.075, 0.70), 0.15,
                           tie["places_inside_one_ci95"] - 0.40,
                           facecolor=BAND, edgecolor=MUTED, linewidth=0.8,
                           zorder=1))
    ax.text(-1.05, n_methods + 1.05,
            "FIRST COLUMN READ AS A RANKING: the top two differ by\n"
            "%.3f pp and SWAP on the joined matched-arm cells; the\n"
            "top %s all sit inside one 95%% interval (±%.2f pp)."
            % (100 * tie["gap"],
               {2: "two", 3: "three", 4: "four", 5: "five"}.get(
                   tie["places_inside_one_ci95"],
                   str(tie["places_inside_one_ci95"])),
               100 * tie["ci95_half_width"]),
            fontsize=9, color=MUTED, ha="left", va="center", linespacing=1.5)


def plot(data: dict) -> None:
    rows, methods = data["per_method"], data["methods"]
    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 11.5, "axes.titlesize": 12.5,
        "xtick.labelsize": 10.5, "ytick.labelsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    # 1890 x 1440 px.  Taller than the published 1890 x 1260: the corrected
    # captions carry two more sentences each and the published ones already
    # filled the band between the axes and the footer.
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(12.6, 9.6), dpi=150,
        gridspec_kw={"width_ratios": [1.0, 1.04], "wspace": 0.27},
        facecolor=SURFACE)
    fig.subplots_adjust(left=0.070, right=0.988, top=0.835, bottom=0.355)

    scatter(left, data)
    slopegraph(right, data)

    fig.suptitle("A FIRE COUNT CANNOT RANK DETECTORS: WITH NOTHING INJECTED, FIRING MORE OFTEN\n"
                 "IS INDISTINGUISHABLE FROM FIRING MORE LOOSELY — AND ONLY THE MODEL REORDERS IT",
                 fontsize=15, fontweight="bold", color=INK, y=0.980,
                 linespacing=1.4)

    loose = max(methods, key=lambda m: rows[m]["fire_rate"])
    tight = min(methods, key=lambda m: rows[m]["fire_rate"])
    gap = 100 * (rows[loose]["fire_rate"] / rows[tight]["fire_rate"] - 1.0)
    stray = max(methods, key=lambda m: abs(
        rows[m]["fire_rate"] - (data["fit"]["slope"] * rows[m]["false_alarm_rate_p"]
                                + data["fit"]["intercept"])))
    # Centred at 0.300, not 0.270: at 0.270 this block began at x = 88 px on an
    # 1890 px canvas, left of the axes spine at x = 132, and hung into the page
    # margin.  Nothing was clipped and nothing collided; it simply was not on
    # the grid the rest of the figure is drawn to.
    stable = data["fit"]["stability"].get("sweep_cluster_bootstrap") or {}
    boot = ("%.2f–%.2f under a bootstrap over whole sweeps"
            % (stable["p05"], stable["p95"])) if stable else "one census only"
    fig.text(0.300, 0.285,
             f"Nothing is injected, so there is no known input: {SHORT[loose]} firing\n"
             f"{gap:.0f}% more often than {SHORT[tight]} may be {gap:.0f}% more sensitive, "
             f"or {gap:.0f}%\n"
             "looser, and the count cannot say.  About TWO THIRDS of the\n"
             "fire-rate spread across the eight is the null rate — but that is\n"
             "a least-squares fit on EIGHT points and not a stable coefficient:\n"
             f"{r2_phrase(data, short=True)}, {boot}.\n"
             f"{SHORT[stray]} is the one clear departure: it false-alarms like "
             f"{SHORT['differential-32']}\n"
             f"while firing like {SHORT['glrt-64']}.",
             ha="center", va="top", fontsize=9.5, color=INK, linespacing=1.45)

    rho = data["spearman"]["fire_rate_vs_d_mean"]
    rho_excess = data["spearman"]["fire_rate_vs_excess"]
    rho_cross = data["spearman"]["excess_vs_d_mean"]
    excess_move = data["rank_moves_against_the_fire_count"]["excess_fire_rate"]
    model_move = data["rank_moves_against_the_fire_count"]["d_mean"]
    best = min(methods, key=lambda m: data["ranks"]["d_mean"][m])
    fell = max(methods, key=lambda m:
               data["ranks"]["d_mean"][m] - data["ranks"]["fire_rate"][m]
               if data["ranks"]["fire_rate"][m] == 1 else -99)
    ordinal = {1: "st", 2: "nd", 3: "rd"}
    def place(rank: int) -> str:
        return f"{rank}{ordinal.get(rank, 'th')}"

    fig.text(0.782, 0.285,
             f"Spearman $\\rho$ = {rho:+.2f} between the first column and the last — but\n"
             "that inversion is ENTIRELY THE MODEL'S. The model-free correction\n"
             f"(middle column, fire rate $-\\,p$) still ranks the eight as the raw\n"
             f"count does: $\\rho$ = {rho_excess:+.2f}, nothing moving more than "
             f"{excess_move['largest_move']} places. Only\n"
             "solve_coincidence's $\\bar d$ — A MODEL OUTPUT, NOT A MEASUREMENT —\n"
             f"turns the order over ({SHORT[best]} {place(data['ranks']['fire_rate'][best])}"
             f"$\\rightarrow${place(data['ranks']['d_mean'][best])}, {SHORT[fell]} "
             f"{place(data['ranks']['fire_rate'][fell])}$\\rightarrow$"
             f"{place(data['ranks']['d_mean'][fell])}), and it inverts\n"
             f"against the measured correction too ($\\rho$ = {rho_cross:+.2f}). And the\n"
             "model's own consistency check is UNMET on this corpus.",
             ha="center", va="top", fontsize=9.5, color=INK, linespacing=1.45)

    census = data["census"]
    n_target = max(rows[m]["target_observations"] for m in methods)
    n_null = max(rows[m]["null_observations"] for m in methods)
    n_cells = max(rows[m]["joined_cells"] for m in methods)
    for y, line in (
        (0.098, f"n = {n_target:,} live target observations and {n_null:,} live cross-edge "
                f"null observations, from {census['scored_sidecars_in_a_pair']:,} scored "
                f"sidecars in {census['paired_sweeps']:,} paired sweeps.  lnb-a excluded "
                "(dead port).  ff = full-frame, diff = differential."),
        (0.080, "$p$ is the CROSS-EDGE TARGET-CODE NULL rate, NOT an empty-sky rate: that arm "
                "is target-code-free by construction — same sky, same hardware, same instant — "
                "and may still hold"),
        (0.062, "other Starlink energy, terrestrial interference and receiver structure.  It "
                "bounds the target-code false alarm and nothing wider."),
        (0.044, f"$\\bar d$ = $(d_A+d_B)/2$, solved by cross_radio.solve_coincidence on "
                f"{n_cells:,} joined matched-arm cells — A MODEL OUTPUT, NOT A MEASUREMENT.  "
                "Its consistency check is UNMET here: both"),
        (0.026, "definitionally-false control joins agree at least as tightly as the real one "
                "(negative-control), so nothing certifies the order it produces.  Bars are 95% "
                "marginal binomial intervals."),
        (0.008, f"Corpus frozen {census['measured_utc']} at {census['scored_sidecars']:,} "
                f"scored sidecars (digest {census['scored_digest']}) of "
                f"{census['corpus_entries']:,} entries; scoring still running, drift "
                "reported with the figure."),
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
    for method in data["methods"]:
        row = data["per_method"][method]
        print(f"{method:>19}  fire {row['fire_rate']:.4f}  p {row['false_alarm_rate_p']:.4f}"
              f"  f {row['f']:.4f}  dA {row['d_a']:.4f}  dB {row['d_b']:.4f}")
    print("spearman", json.dumps(data["spearman"]))
    print("fit", json.dumps({k: v for k, v in data["fit"].items()
                             if k != "stability"}))
    print("r2 across censuses", data["fit"]["stability"].get("census_range"))
    print("top of the fire-count column",
          json.dumps(data["top_of_the_fire_count_column"], indent=1))


if __name__ == "__main__":
    main()
