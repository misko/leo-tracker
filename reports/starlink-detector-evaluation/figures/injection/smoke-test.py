"""E1 figure: the rig reproduces the bench numbers, and the ladder is safe.

Two things have to be true before any of the other experiments mean anything:
the injected pilot is actually arriving (correlation peak/median), and the
receiver is nowhere near saturating (peak counts against the 12-bit rail).
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

RUN = HERE.parent / "e1_smoke.json"
REFERENCE = {"rms": 83.6, "peak_to_median": 60.3, "tx2_gain_db": -20.0}
RX_CEILING = 1500.0


def main() -> None:
    style.apply()
    import matplotlib.pyplot as plt

    data = json.loads(RUN.read_text())
    rungs = data["ladder"]
    gains = np.array([r["tx2_gain_db"] for r in rungs])
    payload = {"figure": "smoke-test", "note": A.LOOPBACK_NOTE,
               "reference_from_bench_notes": REFERENCE,
               "config": data["config"], "tx_waveform": data["tx_waveform"],
               "tx_off": {"rx1_rms": data["tx_off"]["rx1"]["rms"],
                          "rx1_peak_to_median":
                              data["tx_off"]["rx1_corr"]["peak_to_median"]},
               "ladder": [{"tx2_gain_db": r["tx2_gain_db"],
                           "rx1_rms": r["rx1"]["rms"], "rx2_rms": r["rx2"]["rms"],
                           "rx1_peak": r["rx1"]["peak"], "rx2_peak": r["rx2"]["peak"],
                           "rx1_peak_to_median": r["rx1_corr"]["peak_to_median"],
                           "rx2_peak_to_median": r["rx2_corr"]["peak_to_median"]}
                          for r in rungs],
               "verdict": data["verdict"], "rx_peak_ceiling_counts": RX_CEILING}
    (HERE / "smoke-test.json").write_text(json.dumps(payload, indent=1))

    fig = plt.figure(figsize=(12.2, 5.6))
    grid = fig.add_gridspec(1, 2, wspace=0.24, left=0.075, right=0.985,
                            top=0.83, bottom=0.175)
    ax = fig.add_subplot(grid[0, 0])
    ax2 = fig.add_subplot(grid[0, 1])

    for key, colour, label in (("rx1_corr", style.SERIES[0], "RX1"),
                               ("rx2_corr", style.SERIES[1], "RX2")):
        y = [r[key]["peak_to_median"] for r in rungs]
        ax.plot(gains, y, color=colour, marker="o" if key == "rx1_corr" else "s",
                markersize=6, markeredgecolor=style.SURFACE, markeredgewidth=0.7,
                label=label)
    ax.axhline(data["tx_off"]["rx1_corr"]["peak_to_median"], color=style.MUTED,
               linewidth=1.1, linestyle=(0, (4, 2)))
    ax.text(gains.min(), data["tx_off"]["rx1_corr"]["peak_to_median"] + 1.2,
            f"transmitter off: {data['tx_off']['rx1_corr']['peak_to_median']:.1f}",
            fontsize=9, color=style.INK_2, ha="left")
    ax.axhline(REFERENCE["peak_to_median"], color="#008300", linewidth=1.3,
               linestyle=(0, (2, 2)))
    ax.text(gains.max(), REFERENCE["peak_to_median"] - 2.5,
            f"bench note: {REFERENCE['peak_to_median']}", fontsize=9,
            color="#006200", ha="right", va="top")
    ax.axhline(8.0, color="#e34948", linewidth=1.1, linestyle=(0, (1, 2)))
    ax.text(gains.min(), 9.0, "stop-work floor: 8", fontsize=9, color="#a5302f")
    ax.set_xlabel("TX2 hardware gain (dB)")
    ax.set_ylabel("Correlation peak / median against the transmitted frame")
    ax.set_title("The injected pilot is arriving", loc="left", pad=24)
    final = rungs[-1]
    ax.text(0.0, 1.015,
            f"measured {final['rx1_corr']['peak_to_median']:.1f} at TX2 "
            f"{final['tx2_gain_db']:.0f} dB against a noted {REFERENCE['peak_to_median']}",
            transform=ax.transAxes, fontsize=9, color=style.INK_2)
    ax.legend(loc="lower right")

    for key, colour, marker, label in (("rx1", style.SERIES[0], "o", "RX1 peak"),
                                       ("rx2", style.SERIES[1], "s", "RX2 peak")):
        ax2.plot(gains, [r[key]["peak"] for r in rungs], color=colour,
                 marker=marker, markersize=6, markeredgecolor=style.SURFACE,
                 markeredgewidth=0.7, label=label)
    ax2.plot(gains, [r["rx1"]["rms"] for r in rungs], color=style.INK_2,
             linewidth=1.6, linestyle=(0, (4, 2)), marker="^", markersize=5,
             label="RX1 rms")
    ax2.axhline(RX_CEILING, color="#e34948", linewidth=1.4, linestyle=(0, (5, 3)))
    ax2.text(gains.max(), RX_CEILING * 1.10, "safety ceiling 1500 counts",
             fontsize=9, color="#a5302f", ha="right")
    ax2.axhline(2048, color=style.MUTED, linewidth=1.0, linestyle=(0, (1, 3)))
    ax2.text(gains.max(), 2048 * 1.12, "12-bit full scale 2048", fontsize=8.5,
             color=style.INK_2, ha="right")
    ax2.plot([REFERENCE["tx2_gain_db"]], [REFERENCE["rms"]], marker="*",
             markersize=15, color="#008300", markeredgecolor=style.SURFACE,
             markeredgewidth=0.8, linestyle="none", label="bench-note rms 83.6")
    ax2.set_yscale("log")
    ax2.set_ylim(0.9, 4200)
    ax2.set_xlabel("TX2 hardware gain (dB)")
    ax2.set_ylabel("Receiver level (ADC counts)")
    top_peak = max(max(r["rx1"]["peak"], r["rx2"]["peak"]) for r in rungs)
    headroom = 20 * np.log10(RX_CEILING / top_peak)
    ax2.set_title(f"With {headroom:.0f} dB of headroom at the top rung",
                  loc="left", pad=24)
    ax2.text(0.0, 1.015,
             f"highest peak reached {top_peak:.0f} counts against a 1500 count ceiling",
             transform=ax2.transAxes, fontsize=9, color=style.INK_2)
    ax2.legend(loc="lower right", fontsize=8.5, framealpha=0.95,
               frameon=True, facecolor=style.SURFACE, edgecolor=style.GRID)

    style.footer(fig, f"Digital drive fixed at {data['tx_waveform']['digital_peak']:.0f} "
                      f"counts peak so the received level matches the bench note; "
                      f"TX2 returned to −89.75 dB in a finally block.")
    fig.savefig(HERE / "smoke-test.png")
    print("peak/median at -20 dB:", final["rx1_corr"]["peak_to_median"])
    print("rms at -20 dB:", final["rx1"]["rms"], "peak:", final["rx1"]["peak"])


if __name__ == "__main__":
    main()
