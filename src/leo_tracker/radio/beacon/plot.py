"""Compact evidence visualization for exact beacon acquisition reports."""
from __future__ import annotations

from pathlib import Path
import numpy as np


def plot_beacon_report(report: dict, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    checks = report.get("exact_checks", [])
    times = np.asarray([item["start_s"] for item in checks], float)
    figure, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True,
                                constrained_layout=True)
    colors = ("#19b5d8", "#ff9f43")
    for receiver, color in enumerate(colors):
        pss = [item["receivers"][receiver]["pss"]["peak_to_median"] for item in checks]
        margins = [item["receivers"][receiver]["pilot"]["score_margin"] for item in checks]
        match_margins = [item["receivers"][receiver].get("acquisition", {})
                         .get("match_score_margin", np.nan) for item in checks]
        cfo = [item["receivers"][receiver]["pilot"]["frequency_offset_hz"] / 1e3
               for item in checks]
        axes[0].plot(times, match_margins, "o-", color=color, label=f"RX{receiver}")
        axes[1].plot(times, margins, "o-", color=color, label=f"RX{receiver}")
        axes[2].plot(times, pss, "o-", color=color, label=f"RX{receiver}")
        axes[3].plot(times, cfo, "o-", color=color, label=f"RX{receiver}")
    axes[0].axhline(.01, color="#9da7b3", ls="--", lw=1, label="candidate gate")
    axes[0].axhline(.03, color="#78d381", ls=":", lw=1, label="qualified gate")
    axes[1].axhline(.005, color="#9da7b3", ls="--", lw=1, label="candidate gate")
    axes[1].axhline(.02, color="#78d381", ls=":", lw=1, label="qualified gate")
    axes[0].set_ylabel("Joint exact − control\nmatch score")
    axes[1].set_ylabel("Symbolwise exact −\nscrambled control")
    axes[2].set_ylabel("PSS peak / median")
    axes[3].set_ylabel("Estimated CFO (kHz)")
    axes[3].set_xlabel("Capture time (s)")
    axes[3].text(.01, .04, "Absolute CFO includes each LNB's LO offset; Doppler is the common slope.",
                 transform=axes[3].transAxes, color="#596675", fontsize=8)
    for axis in axes:
        axis.grid(alpha=.2); axis.legend(loc="best", ncols=3, fontsize=8)
    manifest = report.get("capture_manifest", {}); metadata = manifest.get("metadata", {})
    state = ("QUALIFIED" if report.get("summary", {}).get("exact_qualified_count") else
             "CANDIDATE" if report.get("summary", {}).get("exact_candidate_count") else
             "control rejected")
    figure.suptitle(
        f"Starlink exact-beacon evidence · ch {metadata.get('channel_number', '?')} "
        f"{metadata.get('region', '?')} · RF {manifest.get('rf_center_hz', 0)/1e9:.6f} GHz · {state}")
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".next.png")
    figure.savefig(temporary, dpi=140)
    plt.close(figure)
    temporary.replace(output)
