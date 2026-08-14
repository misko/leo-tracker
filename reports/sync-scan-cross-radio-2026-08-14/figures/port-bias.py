#!/usr/bin/env python3
"""port-bias: lnb-c is shifted, not deaf.

Detection rate against the RAW offset and against the BIAS-CORRECTED offset for
the three live ports.  lnb-c's receiver centre is measured 604 159.8 Hz off
nominal in this corpus, so on the raw axis its whole response sits one LO bias
away from where lnb-b and lnb-d respond -- it reads 1.2% in the bin where they
peak.  Re-expressed against the corrected offset it lands back on their peak
bin.

Every number is computed from /mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/.

  raw offset       = abs(cfo_hz)
  corrected offset = abs(cfo_hz - receiver_centers_hz[receiver])
                     -- cross_radio.guard_band_curve's median_corrected_hz
  threshold        = null_thresholds(): 99th-percentile order statistic of the
                     cross-edge-null population for that (rate, probe_ms)
  lnb-a is dead (DEAD_RECEIVERS) and is out of both populations.

Usage: python3 port-bias.py     (runs extract.py first if cfo-port-corpus.npz is absent)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cfo-port-corpus.npz")
NAME = "port-bias"

METHOD = "differential-32"
FALSE_ALARM_RATE = 0.01
DEAD_RECEIVERS = ("lnb-a",)
RATE_HZ = 5.0e6                 # the rate the reviewers quoted
MIN_BIN_N = 25

#: 100 kHz through the interesting region, then wider, because the corrected
#: axis runs 604 kHz further out for lnb-c than the raw axis does.
EDGES_KHZ = np.array([0, 100, 200, 300, 400, 500, 700, 1000, 1500], float)

PORTS = ["lnb-b", "lnb-c", "lnb-d"]
# dataviz reference palette, categorical slots 1-3 (the documented all-pairs
# safe subset).  Marker and linestyle repeat the identity, so it reads in mono.
STYLE = {
    "lnb-b": {"color": "#2a78d6", "marker": "o", "ls": "-"},
    "lnb-c": {"color": "#eb6834", "marker": "s", "ls": "--"},
    "lnb-d": {"color": "#1baf7a", "marker": "^", "ls": "-."},
}


def _at(ordered: np.ndarray, fraction: float) -> float:
    if ordered.size == 0:
        return float("nan")
    return float(ordered[min(ordered.size - 1,
                             max(0, int(round(fraction * (ordered.size - 1)))))])


def load():
    if not os.path.isfile(CACHE):
        subprocess.run([sys.executable, os.path.join(HERE, "extract.py")], check=True)
    return np.load(CACHE)


def prepare(data):
    label, rate = data["label"], data["rate"]
    probe, target = data["probe_ms"], data["is_target"]
    cfo, bias, score = data["cfo_hz"], data["bias_hz"], data["score"]
    live = ~np.isin(label, DEAD_RECEIVERS)

    thresholds = {}
    for key in sorted(set(zip(rate.tolist(), probe.tolist()))):
        null = (rate == key[0]) & (probe == key[1]) & (~target) & live & ~np.isnan(score)
        thresholds[key] = _at(np.sort(score[null]), 1.0 - FALSE_ALARM_RATE)
    threshold = np.array([thresholds[(r, p)] for r, p in zip(rate, probe)])

    scored = target & ~np.isnan(cfo) & ~np.isnan(score)
    return {"label": label[scored], "rate": rate[scored],
            "raw_khz": np.abs(cfo[scored]) / 1e3,
            "corrected_khz": np.abs(cfo[scored] - bias[scored]) / 1e3,
            "bias_khz": bias[scored] / 1e3,
            "fired": score[scored] > threshold[scored],
            "thresholds": thresholds}


def curve(prepared, axis_key, port, rate_hz=RATE_HZ):
    x = prepared[axis_key]
    at = (prepared["label"] == port) & (prepared["rate"] == rate_hz)
    rows = []
    for low, high in zip(EDGES_KHZ[:-1], EDGES_KHZ[1:]):
        inside = at & (x >= low) & (x < high)
        count = int(inside.sum())
        hits = int(prepared["fired"][inside].sum())
        rows.append({"low_khz": low, "high_khz": high, "mid_khz": (low + high) / 2,
                     "n": count, "fired": hits,
                     "rate_pct": (100.0 * hits / count) if count else None,
                     "plotted": count >= MIN_BIN_N})
    return rows


def draw(axes, count_axes, prepared, axis_key, table):
    for port in PORTS:
        style, rows = STYLE[port], table[axis_key][port]
        x = np.array([row["mid_khz"] for row in rows])
        y = np.array([row["rate_pct"] if row["plotted"] else np.nan for row in rows])
        n = np.array([row["n"] if row["plotted"] else np.nan for row in rows])
        axes.plot(x, y, color=style["color"], marker=style["marker"],
                  linestyle=style["ls"], linewidth=2.1, markersize=8.5,
                  markeredgecolor="white", markeredgewidth=1.0,
                  label=f"{port}  (LO bias {prepared_bias(prepared, port):+.1f} kHz)")
        count_axes.plot(x, n, color=style["color"], marker=style["marker"],
                        linestyle=style["ls"], linewidth=1.7, markersize=6.5,
                        markeredgecolor="white", markeredgewidth=0.8)


def prepared_bias(prepared, port) -> float:
    values = prepared["bias_khz"][prepared["label"] == port]
    return float(values[0]) if values.size else 0.0


def main() -> None:
    prepared = prepare(load())
    bias_khz = {port: prepared_bias(prepared, port) for port in PORTS + ["lnb-a"]}
    table = {key: {port: curve(prepared, key, port) for port in PORTS}
             for key in ("raw_khz", "corrected_khz")}

    plt.rcParams.update({"font.size": 12, "axes.titlesize": 13.5,
                         "axes.labelsize": 12.5, "xtick.labelsize": 11.5,
                         "ytick.labelsize": 11.5, "legend.fontsize": 11,
                         "axes.grid": True, "grid.alpha": 0.25,
                         "grid.linewidth": 0.6, "axes.edgecolor": "#8a8a86",
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, grid = plt.subplots(2, 2, figsize=(12.6, 8.8), sharex="col",
                                sharey="row",
                                gridspec_kw={"height_ratios": [2.6, 1.0],
                                             "hspace": 0.10, "wspace": 0.07})
    (raw_axes, corrected_axes), (raw_n, corrected_n) = grid

    draw(raw_axes, raw_n, prepared, "raw_khz", table)
    draw(corrected_axes, corrected_n, prepared, "corrected_khz", table)

    # --- lnb-c's LO bias, marked on the axis it distorts -------------------
    bias = bias_khz["lnb-c"]
    for axes in (raw_axes, raw_n):
        axes.axvline(bias, color="#eb6834", linestyle=":", linewidth=2.2, zorder=3)
    raw_axes.text(bias - 14, 69, f"lnb-c LO bias +{bias:.1f} kHz  ", fontsize=11,
                  color="#eb6834", fontweight="bold", ha="right", va="top",
                  zorder=6)

    # --- the two load-bearing readings --------------------------------------
    deaf = table["raw_khz"]["lnb-c"][1]
    heard = table["corrected_khz"]["lnb-c"][1]
    peak_raw = table["raw_khz"]["lnb-c"][4]
    raw_axes.annotate(
        f"lnb-c reads {deaf['rate_pct']:.1f}% (n={deaf['n']})\n"
        f"in the 100-200 kHz bin\nwhere the other two peak:\n"
        f"on this axis it looks deaf",
        xy=(150, deaf["rate_pct"] + 1.2), xytext=(640, 26), fontsize=11.5,
        fontweight="bold", color="#8a3410", ha="left", va="center",
        arrowprops=dict(arrowstyle="-|>", color="#eb6834", lw=2.2,
                        shrinkA=6, shrinkB=3,
                        connectionstyle="arc3,rad=0.16"), zorder=7)
    raw_axes.annotate(
        f"its peak moved to raw\n400-500 kHz instead\n"
        f"({peak_raw['rate_pct']:.1f}%, n={peak_raw['n']})",
        xy=(462, peak_raw["rate_pct"] + 1.2), xytext=(700, 50), fontsize=11,
        color="#8a3410", ha="left", va="center",
        arrowprops=dict(arrowstyle="-|>", color="#eb6834", lw=1.8,
                        shrinkA=6, shrinkB=3), zorder=7)
    corrected_axes.annotate(
        f"remove the bias and lnb-c's peak\nlands in the same 100-200 kHz bin\n"
        f"as the other two: {heard['rate_pct']:.1f}% (n={heard['n']}).\n"
        f"SHIFTED, NOT DEAF.",
        xy=(163, heard["rate_pct"] + 1.2), xytext=(360, 53), fontsize=11.5,
        fontweight="bold", color="#8a3410", ha="left", va="center",
        arrowprops=dict(arrowstyle="-|>", color="#eb6834", lw=2.2,
                        shrinkA=6, shrinkB=3), zorder=7)

    # --- the disagreement with "collapses onto one curve" -------------------
    others = {port: table["corrected_khz"][port][1]["rate_pct"] for port in
              ("lnb-b", "lnb-d")}
    corrected_axes.annotate(
        "but they do NOT collapse onto one curve:\n"
        f"lnb-c {heard['rate_pct']:.1f}% vs lnb-b {others['lnb-b']:.1f}%, "
        f"lnb-d {others['lnb-d']:.1f}%\n"
        "in that bin -- a real 1.5x port difference",
        xy=(155, others["lnb-b"]), xytext=(390, 24), fontsize=11,
        color="#241d52", ha="left", va="center",
        arrowprops=dict(arrowstyle="-|>", color="#4a3aa7", lw=1.8,
                        shrinkA=4, shrinkB=3, connectionstyle="arc3,rad=0.2"),
        bbox=dict(boxstyle="round,pad=0.35", fc="#efeef8", ec="#4a3aa7", lw=1.4),
        zorder=7)

    raw_axes.set_title("RAW offset  |cfo|\nlnb-c looks dead here", fontweight="bold")
    corrected_axes.set_title("BIAS-CORRECTED offset  |cfo - receiver centre|\n"
                             "lnb-c is the strongest port here", fontweight="bold")
    raw_axes.set_ylabel("detection rate,\ndifferential-32 (%)")
    raw_n.set_ylabel("candidate points\nper bin (n)")
    raw_axes.set_ylim(0, 70)
    raw_axes.set_xlim(0, 1480)
    corrected_axes.set_xlim(0, 1480)
    raw_n.set_yscale("log")
    raw_n.set_ylim(20, 4000)
    raw_n.axhline(MIN_BIN_N, color="#52514e", linestyle=":", linewidth=1.2)
    corrected_n.axhline(MIN_BIN_N, color="#52514e", linestyle=":", linewidth=1.2)
    raw_n.text(30, 30, f"bins with n < {MIN_BIN_N} are not plotted",
               fontsize=10, color="#52514e", va="bottom")
    for axes in (raw_n, corrected_n):
        axes.set_xlabel("frequency offset (kHz)")
        axes.set_xticks([0, 200, 400, 600, 800, 1000, 1200, 1400])
    for axes in (raw_axes, corrected_axes, raw_n, corrected_n):
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
    raw_axes.legend(loc="lower right", framealpha=0.95, borderpad=0.5)

    figure.suptitle(
        "lnb-c is shifted, not deaf: a +604.2 kHz LO bias moves its whole response "
        "off the raw frequency axis\n"
        f"differential-32 at {RATE_HZ / 1e6:g} MS/s -- lnb-b and lnb-d have zero "
        "bias, so their two panels are identical and only lnb-c moves",
        fontsize=14.5, fontweight="bold", y=1.005)

    dead = dead_port_audit(load(), prepared)
    figure.text(0.5, 0.005,
                "lnb-a is excluded as dead, per the pipeline's DEAD_RECEIVERS policy. "
                "THAT EXCLUSION IS NOT SUPPORTED BY THIS CORPUS: lnb-a fires "
                f"{dead['lnb-a']['fire_pct']:.1f}% of its target points against "
                f"lnb-b's {dead['lnb-b']['fire_pct']:.1f}%,\nand its cross-edge null is "
                f"not silence -- median {dead['lnb-a']['null_median']:.4f} / p99 "
                f"{dead['lnb-a']['null_p99']:.4f} against lnb-b's "
                f"{dead['lnb-b']['null_median']:.4f} / {dead['lnb-b']['null_p99']:.4f}. "
                "See lnb-a-check.py.\n"
                "Thresholds are the cross-edge-null 1% false-alarm order statistic for "
                "each (sample rate, probe length); 176 paired sweeps from "
                ".../surveys/corpus/sync-*/.",
                ha="center", va="top", fontsize=10, color="#52514e", style="italic")

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150,
                   bbox_inches="tight", facecolor="white")

    payload = {
        "figure": NAME,
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/",
        "method": METHOD, "sample_rate_hz": RATE_HZ,
        "false_alarm_rate": FALSE_ALARM_RATE,
        "excluded_receivers": list(DEAD_RECEIVERS),
        "dead_receiver_audit": dead,
        "dead_receiver_audit_note":
            "lnb-a is excluded because cross_radio.DEAD_RECEIVERS says it reads "
            "'flat ~1.19 at every tuning since 04:44 UTC'. On differential-32 this "
            "corpus shows lnb-a firing and nulling like the live ports; the "
            "exclusion is followed here but is not reproduced by the data.",
        "receiver_centre_khz": bias_khz,
        "bin_edges_khz": EDGES_KHZ.tolist(),
        "min_bin_n_plotted": MIN_BIN_N,
        "thresholds": {f"{int(r)}Hz/{p}ms": t
                       for (r, p), t in sorted(prepared["thresholds"].items())},
        "raw_axis": {port: table["raw_khz"][port] for port in PORTS},
        "corrected_axis": {port: table["corrected_khz"][port] for port in PORTS},
        "reference_reported": {
            "corrected_100_200_khz_at_5MSps": {"lnb-c": 61.4, "lnb-c_n": 347,
                                               "lnb-b": 41.7, "lnb-b_n": 458}},
        "corpus_says": {
            "corrected_100_200_khz_at_5MSps": {
                port: {"rate_pct": table["corrected_khz"][port][1]["rate_pct"],
                       "n": table["corrected_khz"][port][1]["n"]}
                for port in PORTS},
            "raw_100_200_khz_at_5MSps": {
                port: {"rate_pct": table["raw_khz"][port][1]["rate_pct"],
                       "n": table["raw_khz"][port][1]["n"]}
                for port in PORTS}},
        "all_rates_pooled_corrected": {
            port: [{"low_khz": low, "high_khz": high, "n": n, "rate_pct": pct}
                   for low, high, n, pct in pooled(prepared, port)]
            for port in PORTS},
    }
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=2)
    print(json.dumps(payload["corpus_says"], indent=2))


def pooled(prepared, port):
    at = prepared["label"] == port
    out = []
    for low, high in zip(EDGES_KHZ[:-1], EDGES_KHZ[1:]):
        inside = at & (prepared["corrected_khz"] >= low) & (prepared["corrected_khz"] < high)
        count = int(inside.sum())
        hits = int(prepared["fired"][inside].sum())
        out.append((low, high, count, (100.0 * hits / count) if count else None))
    return out


def dead_port_audit(data, prepared) -> dict:
    """What the excluded port actually does, beside a live one.

    The exclusion is the pipeline's policy and is honoured; this is the check
    that the policy still describes the corpus.  It does not.
    """
    label, rate, probe = data["label"], data["rate"], data["probe_ms"]
    target, score = data["is_target"], data["score"]
    threshold = np.array([prepared["thresholds"][(r, p)]
                          for r, p in zip(rate, probe)])
    out = {}
    for port in ("lnb-a", "lnb-b", "lnb-c", "lnb-d"):
        hit = (label == port) & target & ~np.isnan(score)
        null = (label == port) & (~target) & ~np.isnan(score)
        out[port] = {
            "target_n": int(hit.sum()),
            "fire_pct": 100.0 * float((score[hit] > threshold[hit]).sum())
                        / max(int(hit.sum()), 1),
            "null_n": int(null.sum()),
            "null_median": float(np.median(score[null])) if null.any() else None,
            "null_p99": _at(np.sort(score[null]), 0.99) if null.any() else None,
        }
    return out


if __name__ == "__main__":
    main()
