#!/usr/bin/env python3
"""fire-rate-problem: why counting detections cannot rank detectors.

There is no injection anywhere in this corpus, so there is no known input.  A
detector that fires more often may be more sensitive, or merely looser, and the
fire count alone cannot separate those two.  This figure makes that concrete on
the real corpus:

  left    raw fire rate on sky against the MEASURED per-cell empty-sky rate p,
          both counted per live observation, p drawn from the cross-edge null
          arm by the repository's own threshold and false-alarm code.
  right   the same eight algorithms ranked three ways -- by fire count, by the
          model-free excess (fire rate minus p), and by the coincidence model's
          detection probability.  The rankings are not the same ranking, and
          the first and last are close to opposites.

EVERY number is computed from the corpus, via the cached reduction in
../cache/firerate-coincidence.npz (see ../firerate/build_cache.py).  The
detection probability in the third column is a MODEL OUTPUT and is labelled as
one on the figure; the middle column uses nothing but measurements and already
breaks the ranking, so the point does not rest on the model.

Usage:  python3 fire-rate-problem.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache" / "firerate-coincidence.npz"
OUT_PNG = HERE / "fire-rate-problem.png"
OUT_JSON = HERE / "fire-rate-problem.json"

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

    return {
        "figure": "fire-rate-problem",
        "question": "can a fire count rank detectors when there is no injection?",
        "answer": "no: fire rate tracks the measured empty-sky rate, and the "
                  "ranking changes under every correction",
        "cache": str(CACHE),
        "census": census,
        "census_recheck_at_end_of_run": drift(),
        "units": {"fire_rate": "fraction of live target observations where the "
                               "detector fired (max over the cell's candidates)",
                  "false_alarm_rate_p": "fraction of live cross-edge NULL "
                                        "observations where it fired, same decision",
                  "d": "MODEL OUTPUT of solve_coincidence, not a measurement"},
        "methods": methods,
        "per_method": rows,
        "fit": {"basis": "least squares over the 8 algorithm points",
                "slope": float(slope), "intercept": float(intercept),
                "pearson_r": r, "r_squared": r * r},
        "ranks": ranks,
        "spearman": {
            "fire_rate_vs_excess": spearman(ranks["fire_rate"],
                                            ranks["excess_fire_rate"]),
            "fire_rate_vs_d_mean": spearman(ranks["fire_rate"], ranks["d_mean"]),
            "excess_vs_d_mean": spearman(ranks["excess_fire_rate"],
                                         ranks["d_mean"]),
        },
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


def scatter(ax, data: dict) -> None:
    """Fire rate against the measured empty-sky rate, one point per algorithm."""
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
    ax.set_xlabel("MEASURED empty-sky rate $p$   (% of cross-edge null\n"
                  "observations on which the detector fires)")
    ax.set_ylabel("raw fire rate on sky   (% of target observations)")
    ax.set_title(f"how often a detector fires on sky rises with how often it\n"
                 f"fires on a NULL: $r^2$ = {fit['r_squared']:.2f} across the eight",
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
    ax.text(7.36, 34.40,
            f"least squares over the 8 points:\n"
            f"$r$ = {fit['pearson_r']:.2f},   $r^2$ = {fit['r_squared']:.2f}",
            ha="right", va="top", fontsize=9.5, color=MUTED, linespacing=1.4)


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
    ax.set_ylim(n_methods + 0.55, 0.45)
    ax.set_yticks(range(1, n_methods + 1))
    ax.set_ylabel("rank   (1 = top of the list)")
    ax.set_xticks(range(len(COLUMNS)))
    ax.set_xticklabels([label for _, label in COLUMNS], fontsize=10,
                       linespacing=1.5)
    ax.set_title("three rankings of the SAME eight detectors on the SAME\n"
                 "observations — and they are not the same ranking", pad=9)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def plot(data: dict) -> None:
    rows, methods = data["per_method"], data["methods"]
    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 11.5, "axes.titlesize": 12.5,
        "xtick.labelsize": 10.5, "ytick.labelsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
    })
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(12.6, 8.4), dpi=150,
        gridspec_kw={"width_ratios": [1.0, 1.04], "wspace": 0.27},
        facecolor=SURFACE)
    fig.subplots_adjust(left=0.070, right=0.988, top=0.805, bottom=0.295)

    scatter(left, data)
    slopegraph(right, data)

    fig.suptitle("A FIRE COUNT CANNOT RANK DETECTORS: WITH NOTHING INJECTED, FIRING MORE OFTEN\n"
                 "IS INDISTINGUISHABLE FROM FIRING MORE LOOSELY",
                 fontsize=15, fontweight="bold", color=INK, y=0.975,
                 linespacing=1.4)

    loose = max(methods, key=lambda m: rows[m]["fire_rate"])
    tight = min(methods, key=lambda m: rows[m]["fire_rate"])
    gap = 100 * (rows[loose]["fire_rate"] / rows[tight]["fire_rate"] - 1.0)
    stray = max(methods, key=lambda m: abs(
        rows[m]["fire_rate"] - (data["fit"]["slope"] * rows[m]["false_alarm_rate_p"]
                                + data["fit"]["intercept"])))
    fig.text(0.270, 0.205,
             f"Nothing is injected, so there is no known input: {SHORT[loose]} firing "
             f"{gap:.0f}% more often\n"
             f"than {SHORT[tight]} may be {gap:.0f}% more sensitive, or {gap:.0f}% looser, "
             "and the count cannot say.\n"
             f"$r^2$ = {data['fit']['r_squared']:.2f} of the fire-rate spread across the "
             f"eight is the null rate.  {SHORT[stray]} is the\n"
             f"one clear departure: it false-alarms like {SHORT['differential-32']} while "
             f"firing like {SHORT['glrt-64']}.",
             ha="center", va="top", fontsize=10, color=INK, linespacing=1.5)

    rho = data["spearman"]["fire_rate_vs_d_mean"]
    rho_excess = data["spearman"]["fire_rate_vs_excess"]
    best = min(methods, key=lambda m: data["ranks"]["d_mean"][m])
    fig.text(0.778, 0.205,
             f"Spearman $\\rho$ = {rho:+.2f} between the first column and the last. "
             "Correcting for\n"
             "the measured empty-sky rate does not nudge the order, it turns it over:\n"
             f"{SHORT[best]} fires LEAST of the eight and comes out the most sensitive. The\n"
             f"middle column is measurement only ($\\rho$ = {rho_excess:+.2f}): not the "
             "model's doing.",
             ha="center", va="top", fontsize=10, color=INK, linespacing=1.5)

    census = data["census"]
    n_target = max(rows[m]["target_observations"] for m in methods)
    n_null = max(rows[m]["null_observations"] for m in methods)
    n_cells = max(rows[m]["joined_cells"] for m in methods)
    for y, line in (
        (0.074, f"n = {n_target:,} live target observations and {n_null:,} live cross-edge "
                f"null observations, from {census['scored_sidecars_in_a_pair']:,} scored "
                f"sidecars in {census['paired_sweeps']:,} paired sweeps.  "
                "lnb-a excluded (dead port).  ff = full-frame, diff = differential."),
        (0.052, f"$\\bar d$ = $(d_A+d_B)/2$, solved by cross_radio.solve_coincidence on "
                f"{n_cells:,} joined matched-arm cells — A MODEL OUTPUT, NOT A MEASUREMENT "
                "(see coincidence-model)."),
        (0.030, "Bars are 95% marginal binomial intervals; all eight score the SAME "
                "observations, so differences are paired and better determined than the "
                "bars alone suggest."),
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
    print("fit", json.dumps(data["fit"]))


if __name__ == "__main__":
    main()
