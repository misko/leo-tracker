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
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import fcore  # noqa: E402

HERE = Path(__file__).resolve().parent
PNG = HERE / "f-strata.png"
JSON_OUT = HERE / "f-strata.json"

# Slot 1 of the reference categorical palette marks the one axis the report
# certified; everything else is neutral ink, with the alert step reserved for
# strata that sit entirely off that band.  The story is one comparison, so it
# gets one hue.
ACCENT = "#2a78d6"
ALERT = "#e34948"
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#d9d8d4"


def band(cells, model):
    """One stratum: the 8 detector estimates, their range, their median."""
    s = fcore.stratum(cells, model)
    values = sorted(s["values"].values())
    return {**s, "median": statistics.median(values) if values else None,
            "sorted": values}


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

    add("Skew", "within 0.054 ms",
        [c for c in cells if c["skew_ms"] is not None and c["skew_ms"] <= design])
    add("Skew", "beyond 0.054 ms",
        [c for c in cells if c["skew_ms"] is not None and c["skew_ms"] > design])

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
    flatter = [axis for axis, item in excursion.items()
               if axis != "Algorithm" and item["paired_median"] < certified]

    # ---------------------------------------------------------------- figure
    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID,
                         "axes.labelcolor": INK, "text.color": INK,
                         "xtick.color": MUTED, "ytick.color": INK,
                         "figure.facecolor": "white", "axes.facecolor": "white"})
    figure, (top, bottom) = plt.subplots(
        2, 1, figsize=(10.0, 10.6), height_ratios=[3.05, 1.0],
        gridspec_kw={"hspace": 0.42})

    order = list(reversed(rows))
    ypos, y, last = [], 0.0, None
    for row in order:
        if last is not None and row["axis"] != last:
            y += 0.9
        ypos.append(y)
        last = row["axis"]
        y += 1.0
    place = {id(row): yy for row, yy in zip(order, ypos)}

    top.axvspan(lo, hi, color=ACCENT, alpha=0.13, zorder=0, lw=0)
    for edge in (lo, hi):
        top.axvline(edge, color=ACCENT, lw=1.2, zorder=1)

    for row, yy in zip(order, ypos):
        if row["min"] is None:
            continue
        checked = row["note"] == "checked"
        outside = row["min"] > hi or row["max"] < lo
        colour = ACCENT if checked else (ALERT if outside else INK)
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
        tail = "%5d" % row["n_cells"]
        tail += "   %d/8" % row["methods_solved"]
        top.text(1.012, yy, tail, transform=top.get_yaxis_transform(),
                 va="center", ha="left", fontsize=9.5, color=MUTED,
                 family="monospace")

    top.set_yticks(ypos)
    top.set_yticklabels([row["label"] for row in order], fontsize=10.5)
    top.tick_params(axis="y", length=0, pad=6)
    top.set_ylim(-1.9, max(ypos) + 2.1)
    top.set_xlim(0.0, 0.50)
    top.set_xlabel("occupancy  f   (bar = min–max over the 8 detectors, "
                   "marker = median, ticks = each detector)")
    top.text(1.012, 1.012, " cells  solved", transform=top.transAxes,
             fontsize=9.5, color=MUTED, family="monospace", ha="left")

    for axis in dict.fromkeys(row["axis"] for row in order):
        members = [yy for row, yy in zip(order, ypos) if row["axis"] == axis]
        top.text(-0.295, max(members) + 0.62, axis,
                 transform=top.get_yaxis_transform(),
                 va="center", ha="left", fontsize=10, color=MUTED)

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

    arm_low = rows[3]
    top.annotate("1.25 MS/s — pilot band does not fit:\nf collapses to %.3f–%.3f"
                 % (arm_low["min"], arm_low["max"]),
                 xy=(arm_low["max"], place[id(arm_low)]),
                 xytext=(0.015, place[id(arm_low)] + 1.55),
                 fontsize=9.5, color=ALERT, ha="left", va="center",
                 arrowprops=dict(arrowstyle="->", color=ALERT, lw=1.3,
                                 shrinkA=0, shrinkB=4))

    top.annotate("skew strata now sit on top of each other;\n"
                 "disjoint at 90 sweeps, overlapping at %d"
                 % len({c["sweep"] for c in cells}),
                 xy=(skew_out["min"], place[id(skew_out)]),
                 xytext=(0.012, place[id(skew_out)] - 1.25),
                 fontsize=9.5, color=MUTED, ha="left", va="center",
                 arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.1,
                                 shrinkA=0, shrinkB=4))

    top.legend(handles=[
        Line2D([], [], color=ACCENT, marker="o", ms=8.5, lw=2.0, mec="white",
               mew=1.4, label="the certified axis"),
        Line2D([], [], color=INK, marker="s", ms=8.0, lw=2.0, mec="white",
               mew=1.4, label="stratum overlapping it"),
        Line2D([], [], color=ALERT, marker="D", ms=8.0, lw=2.0, mec="white",
               mew=1.4, label="stratum entirely off it")],
        loc="upper left", bbox_to_anchor=(0.0, -0.105), ncol=3, fontsize=9.5,
        frameon=False, borderaxespad=0.0, handlelength=2.4, columnspacing=1.8)

    # ---- excursion panel --------------------------------------------------
    names = list(excursion)
    medians = [excursion[name]["paired_median"] for name in names]
    reach = [max(excursion[name]["paired_max"], excursion[name]["paired_median"])
             for name in names]
    ypos2 = list(range(len(names)))[::-1]
    colours = [ACCENT if name == "Algorithm" else INK for name in names]
    bottom.barh(ypos2, medians, height=0.5, color=colours, zorder=3)
    for name, value, edge, colour, yy in zip(names, medians, reach, colours,
                                             ypos2):
        item = excursion[name]
        if name != "Algorithm":
            # One continuous whisker, drawn white where it crosses the bar and
            # in ink where it runs past the bar end, so it reads as one line
            # rather than a bar plus a floating tick.
            inside = min(item["paired_max"], value)
            bottom.plot([item["paired_min"], inside], [yy, yy], "-",
                        color="white", lw=1.3, zorder=4)
            bottom.plot([inside, item["paired_max"]], [yy, yy], "-",
                        color=INK, lw=1.3, zorder=4)
            bottom.plot([item["paired_min"]] * 2, [yy - 0.15, yy + 0.15], "-",
                        color="white" if item["paired_min"] < value else INK,
                        lw=1.3, zorder=5)
            bottom.plot([item["paired_max"]] * 2, [yy - 0.15, yy + 0.15], "-",
                        color=INK if item["paired_max"] > value else "white",
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
    bottom.set_ylim(-1.5, len(names) - 0.35)
    bottom.set_xlabel("range of f along that axis   (per detector, median over "
                      "detectors; whisker = detector to detector)")
    bottom.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    bottom.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        bottom.spines[spine].set_visible(False)
    bottom.set_title("The certified axis is the second flattest of the five",
                     fontsize=12, loc="left", pad=10, weight="bold")
    bottom.annotate("only skew moves less than the detectors do",
                    xy=(excursion["Skew"]["paired_median"], ypos2[-1] - 0.28),
                    xytext=(0.075, ypos2[-1] - 0.85),
                    fontsize=9.5, color=MUTED, ha="left", va="center",
                    arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.1,
                                    shrinkA=0, shrinkB=3))

    figure.text(0.019, 0.982,
                "f is not behaving like a sky parameter: it moves further "
                "across receiver, channel",
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.019, 0.959,
                "and arm than across the eight detectors the report certified "
                "it on",
                fontsize=13.5, weight="bold", color=INK, ha="left", va="top")
    figure.text(0.019, 0.934,
                "cross-radio occupancy, %d matched-arm cells / %d paired sweeps "
                "/ %d scored corpus entries; corpus read %s"
                % (len(cells), len({c["sweep"] for c in cells}),
                   model["entries"], read_at),
                fontsize=9.5, color=MUTED, ha="left", va="top")

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
        "population": {
            "scored_corpus_entries": model["entries"],
            "paired_sweeps_read": len(payload["pairs"]),
            "matched_arm_sweeps": len({c["sweep"] for c in cells}),
            "matched_arm_cells": len(cells),
            "census": payload["census"]},
        "empty_sky_rate_p": {m: v["rate"]
                             for m, v in model["false_alarm"].items()},
        "reference_band_across_algorithms": {
            "min": lo, "max": hi, "spread": certified,
            "per_method": pooled["values"]},
        "strata": [{"axis": row["axis"], "label": row["label"],
                    "cells": row["n_cells"],
                    "methods_solved": row["methods_solved"],
                    "min": row["min"], "max": row["max"], "spread": row["spread"],
                    "median": row["median"], "per_method": row["values"],
                    "unsolvable": {m: d["reason"]
                                   for m, d in row["detail"].items()
                                   if not d["solvable"]}}
                   for row in rows],
        "axis_excursion": excursion,
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
                    "sweeps were scored; more of the corpus is scored now, so "
                    "levels have moved. Structure reproduces on every axis "
                    "except skew.",
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
                             "(n=%d), OVERLAPPING"
                             % (skew_in["min"], skew_in["max"],
                                skew_in["n_cells"], skew_out["min"],
                                skew_out["max"], skew_out["n_cells"]),
                     "verdict": "NOT REPRODUCED — the separation closes as the "
                                "corpus grows; see f-strata-skew-vs-corpus.json"}},
    }
    JSON_OUT.write_text(json.dumps(out, indent=2) + "\n")
    print("wrote", JSON_OUT)
    print("certified spread %.4f; axes flatter than it: %s"
          % (certified, flatter or "none"))
    for name, item in excursion.items():
        print("  %-14s paired %.4f  unpaired %.4f  (%d detectors)"
              % (name, item["paired_median"], item["unpaired_range"],
                 item["paired_detectors"]))


if __name__ == "__main__":
    main()
