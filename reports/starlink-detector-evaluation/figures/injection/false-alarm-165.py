#!/usr/bin/env python3
"""false-alarm-165 -- what the eight detectors do on a channel with nothing in it.

The sky corpus calibrates on a CROSS-EDGE null: the opposite edge's template on
sky IQ.  That is target-code-free by construction, but it is still sky and may
hold real energy.  This is the other kind of null -- a closed cable with the
transmitter off at -89.75 dB -- where there is nothing at all to hold.

Two panels, two questions.

LEFT is the headline: judged by the thresholds the corpus analysis uses
(drawn from the corpus's own 80ms-5.00MSps arm, the same rate and probe length
as this rig), how often do the eight fire on an empty channel, per point and
per cell?

RIGHT is the diagnostic.  Both arms are scored at the SAME candidate points, and
those points were all proposed by searchers maximising a TARGET-edge statistic.
So the target template is read where it was selected to do well and the opposite
edge's template is read at a place chosen without reference to it.  On a dead
cable neither arm holds a signal, so any gap between them is selection, not sky
-- and it says the cross-edge arm is a colder null than the thing it calibrates.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
# The harness helper lives with the records it reads, under
# injection-data/, not beside the figures. Inserting figures/ instead --
# which is what this did -- leaves the import unresolvable from a clean
# checkout, so every one of these scripts failed at its import line.
sys.path.insert(0, str(HERE.parent.parent / "injection-data" / "radio-165"))
import analyse  # noqa: E402

SRC = HERE.parent / "t2_scores-165.jsonl"
SKY = HERE.parent / "sky_thresholds-165.json"
OUT_PNG = HERE / "false-alarm-165.png"
OUT_JSON = HERE / "false-alarm-165.json"

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"
POINT_COLOUR, CELL_COLOUR = "#2a78d6", "#eb6834"

#: Order taken from the sky report's seriation so the two figures read together.
ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
         "full-frame-full", "full-frame-acquire", "differential-32",
         "differential-16"]

NOMINAL_POINT = 0.01


def percent(axis) -> None:
    axis.xaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _: f"{v * 100:.0f}%"))


def main() -> None:
    header, rows = analyse.load(SRC)
    sky, sky_payload = analyse.sky_thresholds(SKY)
    own = analyse.own_thresholds(rows)

    points_per_cell = float(np.mean([len(row["points"]) for row in rows]))
    predicted = 1.0 - (1.0 - NOMINAL_POINT) ** points_per_cell

    deployed = analyse.rates(rows, {m: sky[m] for m in sky}, arm="target")
    own_target = analyse.rates(rows, own, arm="target")
    own_cross = analyse.rates(rows, own, arm="cross-edge")
    asymmetry = analyse.arm_asymmetry(rows)
    sky_cell = sky_payload["per_cell_false_alarm"]
    sky_range = [min(sky_cell[m]["rate"] for m in ORDER),
                 max(sky_cell[m]["rate"] for m in ORDER)]

    payload = {
        "figure": "false-alarm-165", "radio": "ip:192.168.1.165",
        "rig": "CABLED LOOPBACK TX2 -> split -> RX1+RX2, TX off at -89.75 dB",
        "arm": header.get("arm"), "sample_rate_hz": header.get("sample_rate_hz"),
        "probe_ms": header.get("probe_ms"), "rx_gain_db": header.get("rx_gain_db"),
        "observations": len(rows), "probes": len({row["probe"] for row in rows}),
        "candidate_points": sum(len(row["points"]) for row in rows),
        "mean_points_per_cell": points_per_cell,
        "nominal_per_point": NOMINAL_POINT, "predicted_per_cell": predicted,
        "sky_thresholds": sky, "own_thresholds": own,
        "sky_per_cell_measured": sky_cell,
        "sky_per_cell_range": sky_range,
        "rates_deployed_thresholds_target_arm": deployed,
        "rates_own_thresholds_target_arm": own_target,
        "rates_own_thresholds_cross_edge_arm": own_cross,
        "arm_asymmetry": asymmetry,
        "rms_counts": {"min": min(row["rms_counts"] for row in rows),
                       "max": max(row["rms_counts"] for row in rows)},
    }

    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 12, "axes.titlesize": 12,
        "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 10.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    fig, (left, right) = plt.subplots(1, 2, figsize=(14.0, 7.2), dpi=150,
                                      facecolor=SURFACE, sharey=True)
    positions = {method: len(ORDER) - 1 - index
                 for index, method in enumerate(ORDER)}

    # ---- left: the deployed calibration on an empty channel ---------------
    left.axvspan(sky_range[0], sky_range[1], color=CELL_COLOUR, alpha=0.11,
                 zorder=0)
    left.axvline(NOMINAL_POINT, color=POINT_COLOUR, linestyle=":", linewidth=1.6)
    left.axvline(predicted, color=CELL_COLOUR, linestyle="--", linewidth=1.6)
    for method in ORDER:
        row, y = deployed[method], positions[method]
        left.plot([row["per_point_rate"], row["per_cell_rate"]], [y, y],
                  color=GRID, linewidth=1.8, zorder=2)
        left.plot([row["per_point_rate"]], [y], marker="o", markersize=10,
                  color=POINT_COLOUR, linestyle="none", zorder=3,
                  label="per point" if method == ORDER[0] else None)
        left.plot([row["per_cell_rate"]], [y], marker="D", markersize=9.5,
                  color=CELL_COLOUR, linestyle="none", markerfacecolor=SURFACE,
                  markeredgecolor=CELL_COLOUR, markeredgewidth=2.0, zorder=3,
                  label="per cell (any of ~7 points)" if method == ORDER[0] else None)
    left.set_xlim(0, 0.125)
    left.set_xlabel("false-alarm rate on an empty channel")
    left.set_title("Judged by the thresholds the corpus analysis uses,\n"
                   "an empty channel fires at very close to the nominal rate",
                   pad=34)
    left.legend(frameon=False, loc="upper center", ncol=2,
                bbox_to_anchor=(0.5, -0.155))
    left.text(NOMINAL_POINT - 0.001, len(ORDER) - 0.42, "1% nominal\nper point",
              fontsize=9.5, color=POINT_COLOUR, va="bottom", ha="right")
    left.text(predicted + 0.0015, len(ORDER) - 0.42,
              f"1−0.99$^{{{points_per_cell:.1f}}}$ = {predicted * 100:.1f}%\n"
              f"predicted per cell", fontsize=9.5, color=CELL_COLOUR,
              va="bottom", ha="left")

    # ---- right: the null is colder than what it calibrates ----------------
    right.axvline(NOMINAL_POINT, color=MUTED, linestyle=":", linewidth=1.6)
    for method in ORDER:
        y = positions[method]
        cross = own_cross[method]["per_point_rate"]
        target = own_target[method]["per_point_rate"]
        right.annotate("", xy=(target, y), xytext=(cross, y),
                       arrowprops=dict(arrowstyle="->", color=GRID, linewidth=2.2,
                                       shrinkA=6, shrinkB=6), zorder=2)
        right.plot([cross], [y], marker="s", markersize=9.5, color=MUTED,
                   linestyle="none", markerfacecolor=SURFACE,
                   markeredgecolor=MUTED, markeredgewidth=2.0, zorder=3,
                   label="cross-edge null arm (what calibrates)"
                         if method == ORDER[0] else None)
        right.plot([target], [y], marker="o", markersize=10, color=POINT_COLOUR,
                   linestyle="none", zorder=3,
                   label="target arm, same points (what fires)"
                         if method == ORDER[0] else None)
        ratio = asymmetry[method]["p99_ratio"]
        right.text(max(target, cross) + 0.004, y, f"p99 ×{ratio:.2f}",
                   fontsize=9.5, color=MUTED, va="center")
    right.set_xlim(0, max(0.06, 1.55 * max(
        own_target[m]["per_point_rate"] for m in ORDER)))
    right.set_xlabel("per-point false-alarm rate (thresholds re-drawn here)")
    right.set_title("Re-calibrated the repository's own way, the target arm fires\n"
                    "far above the 1% its cross-edge null was set to deliver",
                    pad=34)
    right.legend(frameon=False, loc="upper center", ncol=1,
                 bbox_to_anchor=(0.5, -0.155))
    right.text(NOMINAL_POINT + 0.0015, len(ORDER) - 0.42,
               "1% — where the cross-edge\nnull puts the threshold",
               fontsize=9.5, color=MUTED, va="bottom", ha="left")

    for axis in (left, right):
        axis.set_yticks(range(len(ORDER)))
        axis.set_yticklabels(list(reversed(ORDER)), fontsize=11)
        axis.set_ylim(-0.6, len(ORDER) - 0.25)
        axis.grid(axis="x", color=GRID, linewidth=0.7)
        axis.set_axisbelow(True)
        percent(axis)
        for side in ("top", "right", "left"):
            axis.spines[side].set_visible(False)

    cells = [deployed[m]["per_cell_rate"] for m in ORDER]
    points = [deployed[m]["per_point_rate"] for m in ORDER]
    fig.suptitle(
        f"An empty channel fires at {min(points) * 100:.2f}–{max(points) * 100:.2f}% "
        f"per point and {min(cells) * 100:.1f}–{max(cells) * 100:.1f}% per cell under "
        f"the corpus thresholds:\nthe ~6% measured on sky is what correct "
        f"per-point calibration predicts, not excess firing\n"
        "CABLED LOOPBACK on radio ip:192.168.1.165 — tests the detectors and the "
        "digital pipeline, NOT the LNBs, the antenna, or real sky",
        fontsize=12.8, y=0.997)
    fig.text(0.5, 0.040,
             f"n = {len(rows):,} observations (cells) from {payload['probes']} "
             f"probes × 2 receivers, {payload['candidate_points']:,} candidate "
             f"points, {points_per_cell:.2f} points per cell.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.text(0.5, 0.022,
             f"80 ms probes of 400,000 samples at 5.000 MS/s, RX manual gain 40 dB, "
             f"TX at −89.75 dB on both ports throughout",
             ha="center", fontsize=9.5, color=MUTED)
    fig.text(0.5, 0.004,
             f"(received rms {payload['rms_counts']['min']:.2f}–"
             f"{payload['rms_counts']['max']:.2f} ADC counts).  Shaded band, left: "
             f"the per-cell rate measured on sky, "
             f"{sky_range[0] * 100:.2f}–{sky_range[1] * 100:.2f}%.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.tight_layout(rect=(0.005, 0.075, 0.995, 0.925))
    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)

    OUT_JSON.write_text(json.dumps(payload, indent=2))
    print(f"observations {len(rows)}  points/cell {points_per_cell:.2f}  "
          f"predicted per cell {predicted:.4f}")
    print(f"\n{'method':20} {'dep.thr':>8} {'point%':>7} {'cell%':>7} "
          f"{'sky cell%':>9} | {'own thr':>8} {'x-edge%':>8} {'targ%':>7} "
          f"{'p99 x':>6}")
    for method in ORDER:
        print(f"{method:20} {deployed[method]['threshold']:8.4f} "
              f"{deployed[method]['per_point_rate'] * 100:6.2f}% "
              f"{deployed[method]['per_cell_rate'] * 100:6.2f}% "
              f"{sky_cell[method]['rate'] * 100:8.2f}% | "
              f"{own[method]['threshold']:8.4f} "
              f"{own_cross[method]['per_point_rate'] * 100:7.2f}% "
              f"{own_target[method]['per_point_rate'] * 100:6.2f}% "
              f"{asymmetry[method]['p99_ratio']:6.2f}")
    print(f"\nwrote {OUT_PNG}")


if __name__ == "__main__":
    main()
