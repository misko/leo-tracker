#!/usr/bin/env python3
"""edge-pilots: what the report's eight algorithms are hunting, and why no
spectrum will ever show it.

WHAT CHANGED FROM THE PUBLISHED VERSION OF THIS FIGURE.  Two claims, both
wording; no plotted value moves and no number is recomputed.

1. "Band lift, best of 2,136 real captures" / "the best real capture".  The
   capture was chosen as the HIGHEST DETECTOR-SCORING observation of the arm --
   rank 1 of 2,136 by the deployed survey bank's own coarse peak-to-median --
   and band lift was never computed for the other 2,135.  A capture that is
   rank 1 on the detector statistic need not be rank 1 on band lift; the two
   statistics are not the same function of the spectrum.  Earning the word
   "best" would take an averaged periodogram of all 2,136, which is 73 GB of
   ci16 off the read-only share (178 captures x 409.6 MB; measured 56 MB/s) and
   about 0.9 s of transform per observation -- roughly half an hour of
   contended NAS traffic and CPU on this host.  That was not spent.  The figure
   therefore says what is true: this is the band lift OF the highest
   detector-scoring capture among 2,136, not the largest band lift in the arm.

2. "16.2 dB below where a spectrum could show it".  That subtracted a
   separately normalised NOISELESS template's lift from a NOISY capture's lift.
   Each trace is referenced to its own 1.05-2.00 MHz shoulder, and the
   reference frame has no noise floor to share with the sky, so the difference
   is not a calibrated SNR deficit and is not a measured signal shortfall.  It
   is now presented as what it is: a visualisation contrast between two traces
   each referenced to its own shoulder.

The empirical result is unchanged and stands: the pilots are not visually
separable in this analysis, and the noiseless reference is a block rather than
a resolvable comb.

Both traces are measured on the same 4096-point grid:

  1. the averaged periodogram of ONE real capture off the read-only scan share;
  2. the averaged periodogram of ``leo_tracker.radio.beacon.pilots
     .edge_pilot_frame`` -- the repository's own noiseless definition of the
     signal, called unmodified -- which is the same thing at infinite SNR.

Nothing about the pilot comb is reconstructed.  Every frequency drawn comes
from ``leo_tracker.radio.beacon.channels``; the 1.875 MHz span is
``fast_scan.PILOT_BANDWIDTH_HZ``; the eight subcarrier indices are
``STARLINK_EDGE_PILOT_SUBCARRIERS``.

INPUT.  The consolidated extract ``../cache/opening-figures.npz`` when it is
present.  When it is not -- this correction pass runs on a host where the
opening pipeline's work directory was not kept -- the published
``edge-pilots.json`` beside this script is read instead.  Every array it
carries under ``plotted`` was written verbatim from that cache by the published
run, so the two paths draw identical traces; ``source`` in the JSON records
which one was used.

    nice -n 15 python3 edge-pilots.py
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
PUBLISHED = Path("/home/satpi01/leo-tracker/reports/starlink-detector-evaluation"
                 "/figures/edge-pilots.json")
PNG = HERE / "edge-pilots.png"
OUT = HERE / "edge-pilots.json"

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
MEASURED = "#e34948"
REFERENCE = "#2a78d6"
BAND = "#e9e7f2"
CAVEAT = "#f4f2ea"

#: The shoulder each trace is referenced to: inside the receiver passband, well
#: clear of the 1.875 MHz pilot span.  Referencing each trace to its OWN
#: shoulder is what puts a noiseless waveform and a real capture on one axis
#: without scaling either into agreement -- and it is also exactly why the gap
#: between the two lifts is not an SNR: neither trace is on the other's scale.
SHOULDER_HZ = (1_050_000.0, 2_000_000.0)


def db(values: np.ndarray, reference: float) -> np.ndarray:
    return 10.0 * np.log10(values / reference)


def from_cache() -> dict:
    """The opening pipeline's consolidated extract, when it is on this host."""
    cache = np.load(CACHE, allow_pickle=True)
    provenance = json.loads(str(cache["provenance"]))
    freqs = np.asarray(cache["freqs_hz"], float)
    measured = np.asarray(cache["spectrum"], float)
    reference = np.asarray(cache["reference_spectrum"], float)
    pilots = np.asarray(cache["pilot_offsets_hz"], float)

    shoulder = ((np.abs(freqs) >= SHOULDER_HZ[0])
                & (np.abs(freqs) <= SHOULDER_HZ[1]))
    in_band = np.abs(freqs) <= PILOT_BANDWIDTH_HZ / 2
    measured_db = db(measured, measured[shoulder].mean())
    reference_db = db(reference, reference[shoulder].mean())
    meta = provenance["spectrum_meta"]

    lift = {"measured": float(db(np.array([measured[in_band].mean()]),
                                 measured[shoulder].mean())[0]),
            "reference": float(db(np.array([reference[in_band].mean()]),
                                  reference[shoulder].mean())[0])}
    sigma_db = float(10 * np.log10(
        1 + 1 / np.sqrt(meta["segments"] * int(in_band.sum()))))

    comb_window = STARLINK_SUBCARRIER_SPACING_HZ / 2
    between = (pilots[:-1] + pilots[1:]) / 2

    def band_mean(trace, centres):
        return np.array([trace[np.abs(freqs - centre) <= comb_window / 2].mean()
                         for centre in centres])

    comb = {}
    for name, trace, scaled in (("measured", measured, measured_db),
                                ("reference", reference, reference_db)):
        on, off = band_mean(trace, pilots), band_mean(trace, between)
        comb[name] = {"window_hz": comb_window,
                      "on_pilot_db": band_mean(scaled, pilots).tolist(),
                      "between_pilot_db": band_mean(scaled, between).tolist(),
                      "contrast_db": float(10 * np.log10(on.mean() / off.mean()))}

    curvature = []
    for low, high in ((1.05e6, 2.00e6), (2.05e6, 3.00e6), (3.05e6, 4.00e6)):
        window = (np.abs(freqs) >= low) & (np.abs(freqs) <= high)
        curvature.append({"low_hz": low, "high_hz": high,
                          "bins": int(window.sum()),
                          "db_rel_shoulder": float(
                              db(np.array([measured[window].mean()]),
                                 measured[shoulder].mean())[0])})

    return {"source": f"{CACHE} (opening-pipeline consolidated extract)",
            "freqs": freqs, "measured_db": measured_db,
            "reference_db": reference_db, "pilots": pilots,
            "meta": meta, "census": provenance["census"],
            "selection": provenance["selection"], "lift": lift,
            "sigma_db": sigma_db, "band_bins": int(in_band.sum()),
            "shoulder_bins": int(shoulder.sum()), "curvature": curvature,
            "comb": comb, "published": None}


def from_published() -> dict:
    """The published figure's own JSON: the same arrays, written verbatim."""
    payload = json.loads(PUBLISHED.read_text())
    plotted = payload["plotted"]
    return {"source": f"{PUBLISHED} (published extract, plotted arrays verbatim)",
            "freqs": np.asarray(plotted["freqs_hz"], float),
            "measured_db": np.asarray(plotted["measured_db_rel_shoulder"], float),
            "reference_db": np.asarray(plotted["reference_db_rel_shoulder"], float),
            "pilots": np.asarray(payload["pilot_offsets_hz"], float),
            "meta": payload["capture"], "census": payload["census_frozen"],
            "selection": payload["selection"], "lift": payload["band_lift_db"],
            "sigma_db": payload["band_lift_sigma_db"],
            "band_bins": payload["band_bins"],
            "shoulder_bins": payload["shoulder_bins"],
            "curvature": payload["passband_curvature"],
            "comb": payload["comb_contrast"], "published": payload}


def main() -> int:
    data = from_cache() if CACHE.is_file() else from_published()
    freqs = data["freqs"]
    measured_db, reference_db = data["measured_db"], data["reference_db"]
    pilots, meta = data["pilots"], data["meta"]
    census, selection = data["census"], data["selection"]
    lift, comb, curvature = data["lift"], data["comb"], data["curvature"]
    sigma_db, band_bins = data["sigma_db"], data["band_bins"]
    shoulder_bins = data["shoulder_bins"]
    comb_window = comb["measured"]["window_hz"]

    edge_baseband = meta["channel_edge_baseband_hz"]
    lobe_hz = 1.0 / OFDM_SYMBOL_DURATION_S
    contrast_db = lift["reference"] - lift["measured"]

    # ------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, (wide, zoom) = plt.subplots(
        2, 1, figsize=(11.6, 10.8), height_ratios=[1.0, 1.05],
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
        "n = %d band bins over n = %d shoulder bins) -- and the NEXT\n"
        "shoulder out is %+.2f dB, so %+.2f dB is this receiver's\n"
        "passband curvature, not pilots."
        % (lift["measured"], sigma_db, band_bins, shoulder_bins,
           curvature[1]["db_rel_shoulder"], lift["measured"]),
        xy=(0.9375, -0.42), xytext=(2.55, -4.05),
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
        "a %+.2f dB BLOCK above ITS OWN shoulder, no comb inside it"
        % lift["reference"],
        xy=(-120.0, 17.4), xytext=(105.0, 24.0),
        fontsize=9.5, color=REFERENCE, ha="left", va="center",
        arrowprops=dict(arrowstyle="->", color=REFERENCE, lw=1.2,
                        shrinkA=3, shrinkB=4))
    zoom.annotate(
        "the sky, same axis,\nre. ITS OWN shoulder: %+.2f dB"
        % lift["measured"],
        xy=(560.0, 0.35), xytext=(-260.0, 7.4),
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

    figure.text(0.016, 0.993,
                "No pilot comb is visible in a spectrum: the 1.875 MHz band "
                "lifts %+.2f dB above its own shoulder in the\nhighest "
                "DETECTOR-SCORING capture of 2,136 -- and the noiseless "
                "reference frame is a block, not a comb"
                % lift["measured"],
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top",
                linespacing=1.32)
    figure.text(0.016, 0.940,
                "Rank 1 of %s target observations in the 640 ms / 10.00 MS/s "
                "arm, ranked by the deployed survey bank's own peak-to-median\n"
                "(%.3f; that arm's own 1%% null bar is %.3f over n = %s null "
                "windows).  %s excluded from target and null.\n"
                "THE RANKING STATISTIC IS THE DETECTOR SCORE, NOT BAND LIFT.  "
                "Band lift was not computed for the other %s observations, so "
                "%+.4f dB is the band lift OF\nthe highest-scoring capture "
                "among %s -- not the largest band lift in the arm."
                % (f"{selection['target_observations_ranked']:,}",
                   selection["chosen_value"],
                   selection["arm_coarse_E_null_threshold"],
                   f"{selection['arm_coarse_E_null_n']:,}",
                   ", ".join(selection["excluded_receivers"]),
                   f"{selection['target_observations_ranked'] - 1:,}",
                   lift["measured"],
                   f"{selection['target_observations_ranked']:,}"),
                fontsize=9.5, color=MUTED, ha="left", va="top",
                linespacing=1.5)
    figure.text(0.016, 0.862,
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

    figure.text(
        0.5, 0.012,
        "THE TWO TRACES ARE SEPARATELY NORMALISED, AND THE %.2f dB BETWEEN "
        "THEM IS NOT AN SNR.  Each is referenced to its own 1.05-2.00 MHz "
        "shoulder (n = %d bins):\nthe noiseless frame carries no noise floor "
        "to share with the sky, and nothing here calibrates one trace against "
        "the other.  Read the pair as a visualisation contrast --\nwhat the "
        "band looks like with the noise removed, beside what it looks like on "
        "the sky -- not as a measured signal deficit.  What is measured is "
        "each trace's own\nlift above its own shoulder (%+.4f dB sky, 1 sigma "
        "%.4f dB; %+.4f dB reference) and each trace's own on-pilot minus "
        "between-pilot contrast."
        % (contrast_db, shoulder_bins, lift["measured"], sigma_db,
           lift["reference"]),
        fontsize=8.8, color=INK, ha="center", va="bottom", linespacing=1.55,
        bbox=dict(boxstyle="round,pad=0.5", facecolor=CAVEAT,
                  edgecolor=GRID, lw=1.0))

    figure.subplots_adjust(left=0.093, right=0.984, top=0.786, bottom=0.140)
    figure.savefig(PNG, dpi=150)

    payload = {
        "figure": "edge-pilots",
        "finding": ("the 1.875 MHz edge-pilot band is %+.4f dB above its own "
                    "shoulder in the highest DETECTOR-SCORING real capture of "
                    "the 640 ms / 10 MS/s arm -- inside this receiver's own "
                    "passband curvature -- where the repository's noiseless "
                    "pilot frame is %+.4f dB above ITS OWN shoulder; and the "
                    "eight pilots do not resolve into a comb even in the "
                    "noiseless frame (on-pilot minus between-pilot %+.4f dB)"
                    % (lift["measured"], lift["reference"],
                       comb["reference"]["contrast_db"])),
        "written_utc": dt.datetime.now(dt.timezone.utc)
                         .isoformat(timespec="seconds"),
        "source": data["source"],
        "corrections_applied": [
            {"was": "Band lift, best of 2,136 real captures / 'the best real "
                    "capture'",
             "now": "band lift in the highest detector-scoring capture among "
                    "2,136",
             "why": "the capture was chosen by rank 1 of the deployed survey "
                    "bank's coarse peak-to-median, not by computing band lift "
                    "over all 2,136 and taking the largest. The two statistics "
                    "are different functions of the spectrum and nothing here "
                    "establishes that the detector-rank-1 observation is also "
                    "band-lift rank 1.",
             "what_would_earn_the_old_wording": "an averaged periodogram of "
                    "all 2,136 target observations in the arm: 178 capture "
                    "directories x 409.6 MB of ci16 = 73 GB off the read-only "
                    "share at a measured 56 MB/s, plus ~0.9 s of transform per "
                    "observation. Not spent on this pass.",
             "band_lift_computed_for_n_observations": 1},
            {"was": "16.2 dB below where a spectrum could show it",
             "now": "a visualisation contrast between two traces each "
                    "referenced to its own shoulder",
             "why": "the difference subtracts a separately normalised NOISELESS "
                    "template's lift from a NOISY capture's lift. Neither trace "
                    "is on the other's scale and the reference frame has no "
                    "noise floor, so the difference is not a calibrated SNR "
                    "deficit and not a measured signal shortfall."}],
        "empirical_result_unchanged": (
            "the pilots are not visually separable in this analysis, and the "
            "noiseless reference is a block rather than a resolvable comb"),
        "census_frozen": census,
        "selection": selection,
        "selection_statistic_is_not_band_lift": True,
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
        "band_bins": band_bins,
        "shoulder_bins": shoulder_bins,
        "reference_minus_measured_db": contrast_db,
        "reference_minus_measured_db_is_not_an_snr": (
            "each trace is referenced to its own 1.05-2.00 MHz shoulder; the "
            "noiseless frame has no noise floor to share with the sky and "
            "nothing calibrates one against the other. This number is a "
            "visualisation contrast, not a signal-to-noise deficit."),
        "passband_curvature": curvature,
        "comb_contrast": comb,
        "pilot_offsets_hz": pilots.tolist(),
        "plotted": {
            "freqs_hz": freqs.tolist(),
            "measured_db_rel_shoulder": measured_db.tolist(),
            "reference_db_rel_shoulder": reference_db.tolist(),
        },
    }
    if data["published"] is not None:
        payload["carried_from_published_json"] = {
            "path": str(PUBLISHED),
            "note": "no plotted value was recomputed on this pass; only the "
                    "figure's wording changed. Census, selection and capture "
                    "blocks are carried forward verbatim.",
            "census_recheck_at_finish":
                data["published"].get("census_recheck_at_finish"),
        }
    OUT.write_text(json.dumps(payload))
    print(json.dumps({key: value for key, value in payload.items()
                      if key not in ("plotted", "capture", "census_frozen",
                                     "repository_constants")}, indent=2))
    print("wrote", PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
