"""E4 figure: detection against a KNOWN imposed carrier offset, at two SNRs.

On sky the offset is estimated and bias-corrected, so a collapse in detection
could be the pipeline's search span or could be the estimator failing.  Here the
offset is applied to the waveform before it is transmitted -- exp(2j.pi.f.t) on
the pilot frame -- so the abscissa is imposed rather than inferred.

Two passes, because offset tolerance can be bought with SNR and a sweep run only
at +4.7 dB could not tell: one far above threshold and one at the detection knee
where the eight sit at Pd ~ 0.99 with zero offset.

The bottom strip is the measured received power of the offset waveform, so a
collapse caused by the analog 5 MHz filter rolling off can be told apart from a
collapse caused by the search span -- separate panels on a shared x axis, never
two y scales on one.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
# The harness helper lives with the records it reads, under
# injection-data/, not beside the figures. Inserting figures/ instead --
# which is what this did -- leaves the import unresolvable from a clean
# checkout, so every one of these scripts failed at its import line.
sys.path.insert(0, str(HERE.parent.parent / "injection-data" / "one-radio"))
import analysis as A
import style

RUNS = [(HERE.parent / "e4_offset.jsonl", "far above threshold"),
        (HERE.parent / "e4_offset_68.jsonl", "at the detection knee")]
THRESHOLDS = HERE.parent / "thresholds.json"
SKY_CLIFF = (350_000.0, 400_000.0)
COARSE_E_SPAN = 700_000.0
COARSE_A_SPAN = 300_000.0
#: measured in E2, so the panels can be labelled in SNR rather than in gain
SNR_BY_GAIN = {-50.0: 4.7, -68.0: -13.5}


def summarise(path: Path, thresholds: dict) -> tuple[dict, list]:
    header, rows = A.read(path)
    by_offset: dict = {}
    for r in rows:
        by_offset.setdefault(round(r["offset_hz"], 1), []).append(r)
    reference = float(np.mean([r["signal_power"] for r in by_offset[0.0]]))
    table = []
    for offset in sorted(by_offset):
        rs = by_offset[offset]
        power = float(np.mean([r["signal_power"] for r in rs]))
        table.append({"offset_hz": offset, "observations": len(rs),
                      "probes": len({r["index"] for r in rs}),
                      "received_relative_db": 10 * np.log10(power / reference),
                      "methods": {m: A.cell_rate(rs, m, thresholds[m])
                                  for m in A.METHODS}})
    return header, table


def last_crossing(table: list, method: str, level: float = 0.5):
    xs = [e["offset_hz"] for e in table]
    ys = [e["methods"][method]["rate"] for e in table]
    cut = None
    for i in range(1, len(xs)):
        if ys[i - 1] >= level > ys[i]:
            span = ys[i - 1] - ys[i]
            cut = xs[i - 1] + (xs[i] - xs[i - 1]) * ((ys[i - 1] - level) / span
                                                     if span else 0)
    return cut


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    thresholds = json.loads(THRESHOLDS.read_text())["empty_channel"]
    passes = []
    for path, label in RUNS:
        if not path.exists():
            continue
        header, table = summarise(path, thresholds)
        gain = float(header["tx2_gain_db"])
        passes.append({"path": str(path), "label": label, "tx2_gain_db": gain,
                       "snr_db": SNR_BY_GAIN.get(gain), "table": table,
                       "collapse_hz_at_pd_50": {m: last_crossing(table, m)
                                                for m in A.METHODS}})

    payload = {"figure": "offset-cliff", "note": A.LOOPBACK_NOTE,
               "offset_applied": "exp(2j*pi*f*t) on the pilot frame before "
                                 "transmission -- imposed, not estimated",
               "coarse_search_spans_hz": {"A": COARSE_A_SPAN, "E": COARSE_E_SPAN},
               "candidate_seed_bank": "E",
               "sky_cliff_hz": list(SKY_CLIFF),
               "passes": passes}
    (HERE / "offset-cliff.json").write_text(json.dumps(payload, indent=1))

    rows_n = len(passes) + 1
    fig = plt.figure(figsize=(12.6, 4.2 * len(passes) + 2.4))
    grid = fig.add_gridspec(rows_n, 1,
                            height_ratios=[2.6] * len(passes) + [0.95],
                            hspace=0.20, left=0.095, right=0.985,
                            top=0.885, bottom=0.125)
    # Every panel shares one x axis: the two passes run different offset grids,
    # and without sharing, the +/-300, +/-700 and 350-400 kHz markers land at
    # different screen positions in each panel and the rows stop being
    # comparable -- which is the whole point of stacking them.
    axes = []
    for i in range(len(passes)):
        axes.append(fig.add_subplot(grid[i, 0],
                                    sharex=axes[0] if axes else None))
    ax_power = fig.add_subplot(grid[len(passes), 0], sharex=axes[0])

    for ax, run in zip(axes, passes):
        khz = np.array([e["offset_hz"] for e in run["table"]]) / 1e3
        ax.axvspan(SKY_CLIFF[0] / 1e3, SKY_CLIFF[1] / 1e3, color="#e34948",
                   alpha=0.13, linewidth=0)
        for span, label in ((COARSE_A_SPAN / 1e3, "coarse-A span ±300"),
                            (COARSE_E_SPAN / 1e3, "coarse-E span ±700")):
            ax.axvline(span, color=style.MUTED, linewidth=1.1,
                       linestyle=(0, (5, 3)))
            ax.text(span - 9, 0.42, label, rotation=90, ha="right", va="center",
                    fontsize=8.5, color=style.INK_2)
        for m in A.METHODS:
            y = np.array([e["methods"][m]["rate"] for e in run["table"]])
            lo = np.array([e["methods"][m]["lo"] for e in run["table"]])
            hi = np.array([e["methods"][m]["hi"] for e in run["table"]])
            s = style.STYLE[m]
            ax.fill_between(khz, lo, hi, color=s["color"], alpha=0.10, linewidth=0)
            style.line(ax, khz, y, m, markeredgecolor=style.SURFACE,
                       markeredgewidth=0.5)
        ax.set_ylim(-0.04, 1.06)
        ax.set_yticks([0, .25, .5, .75, 1.0])
        ax.set_yticklabels(["0", "0.25", "0.50", "0.75", "1"])
        ax.set_ylabel("Detection probability\nper cell", fontsize=10)
        ax.tick_params(labelbottom=False)
        cells = run["table"][0]["observations"]
        probes = run["table"][0]["probes"]
        ax.text(0.006, 0.06,
                f"SNR {run['snr_db']:+.1f} dB — {run['label']}   "
                f"(TX2 {run['tx2_gain_db']:.0f} dB; n = {probes} probes = "
                f"{cells} cells per offset)",
                transform=ax.transAxes, fontsize=9.5, color=style.INK,
                va="bottom", bbox=dict(facecolor=style.SURFACE, edgecolor=style.GRID,
                                       boxstyle="round,pad=0.35", alpha=0.95))

    axes[0].annotate("where sky says detection collapses",
                     xy=(np.mean(SKY_CLIFF) / 1e3, 0.985), xycoords="data",
                     xytext=(np.mean(SKY_CLIFF) / 1e3, 0.66), textcoords="data",
                     ha="center", va="top", fontsize=9.5, color="#a5302f",
                     arrowprops=dict(arrowstyle="->", color="#a5302f",
                                     linewidth=1.2))
    axes[0].legend(loc="center right", ncol=2, handlelength=1.6,
                   columnspacing=1.1, framealpha=0.95, frameon=True,
                   facecolor=style.SURFACE, edgecolor=style.GRID)

    for run, marker in zip(passes, ("o", "s")):
        khz = np.array([e["offset_hz"] for e in run["table"]]) / 1e3
        rel = np.array([e["received_relative_db"] for e in run["table"]])
        ax_power.plot(khz, rel, color=style.INK_2, linewidth=1.6, marker=marker,
                      markersize=4, markeredgecolor=style.SURFACE,
                      markeredgewidth=0.5,
                      label=f"SNR {run['snr_db']:+.1f} dB")
    ax_power.axhline(0, color=style.MUTED, linewidth=0.9, linestyle=(0, (2, 3)))
    ax_power.set_ylabel("Received\npower (dB)", fontsize=10)
    ax_power.set_xlabel("Imposed carrier offset applied to the transmitted pilot "
                        "frame (kHz)")
    ax_power.set_ylim(-0.45, 0.45)
    ax_power.set_xlim(-25, 1025)
    ax_power.legend(loc="lower left", fontsize=8.5, ncol=2)
    ax_power.text(0.006, 0.80, "control: the signal is still arriving at full "
                  "strength — the analog filter is not what ends the curve",
                  transform=ax_power.transAxes, fontsize=9, color=style.INK_2,
                  va="top")

    knee = passes[-1]
    cuts = [v for v in knee["collapse_hz_at_pd_50"].values() if v is not None]
    where = (f"{min(cuts)/1e3:.0f}–{max(cuts)/1e3:.0f} kHz" if cuts
             else "beyond the swept range")
    fig.suptitle("The offset cliff is at the ±700 kHz search span, not at "
                 "350–400 kHz", x=0.095, ha="left", y=0.965, fontsize=14)
    fig.text(0.095, 0.928,
             "Detection is unimpaired through 350–400 kHz at both SNRs; at the "
             "knee it falls off between 700 and 800 kHz, where the coarse-E "
             "bank stops searching.",
             ha="left", fontsize=9.5, color=style.INK_2)
    style.footer(fig, f"At the knee, detection crosses 50% at {where}.  "
                      f"The offset is imposed on the waveform, not estimated "
                      f"from it.")
    fig.savefig(HERE / "offset-cliff.png")

    for run in passes:
        cuts = {m: (None if v is None else round(v / 1e3, 1))
                for m, v in run["collapse_hz_at_pd_50"].items()}
        print(f"SNR {run['snr_db']:+.1f} dB  collapse at Pd=0.5 (kHz): {cuts}")


if __name__ == "__main__":
    main()
