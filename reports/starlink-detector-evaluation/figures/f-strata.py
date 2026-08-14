#!/usr/bin/env python3
"""f-strata: the consistency check was run on the axis with the least variance.

`f` is claimed to be sky occupancy, so it must not depend on how the cells are
sliced.  The report certifies agreement ACROSS ALGORITHMS and reports the spread
there as the model's internal check.  This figure recomputes f on that axis and
on every other axis the corpus offers -- receiver pair, arm, channel, skew --
from the same cells, with the same thresholds, and puts them on one scale.

Everything is computed from /mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/.
Nothing here is typed in by hand.

    nice -n 15 python3 extract_cells.py     # streams the corpus into cells.json.gz
    nice -n 15 python3 f-strata.py          # this file: numbers + PNG + JSON

Estimator, thresholds and the join are the repository's own, imported from
leo_tracker.radio.beacon.cross_radio via fcore.py.

Two definitions of "how far f moves along an axis" are computed, because they
could have disagreed and a reader is entitled to know they do not:

  unpaired  each stratum summarised by its median across the eight detectors,
            then max - min over the strata.
  paired    each detector's own f evaluated on every stratum of the axis, the
            range taken per detector, then the median over detectors.  This is
            the like-for-like comparison, since the certified quantity is a
            range taken with the cells held fixed.

The plotted bar is the paired one; both are written to the JSON.

TWO CORRECTIONS, 2026-08-14
---------------------------

**The SKEW axis is withdrawn, and shown withdrawn.**  Every sweep in this corpus
carries `skew_basis` = "measured at barrier release, a LOWER BOUND on the true
sample-start offset", and `sweep_skew_event` gives the same answer for a record
with no `skew.event` key.  The barrier releasing is not the radios observing:
each thread still has a local oscillator to write, and on an opposite-order
sweep the two are writing DIFFERENT frequencies, so the writes cost different
amounts.  synchronised_scan measured exactly that over 256 paired tunings --
released 0.023 ms (same order) and 0.029 ms (opposite) while sampling began
0.086 ms and 0.274 ms apart, 18.96 ms at worst, with a deterministic ~16.9 ms
bias on tunings 2 and 4 of every opposite-order sweep.

So the stamp is not merely a lower bound: it is BLIND TO THE GEOMETRY it is
being used to stratify.  This figure measures that blindness on its own cells
(same-edge against opposite-edge median stamped skew) and prints the ratio.
Stratifying on a quantity that cannot see the effect it is meant to bound
cannot bound it, so the row is struck through rather than deleted -- its removal
is a finding, and a silently missing row would read as an axis nobody thought to
check.

**The ARM axis is given the prominence it turned out to deserve.**  It is the
largest confound in the report: with the receiver held fixed, stratifying on the
arm removes a little under half of the cross-channel correlation of section 8a,
against a twentieth for the chronological trend that was the one flagged.  The
share is read from analysis-A.json rather than recomputed here; the axis
excursion beside it is this figure's own.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

sys.path.insert(0, "/home/satpi01/leo-tracker/reports/"
                   "sync-scan-cross-radio-2026-08-14/figures")

import fcore  # noqa: E402

HERE = Path(__file__).resolve().parent
PNG = HERE / "f-strata.png"
JSON_OUT = HERE / "f-strata.json"

#: The axis whose stratifying variable does not measure the quantity it is
#: being used to bound.  Named once so the plot, the ranking and the JSON all
#: agree about which row is withdrawn.
WITHDRAWN = "Skew"

#: Where the section-8a residualisation ladder lives.  Read, not recomputed:
#: rebuilding it needs the per-unit tuning table and 1,000 permutation draws,
#: and this figure only quotes the share it attributes to the arm.
ANALYSIS_CANDIDATES = (
    HERE / "analysis-A.json",
    HERE.parent / "analysis" / "analysis-A.json",
    Path("/tmp/claude-1000/-home-satpi01-leo-tracker/"
         "07c4f545-58c8-40cb-8d33-da0c19e82a08/scratchpad/revise/analysis-A.json"),
)

#: The verbatim sidecar mirror the cell table was extracted from.  Used only to
#: read back what each sweep says its skew was measured at, so the withdrawal
#: rests on the corpus rather than on a docstring.
LITE_ROOT = Path(os.environ.get(
    "LITE_ROOT", Path(os.environ.get("REFRESH_WORK", HERE.parent / "work"))
    / "lite"))

# Slot 1 of the reference categorical palette marks the one axis the report
# certified; everything else is neutral ink, with the alert step reserved for
# strata that sit entirely off that band.  The story is one comparison, so it
# gets one hue.
ACCENT = "#2a78d6"
ALERT = "#e34948"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#d9d8d4"
#: The withdrawn axis: drawn, struck through, and never coloured as evidence.
STRUCK = "#a3a29e"
#: The faintest wash that still reads as a group behind the arm rows.
ARM_WASH = "#fbeceb"


def band(cells, model):
    """One stratum: the 8 detector estimates, their range, their median."""
    s = fcore.stratum(cells, model)
    values = sorted(s["values"].values())
    return {**s, "median": statistics.median(values) if values else None,
            "sorted": values}


def skew_provenance(cells: list[dict], payload: dict) -> dict:
    """Everything that decides whether the skew axis may be stratified on.

    Three independent readings, because "the stamp is a lower bound" is the
    weaker half of the problem and the report already said it:

      what the corpus says   every sweep's own ``skew_basis`` string;
      what the module says   the characterisation in synchronised_scan, which
                             measured the reported and the true offset side by
                             side on the real radios;
      what these cells say   the stamped skew on same-edge against opposite-edge
                             cells.  This is the test that matters.  The whole
                             point of the axis is to bound a geometry-dependent
                             offset, and a stamp that reads the same on both
                             geometries cannot see the effect it is bounding.
    """
    from leo_tracker.radio.beacon.cross_radio import DESIGN_MAX_SKEW_MS

    basis: dict = {}
    if LITE_ROOT.is_dir():
        for manifest in sorted(LITE_ROOT.glob("*/manifest.json")):
            try:
                survey = (json.loads(manifest.read_text()).get("metadata")
                          or {}).get("pre_dwell_survey") or {}
            except (OSError, ValueError):
                continue
            scan = survey.get("synchronised_scan") or {}
            key = str(scan.get("skew_basis") or scan.get("skew")
                      or "no skew_basis recorded")
            basis[key] = basis.get(key, 0) + 1

    by_geometry: dict = {}
    for cell in cells:
        if cell.get("skew_ms") is None:
            continue
        by_geometry.setdefault(cell["geometry"], []).append(float(cell["skew_ms"]))
    stamped = {name: {"cells": len(values),
                      "median_ms": statistics.median(values),
                      "mean_ms": sum(values) / len(values),
                      "max_ms": max(values)}
               for name, values in sorted(by_geometry.items())}
    ratio = None
    if len(stamped) == 2:
        low, high = sorted(item["median_ms"] for item in stamped.values())
        ratio = high / low if low else None

    return {
        "verdict": "NOT A USABLE STRATIFYING VARIABLE",
        "what_the_corpus_says": {
            "sweeps_read": sum(basis.values()), "skew_basis": basis,
            "mirror": str(LITE_ROOT)},
        "what_the_module_measured": {
            "source": "leo_tracker.radio.beacon.synchronised_scan, 256 paired "
                      "tunings on the real radios, barrier before the retune",
            "reported_at_barrier_release_ms": {"same order": 0.023,
                                               "opposite order": 0.029},
            "true_sample_start_median_ms": {"same order": 0.086,
                                            "opposite order": 0.274},
            "true_sample_start_max_ms": {"same order": 3.36,
                                         "opposite order": 18.96},
            "true_over_reported_median": {"same order": 0.086 / 0.023,
                                          "opposite order": 0.274 / 0.029},
            "true_offset_opposite_over_same": {"median": 0.274 / 0.086,
                                               "max": 18.96 / 3.36},
            "note": "the error was deterministic in the edge order, not noise: "
                    "tunings 2 and 4 of every opposite-order sweep sat at "
                    "~16.9 ms, always the same way round"},
        "what_these_cells_say": {
            "stamped_skew_by_geometry": stamped,
            "opposite_over_same_median": ratio,
            "design_bound_ms": DESIGN_MAX_SKEW_MS,
            "reading": "the stamp separates the two geometries by a factor of "
                       "%.2f. The offset it stands in for separates them by "
                       "%.1fx on the module's medians and %.1fx on its maxima, "
                       "and it is the geometry that this axis exists to bound."
                       % (ratio or float("nan"), 0.274 / 0.086, 18.96 / 3.36)},
        "consequence": "a stratification on skew_ms splits the corpus on a "
                       "number that is a lower bound on the true offset AND is "
                       "blind to the geometry the offset depends on, so neither "
                       "half of the split means what the axis needs it to mean. "
                       "The row is struck through rather than dropped: its "
                       "removal is itself a finding.",
    }


def arm_confound() -> dict:
    """What section 8a's residualisation ladder attributes to the arm.

    Read from analysis-A.json rather than recomputed: the ladder needs the
    per-unit tuning table and 1,000 permutation draws per rung, and the only
    thing wanted here is the share.
    """
    for candidate in ANALYSIS_CANDIDATES:
        if candidate.is_file():
            break
    else:
        return {"available": False,
                "note": "analysis-A.json not found; the arm is shown as the "
                        "widest axis without its section-8a share"}
    blob = json.loads(candidate.read_text())
    ladder = (blob["A2_time_blocked_null"]["verdict"]
              ["does_the_cross_channel_term_survive_a_time_blocked_null"]
              ["cross_channel_mean_ladder"])
    shares = {}
    for combiner, rungs in ladder.items():
        raw = rungs["raw (no strata)"]
        shares[combiner] = {
            "raw_cross_channel_mean_phi": raw,
            "removed_by_the_arm": (raw - rungs["arm x receiver"]) / raw,
            "removed_by_the_time_trend":
                (raw - rungs["time8 x receiver"]) / raw,
            "removed_by_both": (raw - rungs["time8 x arm x receiver"]) / raw}
    arm = [item["removed_by_the_arm"] for item in shares.values()]
    trend = [item["removed_by_the_time_trend"] for item in shares.values()]
    return {"available": True, "source": str(candidate),
            "basis": "cross-channel mean phi, section 8a, with the receiver "
                     "held fixed in every rung; both combiners (any-of-eight "
                     "and the predeclared glrt-32)",
            "per_combiner": shares,
            "arm_share_range": [min(arm), max(arm)],
            "time_trend_share_range": [min(trend), max(trend)],
            "reading": "the arm — sample rate x probe length — accounts for "
                       "%.0f-%.0f%% of the cross-channel correlation; the "
                       "chronological trend that was the flagged confound "
                       "accounts for %.0f-%.0f%%."
                       % (100 * min(arm), 100 * max(arm),
                          100 * min(trend), 100 * max(trend))}


def corpus_read_utc(payload: dict) -> str:
    """When the sidecars behind this cache were actually read off the share.

    Not "now": the corpus is imported and scored continuously, so the plot's
    population belongs to the moment the extraction pass walked it, and dating
    the figure by the plotting run would overstate how current it is.
    """
    stamp = payload.get("read_utc")
    if stamp:
        return dt.datetime.fromisoformat(stamp).strftime("%Y-%m-%dT%H:%MZ")
    born = dt.datetime.fromtimestamp(fcore.CACHE.stat().st_mtime,
                                     dt.timezone.utc)
    return born.strftime("%Y-%m-%dT%H:%MZ")


def main() -> None:
    payload = fcore.load()
    read_at = corpus_read_utc(payload)
    model = fcore.build(payload)
    cells = model["cells"]
    design = fcore.DESIGN_MAX_SKEW_MS

    rows: list[dict] = []
    slices: dict[str, list] = {}

    def add(axis, label, subset, note=""):
        item = band(subset, model)
        rows.append({"axis": axis, "label": label, "note": note, **item})
        slices.setdefault(axis, []).append(subset)
        return item

    pooled = add("Algorithm", "all matched cells", cells, "checked")

    add("Receiver pair", "lnb-c | lnb-b",
        [c for c in cells if c["receiver_pair"] == "lnb-c|lnb-b"])
    add("Receiver pair", "lnb-d | lnb-b",
        [c for c in cells if c["receiver_pair"] == "lnb-d|lnb-b"])

    for rate in (1.25, 2.50, 5.00, 10.00):
        add("Arm (160 ms)", "%.2f MS/s" % rate,
            [c for c in cells if c["arm"] == "160ms-%.2fMSps" % rate])

    for channel in (1, 2, 3, 4):
        add("Channel", "ch %d" % channel,
            [c for c in cells if c["channel"] == channel])

    add(WITHDRAWN, "within 0.054 ms",
        [c for c in cells if c["skew_ms"] is not None and c["skew_ms"] <= design],
        "withdrawn")
    add(WITHDRAWN, "beyond 0.054 ms",
        [c for c in cells if c["skew_ms"] is not None and c["skew_ms"] > design],
        "withdrawn")

    # ---- why the last two rows are struck through, and why the arm is loud --
    skew_axis = skew_provenance(cells, payload)
    arm_axis = arm_confound()

    # ---- how far f moves along each axis ---------------------------------
    excursion = {}
    for axis in dict.fromkeys(row["axis"] for row in rows):
        group = [row for row in rows if row["axis"] == axis]
        if axis == "Algorithm":
            points = group[0]["sorted"]
            entry = {"slices": len(points),
                     "unpaired_range": max(points) - min(points),
                     "paired_median": max(points) - min(points),
                     "paired_min": max(points) - min(points),
                     "paired_max": max(points) - min(points),
                     "paired_detectors": len(points),
                     "basis": "range over the eight detectors, cells held fixed"}
        else:
            medians = [row["median"] for row in group if row["median"] is not None]
            per = []
            for method in model["methods"]:
                got = [fcore.estimate(subset, method, model)
                       for subset in slices[axis]]
                if all(item.get("solvable") for item in got):
                    values = [item["f"] for item in got]
                    per.append(max(values) - min(values))
            entry = {"slices": len(group),
                     "unpaired_range": max(medians) - min(medians),
                     "paired_median": statistics.median(per) if per else None,
                     "paired_min": min(per) if per else None,
                     "paired_max": max(per) if per else None,
                     "paired_detectors": len(per),
                     "basis": "per-detector range over the strata, median over "
                              "the detectors solvable on every stratum"}
        excursion[axis] = entry

    gap = {method: (rows[2]["values"][method] - rows[1]["values"][method])
           for method in rows[1]["values"] if method in rows[2]["values"]}
    boot = fcore.cluster_bootstrap_gap(cells, model, "lnb-c|lnb-b", "lnb-d|lnb-b")

    lo, hi = pooled["min"], pooled["max"]
    certified = hi - lo
    skew_in, skew_out = rows[-2], rows[-1]

    # The withdrawn axis is drawn but never counted.  It was the ONLY axis that
    # came close to the certified band (0.044 against 0.042), so leaving it in
    # the ranking would let a variable that cannot measure what it stratifies on
    # decide how close the check came to being the flattest test in the building.
    usable = [name for name in excursion if name != WITHDRAWN]
    flatter = [axis for axis in usable
               if axis != "Algorithm"
               and excursion[axis]["paired_median"] < certified]
    ranked = sorted(usable, key=lambda name: excursion[name]["paired_median"])
    rank = ranked.index("Algorithm") + 1
    ordinal = {1: "flattest", 2: "second flattest", 3: "third flattest",
               4: "widest"}[rank]
    widest = ranked[-1]
    times_wider = excursion[widest]["paired_median"] / certified
    # Does the skew split separate, and if so which way round?  The report has
    # it separating with the within-bound stratum HIGHER; both halves of that
    # have to be re-tested, not just the fact of a gap.
    skew_disjoint = (skew_in["min"] > skew_out["max"]
                     or skew_out["min"] > skew_in["max"])
    skew_direction = ("within-bound higher" if skew_in["median"] > skew_out["median"]
                      else "beyond-bound higher")

    # ---------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    # Taller than the published 10.0 x 10.6, and with a wider gap between the
    # panels: the withdrawal of the skew axis needs a paragraph, and a paragraph
    # inside the plot area would sit on the strata it is about.
    figure, (top, bottom) = plt.subplots(
        2, 1, figsize=(10.0, 12.6), height_ratios=[3.05, 1.02],
        gridspec_kw={"hspace": 0.85})

    order = list(reversed(rows))
    ypos, y, last = [], 0.0, None
    for row in order:
        if last is not None and row["axis"] != last:
            y += 0.9
        ypos.append(y)
        last = row["axis"]
        y += 1.0
    place = {id(row): yy for row, yy in zip(order, ypos)}

    # The arm rows get a wash behind them before anything else is drawn: it is
    # the axis that turned out to matter most and it should be findable before
    # the caption is read.
    arm_rows = [yy for row, yy in zip(order, ypos)
                if row["axis"].startswith("Arm")]
    if arm_rows:
        top.axhspan(min(arm_rows) - 0.5, max(arm_rows) + 0.5,
                    color=ARM_WASH, zorder=0, lw=0)

    top.axvspan(lo, hi, color=ACCENT, alpha=0.13, zorder=0, lw=0)
    for edge in (lo, hi):
        top.axvline(edge, color=ACCENT, lw=1.2, zorder=1)

    for row, yy in zip(order, ypos):
        if row["min"] is None:
            continue
        checked = row["note"] == "checked"
        withdrawn = row["note"] == "withdrawn"
        outside = row["min"] > hi or row["max"] < lo
        colour = (STRUCK if withdrawn else
                  ACCENT if checked else (ALERT if outside else INK))
        marker = "o" if checked else ("D" if outside else "s")
        top.plot([row["min"], row["max"]], [yy, yy], "-", color=colour, lw=2.0,
                 zorder=3, solid_capstyle="butt")
        for cap in (row["min"], row["max"]):
            top.plot([cap, cap], [yy - 0.22, yy + 0.22], "-", color=colour,
                     lw=2.0, zorder=3)
        top.plot(row["sorted"], [yy] * len(row["sorted"]), "|", color=colour,
                 ms=7, mew=1.0, alpha=0.5, zorder=4)
        top.plot([row["median"]], [yy], marker, color=colour, ms=8.5,
                 mec="white", mew=1.4, zorder=5)
        if withdrawn:
            # Struck through, label and all: the row stays on the chart because
            # its removal is a finding, and it is crossed out because nothing
            # on it may be read as evidence.
            top.plot([-0.205, 1.075], [yy, yy], "-", color=ALERT, lw=1.3,
                     zorder=7, transform=top.get_yaxis_transform(),
                     clip_on=False)
        tail = "%5d" % row["n_cells"]
        tail += "   %d/8" % row["methods_solved"]
        top.text(1.012, yy, tail, transform=top.get_yaxis_transform(),
                 va="center", ha="left", fontsize=9.5,
                 color=STRUCK if withdrawn else MUTED, family="monospace")

    top.set_yticks(ypos)
    top.set_yticklabels([row["label"] for row in order], fontsize=10.5)
    top.tick_params(axis="y", length=0, pad=6)
    top.set_ylim(-1.2, max(ypos) + 2.1)
    top.set_xlim(0.0, max(row["max"] for row in rows
                          if row["max"] is not None) * 1.07)
    top.set_xlabel("occupancy  f   (bar = min–max over the 8 detectors, "
                   "marker = median, ticks = each detector)")
    top.text(1.012, 1.012, " cells  solved", transform=top.transAxes,
             fontsize=9.5, color=MUTED, family="monospace", ha="left")

    for axis in dict.fromkeys(row["axis"] for row in order):
        members = [yy for row, yy in zip(order, ypos) if row["axis"] == axis]
        emphasis = axis.startswith("Arm")
        top.text(-0.295, max(members) + 0.62,
                 (axis + "  — the largest confound in the report") if emphasis
                 else (axis + "  — WITHDRAWN") if axis == WITHDRAWN else axis,
                 transform=top.get_yaxis_transform(), va="center", ha="left",
                 fontsize=11 if emphasis else 10,
                 weight="bold" if emphasis else "normal",
                 color=ALERT if emphasis else
                       (STRUCK if axis == WITHDRAWN else MUTED))
    for label, row in zip(top.get_yticklabels(), order):
        if row["note"] == "withdrawn":
            label.set_color(STRUCK)

    top.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    top.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        top.spines[spine].set_visible(False)

    top.annotate("the only axis that was checked:  f %.3f–%.3f, spread %.3f"
                 % (lo, hi, certified),
                 xy=(hi, place[id(rows[0])] + 0.30),
                 xytext=(0.145, place[id(rows[0])] + 1.55),
                 fontsize=10.5, color=ACCENT, ha="left", va="center",
                 arrowprops=dict(arrowstyle="->", color=ACCENT, lw=1.4,
                                 shrinkA=2, shrinkB=3))

    # One red block for the arm, not two: the pilot-band note and the confound
    # note are about the same axis, and side by side they collided.  x = 0.012
    # is empty beside the three fast arms, whose bands begin past 0.42.
    arm_low = rows[3]
    arm_rest = [row for row in rows[4:7] if row["min"] is not None]
    arm_note = ("THE ARM IS THE LARGEST CONFOUND IN THE REPORT:\n"
                "f %.3f\u2013%.3f across the four arms, %.0fx the certified\n"
                "spread, and 1.25 MS/s does not fit at all \u2014 %.3f\u2013%.3f,\n"
                "%d/8 solvable, against %.3f\u2013%.3f on the other three."
                % (min(row["min"] for row in rows[3:7]
                       if row["min"] is not None),
                   max(row["max"] for row in rows[3:7]
                       if row["max"] is not None),
                   excursion["Arm (160 ms)"]["paired_median"] / certified,
                   arm_low["min"], arm_low["max"], arm_low["methods_solved"],
                   min(row["min"] for row in arm_rest),
                   max(row["max"] for row in arm_rest)))
    top.text(0.012, 0.5 * (place[id(rows[5])] + place[id(rows[6])]), arm_note,
             fontsize=9.5, color=ALERT, ha="left", va="center", linespacing=1.5)

    top.legend(handles=[
        Line2D([], [], color=ACCENT, marker="o", ms=8.5, lw=2.0, mec="white",
               mew=1.4, label="the certified axis"),
        Line2D([], [], color=INK, marker="s", ms=8.0, lw=2.0, mec="white",
               mew=1.4, label="stratum overlapping it"),
        Line2D([], [], color=ALERT, marker="D", ms=8.0, lw=2.0, mec="white",
               mew=1.4, label="stratum entirely off it"),
        Line2D([], [], color=STRUCK, marker="s", ms=8.0, lw=2.0, mec="white",
               mew=1.4, label="withdrawn: struck through")],
        loc="upper left", bbox_to_anchor=(0.0, -0.078), ncol=4, fontsize=9,
        frameon=False, borderaxespad=0.0, handlelength=1.9, columnspacing=1.1)

    # ---- excursion panel --------------------------------------------------
    names = list(excursion)
    medians = [excursion[name]["paired_median"] for name in names]
    reach = [max(excursion[name]["paired_max"], excursion[name]["paired_median"])
             for name in names]
    ypos2 = list(range(len(names)))[::-1]
    colours = [ACCENT if name == "Algorithm" else
               ALERT if name.startswith("Arm") else
               STRUCK if name == WITHDRAWN else INK for name in names]
    bottom.barh(ypos2, medians, height=0.5, color=colours, zorder=3,
                hatch=["////" if name == WITHDRAWN else "" for name in names],
                edgecolor="white", linewidth=0)
    for name, value, edge, colour, yy in zip(names, medians, reach, colours,
                                             ypos2):
        item = excursion[name]
        if name == WITHDRAWN:
            # Struck through here too, and left out of the ranking above: the
            # bar is on the chart to show that an axis was removed, not to be
            # compared with the ones that were kept.  Two segments because the
            # label is in axes fractions and the bar is in data units.
            bottom.plot([-0.090, -0.004], [yy, yy], "-", color=ALERT, lw=1.3,
                        zorder=6, clip_on=False,
                        transform=bottom.get_yaxis_transform())
            bottom.plot([0.0, edge + 0.105], [yy, yy], "-", color=ALERT,
                        lw=1.3, zorder=6)
            bottom.text(edge + 0.115, yy,
                        "does not measure the offset it stratifies on",
                        va="center", ha="left", fontsize=9.5, color=ALERT)
        if name != "Algorithm":
            # One continuous whisker, drawn white where it crosses the bar and
            # in ink where it runs past the bar end, so it reads as one line
            # rather than a bar plus a floating tick.
            inside = min(item["paired_max"], value)
            outer = STRUCK if name == WITHDRAWN else INK
            bottom.plot([item["paired_min"], inside], [yy, yy], "-",
                        color="white", lw=1.3, zorder=4)
            bottom.plot([inside, item["paired_max"]], [yy, yy], "-",
                        color=outer, lw=1.3, zorder=4)
            bottom.plot([item["paired_min"]] * 2, [yy - 0.15, yy + 0.15], "-",
                        color="white" if item["paired_min"] < value else outer,
                        lw=1.3, zorder=5)
            bottom.plot([item["paired_max"]] * 2, [yy - 0.15, yy + 0.15], "-",
                        color=outer if item["paired_max"] > value else "white",
                        lw=1.3, zorder=5)
        note = "" if item["paired_detectors"] == 8 else \
               "   %d of 8 detectors" % item["paired_detectors"]
        bottom.text(edge + 0.012, yy, "%.3f%s" % (value, note), va="center",
                    ha="left", fontsize=10.5, color=colour, family="monospace")

    bottom.axvline(certified, color=ACCENT, lw=1.2, ls=(0, (4, 3)), zorder=2)
    bottom.set_yticks(ypos2)
    bottom.set_yticklabels(names, fontsize=10.5)
    bottom.tick_params(axis="y", length=0, pad=6)
    bottom.set_xlim(0.0, max(reach) * 1.42)
    bottom.set_ylim(-1.9, len(names) - 0.35)
    bottom.set_xlabel("range of f along that axis   (per detector, median over "
                      "detectors; whisker = detector to detector)")
    bottom.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    bottom.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        bottom.spines[spine].set_visible(False)
    for label, name in zip(bottom.get_yticklabels(), names):
        if name == WITHDRAWN:
            label.set_color(STRUCK)
        elif name.startswith("Arm"):
            label.set_color(ALERT)
            label.set_weight("bold")
    bottom.set_title("The certified axis is the %s of the %d usable axes — "
                     "and the ARM is %.0fx wider"
                     % (ordinal, len(ranked), times_wider),
                     fontsize=12, loc="left", pad=10, weight="bold")
    nearest = min((name for name in ranked if name != "Algorithm"),
                  key=lambda name: excursion[name]["paired_median"])
    bottom.annotate(("no usable axis moves less than the detectors do; %s comes "
                     "closest, at %.3f against %.3f"
                     % (nearest, excursion[nearest]["paired_median"], certified))
                    if not flatter else
                    ("%s move%s less than the detectors do"
                     % (" and ".join(flatter), "" if len(flatter) > 1 else "s")),
                    xy=(excursion[nearest]["paired_median"], ypos2[-1] - 0.30),
                    xytext=(0.075, ypos2[-1] - 1.05),
                    fontsize=9.5, color=MUTED, ha="left", va="center",
                    arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.1,
                                    shrinkA=0, shrinkB=3))

    figure.text(0.019, 0.984,
                "f is not behaving like a sky parameter: the one axis the "
                "report certified is the",
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.019, 0.963,
                "NARROWEST band on this chart, and f moves further along every "
                "axis it did not check",
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top")
    # The two paragraphs live in the gap between the panels: each is about a
    # row on the upper chart AND a bar on the lower one, and inside either plot
    # area they would sit on the strata they are about.
    ratio = (skew_axis["what_these_cells_say"]["opposite_over_same_median"]
             or float("nan"))
    stamped = skew_axis["what_these_cells_say"]["stamped_skew_by_geometry"]
    module = skew_axis["what_the_module_measured"]

    if arm_axis.get("available"):
        figure.text(
            0.019, 0.398,
            "THE ARM \u2014 sample rate x probe length \u2014 IS THE LARGEST "
            "CONFOUND IN THE REPORT.  With the receiver held fixed,\n"
            "stratifying on it removes %.0f\u2013%.0f%% of section 8a's "
            "cross-channel correlation (the any-of-eight union and the\n"
            "predeclared glrt-32 alike); the chronological trend that was the "
            "confound anyone flagged removes %.0f\u2013%.0f%%."
            % (100 * min(arm_axis["arm_share_range"]),
               100 * max(arm_axis["arm_share_range"]),
               100 * min(arm_axis["time_trend_share_range"]),
               100 * max(arm_axis["time_trend_share_range"])),
            fontsize=9, color=ALERT, ha="left", va="top", linespacing=1.55)

    figure.text(
        0.019, 0.348,
        "SKEW \u2014 WITHDRAWN, NOT A USABLE STRATIFYING VARIABLE.  skew_ms is "
        "stamped at BARRIER RELEASE, before the two threads\n"
        "write their LO frequencies: a lower bound on the true sample-start "
        "offset, and blind to the very geometry it is being used\n"
        "to stratify.  On these cells it reads %.4f ms same-edge against %.4f ms "
        "opposite-edge, a ratio of %.2f, while the offset it\n"
        "stands in for separates the two by %.1fx on the module's medians (%.3f "
        "vs %.3f ms) and %.1fx on its maxima (%.2f vs %.2f ms),\n"
        "deterministically and in the edge order.  All %s sweeps say so "
        "themselves: skew_basis = \"measured at barrier release, a\n"
        "LOWER BOUND on the true sample-start offset\".  Struck through rather "
        "than deleted: its removal is itself a finding, and a\n"
        "missing row would read as an axis nobody thought to check."
        % (stamped.get("same-edge", {}).get("median_ms", float("nan")),
           stamped.get("opposite-edge", {}).get("median_ms", float("nan")),
           ratio,
           module["true_sample_start_median_ms"]["opposite order"]
           / module["true_sample_start_median_ms"]["same order"],
           module["true_sample_start_median_ms"]["same order"],
           module["true_sample_start_median_ms"]["opposite order"],
           module["true_sample_start_max_ms"]["opposite order"]
           / module["true_sample_start_max_ms"]["same order"],
           module["true_sample_start_max_ms"]["same order"],
           module["true_sample_start_max_ms"]["opposite order"],
           f"{skew_axis['what_the_corpus_says']['sweeps_read']:,}"),
        fontsize=9, color=ALERT, ha="left", va="top", linespacing=1.55)

    snap = payload.get("snapshot") or {}
    figure.text(0.019, 0.934,
                "cross-radio occupancy: %s matched-arm cells / %s matched-arm "
                "sweeps / %s paired sweeps read / %s scored entries in a pair\n"
                "CENSUS frozen and shared with all six (digest %s): %s sweeps | "
                "%s corpus entries | %s scored sidecars"
                % (f"{len(cells):,}", f"{len({c['sweep'] for c in cells}):,}",
                   f"{len(payload['pairs']):,}", f"{model['entries']:,}",
                   snap.get("scored_digest", "?"),
                   f"{snap.get('sweeps_on_share', 0):,}",
                   f"{snap.get('corpus_entries', 0):,}",
                   f"{snap.get('scored_sidecars', 0):,}"),
                fontsize=9.5, color=MUTED, ha="left", va="top", linespacing=1.5)

    figure.subplots_adjust(left=0.205, right=0.845, top=0.885, bottom=0.072)
    figure.savefig(PNG, dpi=150)
    print("wrote", PNG)

    # ---------------------------------------------------------------- json
    out = {
        "figure": "f-strata",
        "generated_utc": dt.datetime.now(dt.timezone.utc)
                           .isoformat(timespec="seconds"),
        "corpus_read_utc": read_at,
        "corpus": "/mnt/qnap01/mouse9911/leo/surveys/corpus/sync-*/",
        "estimator": "leo_tracker.radio.beacon.cross_radio.solve_coincidence; "
                     "thresholds from the cross-edge null arm at "
                     "false_alarm_rate 0.01; p = per-cell null firing rate; "
                     "matched-arm cells only; lnb-a excluded as dead",
        "snapshot": payload.get("snapshot"),
        "population": {
            "scored_corpus_entries": model["entries"],
            "paired_sweeps_read": len(payload["pairs"]),
            "matched_arm_sweeps": len({c["sweep"] for c in cells}),
            "matched_arm_cells": len(cells),
            "census": payload["census"]},
        "cross_edge_target_code_null_rate_p": {
            m: v["rate"] for m, v in model["false_alarm"].items()},
        "empty_sky_rate_p": {m: v["rate"]
                             for m, v in model["false_alarm"].items()},
        "reference_band_across_algorithms": {
            "min": lo, "max": hi, "spread": certified,
            "per_method": pooled["values"]},
        "strata": [{"axis": row["axis"], "label": row["label"],
                    "status": row["note"] or "not checked",
                    "cells": row["n_cells"],
                    "methods_solved": row["methods_solved"],
                    "min": row["min"], "max": row["max"], "spread": row["spread"],
                    "median": row["median"], "per_method": row["values"],
                    "unsolvable": {m: d["reason"]
                                   for m, d in row["detail"].items()
                                   if not d["solvable"]}}
                   for row in rows],
        "axis_excursion": excursion,
        "usable_axes": ranked,
        "withdrawn_axis": {"axis": WITHDRAWN, **skew_axis},
        "arm_axis_is_the_largest_confound": arm_axis,
        "axes_flatter_than_the_certified_one": flatter,
        "receiver_pair_gap": {
            "definition": "f(lnb-d|lnb-b) - f(lnb-c|lnb-b), per detector",
            "per_method": gap,
            "mean": sum(gap.values()) / len(gap),
            "same_sign_positive": sum(1 for v in gap.values() if v > 0),
            "of": len(gap),
            "cluster_bootstrap_over_sweeps": boot},
        "reviewer_comparison": {
            "note": "the reviewer's figures were taken when ~90-102 paired "
                    "sweeps were scored; the whole corpus is scored now (%d "
                    "paired sweeps, %d matched-arm cells), so levels have "
                    "moved. Reproduces: the receiver-pair gap (same sign, 8/8), "
                    "the channel ordering, and 1.25 MS/s being the weakest arm. "
                    "Does NOT reproduce: the skew split, which is %s here and "
                    "runs %s."
                    % (len(payload["pairs"]), len(cells),
                       "disjoint" if skew_disjoint else "overlapping",
                       skew_direction),
            "algorithms": {"reviewer_min_max_spread": [0.307, 0.362, 0.050],
                           "here_min_max_spread": [lo, hi, certified]},
            "receiver_pair": {
                "reviewer": "f(c|b) 0.244-0.306 vs f(d|b) 0.333-0.387, "
                            "mean gap +0.072, 8/8 same sign, bootstrap "
                            "+0.042..+0.099, 0/300 <= 0",
                "here": "f(c|b) %.3f-%.3f vs f(d|b) %.3f-%.3f, mean gap %+.3f, "
                        "%d/%d same sign, bootstrap %+.3f..%+.3f, %d/%d <= 0"
                        % (rows[1]["min"], rows[1]["max"], rows[2]["min"],
                           rows[2]["max"], sum(gap.values()) / len(gap),
                           sum(1 for v in gap.values() if v > 0), len(gap),
                           boot["p05"], boot["p95"], boot["le_zero"],
                           boot["draws"])},
            "arm": {"reviewer": "1.25 MS/s 160 ms f 0.015; 5.0 MS/s 160 ms "
                                "f 0.401-0.510",
                    "here": "1.25 MS/s 160 ms f %.3f-%.3f (%d/8 solvable); "
                            "5.0 MS/s 160 ms f %.3f-%.3f (%d/8)"
                            % (rows[3]["min"], rows[3]["max"],
                               rows[3]["methods_solved"], rows[5]["min"],
                               rows[5]["max"], rows[5]["methods_solved"])},
            "channel": {"reviewer": "ch1 0.211-0.291 vs ch3 0.381-0.436",
                        "here": "ch1 %.3f-%.3f vs ch3 %.3f-%.3f"
                                % (rows[7]["min"], rows[7]["max"],
                                   rows[9]["min"], rows[9]["max"])},
            "skew": {"reviewer": "within 0.310-0.372 (n=1054) vs beyond "
                                 "0.220-0.288 (n=386), NON-OVERLAPPING",
                     "here": "within %.3f-%.3f (n=%d) vs beyond %.3f-%.3f "
                             "(n=%d), %s"
                             % (skew_in["min"], skew_in["max"],
                                skew_in["n_cells"], skew_out["min"],
                                skew_out["max"], skew_out["n_cells"],
                                "NON-OVERLAPPING" if skew_disjoint
                                else "OVERLAPPING"),
                     "disjoint": bool(skew_disjoint),
                     "direction": skew_direction,
                     "verdict": ("SUPERSEDED — this comparison is withdrawn "
                                 "with the axis. The strata are disjoint here "
                                 "too and run the opposite way round (%s), but "
                                 "neither reading is usable: skew_ms does not "
                                 "measure the offset it is being used to "
                                 "stratify on. See withdrawn_axis."
                                 % skew_direction)}},
        "axis_rank": {"order_flattest_first": ranked,
                      "counted_over": "the axes whose stratifying variable "
                                      "measures what it stratifies on; %s is "
                                      "withdrawn" % WITHDRAWN,
                      "certified_axis_rank": rank,
                      "certified_axis_is": ordinal,
                      "widest_axis": widest,
                      "widest_over_certified": times_wider},
    }
    JSON_OUT.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote", JSON_OUT)
    print("certified spread %.4f (%s of %d usable); axes flatter than it: %s"
          % (certified, ordinal, len(ranked), flatter or "none"))
    print("withdrawn: %s — stamped skew same-edge vs opposite-edge ratio %.3f"
          % (WITHDRAWN,
             skew_axis["what_these_cells_say"]["opposite_over_same_median"]))
    print("skew_basis in the corpus:",
          json.dumps(skew_axis["what_the_corpus_says"]["skew_basis"]))
    if arm_axis.get("available"):
        print("arm share of the cross-channel term %.3f-%.3f, trend %.3f-%.3f"
              % (*arm_axis["arm_share_range"], *arm_axis["time_trend_share_range"]))
    for name, item in excursion.items():
        print("  %-14s paired %.4f  unpaired %.4f  (%d detectors)%s"
              % (name, item["paired_median"], item["unpaired_range"],
                 item["paired_detectors"],
                 "   WITHDRAWN" if name == WITHDRAWN else ""))


if __name__ == "__main__":
    main()
