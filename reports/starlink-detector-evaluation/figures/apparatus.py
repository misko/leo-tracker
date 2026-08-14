#!/usr/bin/env python3
"""apparatus: how the two radios are made simultaneous, and how well it worked.

Panel (a) is a SCHEMATIC and is labelled as one on the figure: it carries no
measured data.  Its every claim is quoted from the scan share's own README
(``/mnt/qnap01/mouse9911/leo-scans/README.md``), which is the primary record of
the collector -- the collector itself is not in this repository; only the
importer that reads it (``leo_tracker.radio.beacon.sync_import``) is.

Panel (b) is measured: every paired tuning in the scored corpus, read from
``skew_ms.per_tuning`` in each sweep's ``sweep.json``, joined into pairs by
``cross_radio``'s own rules, against ``cross_radio.DESIGN_MAX_SKEW_MS``.  The
manifest copy of every one of those numbers was compared with the scan share's
copy tuning by tuning during extraction: 0 disagreements over 1,214 pairs.

THE HONESTY THIS FIGURE EXISTS FOR.  The skew is stamped at barrier release,
not at first sample, so it is a LOWER BOUND on the offset between the two
observations -- and that is drawn on the figure, not left to prose.  Three
things establish it, and all three are read at run time rather than asserted:

  * all 2,428 paired manifests carry the same ``skew_basis`` string, and it
    says barrier release;
  * the share README says the true sample-start offset is 0.2-0.8 ms on
    same-order sweeps and ~4 ms on opposite-order ones -- 5x to 91x the median
    plotted here.  The sentence is quoted verbatim off the README at run time;
  * ``synchronised_scan.sweep_skew_event`` -- the repository's own guard for
    exactly this question -- REFUSES to certify the event for all 1,214 paired
    sweeps, because the interim collector's schema is neither of the two it
    knows.  The claim rests on the corpus and the README, not on the code, and
    the figure says so.

Extraction: summary/opening/{sweep_census,corpus_pairs}.py, consolidated once
by summary/opening/cache_build.py into summary/cache/opening-figures.npz.  This
script reads that cache, the repository and the README; it does not touch the
corpus.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import textwrap
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

sys.path.insert(0, "/home/satpi01/leo-tracker/src")

from leo_tracker.radio.beacon.cross_radio import (  # noqa: E402
    DEAD_RECEIVERS, DESIGN_MAX_SKEW_MS,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "cache" / "opening-figures.npz"
README = Path("/mnt/qnap01/mouse9911/leo-scans/README.md")
PNG = HERE / "apparatus.png"
OUT = HERE / "apparatus.json"

INK, MUTED, GRID = "#0b0b0b", "#52514e", "#d9d8d4"
ALERT = "#e34948"
ACCENT = "#2a78d6"
BOX = "#f2f1ee"
SHADE = "#fbe4e3"

#: What the authoritative 5,097-line run reported, to be reproduced or
#: contradicted here.  Quoted so the disagreement, if any, is visible on the
#: figure instead of being silently absorbed.
AUTHORITATIVE = {"tunings": 9336, "beyond": 2702, "sweeps_beyond": 973,
                 "median_ms": 0.0440, "max_ms": 8.3409,
                 "source": "reports/sync-scan-cross-radio-2026-08-14/"
                           "review-full-corpus.txt"}


def readme_skew() -> dict:
    """The share README's own words on what the stamp is, read at run time."""
    text = README.read_text(encoding="utf-8")
    section = " ".join(text.split("## Skew", 1)[1].split("##", 1)[0].split())
    sentence = re.search(r"The true sample-start offset.*?sweeps,", section)
    clause = re.search(r"measured at ~[0-9.]+[\u2013-][0-9.]+ ms on same-order "
                       r"sweeps and ~[0-9.]+ ms on opposite-order sweeps",
                       section)
    numbers = re.search(r"~([0-9.]+)[–-]([0-9.]+) ms on same-order sweeps "
                        r"and ~([0-9.]+) ms on opposite-order", section)
    if not sentence or not numbers or not clause:
        raise SystemExit("the share README no longer states the sample-start "
                         "offset in the form this figure quotes; refusing to "
                         "draw a caveat from memory")
    low, high, opposite = (float(value) for value in numbers.groups())
    return {"section": section,
            "sentence": sentence.group(0).rstrip(","),
            "clause": clause.group(0),
            "same_order_ms": [low, high], "opposite_order_ms": opposite,
            "states_barrier_release":
                bool(re.search(r"measured at \*\*barrier release\*\*, "
                               r"not at first sample", " ".join(text.split())))}


# ---------------------------------------------------------------- schematic
def box(axes, x, y, width, height, label, *, face=BOX, edge=MUTED,
        size=9.0, weight="normal", colour=INK, lw=1.0, radius=1.6):
    axes.add_patch(FancyBboxPatch(
        (x, y), width, height,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        facecolor=face, edgecolor=edge, lw=lw, zorder=2))
    axes.text(x + width / 2, y + height / 2, label, fontsize=size,
              ha="center", va="center", color=colour, weight=weight,
              zorder=3, linespacing=1.45)


def arrow(axes, start, end, *, colour=MUTED, lw=1.2, style="-|>"):
    axes.add_patch(FancyArrowPatch(
        start, end, arrowstyle=style, color=colour, lw=lw,
        mutation_scale=11, shrinkA=1, shrinkB=1, zorder=4))


def schematic(axes, quote: str) -> None:
    """The design, drawn.  No measured data on this panel; it is a schematic."""
    axes.set_xlim(0, 100)
    axes.set_ylim(0, 100)
    axes.axis("off")

    axes.text(0.0, 100.0,
              "(a)  SCHEMATIC of the design -- no measured data on this panel.  "
              "Every claim is quoted from the scan share's own README.",
              fontsize=10.0, color=INK, ha="left", va="top", weight="bold")
    axes.text(0.0, 94.0,
              "Different radios fail independently -- separate LNBs, separate "
              "Plutos, separate USB controllers on separate buses -- and that is "
              "what makes\ncross-radio agreement usable as evidence here.  The "
              "two receivers INSIDE one Pluto share an ADC clock and a USB bus, "
              "and do not.",
              fontsize=8.6, color=MUTED, ha="left", va="top", linespacing=1.5)

    # the host process
    axes.add_patch(FancyBboxPatch(
        (1.5, 14.0), 63.0, 62.0, boxstyle="round,pad=0,rounding_size=2.0",
        facecolor="white", edgecolor=INK, lw=1.4, ls=(0, (5, 2)), zorder=1))
    axes.text(3.5, 72.8, "ONE PROCESS on SATPI01  --  two threads, one clock",
              fontsize=9.0, weight="bold", color=INK, ha="left", va="center")

    # the barrier
    axes.add_patch(FancyBboxPatch(
        (5.0, 20.0), 5.0, 48.0, boxstyle="round,pad=0,rounding_size=1.2",
        facecolor=INK, edgecolor=INK, lw=1.0, zorder=2))
    axes.text(7.5, 44.0, "threading.Barrier(2)", fontsize=8.6, color="white",
              ha="center", va="center", rotation=90, weight="bold", zorder=3)
    axes.text(10.0, 17.0, "at EVERY tuning (8 per sweep)", fontsize=8.0,
              color=INK, ha="center", va="center")

    for base, name, radio, ports in (
            (52.0, "Thread A", "pluto-19f2", ("rx0  lnb-c", "rx1  lnb-d")),
            (20.0, "Thread B", "pluto-5d4d", ("rx0  lnb-a", "rx1  lnb-b"))):
        box(axes, 13.0, base, 20.5, 13.0, "", size=8.4)
        axes.text(23.25, base + 10.3, name, fontsize=8.6, weight="bold",
                  color=INK, ha="center", va="center", zorder=3)
        axes.text(23.25, base + 4.8,
                  "write local oscillator\n(this radio's channel + edge)",
                  fontsize=8.4, color=INK, ha="center", va="center",
                  zorder=3, linespacing=1.45)
        box(axes, 39.0, base, 21.0, 13.0,
            "start sampling\nthis tuning", size=8.4)
        arrow(axes, (10.0, base + 6.5), (13.0, base + 6.5))
        arrow(axes, (33.5, base + 6.5), (39.0, base + 6.5))
        arrow(axes, (60.0, base + 6.5), (68.5, base + 6.5), colour=INK, lw=1.3)
        axes.text(66.4, base + 6.5, "own USB controller,\nown bus",
                  fontsize=7.4, color=MUTED, ha="center", va="center",
                  rotation=90, zorder=6, linespacing=1.3,
                  bbox=dict(boxstyle="square,pad=0.25", facecolor="white",
                            edgecolor="none"))

        box(axes, 68.5, base - 1.0, 17.0, 15.0, "", face="white", edge=INK,
            lw=1.3)
        axes.text(77.0, base + 11.0, radio, fontsize=8.8, weight="bold",
                  color=INK, ha="center", va="center", zorder=3)
        for offset, port in zip((6.6, 2.6), ports):
            is_dead = any(label in port for label in DEAD_RECEIVERS)
            axes.text(77.0, base + offset,
                      port + ("   DEAD" if is_dead else ""),
                      fontsize=7.8, ha="center", va="center", zorder=3,
                      color=ALERT if is_dead else MUTED,
                      weight="bold" if is_dead else "normal")
        box(axes, 89.0, base + 1.75, 9.5, 9.5, "own\nLNB", face="white",
            edge=INK, size=8.2, lw=1.3)
        arrow(axes, (85.5, base + 6.5), (89.0, base + 6.5), colour=INK, lw=1.3)

    axes.text(83.0, 42.5,
              "lnb-a: flat ~1.19 since 2026-08-13 04:44 UTC.\n"
              "No signal path -- out of target AND null.",
              fontsize=7.8, color=ALERT, ha="center", va="center",
              linespacing=1.5)

    # ---- the one thing this figure exists to say --------------------------
    axes.plot([11.5, 11.5], [14.0, 78.0], color=ACCENT, lw=1.3,
              ls=(0, (3, 2)), zorder=5)
    axes.plot([36.2, 36.2], [14.0, 78.0], color=ALERT, lw=1.3,
              ls=(0, (3, 2)), zorder=5)
    axes.text(11.5, 85.0, "skew_ms is stamped HERE\n(barrier release)",
              fontsize=8.6, color=ACCENT, ha="center", va="top",
              weight="bold", linespacing=1.4)
    axes.text(37.6, 85.0, "the offset that matters starts HERE\n(first sample)",
              fontsize=8.6, color=ALERT, ha="left", va="top",
              weight="bold", linespacing=1.4)
    arrow(axes, (11.5, 45.5), (36.2, 45.5), colour=ALERT, lw=1.4,
          style="<|-|>")
    axes.text(23.9, 47.0, "this gap is NEVER stamped on this build",
              fontsize=8.4, color=ALERT, ha="center", va="bottom",
              weight="bold")
    axes.text(23.9, 44.0, quote,
              fontsize=8.0, color=ALERT, ha="center", va="top",
              linespacing=1.5, zorder=6,
              bbox=dict(boxstyle="square,pad=0.4", facecolor="white",
                        edgecolor="none"))

    # storage
    box(axes, 1.5, 1.0, 97.0, 10.0,
        "IQ written straight to LOCAL NVMe on SATPI01, then copied to the QNAP "
        "scan share in byte-for-byte verified batches.\n"
        "sweep.json is written LAST, after both IQ files close, so its presence "
        "is the commit marker -- a directory without it is ignored.",
        face=BOX, edge=MUTED, size=8.4)
    arrow(axes, (48.0, 14.0), (48.0, 11.0))


def main() -> int:
    cache = np.load(CACHE, allow_pickle=True)
    provenance = json.loads(str(cache["provenance"]))
    census = provenance["census"]
    readme = readme_skew()

    skew = cache["skew_ms"]
    geometry = cache["skew_geometry"]
    sweeps = cache["skew_sweep"]

    beyond = skew > DESIGN_MAX_SKEW_MS
    measured = {
        "tunings": int(skew.size),
        "pairs": provenance["pairs"],
        "median_ms": float(np.median(skew)),
        "min_ms": float(skew.min()),
        "max_ms": float(skew.max()),
        "p90_ms": float(np.percentile(skew, 90)),
        "p99_ms": float(np.percentile(skew, 99)),
        "beyond": int(beyond.sum()),
        "beyond_fraction": float(beyond.mean()),
        "sweeps_beyond": int(len(set(sweeps[beyond].tolist()))),
        "design_max_ms": DESIGN_MAX_SKEW_MS,
    }
    by_geometry = {}
    for name in sorted(set(geometry.tolist())):
        mask = geometry == name
        by_geometry[name] = {
            "n": int(mask.sum()),
            "median_ms": float(np.median(skew[mask])),
            "max_ms": float(skew[mask].max()),
            "beyond_fraction": float((skew[mask] > DESIGN_MAX_SKEW_MS).mean())}

    understatement = {
        "same_order_low": readme["same_order_ms"][0] / measured["median_ms"],
        "same_order_high": readme["same_order_ms"][1] / measured["median_ms"],
        "opposite_order": readme["opposite_order_ms"] / measured["median_ms"]}

    # ------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, (design, measuredax) = plt.subplots(
        2, 1, figsize=(11.6, 11.6), height_ratios=[1.12, 0.92],
        gridspec_kw={"hspace": 0.20})

    quote = "\n".join(textwrap.wrap(
        "share README, verbatim: \u201c%s.\u201d" % readme["clause"], 54))
    schematic(design, quote)

    # -- (b) the measured distribution -------------------------------------
    edges = np.logspace(np.log10(skew.min() * 0.95),
                        np.log10(skew.max() * 1.05), 61)
    counts, _ = np.histogram(skew, bins=edges)
    measuredax.hist(skew, bins=edges, color="#c9c7c1", edgecolor=MUTED,
                    lw=0.4, zorder=3)
    for name, style in (("same-edge", (0, (5, 1.8))),
                        ("opposite-edge", (0, (1.6, 1.6)))):
        measuredax.hist(skew[geometry == name], bins=edges, histtype="step",
                        color=INK, lw=1.25, ls=style, zorder=4)

    measuredax.axvspan(DESIGN_MAX_SKEW_MS, edges[-1], color=SHADE, zorder=1)
    measuredax.axvline(DESIGN_MAX_SKEW_MS, color=ALERT, lw=1.8, zorder=6)
    measuredax.axvline(measured["median_ms"], color=ACCENT, lw=1.5,
                       ls=(0, (4, 2)), zorder=6)

    measuredax.set_xscale("log")
    measuredax.set_yscale("log")
    measuredax.set_xlim(edges[0], edges[-1])
    measuredax.set_ylim(0.7, 7000)
    measuredax.set_xlabel("skew between the two radios at one tuning, "
                          "milliseconds  (log scale)")
    measuredax.set_ylabel("tunings per bin  (log scale)")
    measuredax.grid(color=GRID, lw=0.6, zorder=0)
    measuredax.set_axisbelow(True)

    measuredax.annotate(
        "%s design bound\n(cross_radio.DESIGN_MAX_SKEW_MS)"
        % f"{DESIGN_MAX_SKEW_MS:g} ms",
        xy=(DESIGN_MAX_SKEW_MS, 1400), xytext=(0.0720, 2600),
        fontsize=9.0, color=ALERT, ha="left", va="center", weight="bold",
        arrowprops=dict(arrowstyle="->", color=ALERT, lw=1.3,
                        shrinkA=3, shrinkB=3))
    measuredax.annotate(
        "%s of %s tunings (%.1f%%), in %s of %s sweeps,\nare BEYOND the bound"
        % (f"{measured['beyond']:,}", f"{measured['tunings']:,}",
           100 * measured["beyond_fraction"],
           f"{measured['sweeps_beyond']:,}", f"{measured['pairs']:,}"),
        xy=(0.30, 13.0), xytext=(0.115, 260.0),
        fontsize=9.5, color=ALERT, ha="left", va="center",
        arrowprops=dict(arrowstyle="->", color=ALERT, lw=1.3,
                        shrinkA=3, shrinkB=3))
    measuredax.annotate(
        "worst tuning %.4f ms,\n%.0fx the bound"
        % (measured["max_ms"], measured["max_ms"] / DESIGN_MAX_SKEW_MS),
        xy=(measured["max_ms"], 1.35), xytext=(2.30, 42.0),
        ha="left",
        fontsize=9.0, color=INK, va="center",
        arrowprops=dict(arrowstyle="->", color=INK, lw=1.2,
                        shrinkA=3, shrinkB=3))
    measuredax.annotate(
        "median %.4f ms" % measured["median_ms"],
        xy=(measured["median_ms"], 1900), xytext=(0.0185, 3600),
        fontsize=9.0, color=ACCENT, ha="left", va="center", weight="bold",
        arrowprops=dict(arrowstyle="->", color=ACCENT, lw=1.2,
                        shrinkA=3, shrinkB=3))

    measuredax.legend(handles=[
        Line2D([], [], color="#c9c7c1", lw=7,
               label="all paired tunings, n=%s" % f"{measured['tunings']:,}"),
        Line2D([], [], color=INK, lw=1.25, ls=(0, (5, 1.8)),
               label="same-edge sweeps, n=%s, median %.4f ms"
                     % (f"{by_geometry['same-edge']['n']:,}",
                        by_geometry["same-edge"]["median_ms"])),
        Line2D([], [], color=INK, lw=1.25, ls=(0, (1.6, 1.6)),
               label="opposite-edge sweeps, n=%s, median %.4f ms"
                     % (f"{by_geometry['opposite-edge']['n']:,}",
                        by_geometry["opposite-edge"]["median_ms"]))],
        loc="upper right", fontsize=8.8, frameon=False, borderaxespad=0.6,
        handlelength=2.4, labelspacing=0.5)

    measuredax.set_title(
        "(b)  MEASURED, every paired tuning in the scored corpus -- and every "
        "value is a LOWER BOUND on the offset that matters",
        fontsize=10.0, color=INK, loc="left", pad=6, weight="bold")

    measuredax.text(
        0.0125, 0.026,
        "LOWER BOUND, not the offset.  Stamped at BARRIER RELEASE, not at first "
        "sample.  All %s paired manifests say so in their own skew_basis field, "
        "and the share\nREADME puts the true sample-start offset at %.1f-%.1f ms "
        "same-order and ~%.0f ms opposite-order -- %.0fx to %.0fx the median "
        "plotted above.\nleo_tracker's own sweep_skew_event() REFUSES to certify "
        "the event for all %s of these sweeps (the interim collector's schema is "
        "neither version it knows),\nso this rests on the corpus and the README, "
        "not on the code.  And the stamp cannot tell the two geometries apart "
        "(%.4f vs %.4f ms) although the README\nsays the true offset differs "
        "between them by about %.0fx: the recorded quantity is blind to the very "
        "axis the report strata on."
        % (f"{sum(provenance['skew_basis_in_manifests'].values()):,}",
           readme["same_order_ms"][0], readme["same_order_ms"][1],
           readme["opposite_order_ms"],
           understatement["same_order_low"], understatement["opposite_order"],
           f"{measured['pairs']:,}",
           by_geometry["same-edge"]["median_ms"],
           by_geometry["opposite-edge"]["median_ms"],
           readme["opposite_order_ms"] / readme["same_order_ms"][1]),
        transform=figure.transFigure, fontsize=8.6, color=ALERT,
        ha="left", va="bottom", linespacing=1.6)

    figure.text(0.016, 0.992,
                "Two radios, one process, a barrier at every tuning -- and "
                "%.1f%% of tunings still start further apart\nthan the design "
                "bound allows, on a number that only counts the rendezvous"
                % (100 * measured["beyond_fraction"]),
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top",
                linespacing=1.32)
    figure.text(0.016, 0.937,
                "Reproduces the authoritative full-corpus run (%s of %s "
                "tunings, %.1f%%, median %.4f ms, max %.4f ms) on a corpus "
                "grown by %s pairs since:\n%s of %s tunings, %.1f%%, median "
                "%.4f ms, max %.4f ms."
                % (f"{AUTHORITATIVE['beyond']:,}",
                   f"{AUTHORITATIVE['tunings']:,}",
                   100 * AUTHORITATIVE["beyond"] / AUTHORITATIVE["tunings"],
                   AUTHORITATIVE["median_ms"], AUTHORITATIVE["max_ms"],
                   f"{(measured['tunings'] - AUTHORITATIVE['tunings']) // 8:,}",
                   f"{measured['beyond']:,}", f"{measured['tunings']:,}",
                   100 * measured["beyond_fraction"], measured["median_ms"],
                   measured["max_ms"]),
                fontsize=9.5, color=MUTED, ha="left", va="top",
                linespacing=1.5)
    figure.text(0.016, 0.900,
                "CENSUS frozen before either opening figure was computed "
                "(snapshot.py, digest %s, %s):\n%s sweeps on the scan share  |  "
                "%s corpus entries  |  %s scored sidecars, yielding %s scored "
                "pairs.\nManifest and scan-share copies of every per-tuning "
                "skew were compared: %d disagreements."
                % (census["scored_digest"], census["measured_utc"],
                   f"{census['sweeps_on_share']:,}",
                   f"{census['corpus_entries']:,}",
                   f"{census['scored_sidecars']:,}",
                   f"{measured['pairs']:,}",
                   provenance["skew_mismatches_manifest_vs_share"]),
                fontsize=9.0, color=MUTED, ha="left", va="top", linespacing=1.5)

    figure.subplots_adjust(left=0.070, right=0.988, top=0.838, bottom=0.152)
    figure.savefig(PNG, dpi=150)

    payload = {
        "figure": "apparatus",
        "finding": ("over every paired tuning in the scored corpus the "
                    "barrier-release skew is median %.4f ms and max %.4f ms, "
                    "and %s of %s (%.2f%%) in %s of %s sweeps exceed the "
                    "%.3f ms design bound; the stamp is taken at barrier "
                    "release, so every value is a lower bound on the offset "
                    "between the two observations"
                    % (measured["median_ms"], measured["max_ms"],
                       f"{measured['beyond']:,}", f"{measured['tunings']:,}",
                       100 * measured["beyond_fraction"],
                       f"{measured['sweeps_beyond']:,}",
                       f"{measured['pairs']:,}", DESIGN_MAX_SKEW_MS)),
        "written_utc": dt.datetime.now(dt.timezone.utc)
                         .isoformat(timespec="seconds"),
        "census_frozen": census,
        "panel_a": {"kind": "schematic", "carries_data": False,
                    "source": str(README)},
        "measured": measured,
        "by_geometry": by_geometry,
        "authoritative_run": AUTHORITATIVE,
        "agreement_with_authoritative": {
            "median_ms_delta": measured["median_ms"] - AUTHORITATIVE["median_ms"],
            "max_ms_delta": measured["max_ms"] - AUTHORITATIVE["max_ms"],
            "fraction_delta": (measured["beyond_fraction"]
                               - AUTHORITATIVE["beyond"]
                               / AUTHORITATIVE["tunings"]),
            "extra_tunings": measured["tunings"] - AUTHORITATIVE["tunings"]},
        "skew_stamp_provenance": {
            "skew_basis_in_manifests": provenance["skew_basis_in_manifests"],
            "sweep_skew_event": provenance["sweep_skew_event"],
            "sweep_schemas": provenance["sweep_schemas"],
            "readme": readme,
            "understatement_factor_vs_median": understatement},
        "skew_mismatches_manifest_vs_share":
            provenance["skew_mismatches_manifest_vs_share"],
        "plotted": {
            "bin_edges_ms": edges.tolist(),
            "counts_all": counts.tolist(),
            "counts_same_edge": np.histogram(
                skew[geometry == "same-edge"], bins=edges)[0].tolist(),
            "counts_opposite_edge": np.histogram(
                skew[geometry == "opposite-edge"], bins=edges)[0].tolist()},
    }
    OUT.write_text(json.dumps(payload))
    print(json.dumps({key: value for key, value in payload.items()
                      if key not in ("plotted", "census_frozen")},
                     indent=2)[:3000])
    print("wrote", PNG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
