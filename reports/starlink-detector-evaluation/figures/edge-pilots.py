#!/usr/bin/env python3
"""edge-pilots: what the report's eight algorithms are hunting, and why no
spectrum will ever show it.

The brief asked for a real capture at a Starlink channel edge "with the pilot
comb visible", and said to say so prominently if it is not.  It is not, and the
measurement says something sharper than "too weak": the comb is not resolvable
in a power spectrum AT ALL, at any signal-to-noise ratio, because the pilots are
re-keyed every 4.4 us OFDM symbol and 1/4.4 us is 227 kHz against a 234.375 kHz
subcarrier spacing.  The lobes fill the gaps between themselves.

Both of those are measured here, on the same 4096-point grid:

  1. the averaged periodogram of ONE real capture off the read-only scan share;
  2. the averaged periodogram of ``leo_tracker.radio.beacon.pilots
     .edge_pilot_frame`` -- the repository's own noiseless definition of the
     signal, called unmodified -- which is the same thing at infinite SNR.

Nothing about the pilot comb is reconstructed.  Every frequency drawn comes from
``leo_tracker.radio.beacon.channels``; the 1.875 MHz span is
``fast_scan.PILOT_BANDWIDTH_HZ``; the eight subcarrier indices are
``STARLINK_EDGE_PILOT_SUBCARRIERS``.

WHICH capture was opened is not a choice made by eye.  Every target-arm,
live-receiver observation in the 640 ms / 10 MS/s arm -- 2,136 of them -- was
ranked by the deployed survey bank's own peak-to-median, and rank 1 was opened.
Choosing the picture before the evidence is exactly the failure this avoids.

The one number a reader will want to over-read is the +0.06 dB the pilot band
sits above its shoulder.  The next shoulder out is 0.12 dB BELOW that shoulder
and the one beyond it 0.47 dB below, so a centre 0.06 dB up is the receiver's
own passband curvature and is drawn on the figure as such.

Extraction: summary/opening/{sweep_census,corpus_pairs,rank_highrate,arm_null,
pilot_spectrum}.py, consolidated once by summary/opening/cache_build.py into
summary/cache/opening-figures.npz.  This script reads that cache and the
repository; it does not touch the corpus.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

sys.path.insert(0, "/home/satpi01/leo-tracker/src")

from leo_tracker.radio.beacon.channels import (  # noqa: E402
    STARLINK_EDGE_PILOT_SUBCARRIERS, STARLINK_SUBCARRIER_SPACING_HZ,
)
from leo_tracker.radio.beacon.fast_scan import PILOT_BANDWIDTH_HZ  # noqa: E402
from leo_tracker.radio.beacon.pilots import OFDM_SYMBOL_DURATION_S  # noqa: E402
from leo_tracker.radio.beacon.structure import (  # noqa: E402
    STARLINK_FRAME_DURATION_S,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache" / "opening-figures.npz"
PNG = HERE / "edge-pilots.png"
OUT = HERE / "edge-pilots.json"

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
MEASURED = "#e34948"
REFERENCE = "#2a78d6"
BAND = "#e9e7f2"

#: The shoulder each trace is referenced to: inside the receiver passband, well
#: clear of the 1.875 MHz pilot span.  Referencing each trace to its OWN
#: shoulder is what puts a noiseless waveform and a real capture on one axis
#: without scaling either into agreement.
SHOULDER_HZ = (1_050_000.0, 2_000_000.0)


def db(values: np.ndarray, reference: float) -> np.ndarray:
    return 10.0 * np.log10(values / reference)


def main() -> int:
    cache = np.load(CACHE, allow_pickle=True)
    provenance = json.loads(str(cache["provenance"]))
    meta = provenance["spectrum_meta"]
    census = provenance["census"]
    selection = provenance["selection"]

    freqs = cache["freqs_hz"]
    measured = cache["spectrum"]
    reference = cache["reference_spectrum"]
    pilots = cache["pilot_offsets_hz"]

    shoulder = ((np.abs(freqs) >= SHOULDER_HZ[0])
                & (np.abs(freqs) <= SHOULDER_HZ[1]))
    in_band = np.abs(freqs) <= PILOT_BANDWIDTH_HZ / 2

    measured_db = db(measured, measured[shoulder].mean())
    reference_db = db(reference, reference[shoulder].mean())

    lift = {
        "measured": float(db(np.array([measured[in_band].mean()]),
                             measured[shoulder].mean())[0]),
        "reference": float(db(np.array([reference[in_band].mean()]),
                              reference[shoulder].mean())[0]),
    }
    # 1-sigma on a band mean of N chi-square-2k bins, k = averaged segments.
    sigma_db = float(10 * np.log10(
        1 + 1 / np.sqrt(meta["segments"] * int(in_band.sum()))))

    # Is the comb a comb?  Mean power on each published pilot subcarrier
    # against the gaps exactly halfway between them, each averaged over a
    # half-spacing window so the two sets neither overlap nor rest on one
    # noisy bin.  Same definition for the capture and for the reference.
    comb_window = STARLINK_SUBCARRIER_SPACING_HZ / 2
    between = (pilots[:-1] + pilots[1:]) / 2

    def band_mean(trace: np.ndarray, centres: np.ndarray) -> np.ndarray:
        return np.array([trace[np.abs(freqs - centre) <= comb_window / 2].mean()
                         for centre in centres])

    comb = {}
    for name, trace, scaled in (("measured", measured, measured_db),
                                ("reference", reference, reference_db)):
        on, off = band_mean(trace, pilots), band_mean(trace, between)
        comb[name] = {
            "window_hz": comb_window,
            "on_pilot_db": band_mean(scaled, pilots).tolist(),
            "between_pilot_db": band_mean(scaled, between).tolist(),
            "contrast_db": float(10 * np.log10(on.mean() / off.mean())),
        }

    # Is +0.06 dB a signal, or the shape of the receiver?  The next shoulder
    # out answers it without a model: a passband already falling this fast per
    # MHz explains a centre sitting this far up.
    curvature = []
    for low, high in ((1.05e6, 2.00e6), (2.05e6, 3.00e6), (3.05e6, 4.00e6)):
        window = (np.abs(freqs) >= low) & (np.abs(freqs) <= high)
        curvature.append({
            "low_hz": low, "high_hz": high, "bins": int(window.sum()),
            "db_rel_shoulder": float(db(np.array([measured[window].mean()]),
                                        measured[shoulder].mean())[0])})

    edge_baseband = meta["channel_edge_baseband_hz"]
    lobe_hz = 1.0 / OFDM_SYMBOL_DURATION_S

    # ------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, (wide, zoom) = plt.subplots(
        2, 1, figsize=(11.6, 9.4), height_ratios=[1.0, 1.05],
        gridspec_kw={"hspace": 0.30})

    # -- (a) the whole captured passband, real capture only ---------------
    wide.axvspan(-PILOT_BANDWIDTH_HZ / 2e6, PILOT_BANDWIDTH_HZ / 2e6,
                 color=BAND, zorder=0)
    wide.plot(freqs / 1e6, measured_db, color=MEASURED, lw=0.8, zorder=3)
    wide.axhline(0.0, color=MUTED, lw=0.9, ls=(0, (4, 3)), zorder=2)
    wide.axvline(edge_baseband / 1e6, color=INK, lw=1.3, ls=(0, (1, 2)),
                 zorder=4)

    wide.annotate(
        "nominal 240 MHz channel edge,\n%.4f MHz out from the pilot band centre"
        % (abs(edge_baseband) / 1e6),
        xy=(edge_baseband / 1e6, -1.55), xytext=(-2.55, -3.55),
        fontsize=9.5, color=INK, ha="center", va="center",
        arrowprops=dict(arrowstyle="->", color=INK, lw=1.1,
                        shrinkA=3, shrinkB=4))
    wide.annotate(
        "1.875 MHz pilot span, 8 subcarriers x 234.375 kHz.\n"
        "Band mean %+.2f dB above its own shoulder (1 sigma %.3f dB,\n"
        "n=%d bins) -- and the NEXT shoulder out is %+.2f dB, so\n"
        "%+.2f dB is this receiver's passband curvature, not pilots."
        % (lift["measured"], sigma_db, int(in_band.sum()),
           curvature[1]["db_rel_shoulder"], lift["measured"]),
        xy=(0.9375, -0.42), xytext=(2.45, -3.75),
        fontsize=9.5, color=INK, ha="center", va="center",
        arrowprops=dict(arrowstyle="->", color=INK, lw=1.1,
                        shrinkA=3, shrinkB=4))
    wide.set_xlim(-5.0, 5.0)
    wide.set_ylim(-6.9, 2.4)
    wide.set_xlabel("baseband frequency, MHz   (0 = the published edge-pilot "
                    "band centre, %.6f GHz RF)" % (meta["rf_center_hz"] / 1e9))
    wide.set_ylabel("power spectral density,\ndB re. own shoulder")
    wide.grid(color=GRID, lw=0.6, zorder=1)
    wide.set_axisbelow(True)
    wide.set_title(
        "(a)  the real capture -- 640 ms at 10.00 MS/s, %s on %s, channel %d "
        "%s edge -- is flat where the pilots are"
        % (meta["receiver_label"], meta["radio_id"], meta["channel"],
           meta["edge"]),
        fontsize=10.0, color=INK, loc="left", pad=6)

    # -- (b) same band, real capture against the noiseless definition -----
    zoom.axvspan(-PILOT_BANDWIDTH_HZ / 2e3, PILOT_BANDWIDTH_HZ / 2e3,
                 color=BAND, zorder=0)
    for value in pilots:
        zoom.axvline(value / 1e3, color=MUTED, lw=0.7, ls=(0, (1, 3)), zorder=2)
    zoom.plot(freqs / 1e3, reference_db, color=REFERENCE, lw=1.5, zorder=4)
    zoom.plot(freqs / 1e3, measured_db, color=MEASURED, lw=1.2,
              ls=(0, (5, 1.6)), zorder=5)
    zoom.axhline(0.0, color=MUTED, lw=0.9, ls=(0, (4, 3)), zorder=3)

    zoom.annotate(
        "the SAME eight pilots with the noise taken away:\n"
        "a %+.2f dB BLOCK, and no comb inside it" % lift["reference"],
        xy=(-120.0, 17.4), xytext=(105.0, 24.0),
        fontsize=9.5, color=REFERENCE, ha="left", va="center",
        arrowprops=dict(arrowstyle="->", color=REFERENCE, lw=1.2,
                        shrinkA=3, shrinkB=4))
    zoom.annotate(
        "the sky, same axis and same band: %+.2f dB.\n"
        "%.1f dB below where a spectrum could show it."
        % (lift["measured"], lift["reference"] - lift["measured"]),
        xy=(560.0, 0.35), xytext=(330.0, 7.6),
        fontsize=9.5, color=MEASURED, ha="left", va="center",
        arrowprops=dict(arrowstyle="->", color=MEASURED, lw=1.2,
                        shrinkA=3, shrinkB=4))
    zoom.text(
        0.0, -7.4,
        "Dotted verticals: the eight published pilot subcarriers (%s edge, "
        "%d-%d), from STARLINK_EDGE_PILOT_SUBCARRIERS.\n"
        "On-pilot minus between-pilot power in %.1f kHz windows:   "
        "noiseless reference %+.2f dB    |    real capture %+.2f dB."
        % (meta["edge"],
           min(STARLINK_EDGE_PILOT_SUBCARRIERS[meta["edge"]]),
           max(STARLINK_EDGE_PILOT_SUBCARRIERS[meta["edge"]]),
           comb_window / 1e3,
           comb["reference"]["contrast_db"], comb["measured"]["contrast_db"]),
        fontsize=9.5, color=INK, ha="center", va="center", linespacing=1.5,
        bbox=dict(boxstyle="square,pad=0.45", facecolor="white",
                  edgecolor="none"), zorder=6)
    zoom.set_xlim(-1600.0, 1600.0)
    zoom.set_ylim(-10.2, 27.0)
    zoom.set_xlabel("baseband frequency, kHz   (0 = edge-pilot band centre)")
    zoom.set_ylabel("power spectral density,\ndB re. own shoulder")
    zoom.grid(color=GRID, lw=0.6, zorder=1)
    zoom.set_axisbelow(True)
    zoom.legend(handles=[
        Line2D([], [], color=REFERENCE, lw=1.5,
               label="leo_tracker's own noiseless pilot frame "
                     "(pilots.edge_pilot_frame)"),
        Line2D([], [], color=MEASURED, lw=1.2, ls=(0, (5, 1.6)),
               label="the measured capture from panel (a)")],
        loc="upper left", fontsize=9.0, frameon=False, borderaxespad=0.5,
        handlelength=2.6)
    zoom.set_title(
        "(b)  no comb even with the noise gone: a %.1f us OFDM symbol spreads "
        "each pilot to %.0f kHz, against %.3f kHz spacing"
        % (OFDM_SYMBOL_DURATION_S * 1e6, lobe_hz / 1e3,
           STARLINK_SUBCARRIER_SPACING_HZ / 1e3),
        fontsize=10.0, color=INK, loc="left", pad=6)

    figure.text(0.016, 0.991,
                "The edge pilots are invisible in a spectrum: %+.2f dB in the "
                "best real capture, where the\nrepository's own noiseless "
                "waveform gives %+.2f dB -- and even that is a block, not a comb"
                % (lift["measured"], lift["reference"]),
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top",
                linespacing=1.32)
    figure.text(0.016, 0.929,
                "Rank 1 of %s target observations in the 640 ms / 10.00 MS/s "
                "arm, ranked by the deployed survey bank's own peak-to-median\n"
                "(%.3f; that arm's own 1%% null bar is %.3f over n=%s null "
                "windows).  %s excluded from target and null."
                % (f"{selection['target_observations_ranked']:,}",
                   selection["chosen_value"],
                   selection["arm_coarse_E_null_threshold"],
                   f"{selection['arm_coarse_E_null_n']:,}",
                   ", ".join(selection["excluded_receivers"])),
                fontsize=9.5, color=MUTED, ha="left", va="top",
                linespacing=1.5)
    figure.text(0.016, 0.877,
                "CENSUS frozen before either opening figure was computed "
                "(snapshot.py, digest %s, %s):\n"
                "%s sweeps on the scan share  |  %s corpus entries  |  %s "
                "scored sidecars.   Capture %s, tuning slot %d,\n"
                "%s samples, %d-point transforms averaged over %s segments "
                "(%.1f Hz per bin)."
                % (census["scored_digest"], census["measured_utc"],
                   f"{census['sweeps_on_share']:,}",
                   f"{census['corpus_entries']:,}",
                   f"{census['scored_sidecars']:,}",
                   meta["capture"], meta["iq_index"], f"{meta['samples']:,}",
                   meta["nfft"], f"{meta['segments']:,}",
                   meta["resolution_hz"]),
                fontsize=9.0, color=MUTED, ha="left", va="top", linespacing=1.5)

    figure.subplots_adjust(left=0.093, right=0.984, top=0.800, bottom=0.066)
    figure.savefig(PNG, dpi=150)

    payload = {
        "figure": "edge-pilots",
        "finding": ("the 1.875 MHz edge-pilot band is %+.4f dB above its own "
                    "shoulder in the highest-scoring real capture of the "
                    "640 ms / 10 MS/s arm -- inside this receiver's own "
                    "passband curvature -- where the repository's noiseless "
                    "pilot frame is %+.4f dB above the same shoulder; and the "
                    "eight pilots do not resolve into a comb even in the "
                    "noiseless frame (on-pilot minus between-pilot %+.4f dB)"
                    % (lift["measured"], lift["reference"],
                       comb["reference"]["contrast_db"])),
        "written_utc": dt.datetime.now(dt.timezone.utc)
                         .isoformat(timespec="seconds"),
        "census_frozen": census,
        "selection": selection,
        "capture": meta,
        "repository_constants": {
            "PILOT_BANDWIDTH_HZ": PILOT_BANDWIDTH_HZ,
            "STARLINK_SUBCARRIER_SPACING_HZ": STARLINK_SUBCARRIER_SPACING_HZ,
            "STARLINK_EDGE_PILOT_SUBCARRIERS":
                {key: list(value) for key, value
                 in STARLINK_EDGE_PILOT_SUBCARRIERS.items()},
            "OFDM_SYMBOL_DURATION_S": OFDM_SYMBOL_DURATION_S,
            "STARLINK_FRAME_DURATION_S": STARLINK_FRAME_DURATION_S,
            "single_symbol_lobe_hz": lobe_hz,
        },
        "shoulder_hz": list(SHOULDER_HZ),
        "band_lift_db": lift,
        "band_lift_sigma_db": sigma_db,
        "band_bins": int(in_band.sum()),
        "shoulder_bins": int(shoulder.sum()),
        "passband_curvature": curvature,
        "comb_contrast": comb,
        "pilot_offsets_hz": pilots.tolist(),
        "plotted": {
            "freqs_hz": freqs.tolist(),
            "measured_db_rel_shoulder": measured_db.tolist(),
            "reference_db_rel_shoulder": reference_db.tolist(),
        },
    }
    OUT.write_text(json.dumps(payload))
    print(json.dumps({key: value for key, value in payload.items()
                      if key not in ("plotted", "capture", "census_frozen",
                                     "repository_constants")}, indent=2))
    print("wrote", PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
