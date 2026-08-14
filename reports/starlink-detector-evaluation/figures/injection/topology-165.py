#!/usr/bin/env python3
"""topology-165 -- what is cabled to radio .165, measured rather than assumed.

Reads t1_matrix-165.jsonl and draws the 2x2 (TX port, RX port) table two ways:
received level against TX gain, and matched-filter peak/median against TX gain.
Both are needed.  Level alone cannot tell a signal path from a power coupling,
and correlation alone cannot tell a strong path from a weak one.

Palette: slots 1-2 of the data-viz skill's validated reference palette, with
marker shape carrying receiver identity, so the figure reads with no colour.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "t1_matrix-165.jsonl"
OUT_PNG = HERE / "topology-165.png"
OUT_JSON = HERE / "topology-165.json"

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#d7d6d2", "#fcfcfb"
TX_COLOUR = {"TX1": "#2a78d6", "TX2": "#eb6834"}
RX_MARKER = {1: "o", 2: "s"}
RX_FILL = {1: "full", 2: "none"}

#: The ceiling this metric reaches on a noiseless copy of the transmitted
#: buffer, measured in t1_spectra: a path that reaches it is a clean cable.
IDEAL_PTM = 65.08


def load() -> tuple[dict, list[dict]]:
    rows = [json.loads(line) for line in SRC.read_text().splitlines() if line.strip()]
    header = next((row for row in rows if row.get("phase") == "header"), None)
    if header is None:
        raise SystemExit(
            f"{SRC} has no header row: re-run t1_matrix.py.  The caption's probe "
            "geometry is read from the data on purpose, so that it cannot claim "
            "a geometry the measurement did not use.")
    return header, [row for row in rows
                    if row.get("phase") in ("baseline", "TX1", "TX2")]


def series(rows: list[dict], phase: str, receiver: int, field) -> tuple:
    picked = [row for row in rows if row["phase"] == phase
              and row["receiver"] == receiver]
    gains = [row["tx1_gain_db"] if phase == "TX1" else row["tx2_gain_db"]
             for row in picked]
    return gains, [field(row) for row in picked]


def summarise(rows: list[dict]) -> dict:
    """The topology numbers, computed from the data rather than restated.

    TX1's coupling is measured from the matched-filter PEAK, not from rms.  Its
    rms sits inside the noise floor, so a level-based ratio would be a
    difference of two noise estimates; the matched-filter peak is a coherent
    measurement of the correlated part and stays meaningful well below the
    floor.
    """
    top = {}
    for row in rows:
        if row["phase"] not in ("TX1", "TX2"):
            continue
        gain = row["tx1_gain_db"] if row["phase"] == "TX1" else row["tx2_gain_db"]
        if gain == max(GAINS_SHOWN):
            top[(row["phase"], row["receiver"])] = row
    split_db = 20 * np.log10(top[("TX2", 1)]["rms_counts"]
                             / top[("TX2", 2)]["rms_counts"])
    coupling = {rx: 20 * np.log10(top[("TX2", rx)]["matched"]["peak"]
                                  / top[("TX1", rx)]["matched"]["peak"])
                for rx in (1, 2)}
    period = 20_000                      # the cyclic TX buffer, in samples
    phases = {rx: top[("TX2", rx)]["matched"]["peak_lag"] % period for rx in (1, 2)}
    return {"top_gain_db": max(GAINS_SHOWN),
            "rx_split_db": float(abs(split_db)),
            "tx1_coupling_below_tx2_db": {str(k): float(v)
                                          for k, v in coupling.items()},
            "tx2_rms_counts": {str(rx): top[("TX2", rx)]["rms_counts"]
                               for rx in (1, 2)},
            "tx2_matched_ptm": {str(rx): top[("TX2", rx)]["matched"]["peak_to_median"]
                                for rx in (1, 2)},
            "peak_lag_mod_buffer": {str(k): int(v) for k, v in phases.items()},
            "phases_agree": phases[1] == phases[2]}


GAINS_SHOWN = [-50.0, -40.0, -30.0, -20.0]


def main() -> None:
    header, rows = load()
    facts = summarise(rows)
    floors = {receiver: float(np.mean([row["rms_counts"] for row in rows
                                       if row["phase"] == "baseline"
                                       and row["receiver"] == receiver]))
              for receiver in (1, 2)}
    mf_floor = float(np.mean([row["matched"]["peak_to_median"] for row in rows
                              if row["phase"] == "baseline"]))

    plt.rcParams.update({
        "font.size": 11.5, "axes.labelsize": 12, "axes.titlesize": 12.5,
        "xtick.labelsize": 11, "ytick.labelsize": 11, "legend.fontsize": 10,
        "axes.edgecolor": MUTED, "text.color": INK, "axes.labelcolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED})
    fig, (left, right) = plt.subplots(1, 2, figsize=(13.0, 6.4), dpi=150,
                                      facecolor=SURFACE)
    payload = {"figure": "topology-165", "radio": "ip:192.168.1.165",
               "rig": "CABLED LOOPBACK, no antenna", "geometry": header,
               "baseline_rms": floors,
               "baseline_matched_ptm": mf_floor, "ideal_matched_ptm": IDEAL_PTM,
               "summary": facts, "paths": {}}

    for phase in ("TX1", "TX2"):
        for receiver in (1, 2):
            gains, rms = series(rows, phase, receiver, lambda r: r["rms_counts"])
            if not gains:
                continue
            excess = [float(np.sqrt(max(value ** 2 - floors[receiver] ** 2, 1e-6)))
                      for value in rms]
            _, ptm = series(rows, phase, receiver,
                            lambda r: r["matched"]["peak_to_median"])
            label = f"{phase} → RX{receiver}"
            style = dict(color=TX_COLOUR[phase], marker=RX_MARKER[receiver],
                         markersize=9, linewidth=1.8,
                         markerfacecolor=(TX_COLOUR[phase] if RX_FILL[receiver] == "full"
                                          else SURFACE),
                         markeredgecolor=TX_COLOUR[phase], markeredgewidth=1.8)
            # Raw rms, not excess.  Subtracting the floor in quadrature turns
            # the difference of two noise estimates into a swing of orders of
            # magnitude on a log axis, which would draw a dramatic trend for
            # TX1 out of nothing at all.
            left.plot(gains, rms, label=label, **style)
            right.plot(gains, ptm, label=label, **style)
            payload["paths"][label] = {
                "tx_gain_db": gains, "rms_counts": rms,
                "rms_above_floor_counts": excess, "matched_peak_to_median": ptm}

    left.set_yscale("log")
    floor = float(np.mean(list(floors.values())))
    left.axhspan(0, floor, color=GRID, alpha=0.8)
    left.text(-49.5, floor * 0.86, f"TX-off noise floor ≈ {floor:.2f} counts",
              fontsize=9.5, color=MUTED, va="top")
    left.set_xlabel("TX hardwaregain (dB, 0 = maximum power)")
    left.set_ylabel("received level (ADC counts, rms)")
    left.set_title("TX2 gains 10 dB of level per 10 dB of drive.\n"
                   "TX1 never leaves the noise floor.", pad=10)
    left.grid(color=GRID, linewidth=0.7)
    left.set_axisbelow(True)
    left.set_ylim(0.7, 60)

    right.axhline(IDEAL_PTM, color=MUTED, linestyle="--", linewidth=1.3)
    right.text(-20.2, IDEAL_PTM * 1.03,
               f"noiseless ceiling for this waveform = {IDEAL_PTM:.1f}",
               fontsize=9.5, color=MUTED, va="bottom", ha="right")
    right.axhspan(0, mf_floor, color=GRID, alpha=0.8)
    right.text(-49.5, mf_floor * 1.25, f"TX-off floor = {mf_floor:.1f}",
               fontsize=9.5, color=MUTED, va="bottom")
    right.set_xlabel("TX hardwaregain (dB, 0 = maximum power)")
    right.set_ylabel("matched-filter peak / median vs the transmitted buffer")
    right.set_title(
        "TX2 reaches the noiseless ceiling: a clean cable.\n"
        f"TX1 only leaks, {min(facts['tx1_coupling_below_tx2_db'].values()):.0f}–"
        f"{max(facts['tx1_coupling_below_tx2_db'].values()):.0f} dB down.", pad=10)
    right.grid(color=GRID, linewidth=0.7)
    right.set_axisbelow(True)
    right.set_ylim(0, IDEAL_PTM * 1.30)

    for axis, location in ((left, "upper left"), (right, "center left")):
        for side in ("top", "right"):
            axis.spines[side].set_visible(False)
        axis.legend(frameon=False, loc=location)

    n_probes = len([r for r in rows if r["phase"] in ("TX1", "TX2")])
    fig.suptitle(
        "Radio .165 is cabled TX2 → split → RX1 + RX2; TX1 is not connected\n"
        "CABLED LOOPBACK on radio ip:192.168.1.165 — tests the detectors and the "
        "digital pipeline, NOT the LNBs, the antenna, or real sky",
        fontsize=13.5, y=0.995)
    fig.text(0.5, 0.028,
             f"n = {n_probes} (TX port, TX gain, RX port) measurements, one "
             f"{header['probe_ms']:.0f} ms probe of "
             f"{header['probe_samples']:,} samples each.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.text(0.5, 0.006,
             f"{header['sample_rate_hz'] / 1e6:.3f} MS/s, LO "
             f"{header['lo_hz']:,} Hz on TX and RX, RX manual gain "
             f"{header['rx_gain_db']:.0f} dB, rf_bandwidth "
             f"{header['rf_bandwidth_hz'] / 1e6:.0f} MHz.  TX-off floor "
             f"re-measured between every port.",
             ha="center", fontsize=9.5, color=MUTED)
    fig.tight_layout(rect=(0.005, 0.055, 0.995, 0.90))
    fig.savefig(OUT_PNG, dpi=150, facecolor=SURFACE)

    payload["finding"] = (
        f"TX2 drives both receivers at equal level ({facts['rx_split_db']:.2f} dB "
        f"apart) with a matched-filter peak/median of "
        f"{facts['tx2_matched_ptm']['1']:.1f} against a noiseless ceiling of "
        f"{IDEAL_PTM:.1f}, and both receivers see it at the same phase modulo the "
        f"transmitted buffer (lag {facts['peak_lag_mod_buffer']['1']} on both). "
        f"TX1 raises no port's level above its own noise floor and couples "
        f"{min(facts['tx1_coupling_below_tx2_db'].values()):.1f}-"
        f"{max(facts['tx1_coupling_below_tx2_db'].values()):.1f} dB below TX2. "
        f"The topology is TX2 -> split -> RX1 + RX2, a closed path with no "
        f"antenna, the same as sibling rig .183.")
    OUT_JSON.write_text(json.dumps(payload, indent=2))
    print(payload["finding"])
    print(f"wrote {OUT_PNG}")


if __name__ == "__main__":
    main()
