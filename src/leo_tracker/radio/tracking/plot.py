"""Waterfall overlays for qualified ensemble trajectories."""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter
import numpy as np

from .observation import load_tracking_observation


COLORS = {"dedoppler-linear/v1": "#00e5ff", "viterbi-ridge/v1": "#ffca28",
          "connected-component-centroid/v1": "#ef5350", "comb-viterbi/v1": "#ab47bc",
          "multi-pilot-consensus/v1": "#66bb6a", "spectral-texture-translation/v1": "#ffffff",
          "broadband-envelope/v1": "#ff7043", "broadband-lower-edge/v1": "#26a69a",
          "broadband-upper-edge/v1": "#26a69a"}


def plot_tracker_report(measurement: Path, report: dict, output: Path) -> None:
    windows = [tuple(item) for item in report["configuration"]["analysis_windows_s"]]
    observation = load_tracking_observation(measurement).select_windows(windows)
    rf_origin = observation.center_frequency_hz+(observation.lnb_lo_hz or 0)
    candidates = report.get("candidates", [])
    qualified_members = {index for joint in report.get("joint_tracks", []) if joint.get("qualified")
                         for index in joint.get("member_indexes", [])}
    fig, axes = plt.subplots(len(windows), 2, figsize=(15, max(4, 3.8*len(windows))),
                             squeeze=False, constrained_layout=True)
    for window_index, (start, stop) in enumerate(windows):
        selected = observation.window(start, stop)
        frequency_ghz = (rf_origin+selected.frequency_hz)/1e9
        for receiver in (0, 1):
            axis = axes[window_index, receiver]
            image = selected.spectra_db[receiver]
            low, high = np.percentile(image, (5, 99.5))
            axis.imshow(image, origin="lower", aspect="auto",
                extent=[frequency_ghz[0], frequency_ghz[-1], selected.time_s[0],
                        selected.time_s[-1]], cmap="viridis", vmin=low, vmax=high)
            for index, candidate in enumerate(candidates):
                if candidate.get("receiver") != receiver or not candidate.get("qualified"):
                    continue
                times = np.asarray(candidate.get("time_s", []), float)
                frequencies = np.asarray(candidate.get("frequency_hz", []), float)
                visible = (times >= start)&(times <= stop)
                if visible.sum() < 2:
                    continue
                color = COLORS.get(candidate.get("tracker"), "#eeeeee")
                paired = index in qualified_members
                axis.plot((rf_origin+frequencies[visible])/1e9, times[visible], color=color,
                          linewidth=2.2 if paired else 1.0, alpha=1 if paired else .65)
                if paired:
                    middle = np.flatnonzero(visible)[len(np.flatnonzero(visible))//2]
                    axis.annotate(f"{candidate['drift_hz_s']/1e3:+.2f} kHz/s",
                        ((rf_origin+frequencies[middle])/1e9, times[middle]), color=color,
                        fontsize=7, xytext=(4, 4), textcoords="offset points",
                        bbox={"facecolor": "black", "alpha": .55, "edgecolor": "none"})
            axis.set_title(f"RX{receiver} · {start:.1f}–{stop:.1f} s")
            axis.set_xlabel("Ku-band RF (GHz, using configured LNB LO)")
            axis.xaxis.set_major_formatter(FormatStrFormatter("%.5f"))
            axis.set_ylabel("Elapsed capture time (s)")
    fig.suptitle("Qualified Doppler tracks — thin: receiver-local; thick/labeled: dual-RX")
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=140)
    plt.close(fig)
