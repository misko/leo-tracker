#!/usr/bin/env python3
"""cfo-bandwidth -- does the detection cliff move with sample rate or with RX
bandwidth?  A 3 x 4 factorial that separates the two limits the sky corpus
cannot.

On sky, ``fast_scan.py:1077`` writes ``rf_bandwidth = sampling_frequency`` on
every arm, so the digital Nyquist window and the AD9361 analog baseband filter
are the same number and ``min(Fs, B_RX)`` is always ``Fs``.  Nothing measured on
the sky can tell a digital clip from an analog one.  Here they are set
independently -- Fs in {2.5, 5, 10} MS/s crossed with B_RX in {1.25, 2.5, 5, 10}
MHz -- over a closed 60 dB-attenuated cable, with the carrier offset IMPOSED on
the transmitted waveform so the x axis is known by construction and no estimator
sits between it and the answer.

Two limits are predicted to move:
    digital  |CFO| > (Fs    - 1,875,000)/2   ->  312.5 / 1,562.5 / 4,062.5 kHz
    analog   |CFO| > (B_RX  - 1,875,000)/2   ->  does-not-fit / 312.5 / 1,562.5
                                                 / 4,062.5 kHz
and one is predicted not to: the coarse bank's own ``offset_span_hz``, 300 kHz
for the deployed ``coarse-A`` and 700 kHz for the candidate ``coarse-E``.

WHY THE HEADLINE PANELS PLOT A CONTINUOUS STATISTIC AND NOT Pd.  At the -20 dB
drive the thresholds -- drawn on this radio's own TX-off null at 1% per point --
are cleared even by a collapsed correlation: ``full-frame-full`` falls 0.949 ->
0.056, a factor of 17, while Pd stays at 1.00 because 0.056 still sits above the
0.037 bar.  A thresholded Pd would report no cliff anywhere in the matrix and
would be true and useless.  The score itself is plotted; Pd is carried in the
sidecar for every cell, and the Pd panel that IS informative -- the -64 dB arm,
where the bar bites -- is summarised there too.
"""
from __future__ import annotations

import json
import textwrap
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent / "injection-data" / "cfo-bandwidth" / "cfo-bandwidth.json"
SKY = HERE.parent.parent / "injection-data" / "radio-165" / "sky_cliff_reference-165.json"
OUT_PNG = HERE / "cfo-bandwidth.png"
OUT_JSON = HERE / "cfo-bandwidth.json"

# --- palette ---------------------------------------------------------------
# style.py's palette, in its documented order, so colour follows the entity.
# Four categorical slots are in play (one per RX bandwidth) and each also
# carries its own marker and dash pattern, which is what makes the tritan-band
# adjacent pair legal.  Reference lines are ink/grey and never a series hue.
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8985", "#e3e2de"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
MARKERS = ["o", "s", "^", "D"]
DASHES = [(None, None), (5, 1.5), (1.5, 1.5), (7, 2, 1.5, 2)]
CRITICAL = "#d03b3b"

BANDWIDTHS = [1_250_000.0, 2_500_000.0, 5_000_000.0, 10_000_000.0]
RATES = [2_500_000.0, 5_000_000.0, 10_000_000.0]
BW_STYLE = {b: dict(color=SERIES[i], marker=MARKERS[i], dashes=DASHES[i])
            for i, b in enumerate(BANDWIDTHS)}

HEADLINE_ARM = "strong-tx-20"
SKY_CLIFF_HZ = (350_000.0, 400_000.0)
COARSE_SPAN = {"coarse-A": 300_000.0, "coarse-E": 700_000.0}
#: build_bank lays 3 and 13 hypotheses over +/-span, so the worst residual a
#: signal past the edge hands downstream grows as |CFO| - span; the shoulder is
#: one grid half-step for A (150 kHz) and the relative-phase uniqueness window
#: (113.6 kHz) for E, which is the wider bank's real limit.
COARSE_SHOULDER = {"coarse-A": 116_667.0, "coarse-E": 113_636.0}
OCCUPIED_HALF_HZ = 937_500.0

X_MAX_KHZ = 1040.0


def load() -> dict:
    return json.loads(DATA.read_text())


def grid(cells: list[dict], arm: str) -> dict:
    out: dict = defaultdict(dict)
    for cell in cells:
        if cell["arm"] == arm:
            key = (cell["sample_rate_requested_hz"], cell["rx_bandwidth_requested_hz"])
            out[key][cell["cfo_requested_hz"]] = cell
    return out


def knee(offsets: list[float], values: list[float], fraction: float = 0.5) -> dict:
    """The half-power knee: last offset at or above half the zero-offset value.

    Read from the top down so a single dip in the middle of an otherwise healthy
    curve does not get called a cliff -- the knee is where the statistic goes
    below half and STAYS below.  Returned as the bracket the sweep resolves it
    at, because the offset grid is 30-100 kHz coarse and a point estimate would
    claim precision the sweep does not have.
    """
    reference = values[0]
    if reference <= 0:
        return {"last_good_hz": None, "first_bad_hz": None, "midpoint_hz": None,
                "reference": reference}
    first_bad = None
    for index in range(len(offsets) - 1, -1, -1):
        if values[index] >= fraction * reference:
            break
        first_bad = offsets[index]
    if first_bad is None:
        return {"last_good_hz": offsets[-1], "first_bad_hz": None,
                "midpoint_hz": None, "reference": reference,
                "note": "no knee inside the swept span"}
    index = offsets.index(first_bad)
    last_good = offsets[index - 1] if index > 0 else None
    midpoint = None if last_good is None else 0.5 * (last_good + first_bad)
    return {"last_good_hz": last_good, "first_bad_hz": first_bad,
            "midpoint_hz": midpoint, "reference": reference}


def series(cells_by_offset: dict, pick) -> tuple[list[float], list[float]]:
    offsets = sorted(cells_by_offset)
    return offsets, [pick(cells_by_offset[o]) for o in offsets]


FF_SCORE = lambda c: c["scores"]["full-frame-full"]["median"]          # noqa: E731
PTM_A = lambda c: c["coarse"]["coarse-A"]["peak_to_median_mean"]       # noqa: E731
PTM_E = lambda c: c["coarse"]["coarse-E"]["peak_to_median_mean"]       # noqa: E731
POWER = lambda c: c["power_dbfs"]                                      # noqa: E731


def reference_lines(ax, rate: float, cells: dict, kind: str) -> None:
    """The three limits, drawn as reference verticals and never as steps.

    pilot_guard_hz's own docstring puts the cost of sliding the first of eight
    subcarriers past Nyquist at 0.58 dB and 'gracefully worse after that', so
    every one of these is a knee spread over ~117 kHz per pilot, not a step.
    """
    digital = (rate - 2 * OCCUPIED_HALF_HZ) / 2.0
    if digital / 1e3 < X_MAX_KHZ:
        ax.axvline(digital / 1e3, color=INK, lw=1.4, ls=(0, (6, 3)), zorder=1.5)
        ax.annotate("digital edge\n(Fs/2 - 937.5 kHz)", xy=(digital / 1e3, 1.0),
                    xycoords=("data", "axes fraction"), xytext=(4, -12),
                    textcoords="offset points", fontsize=8, color=INK2,
                    ha="left", va="top")
    else:
        ax.annotate(f"digital edge {digital/1e3:,.0f} kHz  →  off panel",
                    xy=(0.985, 0.965), xycoords="axes fraction", fontsize=8.5,
                    color=INK2, ha="right", va="top",
                    bbox=dict(fc=SURFACE, ec=GRID, lw=0.8, pad=2.5))

    span = COARSE_SPAN[kind]
    ax.axvspan(span / 1e3, (span + COARSE_SHOULDER[kind]) / 1e3,
               color=INK, alpha=0.055, lw=0, zorder=0.6)
    ax.axvline(span / 1e3, color=INK, lw=1.6, ls=(0, (1, 2)), zorder=1.5)
    ax.annotate(f"{kind} span\n{span/1e3:,.0f} kHz", xy=(span / 1e3, 0.0),
                xycoords=("data", "axes fraction"), xytext=(-4, 6),
                textcoords="offset points", fontsize=8, color=INK2,
                ha="right", va="bottom")

    # Measured analog corner, per bandwidth, colour-matched to its curve.  The
    # readback is not usable for this: the driver reports the requested
    # baseband bandwidth, not the corner it tuned to.  filter_shape measures it
    # off the receiver's own thermal noise, which is white by construction, so
    # the received power spectrum IS the filter's power response.
    for bandwidth in BANDWIDTHS:
        cell = cells.get((rate, bandwidth), {}).get(0.0)
        if cell is None:
            continue
        clip = cell["measured_clip_offset_hz"] / 1e3
        if 0.0 < clip < X_MAX_KHZ:
            ax.axvline(clip, color=BW_STYLE[bandwidth]["color"], lw=1.1,
                       alpha=0.55, ls=(0, (2, 2)), zorder=1.2)


def sky_cliff_band(ax) -> None:
    ax.axvspan(SKY_CLIFF_HZ[0] / 1e3, SKY_CLIFF_HZ[1] / 1e3, color=CRITICAL,
               alpha=0.10, lw=0, zorder=0.5)


def draw_curves(ax, rate: float, cells: dict, pick) -> dict:
    plotted = {}
    for bandwidth in BANDWIDTHS:
        by_offset = cells.get((rate, bandwidth))
        if not by_offset:
            continue
        offsets, values = series(by_offset, pick)
        style = BW_STYLE[bandwidth]
        handle, = ax.plot([o / 1e3 for o in offsets], values,
                          color=style["color"], marker=style["marker"],
                          markersize=4.6, lw=1.9,
                          label=f"B_RX {bandwidth/1e6:g} MHz", zorder=3)
        if style["dashes"][0] is not None:
            handle.set_dashes(list(style["dashes"]))
        plotted[bandwidth] = {"offsets_hz": offsets, "values": values}
    return plotted


def main() -> int:
    payload = load()
    sky = json.loads(SKY.read_text())
    cells = grid(payload["cells"], HEADLINE_ARM)
    header = payload["arms"][HEADLINE_ARM]["source"]

    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE, "savefig.dpi": 150, "figure.dpi": 150,
        "font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"],
        "font.size": 10.5, "axes.titlesize": 11.5, "axes.labelsize": 10,
        "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK2, "text.color": INK,
        "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
        "xtick.labelsize": 9, "ytick.labelsize": 9,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False,
        "legend.frameon": False, "legend.fontsize": 9,
    })

    fig = plt.figure(figsize=(15.4, 19.6))
    outer = fig.add_gridspec(4, 3, left=0.062, right=0.985, top=0.900,
                             bottom=0.215, hspace=0.55, wspace=0.20,
                             height_ratios=[1.10, 1.00, 0.66, 1.30])

    def caption(axis, text: str, drop: float = 0.026) -> None:
        """Row caption, placed under the row from the axes' realised position.

        Anchoring to the axes rather than to an axes-fraction offset is what
        keeps these off the panels below when the row heights differ.
        """
        box = axis.get_position()
        fig.text(box.x0, box.y0 - drop, text, ha="left", va="top",
                 fontsize=8.8, color=INK2, linespacing=1.45)

    record: dict = {"panels": {}, "cell_table": [], "knees": {}}

    # ---- row 0: coarse-A, the DEPLOYED front end ---------------------------
    row0 = [fig.add_subplot(outer[0, i]) for i in range(3)]
    for axis, rate in zip(row0, RATES):
        sky_cliff_band(axis)
        reference_lines(axis, rate, cells, "coarse-A")
        record["panels"].setdefault("coarse_A_peak_to_median", {})[f"{rate:.0f}"] = \
            draw_curves(axis, rate, cells, PTM_A)
        axis.axhline(1.0, color=MUTED, lw=1.0, ls=":", zorder=1)
        axis.set_xlim(-25, X_MAX_KHZ)
        axis.set_ylim(0.8, 9.2)
        axis.set_title(f"Fs = {rate/1e6:g} MS/s", color=INK, fontweight="bold")
    row0[0].set_ylabel("coarse-A peak-to-median")
    row0[0].legend(loc="upper right", ncols=1, handlelength=2.6)

    # ---- row 1: the headline point statistic -------------------------------
    row1 = [fig.add_subplot(outer[1, i]) for i in range(3)]
    for axis, rate in zip(row1, RATES):
        sky_cliff_band(axis)
        reference_lines(axis, rate, cells, "coarse-E")
        record["panels"].setdefault("full_frame_full_median", {})[f"{rate:.0f}"] = \
            draw_curves(axis, rate, cells, FF_SCORE)
        axis.set_xlim(-25, X_MAX_KHZ)
        axis.set_ylim(0.0, 1.06)
    row1[0].set_ylabel("full-frame-full, median score")

    # ---- row 2: received power, the confound control -----------------------
    row2 = [fig.add_subplot(outer[2, i]) for i in range(3)]
    power_record: dict = {}
    for axis, rate in zip(row2, RATES):
        sky_cliff_band(axis)
        for bandwidth in BANDWIDTHS:
            by_offset = cells.get((rate, bandwidth))
            if not by_offset:
                continue
            offsets, values = series(by_offset, POWER)
            relative = [v - values[0] for v in values]
            style = BW_STYLE[bandwidth]
            handle, = axis.plot([o / 1e3 for o in offsets], relative,
                                color=style["color"], marker=style["marker"],
                                markersize=4.0, lw=1.7, zorder=3)
            if style["dashes"][0] is not None:
                handle.set_dashes(list(style["dashes"]))
            power_record.setdefault(f"{rate:.0f}", {})[f"{bandwidth:.0f}"] = {
                "offsets_hz": offsets, "power_dbfs": values,
                "power_relative_db": relative}
        axis.axvline(COARSE_SPAN["coarse-A"] / 1e3, color=INK, lw=1.2,
                     ls=(0, (1, 2)), zorder=1)
        axis.set_xlim(-25, X_MAX_KHZ)
        axis.set_ylim(-4.2, 1.2)
        axis.set_xlabel("imposed carrier offset (kHz)")
    row2[0].set_ylabel("received power,\ndB relative to zero offset")
    record["panels"]["received_power"] = power_record

    caption(row0[0],
            "DEPLOYED front end (coarse-A, 3 x 8, ±300 kHz).  Its knee lands in the red band in every one of the twelve cells — the sky's own 350-400 kHz collapse — and moves by at most one 50 kHz\n"
            "sweep step across a 4x change in sample rate and an 8x change in RX bandwidth.  The ripple inside ±300 kHz is the bank's own 300 kHz hypothesis grid: peaks where the offset lands on a\n"
            "hypothesis (0, 300 kHz), troughs midway between.  Colour-matched dashed verticals mark each bandwidth's MEASURED analog clip offset; the black dashed vertical is the digital edge.")
    caption(row1[0],
            "Headline point statistic, conditioned on coarse-E (13 x 8, ±700 kHz).  Knee at 810 → 840 kHz in all twelve cells: the wider bank's span plus its ±113.6 kHz relative-phase uniqueness window.\n"
            "Same signal, same rig, a different bank — and the cliff moves to that bank's span.  Nothing at 350-400 kHz here, and nothing at any of the four predicted analog edges.")
    caption(row2[0],
            "THE CONFOUND CONTROL, and the positive control for the analog arm.  At B_RX = 1.25 MHz the filter really is cutting the block: power rolls off 2.2-3.5 dB across the sweep and sits ~1 dB down\n"
            "at zero offset.  At B_RX = 10 MHz it is flat inside 0.9 dB.  Both rows die at the same offset in the two panels above.  Power falls without the score following it, and the score falls where power\n"
            "is flat — so the collapse is geometric in the search, not energetic in the passband.")

    # ---- row 3a: predicted against measured, the whole matrix at once ------
    summary = fig.add_subplot(outer[3, 0:2])
    labels, x_positions = [], []
    digital_y, analog_y, analog_unfit_x, measured_clip_y = [], [], [], []
    knee_a_lo, knee_a_hi, knee_ff_lo, knee_ff_hi = [], [], [], []
    position = 0.0
    for rate in RATES:
        for bandwidth in BANDWIDTHS:
            by_offset = cells.get((rate, bandwidth))
            if not by_offset:
                continue
            offsets, ptm_a = series(by_offset, PTM_A)
            _, ptm_e = series(by_offset, PTM_E)
            _, ff = series(by_offset, FF_SCORE)
            _, power = series(by_offset, POWER)
            zero = by_offset[0.0]

            knee_a = knee(offsets, [v - 1.0 for v in ptm_a])
            knee_e = knee(offsets, [v - 1.0 for v in ptm_e])
            knee_f = knee(offsets, ff)

            digital = zero["predicted_digital_edge_hz"]
            analog = zero["predicted_analog_edge_hz"]
            clip = zero["measured_clip_offset_hz"]

            x_positions.append(position)
            labels.append(f"{bandwidth/1e6:g}")
            digital_y.append(digital / 1e3)
            analog_y.append(analog / 1e3 if analog > 0 else np.nan)
            if analog <= 0:
                analog_unfit_x.append(position)
            measured_clip_y.append(clip / 1e3 if clip > 0 else np.nan)
            knee_a_lo.append(knee_a["last_good_hz"] / 1e3)
            knee_a_hi.append(knee_a["first_bad_hz"] / 1e3)
            knee_ff_lo.append(knee_f["last_good_hz"] / 1e3)
            knee_ff_hi.append(knee_f["first_bad_hz"] / 1e3)

            record["cell_table"].append({
                "sample_rate_requested_hz": rate,
                "sample_rate_readback_hz": zero["sample_rate_readback_hz"],
                "rx_bandwidth_requested_hz": bandwidth,
                "rx_bandwidth_readback_hz": zero["rx_bandwidth_readback_hz"],
                "readback_matched_request": (
                    zero["sample_rate_readback_hz"] == rate
                    and zero["rx_bandwidth_readback_hz"] == bandwidth),
                "rx_gain_requested_db": zero["rx_gain_requested_db"],
                "rx_gain_readback_db": zero["rx_gain_readback_db"],
                "rx_gain_mode_readback": zero["rx_gain_mode_readback"],
                "rx_fir_en_readback": zero["rx_fir_en_readback"],
                "tx_bandwidth_readback_hz": zero["tx_bandwidth_readback_hz"],
                "predicted_digital_edge_hz": digital,
                "predicted_analog_edge_hz": analog,
                "measured_rx_half_corner_hz": zero["measured_rx_half_corner_hz"],
                "measured_rx_bandwidth_hz": zero["measured_rx_bandwidth_hz"],
                "measured_over_requested": (zero["measured_rx_bandwidth_hz"]
                                            / bandwidth),
                "measured_effective_half_window_hz":
                    zero["measured_effective_half_window_hz"],
                "measured_clip_offset_hz": clip,
                "offsets_hz": offsets,
                "knee_coarse_A_hz": knee_a,
                "knee_coarse_E_hz": knee_e,
                "knee_full_frame_full_hz": knee_f,
                "power_dbfs_at_zero": power[0],
                "power_dbfs_span_db": max(power) - min(power),
                "power_db_at_coarse_A_knee_relative_to_zero":
                    power[offsets.index(knee_a["first_bad_hz"])] - power[0],
                "bandwidth_loss_db_at_zero": zero["bandwidth_loss_db"],
                "snr_db_at_zero": zero["snr_db"],
                "pd_headline_at_zero": zero["pd_headline"],
                "pd_headline_at_1000khz": by_offset[1_000_000.0]["pd_headline"],
                "probes_per_offset": zero["probes"],
            })
            position += 1.0
        position += 0.8

    summary.set_yscale("log")
    summary.plot(x_positions, digital_y, ls="none", marker="v",
                 markersize=11, mfc="none", mec=INK, mew=1.8,
                 label="predicted digital edge  (Fs/2 - 937.5 kHz)")
    summary.plot(x_positions, analog_y, ls="none", marker="^",
                 markersize=11, mfc="none", mec=MUTED, mew=1.8,
                 label="predicted analog edge  (B_RX/2 - 937.5 kHz)")
    summary.plot(x_positions, measured_clip_y, ls="none", marker="_",
                 markersize=16, mec="#4a3aa7", mew=2.4,
                 label="MEASURED analog clip (corner off noise)")
    for x in analog_unfit_x:
        summary.plot([x], [122], marker="x", markersize=9, color=CRITICAL,
                     mew=2.0, ls="none")
    summary.annotate("✕ = analog block does not fit even at zero offset",
                     xy=(analog_unfit_x[-1], 122), xytext=(12, -2),
                     textcoords="offset points", fontsize=8.5, color=CRITICAL,
                     va="center")

    for index, x in enumerate(x_positions):
        summary.plot([x, x], [knee_a_lo[index], knee_a_hi[index]],
                     color=CRITICAL, lw=6.0, solid_capstyle="butt", alpha=0.85,
                     zorder=4,
                     label="MEASURED coarse-A knee (deployed)" if index == 0 else None)
        summary.plot([x, x], [knee_ff_lo[index], knee_ff_hi[index]],
                     color="#2a78d6", lw=6.0, solid_capstyle="butt", alpha=0.85,
                     zorder=4,
                     label="MEASURED full-frame-full knee" if index == 0 else None)
    # The measured knees are constant down the whole matrix, so draw them as
    # bands: a flat horizontal stripe against markers that climb sixteenfold is
    # the entire finding in one glance.
    # A 30 kHz band on a log axis is two pixels tall, so the constant knees are
    # also drawn as thick low-alpha rules: the flat stripe against markers that
    # climb sixteenfold is the whole finding in one glance.
    for lo, hi, colour in ((min(knee_a_lo), max(knee_a_hi), CRITICAL),
                           (min(knee_ff_lo), max(knee_ff_hi), "#2a78d6")):
        summary.axhspan(lo, hi, color=colour, alpha=0.13, lw=0, zorder=0.4)
        summary.axhline((lo * hi) ** 0.5, color=colour, alpha=0.16, lw=11,
                        zorder=0.35)
    summary.annotate(
        f"coarse-A knee, all 12 cells: {min(knee_a_lo):,.0f}-{max(knee_a_hi):,.0f} kHz",
        xy=(0.012, 0.965), xycoords="axes fraction", ha="left", va="top",
        fontsize=10, color=CRITICAL, fontweight="bold")
    summary.annotate(
        f"full-frame-full knee, all 12 cells: {min(knee_ff_lo):,.0f}-{max(knee_ff_hi):,.0f} kHz",
        xy=(0.012, 0.905), xycoords="axes fraction", ha="left", va="top",
        fontsize=10, color="#2a78d6", fontweight="bold")
    summary.set_xticks(x_positions)
    summary.set_xticklabels(labels)
    summary.set_ylim(100, 9000)
    summary.set_xlim(-0.9, x_positions[-1] + 0.9)
    summary.set_ylabel("offset (kHz), log scale")
    summary.set_xlabel("RX bandwidth B_RX (MHz), grouped by sample rate",
                       labelpad=30)
    for index, rate in enumerate(RATES):
        centre = float(np.mean(x_positions[index * 4:index * 4 + 4]))
        summary.annotate(f"Fs = {rate/1e6:g} MS/s", xy=(centre, 0.0),
                         xycoords=("data", "axes fraction"), xytext=(0, -30),
                         textcoords="offset points", ha="center", fontsize=10,
                         color=INK, fontweight="bold")
        if index:
            summary.axvline(x_positions[index * 4] - 0.9, color=GRID, lw=1.2)
    summary.legend(loc="lower left", bbox_to_anchor=(0.0, 1.015, 1.0, 0.10),
                   mode="expand", ncols=3, fontsize=9, handlelength=2.2,
                   columnspacing=1.2, borderaxespad=0.0)
    summary.set_title(
        "Predicted edges sweep a factor of sixteen across the matrix.  The measured knees do not move at all.",
        color=INK, fontweight="bold", pad=64)

    # ---- row 3b: the sky, for scale ---------------------------------------
    sky_axis = fig.add_subplot(outer[3, 2])
    for axis in (summary, sky_axis):
        box = axis.get_position()
        axis.set_position([box.x0, box.y0 - 0.034, box.width, box.height - 0.008])
    sky_record = {}
    for index, (name, bins) in enumerate(sky["series"].items()):
        x = [b["mid_khz"] for b in bins]
        y = [b["rate_pct"] for b in bins]
        handle, = sky_axis.plot(x, y, color=SERIES[index], marker=MARKERS[index],
                                markersize=4.6, lw=1.9, label=f"sky, {name}")
        if DASHES[index][0] is not None:
            handle.set_dashes(list(DASHES[index]))
        sky_record[name] = {"mid_khz": x, "rate_pct": y,
                            "n": [b["n"] for b in bins]}
    sky_axis.axvspan(SKY_CLIFF_HZ[0] / 1e3, SKY_CLIFF_HZ[1] / 1e3,
                     color=CRITICAL, alpha=0.10, lw=0, zorder=0.5)
    sky_axis.axvline(COARSE_SPAN["coarse-A"] / 1e3, color=INK, lw=1.6,
                     ls=(0, (1, 2)), zorder=1.5)
    sky_axis.annotate("coarse-A span\n300 kHz", xy=(300, 1.0),
                      xycoords=("data", "axes fraction"), xytext=(6, -8),
                      textcoords="offset points", fontsize=8, color=INK2,
                      ha="left", va="top")
    sky_axis.annotate(
        "x here is the pipeline's own\nbias-corrected ESTIMATE of the\noffset, not an imposed one --\n"
        "which is why the panels to the\nleft impose it instead.",
        xy=(0.42, 0.68), xycoords="axes fraction", fontsize=8.5, color=INK2,
        ha="left", va="top", linespacing=1.4)
    sky_axis.set_xlim(-25, X_MAX_KHZ)
    sky_axis.set_ylim(0, 70)
    sky_axis.set_xlabel("bias-corrected offset (kHz)")
    sky_axis.set_ylabel("sky detection rate (%)")
    sky_axis.legend(loc="upper right")
    sky_axis.set_title("What is being explained", color=INK, fontweight="bold")
    record["panels"]["sky_reference"] = sky_record

    # ---- titles, stamp -----------------------------------------------------
    fig.suptitle(
        "Is the 350-400 kHz detection cliff a bandwidth effect?  No.  It is the coarse bank's search span.",
        x=0.062, y=0.975, ha="left", fontsize=17, color=INK, fontweight="bold")
    fig.text(0.062, 0.947,
             "Sample rate and RX bandwidth set INDEPENDENTLY over a closed cable "
             "(on sky, fast_scan.py:1077 ties them together and no sky measurement can separate them).\n"
             "Carrier offset imposed on the transmitted waveform, so the x axis is "
             "known by construction.  Red band = the sky's own 350-400 kHz collapse.",
             ha="left", va="top", fontsize=10.5, color=INK2)

    a_lo = sorted({v for v in knee_a_lo})
    a_hi = sorted({v for v in knee_a_hi})
    ff_lo, ff_hi = sorted({v for v in knee_ff_lo}), sorted({v for v in knee_ff_hi})
    paragraphs = [
        "CENSUS.  3 sample rates x 4 RX bandwidths = 12 cells, x 19 imposed offsets = 228 cells in the plotted arm; 40 scored probes per cell "
        "(20 captures x 2 receivers) = 9,120 probes, plus 480 TX-off null probes.  Two further drives (-55 dB, -64 dB) over the same matrix bring "
        f"the record to {payload['cells_run']} cells and {payload['cells_run'] * 40:,} probes; {payload['cells_failed']} cells failed and were re-run.",

        f"RIG.  Radio ip:192.168.1.165, hw_serial {header['context']['hw_serial']}, fw {header['context']['fw_version']}, kernel "
        f"{header['context']['local,kernel']}.  CABLED LOOPBACK TX2 -> SMA splitter -> 2x30 dB attenuator -> RX1, RX2.  No antenna, no LNB, no sky; "
        "TX and RX share one oscillator, so nothing here speaks to LO drift.  "
        f"TX drive {header['tx_gain_db']:+.0f} dB on TX2, TX rf_bandwidth {header['tx_bandwidth_hz']/1e6:g} MHz held fixed for the whole sweep; "
        f"RX gain {header['rx_gain_db']:.0f} dB MANUAL — no AGC — requested and read back on every cell; probe {header['probe_ms']:.0f} ms; "
        f"{header['edge']} edge scored against the {header['null_edge']} edge as null; thresholds at 1% per point on this radio's own TX-off null.",

        "DETECTORS.  survey_scoring's own comparison family, through search_observation / distinct_points / confirm_points.  Plotted: coarse-A "
        "(3x8, ±300 kHz — the DEPLOYED front end), coarse-E (13x8, ±700 kHz), and full-frame-full conditioned at coarse-E.  The panels plot the "
        "CONTINUOUS statistic and not Pd, deliberately: at this drive the 1% thresholds are cleared even by a collapsed score — full-frame-full "
        "falls 0.949 -> 0.056, a factor of 17, while Pd stays at 1.00 — so a thresholded Pd reports no cliff anywhere in the matrix.  Pd for every "
        "cell, offset and arm is carried in the sidecar.",

        f"READBACK.  sampling_frequency and rf_bandwidth read off the ad9361-phy channel through raw libiio after every write: "
        f"{payload['readback']['cells_with_bandwidth_mismatch']} bandwidth and {payload['readback']['cells_with_sample_rate_mismatch']} sample-rate "
        f"mismatches across all {payload['cells_run']} cells.  That proves the driver accepted the write and NOTHING about the analog corner, which "
        "the driver does not report — so the corner is measured off the receiver's own thermal noise (white by construction, so the received power "
        "spectrum IS the filter's response) and comes back 0.95-1.27x the request.  Both the readbacks and the measured corners are in the sidecar.",

        f"MEASURED KNEES (half of the zero-offset statistic, bracketed by the sweep's own grid).  coarse-A: {a_lo[0]:,.0f}-{a_lo[-1]:,.0f} -> "
        f"{a_hi[0]:,.0f}-{a_hi[-1]:,.0f} kHz.  full-frame-full: {ff_lo[0]:,.0f} -> {ff_hi[0]:,.0f} kHz, identical in 12 of 12 cells.  "
        f"Recorded {header['started_utc'][:10]} UTC.  Figure and sidecar built by figures/injection/cfo-bandwidth.py from "
        "injection-data/cfo-bandwidth/cfo-bandwidth.json.",
    ]
    stamp = "\n".join(textwrap.fill(p, width=196) for p in paragraphs)
    fig.text(0.062, 0.135, stamp, ha="left", va="top", fontsize=8.0, color=INK2,
             linespacing=1.55)

    fig.savefig(OUT_PNG)

    # ---- sidecar -----------------------------------------------------------
    verdict = {
        "question": "Is the sky's 350-400 kHz detection cliff a bandwidth effect?",
        "answer": "No.",
        "because": [
            "full-frame-full's knee sits at 810 -> 840 kHz in all 12 cells while the "
            "predicted digital edge ranges 312.5 -> 4,062.5 kHz and the predicted "
            "analog edge -312.5 -> 4,062.5 kHz.  A 4x change in sample rate and an 8x "
            "change in RX bandwidth move it by nothing the 30 kHz grid can resolve.",
            "coarse-A -- the DEPLOYED front end, the one the sky corpus ran -- has its "
            "knee at 350-400 kHz or 400-450 kHz in all 12 cells, which is the sky's own "
            "collapse, and it does not move with Fs at all.  It is the +/-300 kHz "
            "offset_span_hz plus roughly one grid half-step.",
            "the analog filter is demonstrably working: at B_RX = 1.25 MHz received "
            "power rolls off 2.2-3.5 dB across the sweep and sits ~1 dB down at zero "
            "offset, so the positive control passes -- and those cells die at the same "
            "offset as the flat 10 MHz ones.  Power falls without the score following.",
        ],
    }

    sidecar = {
        "figure": "cfo-bandwidth",
        "generated_from": str(DATA.relative_to(HERE.parent.parent.parent)),
        "sky_reference_source": str(SKY.relative_to(HERE.parent.parent.parent)),
        "schema": payload["schema"],
        "experiment": payload["experiment"],
        "headline_arm": HEADLINE_ARM,
        "provenance": {
            "radio_uri": header["uri"],
            "hw_model": header["context"]["hw_model"],
            "hw_serial": header["context"]["hw_serial"],
            "fw_version": header["context"]["fw_version"],
            "kernel": header["context"]["local,kernel"],
            "recorded_utc": header["started_utc"],
            "rig": "CABLED LOOPBACK TX2 -> SMA splitter -> 2x 30 dB attenuator -> RX1, RX2",
            "no_antenna_no_lnb_no_sky": True,
            "tx_and_rx_share_one_oscillator": True,
            "lo_hz": 1_190_312_500,
            "tx_port": header["tx_port"],
            "tx_gain_db": header["tx_gain_db"],
            "tx_bandwidth_hz": header["tx_bandwidth_hz"],
            "tx_amplitude_counts": header["tx_amplitude_counts"],
            "rx_gain_db": header["rx_gain_db"],
            "rx_gain_mode": header["rx_gain_mode"],
            "probe_ms": header["probe_ms"],
            "edge_scored": header["edge"],
            "null_edge": header["null_edge"],
            "captures_per_cell": header["captures_per_cell"],
            "probes_per_cell": 2 * header["captures_per_cell"],
            "null_captures_per_bandwidth": header["null_captures"],
            "offsets_planned_hz": header["offsets_hz"],
            "offsets_realised_hz": sorted({c["cfo_requested_hz"]
                                           for c in payload["cells"]
                                           if c["arm"] == HEADLINE_ARM}),
        },
        "census": {
            "cells_in_headline_arm": payload["arms"][HEADLINE_ARM]["cells_run"],
            "cells_all_arms": payload["cells_run"],
            "cells_failed_all_arms": payload["cells_failed"],
            # The header's offsets_hz is the PLANNED list; the strong arm was
            # extended past it while it ran, so the realised set is read back
            # off the records instead and the two are both kept.
            "arms": {name: {"tx_gain_db": arm["source"]["tx_gain_db"],
                            "cells_run": arm["cells_run"],
                            "offsets_planned_hz": arm["source"]["offsets_hz"],
                            "offsets_realised_hz": sorted(
                                {c["cfo_requested_hz"] for c in payload["cells"]
                                 if c["arm"] == name})}
                     for name, arm in payload["arms"].items()},
            "probes_headline_arm": payload["arms"][HEADLINE_ARM]["cells_run"] * 40,
            "probes_all_arms": payload["cells_run"] * 40,
            "sample_rates_hz": RATES,
            "rx_bandwidths_hz": BANDWIDTHS,
            "detectors_plotted": ["coarse-A", "coarse-E", "full-frame-full"],
            "detector_headline": "full-frame-full conditioned at coarse-E",
            "detector_deployed": "coarse-A",
            "false_alarm_rate": payload["arms"][HEADLINE_ARM]["thresholds"]["false_alarm_rate"],
        },
        "geometry": {
            "subcarrier_spacing_hz": 234_375.0,
            "edge_pilot_subcarriers": 8,
            "occupied_bandwidth_hz": 2 * OCCUPIED_HALF_HZ,
            "occupied_half_width_hz": OCCUPIED_HALF_HZ,
            "pilot_centre_half_width_hz": payload["geometry"]["pilot_centre_half_width_hz"],
            "coarse_span_hz": COARSE_SPAN,
            "coarse_shoulder_hz": COARSE_SHOULDER,
        },
        "readback": payload["readback"],
        "readback_grid": [
            {"sample_rate_requested_hz": row["sample_rate_requested_hz"],
             "sample_rate_readback_hz": row["sample_rate_readback_hz"],
             "rx_bandwidth_requested_hz": row["rx_bandwidth_requested_hz"],
             "rx_bandwidth_readback_hz": row["rx_bandwidth_readback_hz"],
             "readback_matched_request": row["readback_matched_request"],
             "rx_gain_requested_db": row["rx_gain_requested_db"],
             "rx_gain_readback_db": row["rx_gain_readback_db"],
             "rx_gain_mode_readback": row["rx_gain_mode_readback"],
             "rx_fir_en_readback": row["rx_fir_en_readback"],
             "measured_rx_bandwidth_hz": row["measured_rx_bandwidth_hz"],
             "measured_over_requested": row["measured_over_requested"]}
            for row in record["cell_table"]
        ],
        "filter_shape": payload["filter_shape"],
        "cells": record["cell_table"],
        "panels": record["panels"],
        "knee_definition": (
            "last swept offset at which the continuous statistic is at or above "
            "half its zero-offset value and stays there; reported as the bracket "
            "(last_good, first_bad) the 30-100 kHz offset grid resolves it at.  "
            "Coarse statistics use the excess over 1.0, since peak-to-median has "
            "a floor of 1."),
        "measured_edge_per_cell": [
            {"sample_rate_hz": row["sample_rate_requested_hz"],
             "rx_bandwidth_hz": row["rx_bandwidth_requested_hz"],
             "predicted_digital_edge_hz": row["predicted_digital_edge_hz"],
             "predicted_analog_edge_hz": row["predicted_analog_edge_hz"],
             "measured_analog_clip_offset_hz": row["measured_clip_offset_hz"],
             "measured_knee_coarse_A_hz": [row["knee_coarse_A_hz"]["last_good_hz"],
                                           row["knee_coarse_A_hz"]["first_bad_hz"]],
             "measured_knee_coarse_E_hz": [row["knee_coarse_E_hz"]["last_good_hz"],
                                           row["knee_coarse_E_hz"]["first_bad_hz"]],
             "measured_knee_full_frame_full_hz": [row["knee_full_frame_full_hz"]["last_good_hz"],
                                                  row["knee_full_frame_full_hz"]["first_bad_hz"]],
             "power_db_at_coarse_A_knee_relative_to_zero":
                 row["power_db_at_coarse_A_knee_relative_to_zero"],
             "power_dbfs_span_db": row["power_dbfs_span_db"]}
            for row in record["cell_table"]
        ],
        "verdict": verdict,
    }

    # Pd is not plotted, so carry it in full: every cell, every offset, every arm.
    pd_by_arm: dict = {}
    for arm_name in payload["arms"]:
        arm_cells = grid(payload["cells"], arm_name)
        block: dict = {}
        for (rate, bandwidth), by_offset in sorted(arm_cells.items()):
            offsets = sorted(by_offset)
            block[f"{rate:.0f}|{bandwidth:.0f}"] = {
                "offsets_hz": offsets,
                "pd_headline": [by_offset[o]["pd_headline"] for o in offsets],
                "pd_coarse_A": [by_offset[o]["pd"]["coarse-A"] for o in offsets],
                "pd_coarse_E": [by_offset[o]["pd"]["coarse-E"] for o in offsets],
                "full_frame_full_median": [by_offset[o]["scores"]["full-frame-full"]["median"]
                                           for o in offsets],
                "coarse_A_peak_to_median": [by_offset[o]["coarse"]["coarse-A"]["peak_to_median_mean"]
                                            for o in offsets],
                "coarse_E_peak_to_median": [by_offset[o]["coarse"]["coarse-E"]["peak_to_median_mean"]
                                            for o in offsets],
                "coarse_E_recovered_offset_hz": [by_offset[o]["coarse"]["coarse-E"]["recovered_offset_median_hz"]
                                                 for o in offsets],
                "power_dbfs": [by_offset[o]["power_dbfs"] for o in offsets],
                "snr_db": [by_offset[o]["snr_db"] for o in offsets],
            }
        pd_by_arm[arm_name] = block
    sidecar["all_arms"] = pd_by_arm

    OUT_JSON.write_text(json.dumps(sidecar, indent=1))
    print(f"wrote {OUT_PNG} and {OUT_JSON}")
    print("coarse-A knees:", sorted({(lo, hi) for lo, hi in zip(knee_a_lo, knee_a_hi)}))
    print("full-frame knees:", sorted({(lo, hi) for lo, hi in zip(knee_ff_lo, knee_ff_hi)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
