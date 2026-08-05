"""Compact evidence visualization for exact beacon acquisition reports."""
from __future__ import annotations

from pathlib import Path
import json
import numpy as np

from .analysis import detection_gates


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
    analysis = report.get("analysis", {})
    method = analysis.get("exact_acquisition_method", "coherent_grid_v1")
    gates = analysis.get("detection_gates") or detection_gates(method)
    axes[0].axhline(gates["dual_match_margin"], color="#9da7b3", ls="--", lw=1,
                    label="candidate gate")
    axes[0].axhline(gates["qualified_match_margin"], color="#78d381", ls=":", lw=1,
                    label="qualified gate")
    axes[1].axhline(gates["dual_symbol_margin"], color="#9da7b3", ls="--", lw=1,
                    label="candidate gate")
    axes[1].axhline(gates["qualified_symbol_margin"], color="#78d381", ls=":", lw=1,
                    label="qualified gate")
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
        f"{metadata.get('region', '?')} · RF {manifest.get('rf_center_hz', 0)/1e9:.6f} GHz · "
        f"{method} · {state}")
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".next.png")
    figure.savefig(temporary, dpi=140)
    plt.close(figure)
    temporary.replace(output)


def plot_beacon_followup(report: dict, output: Path, *, start_s: float | None = None,
                         stop_s: float | None = None) -> None:
    """Render saved dense checks without repeating expensive IQ analysis."""
    source_path = Path(report.get("source_analysis", ""))
    if not source_path.is_file():
        raise ValueError("follow-up source analysis is unavailable")
    source = json.loads(source_path.read_text())
    if start_s is not None and stop_s is not None and stop_s <= start_s:
        raise ValueError("follow-up plot stop must be after start")
    checks = [item for item in report.get("checks", [])
              if (start_s is None or item["start_s"] >= start_s) and
                 (stop_s is None or item["start_s"] <= stop_s)]
    if not checks:
        raise ValueError("no follow-up checks fall inside the requested plot interval")
    source["exact_checks"] = checks
    source["summary"] = {**source.get("summary", {}),
        "exact_candidate_count": sum(item.get("candidate", False) for item in checks),
        "exact_qualified_count": sum(item.get("qualified", False) for item in checks)}
    plot_beacon_report(source, output)
