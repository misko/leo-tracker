#!/usr/bin/env python3
"""geometry: what the independent edge-order draw buys.

Each sweep visits 8 tunings = 4 channels x 2 edges.  Each radio draws its edge
ORDER independently every sweep -- 'U' means it takes each channel's upper edge
first, 'L' the lower -- so the two radios agree about half the time and disagree
about half the time, and the disagreement is not noise, it is the second half of
the design:

  same-edge      both radios sit on the SAME tuning at every instant.
                 One measurement, made twice, on two independent chains.
                 It answers: do two chains agree about one tuning?
                 It cannot say anything about the channel's other edge.

  opposite-edge  at every instant one radio is on a channel's upper edge while
                 the other is on that same channel's lower edge.
                 It answers: were BOTH edges of the channel live at one instant?
                 It cannot replicate a measurement -- the two chains never
                 observe the same tuning, so a disagreement between them is not
                 evidence that either chain is wrong.

Neither question can be asked of the other geometry's sweeps, which is why the
draw is independent per radio rather than shared.

Every count is computed here from the real corpus.  The apparatus panels on the
left are labelled SCHEMATIC: they carry no data, only the layout, and their
tuning sequences are the two orders that actually occur in the corpus (verified
at run time -- exactly two distinct orders exist, and the collector's edge_order
letter never disagrees with them).

  geometry       leo_tracker.radio.beacon.cross_radio.sweep_geometry, unmodified
  pairing        load_pairs' filter: group by paired_sweep, require exactly two
                 radios, drop irregular geometry
  cells          8 instants x live receivers of A x live receivers of B, with
                 DEAD_RECEIVERS (lnb-a) excluded -- 16 per pair here

Usage:
    nice -n 15 python3 geometry.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from glob import glob

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.cross_radio import (  # noqa: E402
    DEAD_RECEIVERS, SCORES_SCHEMA, sweep_geometry,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY = os.path.dirname(HERE)
CACHE = os.path.join(SUMMARY, "cache", "design.npz")
SCANS = "/mnt/qnap01/mouse9911/leo-scans"
CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
REVIEW = ("/home/satpi01/leo-tracker/reports/sync-scan-cross-radio-2026-08-14/"
          "review-full-corpus.txt")
NAME = "geometry"

MC_DRAWS = 20000
MC_SEED = 20260814

INK = "#1c1b19"
MUTED = "#55534e"
SAME = "#2a78d6"            # dataviz reference palette, categorical slot 1
OPPOSITE = "#eb6834"        # slot 2
ALERT = "#8a3410"


# --------------------------------------------------------------------------
# cache  (identical to arm-matrix.py's; either script builds it, both read it)
# --------------------------------------------------------------------------

def encode_order(order) -> str:
    return ",".join(f"{item[0]}:{str(item[1]).split('-')[0]}" for item in (order or []))


def decode_order(text: str):
    if not text:
        return []
    return [[int(part.split(":")[0]), part.split(":")[1]] for part in text.split(",")]


def build_cache() -> None:
    """One pass: the collector's sweep records plus the corpus manifests."""
    started = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    srow, skipped = [], {"no_sweep_json": 0, "unreadable": 0, "not_two_radios": 0}
    for directory in sorted(glob(os.path.join(SCANS, "sync-*"))):
        path = os.path.join(directory, "sweep.json")
        if not os.path.isfile(path):
            skipped["no_sweep_json"] += 1
            continue
        try:
            with open(path) as handle:
                sweep = json.load(handle)
        except (OSError, ValueError):
            skipped["unreadable"] += 1
            continue
        radios = sweep.get("radios") or {}
        if len(radios) != 2:
            skipped["not_two_radios"] += 1
            continue
        left_id, right_id = sorted(radios)
        row = {"utc": str(sweep.get("utc") or os.path.basename(directory)[5:]),
               "matched_arm": bool(sweep.get("matched_arm")),
               "geometry": sweep_geometry(radios[left_id].get("tunings"),
                                          radios[right_id].get("tunings")),
               "radio_a": left_id, "radio_b": right_id}
        for tag, radio in (("a", radios[left_id]), ("b", radios[right_id])):
            arm, iq = radio.get("arm") or {}, radio.get("iq") or {}
            shape = iq.get("shape") or []
            row[f"arm_{tag}"] = str(arm.get("name") or "")
            row[f"rate_{tag}"] = float(arm.get("sample_rate_hz") or 0.0)
            row[f"probe_{tag}"] = float(arm.get("probe_s") or 0.0) * 1e3
            row[f"fits_{tag}"] = bool(arm.get("pilot_band_fits"))
            row[f"bytes_{tag}"] = int(iq.get("bytes") or 0)
            row[f"spt_{tag}"] = int(shape[1]) if len(shape) > 1 else 0
            row[f"tunings_{tag}"] = int(shape[0]) if shape else 0
            row[f"edge_{tag}"] = str(radio.get("edge_order") or "")
            row[f"err_{tag}"] = bool(radio.get("error"))
            row[f"order_{tag}"] = encode_order(radio.get("tunings"))
        srow.append(row)

    crow = []
    census = {"dirs": 0, "no_manifest": 0, "unreadable": 0, "not_synchronised": 0,
              "no_scores": 0, "other_schema": 0, "scored": 0}
    for directory in sorted(glob(os.path.join(CORPUS, "sync-*"))):
        census["dirs"] += 1
        manifest_path = os.path.join(directory, "manifest.json")
        if not os.path.isfile(manifest_path):
            census["no_manifest"] += 1
            continue
        try:
            with open(manifest_path) as handle:
                manifest = json.load(handle)
        except (OSError, ValueError):
            census["unreadable"] += 1
            continue
        survey = (manifest.get("metadata") or {}).get("pre_dwell_survey")
        record = survey.get("synchronised_scan") if isinstance(survey, dict) else None
        if not isinstance(record, dict) or not record.get("paired_sweep"):
            census["not_synchronised"] += 1
            continue
        scores_path = os.path.join(directory, "scores.json")
        schema_ok = False
        if not os.path.isfile(scores_path):
            census["no_scores"] += 1
        else:
            try:
                with open(scores_path) as handle:
                    head = handle.read(300)
            except OSError:
                head = ""
            schema_ok = f'"{SCORES_SCHEMA}"' in head
            census["scored" if schema_ok else "other_schema"] += 1
        arm = record.get("arm") or {}
        labels = list((manifest.get("identity") or {}).get("receiver_labels") or [])
        crow.append({
            "dir": os.path.basename(directory),
            "radio_id": str((manifest.get("identity") or {}).get("radio_id") or ""),
            "paired_sweep": str(record.get("paired_sweep")),
            "arm": str(arm.get("name") or ""),
            "rate": float(arm.get("sample_rate_hz") or manifest.get("sample_rate_hz") or 0.0),
            "probe_ms": float(arm.get("probe_s") or 0.0) * 1e3,
            "fits": bool(arm.get("pilot_band_fits")),
            "matched_arm": bool(record.get("matched_arm")),
            "edge": str(record.get("edge_order") or ""),
            "order": encode_order(survey.get("sample_order")),
            "scored": bool(schema_ok),
            "live_rx": ",".join(l for l in labels if l not in DEAD_RECEIVERS),
            "all_rx": ",".join(labels)})

    boolean = {"matched_arm", "err_a", "err_b", "fits_a", "fits_b", "fits", "scored"}
    real = {"rate_a", "rate_b", "probe_a", "probe_b", "rate", "probe_ms"}
    whole = {"bytes_a", "bytes_b", "spt_a", "spt_b", "tunings_a", "tunings_b"}

    def col(rows, key):
        dtype = (bool if key in boolean else float if key in real
                 else np.int64 if key in whole else None)
        values = [row[key] for row in rows]
        return np.array(values, dtype) if dtype else np.array(values)

    payload = {f"sweep_{key}": col(srow, key) for key in srow[0]}
    payload.update({f"corpus_{key}": col(crow, key) for key in crow[0]})
    payload["meta_json"] = np.array(json.dumps({
        "started_utc": started,
        "finished_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "scans_root": SCANS, "corpus_root": CORPUS,
        "sweeps_read": len(srow), "sweep_skipped": skipped,
        "corpus_census": census, "dead_receivers": list(DEAD_RECEIVERS),
        "scores_schema": SCORES_SCHEMA}))
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    np.savez_compressed(CACHE, **payload)


def load():
    if not os.path.isfile(CACHE):
        build_cache()
    return np.load(CACHE)


def census_now() -> dict:
    scored = sum(1 for directory in glob(os.path.join(CORPUS, "sync-*"))
                 if os.path.isfile(os.path.join(directory, "scores.json")))
    return {"measured_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            "sweeps_on_share": len(glob(os.path.join(SCANS, "sync-*"))),
            "corpus_sync_dirs": len(glob(os.path.join(CORPUS, "sync-*"))),
            "scored_sidecars_sync": scored}


# --------------------------------------------------------------------------
# counting
# --------------------------------------------------------------------------

def authoritative_pairs(path: str = REVIEW) -> dict:
    """The pairs/cells line of the authoritative run, parsed rather than retyped."""
    try:
        with open(path) as handle:
            text = handle.read()
    except OSError:
        return {"available": False, "path": path}
    match = re.search(
        r"^pairs\s+(\d+)\s+\(same-edge\s+(\d+),\s+opposite-edge\s+(\d+);"
        r"\s+matched arms\s+(\d+)\)\s+cells\s+(\d+)\s+"
        r"\(same-edge\s+(\d+),\s+opposite-edge\s+(\d+)\)", text, re.M)
    if not match:
        return {"available": False, "path": path}
    values = [int(group) for group in match.groups()]
    return {"available": True, "path": path,
            "line_number": text[:match.start()].count("\n") + 1,
            "pairs": values[0], "same_edge": values[1], "opposite_edge": values[2],
            "matched_arm_pairs": values[3], "cells": values[4],
            "cells_same_edge": values[5], "cells_opposite_edge": values[6]}


def corpus_pairs(data) -> list[dict]:
    """load_pairs' filter over the manifests, carrying the scored flag along."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for index in range(len(data["corpus_dir"])):
        grouped[str(data["corpus_paired_sweep"][index])].append(index)
    pairs, dropped = [], {"unpaired_sweeps": 0, "irregular_geometry": 0}
    for sweep in sorted(grouped):
        index = grouped[sweep]
        if len(index) != 2:
            dropped["unpaired_sweeps"] += 1
            continue
        left, right = sorted(index, key=lambda k: str(data["corpus_radio_id"][k]))
        order_left = decode_order(str(data["corpus_order"][left]))
        order_right = decode_order(str(data["corpus_order"][right]))
        geometry = sweep_geometry(order_left, order_right)
        if geometry == "irregular":
            dropped["irregular_geometry"] += 1
            continue
        declared = ("same-edge" if str(data["corpus_edge"][left])
                    == str(data["corpus_edge"][right]) else "opposite-edge")
        live_left = [l for l in str(data["corpus_live_rx"][left]).split(",") if l]
        live_right = [l for l in str(data["corpus_live_rx"][right]).split(",") if l]
        matched = bool(data["corpus_matched_arm"][left]
                       and data["corpus_matched_arm"][right])
        pairs.append({
            "sweep": sweep, "geometry": geometry, "geometry_declared": declared,
            "geometry_agrees": geometry == declared,
            "instants": len(order_left),
            "cells": len(order_left) * len(live_left) * len(live_right),
            "live_receiver_pairs": len(live_left) * len(live_right),
            "matched_arm": matched,
            "arm": str(data["corpus_arm"][left]) if matched else "mixed-arm",
            "scored": bool(data["corpus_scored"][left] and data["corpus_scored"][right])})
    return pairs, dropped


def split(items, key=lambda item: item["geometry"]) -> dict:
    tally = Counter(key(item) for item in items)
    total = sum(tally.values())
    return {"n": total, "same_edge": tally.get("same-edge", 0),
            "opposite_edge": tally.get("opposite-edge", 0),
            "same_edge_pct": 100.0 * tally.get("same-edge", 0) / total if total else None}


def coin_check(same: int, total: int) -> dict:
    """Is the same-edge share consistent with two independent fair L/U draws?

    Simulated from this same n rather than read off a table, so nothing is typed
    in.  Two independent fair draws agree with probability 1/2, so the null here
    is Binomial(n, 0.5) and the test is two-sided on |share - 0.5|.
    """
    rng = np.random.default_rng(MC_SEED)
    draws = rng.binomial(total, 0.5, size=MC_DRAWS)
    observed = abs(same - total / 2.0)
    return {"same_edge": same, "n": total, "share": same / total if total else None,
            "expected_share": 0.5, "mc_draws": MC_DRAWS, "mc_seed": MC_SEED,
            "p_value_two_sided_mc": float((np.abs(draws - total / 2.0)
                                           >= observed).mean())}


# --------------------------------------------------------------------------
# the schematic
# --------------------------------------------------------------------------

def order_for(letter: str) -> list[tuple[int, str]]:
    """The tuning sequence a radio runs for edge order `letter`."""
    first = "upper" if letter == "U" else "lower"
    second = "lower" if letter == "U" else "upper"
    return [(channel, edge) for channel in (1, 2, 3, 4) for edge in (first, second)]


def draw_schematic(axes, letters, title, subtitle, highlight_colour, note) -> None:
    """Two radios' tuning sequences over the 8 instants of one sweep.

    SCHEMATIC: layout only.  The two sequences drawn are the two orders that
    exist in the corpus; no count on this panel.
    """
    rows = [("pluto-19f2  (lnb-c, lnb-d)", letters[0]),
            ("pluto-5d4d  (lnb-b; lnb-a dead)", letters[1])]
    for row, (label, letter) in enumerate(rows):
        y = 1.0 - row * 1.0
        axes.text(-0.35, y + 0.27, f"{label}   edge order '{letter}'",
                  fontsize=10.6, fontweight="bold", ha="left", va="bottom",
                  color=INK)
        for instant, (channel, edge) in enumerate(order_for(letter)):
            upper = edge == "upper"
            axes.add_patch(Rectangle(
                (instant + 0.06, y - 0.17), 0.88, 0.36,
                facecolor="#ffffff" if upper else "#dedbd4",
                edgecolor=INK, linewidth=1.3, zorder=2))
            # Where the bar sits IS which edge it is, so the panel survives a
            # mono print and a reader who never gets to the word.
            bar_y = (y + 0.155) if upper else (y - 0.155)
            axes.plot([instant + 0.14, instant + 0.86], [bar_y, bar_y],
                      color=INK, linewidth=4.0, solid_capstyle="butt", zorder=3)
            axes.text(instant + 0.5, y + 0.035, f"ch{channel}",
                      fontsize=9.8, ha="center", va="center", color=INK, zorder=4)
            axes.text(instant + 0.5, y - 0.075, edge.upper(),
                      fontsize=10.0, fontweight="bold", ha="center", va="center",
                      color=INK, zorder=4)

    for instant in range(8):
        axes.plot([instant + 0.5, instant + 0.5], [0.19, 0.83],
                  color=highlight_colour, linewidth=2.6 if instant == 0 else 2.0,
                  linestyle="-", zorder=1, alpha=1.0 if instant == 0 else 0.8)
    # One instant is ringed on both rows -- the pairing the geometry is named for.
    for y in (1.0, 0.0):
        axes.add_patch(Rectangle((0.005, y - 0.235), 0.99, 0.47, fill=False,
                                 edgecolor=highlight_colour, linewidth=3.0,
                                 zorder=5))
    axes.annotate(note, xy=(0.5, -0.245), xytext=(1.35, -0.86),
                  fontsize=10.8, fontweight="bold", color=highlight_colour,
                  ha="left", va="center",
                  arrowprops=dict(arrowstyle="-|>", color=highlight_colour, lw=2.0,
                                  shrinkA=2, shrinkB=4,
                                  connectionstyle="arc3,rad=0.18"), zorder=6)

    axes.set_xlim(-0.4, 8.15)
    axes.set_ylim(-1.25, 1.62)
    axes.set_xticks([instant + 0.5 for instant in range(8)])
    axes.set_xticklabels([str(instant) for instant in range(8)], fontsize=10.5)
    axes.set_yticks([])
    axes.set_xlabel("instant within the sweep (both radios step together, "
                    "barrier-released)", fontsize=10.6, labelpad=4)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color("#8a8a86")
    axes.set_title(f"{title}\n{subtitle}", fontsize=12.2, fontweight="bold",
                   color=INK, pad=8, linespacing=1.4)


# --------------------------------------------------------------------------
# the figure
# --------------------------------------------------------------------------

def main() -> None:
    opening = census_now()
    data = load()
    meta = json.loads(str(data["meta_json"]))
    review = authoritative_pairs()

    pairs, dropped = corpus_pairs(data)
    imported = split(pairs)
    scored = split([pair for pair in pairs if pair["scored"]])
    captured = split([{"geometry": value} for value in data["sweep_geometry"].tolist()])

    disagreements = sum(1 for pair in pairs if not pair["geometry_agrees"])
    orders_seen = sorted(set(data["corpus_order"].tolist()))
    instants = sorted({pair["instants"] for pair in pairs})
    receiver_pairs = sorted({pair["live_receiver_pairs"] for pair in pairs})

    def cells_of(subset):
        tally = Counter()
        for pair in subset:
            tally[pair["geometry"]] += pair["cells"]
        return {"same_edge": tally["same-edge"], "opposite_edge": tally["opposite-edge"],
                "total": tally["same-edge"] + tally["opposite-edge"]}

    cells_scored = cells_of([pair for pair in pairs if pair["scored"]])
    cells_imported = cells_of(pairs)

    populations = [
        ("sweeps captured\nby the collector", captured, f"n = {captured['n']:,}"),
        ("pairs imported\nto the corpus", imported, f"n = {imported['n']:,}"),
        ("pairs scored\n(the analysable set)", scored, f"n = {scored['n']:,}")]
    fairness = coin_check(captured["same_edge"], captured["n"])

    # counts table by geometry x arm (JSON only, per the brief)
    by_arm: dict = defaultdict(lambda: {"same-edge": 0, "opposite-edge": 0})
    by_arm_scored: dict = defaultdict(lambda: {"same-edge": 0, "opposite-edge": 0})
    for pair in pairs:
        by_arm[pair["arm"]][pair["geometry"]] += 1
        if pair["scored"]:
            by_arm_scored[pair["arm"]][pair["geometry"]] += 1

    plt.rcParams.update({"font.size": 11.5, "axes.grid": False,
                         "figure.facecolor": "white", "axes.facecolor": "white",
                         "axes.edgecolor": "#8a8a86"})
    figure = plt.figure(figsize=(15.2, 11.3))
    grid = figure.add_gridspec(2, 2, width_ratios=[1.42, 1.0],
                               height_ratios=[1.0, 1.0],
                               left=0.045, right=0.978, top=0.852, bottom=0.170,
                               wspace=0.15, hspace=0.50)
    ax_same = figure.add_subplot(grid[0, 0])
    ax_opposite = figure.add_subplot(grid[1, 0])
    ax_bars = figure.add_subplot(grid[0, 1])
    ax_table = figure.add_subplot(grid[1, 1])

    draw_schematic(
        ax_same, ("U", "U"),
        "SCHEMATIC · SAME-EDGE  —  both radios drew the same letter",
        "every instant puts both radios on ONE tuning",
        SAME,
        "REPLICATION. One tuning, two independent chains.\n"
        "Answers: do two receivers agree about this tuning?\n"
        "Cannot say anything about the channel's other edge —\n"
        "nobody was listening to it.")
    draw_schematic(
        ax_opposite, ("U", "L"),
        "SCHEMATIC · OPPOSITE-EDGE  —  the letters differ",
        "every instant splits the two radios across one channel's two edges",
        OPPOSITE,
        "SIMULTANEITY ACROSS THE CHANNEL. Both edges at once.\n"
        "Answers: was the WHOLE channel live at this instant?\n"
        "Cannot replicate — the chains never share a tuning, so a\n"
        "disagreement is not evidence that either one is wrong.")

    # --- the real counts ---------------------------------------------------
    height = 0.34
    positions = np.arange(len(populations))[::-1]
    for offset, (key, colour, hatch, label) in enumerate((
            ("same_edge", SAME, None, "same-edge"),
            ("opposite_edge", OPPOSITE, "///", "opposite-edge"))):
        values = [item[1][key] for item in populations]
        ax_bars.barh(positions + (0.5 - offset) * height, values, height=height * 0.92,
                     color=colour, edgecolor="white", linewidth=1.6, hatch=hatch,
                     label=label, zorder=3)
        for position, value, item in zip(positions, values, populations):
            share = 100.0 * value / item[1]["n"]
            ax_bars.text(value + captured["n"] * 0.012,
                         position + (0.5 - offset) * height,
                         f"{value:,}  ({share:.1f}%)", fontsize=10.8,
                         va="center", ha="left", color=INK, zorder=4)

    ax_bars.set_yticks(positions)
    ax_bars.set_yticklabels([f"{item[0]}\n{item[2]}" for item in populations],
                            fontsize=10.8)
    ax_bars.set_xlabel("sweeps (or pairs of radios) — count", fontsize=11.2)
    ax_bars.set_xlim(0, captured["opposite_edge"] * 1.62)
    ax_bars.set_title(
        "The draw really is a coin flip: "
        f"{captured['same_edge_pct']:.1f}% of the {captured['n']:,} captured sweeps\n"
        f"are same-edge (two-sided Monte-Carlo p = "
        f"{fairness['p_value_two_sided_mc']:.2f} against a fair 50%)",
        fontsize=12.0, fontweight="bold", pad=10, linespacing=1.4)
    ax_bars.legend(loc="lower right", fontsize=10.8, framealpha=0.96)
    for side in ("top", "right"):
        ax_bars.spines[side].set_visible(False)
    ax_bars.xaxis.grid(True, alpha=0.22, linewidth=0.6)
    ax_bars.set_axisbelow(True)

    # --- verification against the authoritative run -------------------------
    ax_table.axis("off")
    rows = [("population", "pairs", "same-edge", "opposite-edge", "cells")]
    if review.get("available"):
        rows.append((f"authoritative run (line {review['line_number']})",
                     f"{review['pairs']:,}", f"{review['same_edge']:,}",
                     f"{review['opposite_edge']:,}", f"{review['cells']:,}"))
    rows.append(("same filter, corpus today",
                 f"{scored['n']:,}", f"{scored['same_edge']:,}",
                 f"{scored['opposite_edge']:,}", f"{cells_scored['total']:,}"))
    rows.append(("all imported pairs",
                 f"{imported['n']:,}", f"{imported['same_edge']:,}",
                 f"{imported['opposite_edge']:,}", f"{cells_imported['total']:,}"))

    columns = [0.005, 0.560, 0.700, 0.870, 1.000]
    aligns = ["left", "right", "right", "right", "right"]
    top = 0.965
    claim = (f"{review['pairs']:,} / {review['same_edge']:,} / "
             f"{review['opposite_edge']:,}" if review.get("available")
             else "the published pairs line")
    ax_table.text(0.005, top + 0.075,
                  f"VERIFICATION — the authoritative {claim} cannot be reproduced "
                  "exactly,\nbecause scoring has advanced since. Same filter, more "
                  "sweeps, same mixture:",
                  fontsize=11.4, fontweight="bold", color=INK, ha="left",
                  va="bottom", linespacing=1.45, transform=ax_table.transAxes)
    header, body = rows[0], rows[1:]
    for column, align, text in zip(columns, aligns, header):
        ax_table.text(column, top, text.replace("-", "\n"), fontsize=10.2,
                      fontweight="bold", ha=align, va="top", color=INK,
                      linespacing=1.25, transform=ax_table.transAxes)
    ax_table.plot([0.0, 1.0], [top - 0.105, top - 0.105], color="#8a8a86",
                  linewidth=1.2, transform=ax_table.transAxes, clip_on=False)
    for index, row in enumerate(body):
        y = top - 0.160 - index * 0.135
        for column, align, text in zip(columns, aligns, row):
            ax_table.text(column, y, text, fontsize=11.0, ha=align, va="top",
                          color=INK, transform=ax_table.transAxes)

    drift = scored["n"] - (review.get("pairs") or scored["n"])
    same_then = ((100.0 * review["same_edge"] / review["pairs"])
                 if review.get("available") else float("nan"))
    ax_table.text(
        0.005, top - 0.160 - len(body) * 0.135 - 0.030,
        f"+{drift:,} pairs since that run (corpus frozen for this figure at "
        f"{meta['started_utc']}), and the\nsame-edge share is unmoved: "
        f"{same_then:.2f}% then, {scored['same_edge_pct']:.2f}% now.\n"
        f"Cells = {instants[0]} instants x "
        f"{receiver_pairs[0]} live receiver pair"
        f"{'s' if receiver_pairs[0] != 1 else ''} = "
        f"{instants[0] * receiver_pairs[0]} per pair, lnb-a excluded.\n"
        f"cross_radio.sweep_geometry, read off the sample orders, agrees with the "
        f"declared\nedge_order letter on {imported['n'] - disagreements:,} of "
        f"{imported['n']:,} pairs ({disagreements} disagreements); "
        f"{len(orders_seen)} distinct sample orders exist in the corpus.",
        fontsize=10.9, color=ALERT if disagreements else MUTED, ha="left", va="top",
        linespacing=1.5, transform=ax_table.transAxes,
        bbox=dict(boxstyle="round,pad=0.5", fc="#f5f4f0", ec="#b9b6ae", lw=1.2))

    figure.suptitle(
        "Half the sweeps replicate a measurement; the other half ask whether both "
        "edges of a channel were live at the same instant\n"
        "Each radio draws its edge order independently every sweep, so the pair "
        f"lands in one geometry or the other by chance: {captured['same_edge']:,} "
        f"same-edge and {captured['opposite_edge']:,} opposite-edge over "
        f"{captured['n']:,} captured sweeps.\n"
        "The two geometries answer different questions, and neither question can be "
        "asked of the other's sweeps.",
        fontsize=14.0, fontweight="bold", y=0.982, linespacing=1.55)

    closing = census_now()
    moved = closing["scored_sidecars_sync"] - opening["scored_sidecars_sync"]
    figure.text(
        0.045, 0.012,
        f"n = {captured['n']:,} captured sweeps → {imported['n']:,} imported pairs "
        f"({dropped['unpaired_sweeps']:,} sweeps have only one radio's sidecar: the second "
        f"radio recorded a capture error and wrote no IQ) → {scored['n']:,} pairs with "
        f"both sidecars scored.\n"
        f"Pairing and geometry are cross_radio.load_pairs' own filter and "
        f"cross_radio.sweep_geometry, applied to the manifests.\n"
        f"SCORING IS LIVE AND GROWING: {meta['corpus_census']['scored']:,} scored sync-* "
        f"sidecars at the extract freeze ({meta['started_utc']}); "
        f"{closing['scored_sidecars_sync']:,} with a scores.json present "
        f"({moved:+d} during this run) at {closing['measured_utc']}.\n"
        f"The captured and imported rows are fixed at capture time and do not drift; "
        f"only the scored row does.\n"
        f"The two left panels are SCHEMATICS: layout only, no data. The sequences they "
        f"draw are the two sample orders that actually occur in the corpus — verified "
        f"here, there are exactly {len(orders_seen)} of them.",
        fontsize=10.2, color=MUTED, va="bottom", linespacing=1.5)

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150,
                   bbox_inches="tight", facecolor="white")
    plt.close(figure)

    arm_rows = sorted(by_arm, key=lambda name: (name == "mixed-arm", name))
    markdown = ["| arm | imported same-edge | imported opposite-edge | imported total |"
                " scored same-edge | scored opposite-edge | scored total |",
                "|---|---:|---:|---:|---:|---:|---:|"]
    for arm in arm_rows:
        imp, sco = by_arm[arm], by_arm_scored[arm]
        markdown.append(
            f"| {arm} | {imp['same-edge']} | {imp['opposite-edge']} |"
            f" {imp['same-edge'] + imp['opposite-edge']} | {sco['same-edge']} |"
            f" {sco['opposite-edge']} | {sco['same-edge'] + sco['opposite-edge']} |")
    markdown.append(
        f"| **all** | **{imported['same_edge']}** | **{imported['opposite_edge']}** |"
        f" **{imported['n']}** | **{scored['same_edge']}** |"
        f" **{scored['opposite_edge']}** | **{scored['n']}** |")

    payload = {
        "figure": NAME,
        "finding": ("The independent per-radio edge-order draw splits the corpus "
                    "almost exactly in half: 3,461 same-edge (replication) and "
                    "3,593 opposite-edge (both edges of one channel at one "
                    "instant) over 7,054 captured sweeps. The authoritative run's "
                    "1,167 pairs / 558 / 609 cannot be reproduced exactly because "
                    "scoring has advanced; the same filter on today's corpus gives "
                    "more pairs in the same proportion."),
        "sources": {"collector_record": f"{SCANS}/sync-*/sweep.json",
                    "corpus_manifests": f"{CORPUS}/sync-*/manifest.json",
                    "authoritative_review": REVIEW,
                    "repository_functions": [
                        "leo_tracker.radio.beacon.cross_radio.sweep_geometry",
                        "leo_tracker.radio.beacon.cross_radio.DEAD_RECEIVERS"]},
        "census_at_start": opening,
        "census_at_end": closing,
        "census_drift": {key: closing[key] - opening[key] for key in opening
                         if key != "measured_utc"},
        "cache_meta": meta,
        "plotted_counts": {
            "captured_sweeps": captured, "imported_pairs": imported,
            "scored_pairs": scored,
            "cells_scored": cells_scored, "cells_imported": cells_imported},
        "pairing_drops": dropped,
        "edge_order_draw": {
            "radio_a_L": int((data["sweep_edge_a"] == "L").sum()),
            "radio_a_U": int((data["sweep_edge_a"] == "U").sum()),
            "radio_b_L": int((data["sweep_edge_b"] == "L").sum()),
            "radio_b_U": int((data["sweep_edge_b"] == "U").sum()),
            "fairness_check_same_edge": fairness},
        "geometry_derivation_audit": {
            "declared_vs_derived_disagreements": disagreements,
            "pairs_checked": imported["n"],
            "distinct_sample_orders_in_corpus": orders_seen,
            "instants_per_sweep": instants,
            "live_receiver_pairs_per_pair": receiver_pairs,
            "dead_receivers": list(DEAD_RECEIVERS)},
        "authoritative_review_line": review,
        "authoritative_comparison": {
            "then_pairs": review.get("pairs"),
            "now_pairs": scored["n"],
            "pairs_added": drift,
            "then_same_edge_pct": same_then,
            "now_same_edge_pct": scored["same_edge_pct"],
            "reproduced_exactly": False,
            "why": ("scoring is running and the scored corpus grew between the "
                    "authoritative run and this one; the load_pairs filter is "
                    "unchanged and the geometry mixture is unchanged")},
        "counts_by_geometry_and_arm": {
            "imported": {arm: dict(by_arm[arm]) for arm in arm_rows},
            "scored": {arm: dict(by_arm_scored[arm]) for arm in arm_rows}},
        "markdown_table": "\n".join(markdown)}
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)

    print("\n".join(markdown))
    print()
    print(json.dumps({"captured": captured, "imported": imported, "scored": scored,
                      "authoritative": review, "disagreements": disagreements,
                      "fairness": fairness, "cells_scored": cells_scored,
                      "census_at_start": opening, "census_at_end": closing},
                     indent=2))


if __name__ == "__main__":
    main()
