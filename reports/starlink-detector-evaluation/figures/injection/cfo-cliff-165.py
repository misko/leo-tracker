#!/usr/bin/env python3
"""cfo-cliff-165 -- where detection dies, measured against a KNOWN offset.

On sky the collapse between 350 and 400 kHz is measured against a bias-corrected
ESTIMATE of the offset, produced by the same pipeline whose sensitivity is in
question, so it cannot distinguish "the search does not reach there" from "the
estimate does not reach there".  Here the offset is IMPOSED on the waveform
before the DAC, so the x axis is known by construction and no estimator sits
between it and the answer.

Two TX gains over the same offsets: if the collapse sits at the same place at
both, it is structural rather than a sensitivity effect.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
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

SRC = HERE.parent / "t3_cliff-165.jsonl"
SKY = HERE.parent / "sky_thresholds-165.json"
SKY_CLIFF = HERE.parent / "sky_cliff_reference-165.json"
OUT_PNG = HERE / "cfo-cliff-165.png"
OUT_JSON = HERE / "cfo-cliff-165.json"

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"

#: Same order, colours and markers as the sky report's figures, so the two can
#: be laid side by side.  Marker shape is the primary identity channel.
ORDER = ["anchor-8", "glrt-32", "glrt-64", "full-frame-verify",
         "full-frame-full", "full-frame-acquire", "differential-32",
         "differential-16"]
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

#: Where sky says detection collapses, and what the deployed coarse banks reach.
SKY_CLIFF_HZ = (350_000, 400_000)
BANK_SPAN_HZ = {"coarse-A (deployed 3x8)": 300_000,
                "coarse-E (candidate 13x8)": 700_000}


def detection(rows: list[dict], thresholds: dict) -> dict:
    """Per (gain, offset): detection rates, and how well the offset is estimated.

    Both per-cell and per-point are kept.  Per-cell -- did this probe get
    detected at all -- is the operationally meaningful one and is what the
    figure plots.  Per-point is carried because the sky cliff figure
    (``cross_radio.guard_band_curve``) counts firing per candidate point, and a
    comparison across the two has to know which is which.
    """
    grouped: dict = defaultdict(list)
    for row in rows:
        grouped[(row["tx_gain_db"], row["offset_nominal_hz"])].append(row)
    out: dict = {}
    for key, group in sorted(grouped.items()):
        rates = analyse.rates(group, thresholds, arm="target")
        # What the pipeline's own coarse stage THOUGHT the offset was.  The
        # imposed value is the truth; this is the estimate whose failure the sky
        # axis cannot separate from a failure to detect.
        estimated = [row["coarse_offset_hz"]["E"] for row in group]
        imposed = key[1]
        out[key] = {"cells": len(group),
                    "rms": float(np.mean([r["rms_counts"] for r in group])),
                    "coarse_E": float(np.mean([r["coarse"]["E"] for r in group])),
                    "estimated_offset_hz": float(np.mean(estimated)),
                    "estimated_offset_sd_hz": float(np.std(estimated)),
                    "estimate_error_hz": float(np.mean(estimated) - imposed),
                    "methods": {m: rates[m]["per_cell_rate"] for m in rates},
                    "methods_per_point": {m: rates[m]["per_point_rate"]
                                          for m in rates},
                    # How far above its threshold the best point in a cell sits.
                    # A detection rate of 1.00 says nothing about margin, and a
                    # null result about a cliff is only worth reading if the
                    # detections behind it were not scraping the threshold.
                    "score_over_threshold": {
                        m: float(np.mean([
                            max(point["methods"][m]["score"]
                                for point in row["points"])
                            for row in group]) / thresholds[m]["threshold"])
                        for m in rates}}
    return out


def collapse_point(offsets, values, level: float = 0.5):
    """Lowest offset at which detection has fallen below ``level`` and stays there."""
    for index, offset in enumerate(offsets):
        if values[index] < level and all(v < level for v in values[index:]):
            return offset
    return None


def main() -> None:
    header, rows = analyse.load(SRC)
    sky, _ = analyse.sky_thresholds(SKY)
    sky_curve = []
    if SKY_CLIFF.exists():
        sky_curve = [row for row in
                     json.loads(SKY_CLIFF.read_text())["series"]["5 MS/s"]
                     if row["mid_khz"] <= 820]
    thresholds = {m: sky[m] for m in sky}
    table = detection(rows, thresholds)
    gains = sorted({gain for gain, _ in table}, reverse=True)
    #: One x range for every panel: two gains swept the same offsets, and a
    #: column auto-scaled to its own subset would put the shaded sky band and
    #: the bank limits in different places in each half of the figure.
    span_khz = max(offset for _, offset in table) / 1e3

    payload = {"figure": "cfo-cliff-165", "radio": "ip:192.168.1.165",
               "rig": "CABLED LOOPBACK TX2 -> split -> RX1+RX2",
               "offset_basis": "IMPOSED on the waveform, exp(2j*pi*f*t), not estimated",
               "arm": header.get("arm"),
               "sample_rate_hz": header.get("sample_rate_hz"),
               "probe_ms": header.get("probe_ms"),
               "thresholds": "sky corpus 80ms-5.00MSps, 1% per point",
               "sky_cliff_hz": SKY_CLIFF_HZ, "bank_span_hz": BANK_SPAN_HZ,
               "sky_reference_5MSps": sky_curve,
               "gains_db": gains, "collapse_hz": {}, "curves": {}}

    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 12, "axes.titlesize": 12,
        "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 9.5,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    fig, axes = plt.subplots(2, len(gains), figsize=(8.0 * len(gains), 10.8),
                             dpi=150, facecolor=SURFACE, squeeze=False,
                             gridspec_kw={"height_ratios": [1.85, 1.0]})
    for column in range(1, len(gains)):        # never share an axis with itself
        axes[0][column].sharey(axes[0][0])
        axes[1][column].sharey(axes[1][0])

    for column, gain in enumerate(gains):
        axis = axes[0][column]
        offsets = sorted({offset for g, offset in table if g == gain})
        axis.axvspan(SKY_CLIFF_HZ[0] / 1e3, SKY_CLIFF_HZ[1] / 1e3,
                     color="#eb6834", alpha=0.13, zorder=0)
        for name, span in BANK_SPAN_HZ.items():
            axis.axvline(span / 1e3, color=MUTED, linestyle="-.", linewidth=1.2,
                         zorder=1)
        # The sky curve this experiment exists to test, drawn on the same axis.
        # Its LEVEL is not comparable -- it counts per candidate point on sky,
        # where most points hold no signal at all, so it peaks near 60% while a
        # cable with a signal in it reaches 100%.  Its SHAPE is the whole claim,
        # and the shape is what sits against the shaded band.
        if sky_curve:
            axis.plot([row["mid_khz"] for row in sky_curve],
                      [row["rate_pct"] / 100 for row in sky_curve],
                      color=MUTED, linestyle=(0, (5, 2)), linewidth=2.0,
                      marker="", zorder=2,
                      label="sky at 5 MS/s — per point, offset ESTIMATED")

        collapses = {}
        for method in ORDER:
            colour, marker, fill = STYLE[method]
            values = [table[(gain, offset)]["methods"][method] for offset in offsets]
            axis.plot([o / 1e3 for o in offsets], values, color=colour,
                      linewidth=1.7, marker=marker, markersize=8.5,
                      markerfacecolor=colour if fill == "full" else SURFACE,
                      markeredgecolor=colour, markeredgewidth=1.6,
                      label=method, zorder=3, alpha=0.95)
            collapses[method] = collapse_point(offsets, values)
            payload["curves"].setdefault(str(gain), {})[method] = {
                "offset_hz": offsets, "per_cell_detection": values,
                "per_point_detection": [table[(gain, o)]["methods_per_point"][method]
                                        for o in offsets],
                "score_over_threshold": [
                    table[(gain, o)]["score_over_threshold"][method]
                    for o in offsets]}
        payload["collapse_hz"][str(gain)] = collapses
        solved = [v for v in collapses.values() if v is not None]
        rms = np.mean([table[(gain, o)]["rms"] for o in offsets])
        axis.set_title(
            f"TX2 at {gain:+.0f} dB  ·  received rms ≈ {rms:.1f} counts\n"
            + (f"detection halves at {min(solved) / 1e3:.0f}–"
               f"{max(solved) / 1e3:.0f} kHz"
               if solved else "detection never collapses across this sweep"),
            pad=12, fontsize=12.5)
        axis.grid(color=GRID, linewidth=0.7)
        axis.set_axisbelow(True)
        axis.set_ylim(-0.04, 1.06)
        axis.set_xlim(-20, span_khz + 20)
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        if column == 0:
            axis.set_ylabel("per-cell detection rate\n(any of ~7 points fires)")
        for name, span in BANK_SPAN_HZ.items():
            axis.text(span / 1e3 - 10, 0.46, name, fontsize=8.5, color=MUTED,
                      rotation=90, ha="right", va="center")

        # ---- lower row: does the pipeline's own estimate track the truth? ---
        lower = axes[1][column]
        estimated = [table[(gain, offset)]["estimated_offset_hz"] / 1e3
                     for offset in offsets]
        spread = [table[(gain, offset)]["estimated_offset_sd_hz"] / 1e3
                  for offset in offsets]
        kilohertz = [offset / 1e3 for offset in offsets]
        lower.axvspan(SKY_CLIFF_HZ[0] / 1e3, SKY_CLIFF_HZ[1] / 1e3,
                      color="#eb6834", alpha=0.13, zorder=0)
        lower.plot(kilohertz, kilohertz, color=MUTED, linestyle="--",
                   linewidth=1.3, zorder=1, label="perfect estimate (y = x)")
        lower.errorbar(kilohertz, estimated, yerr=spread, color="#2a78d6",
                       marker="o", markersize=7, linewidth=1.7, capsize=3,
                       zorder=3, label="coarse-E estimate (mean ± sd)")
        for name, span in BANK_SPAN_HZ.items():
            lower.axvline(span / 1e3, color=MUTED, linestyle="-.", linewidth=1.2)
            lower.axhline(span / 1e3, color=MUTED, linestyle="-.", linewidth=0.8)

        lower.set_xlim(-20, span_khz + 20)
        lower.grid(color=GRID, linewidth=0.7)
        lower.set_axisbelow(True)
        for side in ("top", "right"):
            lower.spines[side].set_visible(False)
        if column == 0:
            lower.set_ylabel("offset the pipeline\nestimated (kHz)")
            lower.legend(frameon=False, loc="upper left", fontsize=9)
        payload["curves"].setdefault(str(gain), {})["_estimate"] = {
            "offset_hz": offsets,
            "estimated_hz": [table[(gain, o)]["estimated_offset_hz"]
                             for o in offsets],
            "error_hz": [table[(gain, o)]["estimate_error_hz"] for o in offsets]}

    all_collapse = [v for row in payload["collapse_hz"].values()
                    for v in row.values() if v is not None]
    headline = (f"detection survives to {min(all_collapse) / 1e3:.0f}–"
                f"{max(all_collapse) / 1e3:.0f} kHz"
                if all_collapse else
                f"detection never collapses out to {span_khz:.0f} kHz")
    fig.suptitle(
        f"Against a KNOWN injected offset, {headline} — "
        f"the sky corpus's 350–400 kHz cliff is not reproduced here\n"
        "CABLED LOOPBACK on radio ip:192.168.1.165 — tests the detectors and the "
        "digital pipeline, NOT the LNBs, the antenna, or real sky",
        fontsize=13.0, y=0.996)
    cells = sum(v["cells"] for v in table.values())
    fig.text(0.5, 0.016,
             f"n = {cells:,} observations (cells): {len(gains)} TX gains × "
             f"{len(set(o for _, o in table))} offsets × "
             f"{header.get('probes_per_point')} probes × 2 receivers.  "
             f"80 ms probes of 400,000 samples at 5.000 MS/s, RX manual gain 40 dB.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.text(0.5, 0.001,
             "Thresholds are the sky corpus's own, 80ms-5.00MSps arm at 1% per "
             "point.  Shaded band: 350–400 kHz, where sky detection collapses.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.supxlabel("IMPOSED carrier offset (kHz) — known by construction, "
                  "not estimated by the pipeline under test",
                  fontsize=12, y=0.118)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, ncol=5, fontsize=10,
               loc="lower center", bbox_to_anchor=(0.5, 0.055),
               columnspacing=1.6, handletextpad=0.6)
    fig.tight_layout(rect=(0.006, 0.135, 0.994, 0.935))
    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)
    OUT_JSON.write_text(json.dumps(payload, indent=2))

    for gain in gains:
        print(f"\n== TX {gain:+.0f} dB ==")
        offsets = sorted({offset for g, offset in table if g == gain})
        print(f"{'kHz':>6} {'rms':>6} {'coarseE':>8} " +
              " ".join(f"{m[:9]:>9}" for m in ORDER))
        for offset in offsets:
            row = table[(gain, offset)]
            print(f"{offset / 1e3:6.0f} {row['rms']:6.2f} {row['coarse_E']:8.3f} " +
                  " ".join(f"{row['methods'][m]:9.2f}" for m in ORDER))
    print(f"\ncollapse points: {json.dumps(payload['collapse_hz'])}")
    print(f"wrote {OUT_PNG}")


if __name__ == "__main__":
    main()
