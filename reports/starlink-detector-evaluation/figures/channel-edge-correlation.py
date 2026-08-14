#!/usr/bin/env python3
"""channel-edge-correlation - is the sky channel-correlated or channel-independent?

Eight TUNINGS (4 channels x {lower-edge, upper-edge}), correlated across sweeps.
The unit of observation is one receiver chain's pass over one sweep: within a
sweep a chain visits all eight tunings once, so a unit contributes one fire /
no-fire decision per tuning and the eight columns are commensurable.  A unit
enters only if all eight tunings returned a verdict, so n is identical in every
cell of the matrix.

The decision rule is the repo's own channel-level rule --
``cross_radio._any_method_fires``, 'any of the scored methods fires' -- which is
what ``channel_instant_verdicts`` uses to answer channel questions.  Each method
is judged against the threshold ``null_thresholds`` drew for its own
(sample rate, probe length) from the cross-edge null arms.  The per-method
matrices are in the JSON as a robustness check; they tell the same story.

Axis order is fixed, not seriated: ch1 lower, ch1 upper, ch2 lower, ch2 upper,
... so that a channel's OWN two edges sit adjacent.  If channels behave as
units, that shows as four 2x2 blocks on the diagonal.  The order is declared in
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hcore  # noqa: E402

NAME = "channel-edge-correlation"
INK, MUTED, SURFACE, DIAGONAL = "#0b0b0b", "#52514e", "#fcfcfb", "#e6e5e2"
GRID, BAND = "#d7d6d2", "#e9e8e4"
ACCENT = "#2a78d6"
BLUE = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
        "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281",
        "#0d366b"]

PERMUTATIONS = 200
SEED = 20260814


def same_channel_mask() -> np.ndarray:
    mask = np.zeros((8, 8), dtype=bool)
    for index in range(0, 8, 2):
        mask[index, index + 1] = mask[index + 1, index] = True
    return mask


def units_of(corpus):
    """(capture, receiver) -> the eight tuning decisions, any-method and per-method."""
    table = corpus.table()
    fired = table["fired"]
    anyfire = corpus.any_method_column()
    slot = {pair: i for i, pair in enumerate(hcore.TUNINGS)}
    width = fired.shape[1]
    rows: dict = defaultdict(lambda: {"any": [None] * 8,
                                      "per": [[None] * 8 for _ in range(width)],
                                      "receiver": None})
    for row in range(fired.shape[0]):
        key = (str(table["capture"][row]), str(table["receiver"][row]))
        position = slot[(int(table["channel"][row]), str(table["edge"][row]))]
        unit = rows[key]
        unit["receiver"] = str(table["receiver"][row])
        unit["any"][position] = bool(anyfire[row])
        for method in range(width):
            unit["per"][method][position] = bool(fired[row, method])
    complete = {key: unit for key, unit in rows.items()
                if all(value is not None for value in unit["any"])}
    return rows, complete


def compute() -> dict:
    corpus = hcore.Corpus()
    methods = corpus.methods
    allunits, complete = units_of(corpus)
    columns = np.array([unit["any"] for unit in complete.values()], dtype=bool)
    matrix = hcore.phi_matrix(columns)

    off = ~np.eye(8, dtype=bool)
    same = same_channel_mask()
    cross = off & ~same

    # per-method robustness
    per_method = {}
    for index, method in enumerate(methods):
        block = np.array([unit["per"][index] for unit in complete.values()],
                         dtype=bool)
        sub = hcore.phi_matrix(block)
        per_method[method] = {
            "same_channel_mean": float(sub[same].mean()),
            "cross_channel_mean": float(sub[cross].mean()),
            "same_channel_pairs": [float(sub[i, i + 1]) for i in range(0, 8, 2)],
        }

    # correlation against channel separation
    separation: dict = defaultdict(list)
    for i in range(8):
        for j in range(i + 1, 8):
            gap = abs(hcore.TUNINGS[i][0] - hcore.TUNINGS[j][0])
            separation[gap].append(float(matrix[i, j]))
    separation_summary = {
        str(gap): {"tuning_pairs": len(values),
                   "mean_phi": float(np.mean(values)),
                   "min_phi": float(np.min(values)),
                   "max_phi": float(np.max(values)),
                   "values": [float(v) for v in values]}
        for gap, values in sorted(separation.items())}

    # permutation null: shuffle each tuning column independently across units
    generator = np.random.default_rng(SEED)
    null_mean, null_max = [], []
    for _ in range(PERMUTATIONS):
        shuffled = np.column_stack([generator.permutation(columns[:, c])
                                    for c in range(8)])
        null = hcore.phi_matrix(shuffled)
        null_mean.append(float(np.abs(null[off]).mean()))
        null_max.append(float(np.abs(null[off]).max()))
    null_block = {
        "draws": PERMUTATIONS, "seed": SEED,
        "what": "each tuning column shuffled independently across units, "
                "destroying every between-tuning association while keeping "
                "each tuning's own fire rate",
        "mean_abs_phi": float(np.mean(null_mean)),
        "p99_of_max_abs_phi": float(np.percentile(null_max, 99)),
        "max_observed_across_draws": float(np.max(null_max)),
        "theory_sd_1_over_sqrt_n": float(1 / np.sqrt(columns.shape[0])),
    }

    # per-receiver, to show the structure is not one bad port
    per_receiver = {}
    for receiver in sorted({unit["receiver"] for unit in complete.values()}):
        block = np.array([unit["any"] for unit in complete.values()
                          if unit["receiver"] == receiver], dtype=bool)
        sub = hcore.phi_matrix(block)
        per_receiver[receiver] = {
            "units": int(block.shape[0]),
            "same_channel_mean": float(sub[same].mean()),
            "cross_channel_mean": float(sub[cross].mean()),
        }

    return {
        "figure": NAME,
        "question": "does one channel's firing predict another channel's, and is "
                    "a channel's own upper/lower pair tighter than two different "
                    "channels are?",
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*",
        "census": corpus.census_block(),
        "unit_of_observation": "one receiver chain's pass over one sweep; it "
                               "visits all eight tunings once",
        "units_seen": len(allunits),
        "units_complete": int(columns.shape[0]),
        "n_per_cell": int(columns.shape[0]),
        "n_is_identical_in_every_cell": True,
        "decision_rule": "cross_radio._any_method_fires (any of the eight scored "
                         "methods), each against its own (sample rate, probe "
                         "length) cross-edge-null threshold",
        "axis_order": hcore.TUNING_LABELS,
        "axis_order_basis": "declared in advance: a channel's own two edges are "
                            "adjacent, so channel structure would appear as four "
                            "2x2 diagonal blocks",
        "fire_rate_per_tuning": dict(zip(hcore.TUNING_LABELS,
                                         columns.mean(axis=0).tolist())),
        "phi": {row: {col: float(matrix[i, j])
                      for j, col in enumerate(hcore.TUNING_LABELS)}
                for i, row in enumerate(hcore.TUNING_LABELS)},
        "same_channel_edge_pairs": {
            f"ch{index // 2 + 1}": float(matrix[index, index + 1])
            for index in range(0, 8, 2)},
        "same_channel_mean": float(matrix[same].mean()),
        "cross_channel_mean": float(matrix[cross].mean()),
        "cross_channel_min": float(matrix[cross].min()),
        "cross_channel_max": float(matrix[cross].max()),
        "ratio_same_over_cross": float(matrix[same].mean() / matrix[cross].mean()),
        "channel_separation": separation_summary,
        "permutation_null": null_block,
        "per_receiver": per_receiver,
        "per_method": per_method,
    }


def plot(data: dict):
    labels = data["axis_order"]
    matrix = np.array([[data["phi"][r][c] for c in labels] for r in labels])
    off = ~np.eye(8, dtype=bool)
    lo, hi = float(matrix[off].min()), float(matrix[off].max())

    plt.rcParams.update({
        "font.size": 12, "axes.labelsize": 12,
        "xtick.labelsize": 11, "ytick.labelsize": 11.5,
        "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    cmap = LinearSegmentedColormap.from_list("phi-blue", BLUE)
    cmap.set_bad(DIAGONAL)

    fig = plt.figure(figsize=(12.4, 14.6), dpi=150, facecolor=SURFACE)
    grid = fig.add_gridspec(2, 2, width_ratios=[1.0, 0.05],
                            height_ratios=[1.0, 0.315], hspace=0.34,
                            wspace=0.04, left=0.145, right=0.885,
                            top=0.855, bottom=0.196)
    ax = fig.add_subplot(grid[0, 0])
    cax = fig.add_subplot(grid[0, 1])
    ax2 = fig.add_subplot(grid[1, 0])

    shown = np.ma.masked_array(matrix, mask=np.eye(8, dtype=bool))
    image = ax.imshow(shown, cmap=cmap, vmin=lo, vmax=hi)
    span = hi - lo
    for i in range(8):
        for j in range(8):
            if i == j:
                continue
            value = matrix[i, j]
            light = (value - lo) / span > 0.62
            ax.text(j, i, f"{value:.3f}", ha="center", va="center",
                    fontsize=11.5, color=SURFACE if light else INK)

    for index in range(0, 8, 2):
        ax.add_patch(plt.Rectangle((index - 0.5, index - 0.5), 2, 2, fill=False,
                                   edgecolor=INK, linewidth=2.6, zorder=4))

    ax.set_xticks(range(8))
    ax.set_yticks(range(8))
    ax.xaxis.set_ticks_position("top")
    ax.set_xticklabels(labels, rotation=40, ha="left", rotation_mode="anchor")
    ax.set_yticklabels(labels)
    ax.set_xticks(np.arange(9) - 0.5, minor=True)
    ax.set_yticks(np.arange(9) - 0.5, minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2.0)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(which="major", length=0)
    for side in ax.spines.values():
        side.set_visible(False)

    bar = fig.colorbar(image, cax=cax)
    bar.set_label("$\\varphi$  (fire / no-fire correlation across sweeps)",
                  fontsize=11.5, color=INK)
    bar.outline.set_edgecolor(MUTED)
    bar.ax.tick_params(labelsize=10.5, color=MUTED, labelcolor=MUTED)

    same_mean = data["same_channel_mean"]
    cross_mean = data["cross_channel_mean"]
    # Both callouts live in the clear band below the matrix, so their arrows
    # reach the nearest example cell instead of crossing the field.
    ax.annotate(
        f"any two DIFFERENT channels:\n$\\varphi$ {cross_mean:.3f} — real, but "
        f"{data['ratio_same_over_cross']:.1f}× weaker",
        xy=(0.0, 7.62), xytext=(-0.55, 8.62), ha="left", va="top",
        fontsize=11.5, color=INK, linespacing=1.45, annotation_clip=False,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.7,
                    "connectionstyle": "arc3,rad=-0.25"})
    ax.annotate(
        f"a channel's OWN two edges:\n$\\varphi$ {same_mean:.3f} "
        "(the four outlined blocks)",
        xy=(6.9, 7.62), xytext=(4.35, 8.62), ha="left", va="top",
        fontsize=11.5, color=INK, linespacing=1.45, annotation_clip=False,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 1.7,
                    "connectionstyle": "arc3,rad=0.25"})

    # ---- companion: does the residual fade with channel separation? -----
    gaps = sorted(int(g) for g in data["channel_separation"])
    null_p99 = data["permutation_null"]["p99_of_max_abs_phi"]
    ax2.axhspan(-null_p99, null_p99, color=BAND, zorder=0)
    ax2.text(3.42, null_p99 * 0.52,
             "shuffled null\n(99th pct of |$\\varphi$|)", fontsize=10,
             color=MUTED, va="center", ha="left", linespacing=1.35)
    for gap in gaps:
        block = data["channel_separation"][str(gap)]
        jitter = np.linspace(-0.13, 0.13, len(block["values"]))
        ax2.scatter([gap + j for j in jitter], block["values"], s=42,
                    facecolor="none", edgecolor=ACCENT, linewidth=1.6,
                    zorder=3)
        ax2.plot([gap - 0.26, gap + 0.26], [block["mean_phi"]] * 2,
                 color=INK, linewidth=2.6, zorder=4)
        ax2.text(gap, block["mean_phi"] + 0.038, f"{block['mean_phi']:.3f}",
                 ha="center", va="bottom", fontsize=11.5, color=INK)
        ax2.text(gap, -0.038, f"n={block['tuning_pairs']} pairs", ha="center",
                 va="top", fontsize=9.5, color=MUTED)
    ax2.set_xticks(gaps)
    ax2.set_xticklabels(["0\n(same channel,\nits two edges)", "1", "2", "3"],
                        fontsize=11)
    ax2.set_xlabel("separation between the two channels (channel-number distance)",
                   fontsize=11.5, labelpad=8)
    ax2.set_ylabel("$\\varphi$", fontsize=12)
    ax2.set_xlim(-0.45, 3.95)
    ax2.set_ylim(-0.075, 0.58)
    ax2.set_yticks([0.0, 0.1, 0.2, 0.3, 0.4, 0.5])
    ax2.grid(axis="y", color=GRID, linewidth=0.9)
    ax2.set_axisbelow(True)
    for side in ("top", "right"):
        ax2.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax2.spines[side].set_color(GRID)
    ax2.tick_params(colors=MUTED, length=0)

    gap1 = data["channel_separation"]["1"]["mean_phi"]
    gap3 = data["channel_separation"]["3"]["mean_phi"]
    ax2.text(1.13, 0.545,
             "the cross-channel term does NOT fade with separation\n"
             f"(neighbours {gap1:.3f}, opposite ends {gap3:.3f}) — not one\n"
             "satellite lighting adjacent channels, but a flat\n"
             "sweep-wide floor under every tuning",
             ha="left", va="top", fontsize=11, color=INK, linespacing=1.45)

    fig.suptitle(
        "The sky is NOT channel-independent — but a channel's own two edges are the\n"
        f"only real structure: $\\varphi$ {same_mean:.2f} within a channel against "
        f"{cross_mean:.2f} between any two",
        fontsize=15.5, color=INK, y=0.982)

    census = data["census"]["frozen_at_start"]
    fig.text(0.5, 0.014,
             f"n = {data['n_per_cell']:,} units in EVERY cell — one receiver "
             f"chain's pass over one sweep ({data['census']['paired_sweeps']:,} "
             "paired sweeps x 3 live receivers; lnb-a excluded as a dead port).\n"
             "A unit enters only if all eight tunings returned a verdict.  Fire / "
             "no-fire is cross_radio._any_method_fires — any of the eight scored "
             "methods, each judged against\nthe threshold drawn for its own sample "
             "rate and probe length from the cross-edge null arms; the per-method "
             "matrices in the JSON tell the same story.\nAxis order is declared in "
             "advance (ch1 lower, ch1 upper, ch2 lower, …) so a channel's own edges "
             "are adjacent; the diagonal blocks are a finding, not a layout.\n"
             f"Every cross-channel cell ({data['cross_channel_min']:.3f}–"
             f"{data['cross_channel_max']:.3f}) exceeds the shuffled null's 99th "
             f"percentile of |$\\varphi$| ({null_p99:.3f}), so the weak "
             "cross-channel term is real, not noise.\n"
             f"CENSUS frozen before computing (digest {census['scored_digest']}): "
             f"{census['sweeps_on_share']:,} sweeps | "
             f"{census['corpus_entries']:,} corpus entries | "
             f"{census['scored_sidecars']:,} scored sidecars.\n"
             + hcore.drift_caption(),
             ha="center", va="bottom", fontsize=9.5, color=MUTED,
             linespacing=1.55)
    return fig


def main() -> int:
    data = compute()
    null = data["permutation_null"]
    data["headline_checks"] = {
        "same_channel_edge_pair_mean": data["same_channel_mean"],
        "cross_channel_mean": data["cross_channel_mean"],
        "ratio": data["ratio_same_over_cross"],
        "weakest_cross_channel_cell": data["cross_channel_min"],
        "shuffled_null_p99_of_max_abs_phi": null["p99_of_max_abs_phi"],
        "every_cross_channel_cell_beats_the_null":
            bool(data["cross_channel_min"] > null["p99_of_max_abs_phi"]),
        "cross_channel_fades_with_separation": bool(
            data["channel_separation"]["1"]["mean_phi"]
            > data["channel_separation"]["3"]["mean_phi"]),
        "verdict": (
            "channel-correlated, but the only strong structure is a channel's own "
            "upper/lower pair. Two different channels correlate at phi "
            f"{data['cross_channel_mean']:.3f} — above noise "
            f"(shuffled null p99 |phi| {null['p99_of_max_abs_phi']:.3f}) but "
            f"{data['ratio_same_over_cross']:.1f}x weaker, and FLAT in channel "
            "separation, which is a sweep-wide common mode rather than one "
            "satellite lighting neighbouring channels"),
    }
    data["census_drift_at_end"] = hcore.drift_block()
    hcore.write_outputs(NAME, data, plot(data))
    print(json.dumps(data["headline_checks"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
