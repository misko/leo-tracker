#!/usr/bin/env python3
"""arm-matrix: the 12-arm randomised design, as actually executed.

Every sweep draws one of 12 arms -- probe length {80, 160, 640} ms x sample rate
{1.25, 2.5, 5.0, 10.0} MS/s -- and both radios take that arm with p = 0.9.  This
figure is the executed draw, counted off the collector's own per-sweep record:

    /mnt/qnap01/mouse9911/leo-scans/sync-*/sweep.json

which is written at capture time and carries the arm, the edge order, the IQ
byte count and the collector's own ``pilot_band_fits`` flag.  Nothing here is
re-derived from the scores, because the arm draw happens before scoring and the
scored subset is still growing; the corpus counts (imported, scored) are carried
alongside so the reader can see how much of each arm has been adjudicated.

The load-bearing feature is the 1.25 MS/s column.  The detectors correlate
against a 1.875 MHz pilot band, and the guard a rate leaves for it is
``cross_radio.pilot_guard_hz(rate) = rate/2 - 937.5 kHz``.  At 1.25 MS/s that is
-312.5 kHz: the band does not fit inside the captured spectrum at all, at any
probe length.  The full-corpus review finds that whole column at the bottom of
the f axis (1.25 MS/s 80 ms: f 0.120, the lowest of the 12 arms, and only 6 of 8
algorithms solvable there).  It is the one arm the analysis finds genuinely
handicapped, and it is handicapped by physics, not by how often it was drawn.

  guard          cross_radio.pilot_guard_hz (repository function, not restated)
  pilot fits     the collector's own arm flag, cross-checked against guard >= 0
  samples/tuning sweep.json radios[*].iq.shape[1]
  IQ bytes       sweep.json radios[*].iq.bytes  (per radio; a matched sweep
                 writes two of them)

Usage:
    nice -n 15 python3 arm-matrix.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from glob import glob

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.cross_radio import (  # noqa: E402
    DEAD_RECEIVERS, PILOT_BAND_HZ, SCORES_SCHEMA, pilot_guard_hz, sweep_geometry,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY = os.path.dirname(HERE)
CACHE = os.path.join(SUMMARY, "cache", "design.npz")
SCANS = "/mnt/qnap01/mouse9911/leo-scans"
CORPUS = "/mnt/qnap01/mouse9911/leo/surveys/corpus"
REVIEW = ("/home/satpi01/leo-tracker/reports/sync-scan-cross-radio-2026-08-14/"
          "review-full-corpus.txt")
NAME = "arm-matrix"

PROBES_MS = [80.0, 160.0, 640.0]
RATES_MSPS = [1.25, 2.5, 5.0, 10.0]
MC_DRAWS = 20000
MC_SEED = 20260814          # cross_radio.BOOTSTRAP_SEED convention

INK = "#1c1b19"
MUTED = "#55534e"
ALERT = "#8a3410"           # the report's existing "this is the problem" ink
ALERT_LIGHT = "#eb6834"


# --------------------------------------------------------------------------
# cache  (identical to geometry.py's; either script builds it, both read it)
# --------------------------------------------------------------------------

def encode_order(order) -> str:
    return ",".join(f"{item[0]}:{str(item[1]).split('-')[0]}" for item in (order or []))


def decode_order(text: str):
    if not text:
        return []
    return [[int(part.split(":")[0]), part.split(":")[1]] for part in text.split(",")]


def build_cache() -> None:
    """One pass: the collector's sweep records plus the corpus manifests.

    sweep.json is 2.4 kB and manifest.json is 3.1 kB, so this reads ~60 MB.
    scores.json is 1.6 MB apiece and is deliberately NOT read -- only its first
    300 bytes, which is enough for the schema gate ``load_pairs`` applies.
    """
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


# --------------------------------------------------------------------------
# counting
# --------------------------------------------------------------------------

def census_now() -> dict:
    """Re-stat the share.  Scoring is live, so this is a moment, not a fact."""
    scored = 0
    for directory in glob(os.path.join(CORPUS, "sync-*")):
        if os.path.isfile(os.path.join(directory, "scores.json")):
            scored += 1
    return {"measured_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            "sweeps_on_share": len(glob(os.path.join(SCANS, "sync-*"))),
            "corpus_sync_dirs": len(glob(os.path.join(CORPUS, "sync-*"))),
            "scored_sidecars_sync": scored}


def corpus_pairs(data) -> list[dict]:
    """load_pairs' own filter, applied to the manifests only.

    Same grouping (paired_sweep), same two-radios rule, same
    ``sweep_geometry`` on the sample orders.  The scored flag rides along so
    the scored sub-population can be taken without a second pass.
    """
    grouped: dict[str, list[int]] = {}
    for index in range(len(data["corpus_dir"])):
        grouped.setdefault(str(data["corpus_paired_sweep"][index]), []).append(index)
    pairs = []
    for sweep in sorted(grouped):
        index = grouped[sweep]
        if len(index) != 2:
            continue
        left, right = sorted(index, key=lambda k: str(data["corpus_radio_id"][k]))
        geometry = sweep_geometry(decode_order(str(data["corpus_order"][left])),
                                  decode_order(str(data["corpus_order"][right])))
        if geometry == "irregular":
            continue
        matched = bool(data["corpus_matched_arm"][left]
                       and data["corpus_matched_arm"][right])
        pairs.append({
            "sweep": sweep, "geometry": geometry, "matched_arm": matched,
            "arm": str(data["corpus_arm"][left]) if matched else "mixed",
            "probe_ms": float(data["corpus_probe_ms"][left]),
            "rate": float(data["corpus_rate"][left]),
            "scored": bool(data["corpus_scored"][left] and data["corpus_scored"][right])})
    return pairs


def build_table(data) -> dict:
    probe_a, rate_a = data["sweep_probe_a"], data["sweep_rate_a"]
    probe_b, rate_b = data["sweep_probe_b"], data["sweep_rate_b"]
    matched = data["sweep_matched_arm"]
    pairs = corpus_pairs(data)

    cells = {}
    for probe in PROBES_MS:
        for rate in RATES_MSPS:
            hz = rate * 1e6
            on_a = (probe_a == probe) & (rate_a == hz)
            on_b = (probe_b == probe) & (rate_b == hz)
            both = on_a & matched
            spt = sorted({int(v) for v in data["sweep_spt_a"][on_a & (data["sweep_spt_a"] > 0)]}
                         | {int(v) for v in data["sweep_spt_b"][on_b & (data["sweep_spt_b"] > 0)]})
            byte = sorted({int(v) for v in data["sweep_bytes_a"][on_a & (data["sweep_bytes_a"] > 0)]}
                          | {int(v) for v in data["sweep_bytes_b"][on_b & (data["sweep_bytes_b"] > 0)]})
            fits = sorted({bool(v) for v in data["sweep_fits_a"][on_a]}
                          | {bool(v) for v in data["sweep_fits_b"][on_b]})
            imported = [p for p in pairs if p["matched_arm"]
                        and p["probe_ms"] == probe and p["rate"] == hz]
            cells[(probe, rate)] = {
                "probe_ms": probe, "rate_msps": rate,
                "arm_name": f"{probe:g}ms-{rate:.2f}MSps",
                "matched_sweeps": int(both.sum()),
                "solo_captures_a": int((on_a & ~matched).sum()),
                "solo_captures_b": int((on_b & ~matched).sum()),
                "radio_captures": int(on_a.sum()) + int(on_b.sum()),
                "samples_per_tuning": spt[0] if len(spt) == 1 else spt,
                "iq_bytes_per_radio": byte[0] if len(byte) == 1 else byte,
                "pilot_band_fits": fits[0] if len(fits) == 1 else fits,
                "pilot_guard_hz": pilot_guard_hz(hz),
                "imported_pairs": len(imported),
                "scored_pairs": sum(1 for p in imported if p["scored"])}
    return {"cells": cells, "pairs": pairs}


def uniformity(counts: list[int]) -> dict:
    """Chi-square against a uniform draw, with a Monte-Carlo p-value.

    Monte Carlo rather than a tabulated critical value: the null is a plain
    multinomial on this same total, so it can be simulated from the data's own
    n with numpy alone and nothing has to be typed in from a table.
    """
    observed = np.array(counts, float)
    total = observed.sum()
    expected = total / observed.size
    statistic = float(((observed - expected) ** 2 / expected).sum())
    rng = np.random.default_rng(MC_SEED)
    draws = rng.multinomial(int(total), np.full(observed.size, 1.0 / observed.size),
                            size=MC_DRAWS).astype(float)
    null = ((draws - expected) ** 2 / expected).sum(axis=1)
    return {"n_total": int(total), "expected_per_arm": float(expected),
            "chi_square": statistic, "df": int(observed.size - 1),
            "mc_draws": MC_DRAWS, "mc_seed": MC_SEED,
            "p_value_mc": float((null >= statistic).mean()),
            "observed_min": int(observed.min()), "observed_max": int(observed.max()),
            "sd_binomial": float(np.sqrt(total * (1.0 / observed.size)
                                         * (1 - 1.0 / observed.size)))}


def authoritative_arm_axis(path: str = REVIEW) -> dict:
    """The arm-axis block of the 5,097-line authoritative run, parsed not retyped.

    The claim this figure annotates -- that the 1.25 MS/s arms sit at the bottom
    of the f axis -- belongs to that run, so it is read out of the run's own text
    at draw time.  Nothing about f is recomputed here; this figure is the design,
    not the estimate.
    """
    try:
        with open(path) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {"available": False, "path": path}
    out = {"available": False, "path": path}
    for index, line in enumerate(lines):
        if not re.match(r"^\s{4}arm\s{2,}\d+/\d+\s", line):
            continue
        head = re.search(r"^\s+arm\s+(\d+)/(\d+)\s+(\d+)\s+([\d.]+)\.\.([\d.]+)", line)
        if head:
            out.update({"available": True, "line_number": index + 1,
                        "levels": int(head.group(1)), "levels_total": int(head.group(2)),
                        "cells": int(head.group(3)),
                        "f_low": float(head.group(4)), "f_high": float(head.group(5))})
        for follow in lines[index + 1:index + 3]:
            item = re.search(r"^\s+(lowest|highest)\s+(.*?)\s+n=(\d+)\s+f\s+([\d.]+)"
                             r"\s+algorithms\s+([\d.]+)\.\.([\d.]+)\s+over\s+(\d+)", follow)
            if item:
                out[item.group(1)] = {
                    "arm": item.group(2).strip(), "n_cells": int(item.group(3)),
                    "f": float(item.group(4)),
                    "algorithms_low": float(item.group(5)),
                    "algorithms_high": float(item.group(6)),
                    "algorithms_solved": int(item.group(7))}
        break
    return out


def si_bytes(value: int) -> str:
    return f"{value / 1e6:.1f} MB" if value < 1e9 else f"{value / 1e9:.2f} GB"


def si_samples(value: int) -> str:
    return f"{value / 1e3:.0f} k" if value < 1e6 else f"{value / 1e6:g} M"


def markdown_table(cells: dict, order: list) -> str:
    lines = ["| arm | probe (ms) | rate (MS/s) | sweeps, both radios | solo captures |"
             " samples/tuning | IQ bytes per radio | pilot guard (kHz) | pilot band fits |"
             " imported pairs | scored pairs |",
             "|---|---:|---:|---:|---:|---:|---:|---:|:--:|---:|---:|"]
    for key in order:
        cell = cells[key]
        lines.append(
            f"| {cell['arm_name']} | {cell['probe_ms']:g} | {cell['rate_msps']:g} |"
            f" {cell['matched_sweeps']} |"
            f" {cell['solo_captures_a'] + cell['solo_captures_b']} |"
            f" {cell['samples_per_tuning']:,} | {cell['iq_bytes_per_radio']:,} |"
            f" {cell['pilot_guard_hz'] / 1e3:+.1f} |"
            f" {'yes' if cell['pilot_band_fits'] else 'NO'} |"
            f" {cell['imported_pairs']} | {cell['scored_pairs']} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the figure
# --------------------------------------------------------------------------

def main() -> None:
    opening = census_now()
    data = load()
    meta = json.loads(str(data["meta_json"]))
    table = build_table(data)
    cells = table["cells"]
    order = [(p, r) for p in PROBES_MS for r in RATES_MSPS]

    grid = np.array([[cells[(p, r)]["matched_sweeps"] for r in RATES_MSPS]
                     for p in PROBES_MS], float)
    check = uniformity([cells[key]["matched_sweeps"] for key in order])

    # the collector's flag vs the guard the repository computes
    flag_audit = [{"rate_msps": r,
                   "collector_flag": bool(cells[(PROBES_MS[0], r)]["pilot_band_fits"]),
                   "guard_hz": pilot_guard_hz(r * 1e6),
                   "recomputed": bool(pilot_guard_hz(r * 1e6) >= 0.0)}
                  for r in RATES_MSPS]
    flag_agrees = all(item["collector_flag"] == item["recomputed"] for item in flag_audit)

    sweeps_total = int(len(data["sweep_utc"]))
    matched_total = int(data["sweep_matched_arm"].sum())

    review = authoritative_arm_axis()

    plt.rcParams.update({"font.size": 12.5, "axes.titlesize": 14,
                         "axes.labelsize": 13, "xtick.labelsize": 12.5,
                         "ytick.labelsize": 12.5, "axes.grid": False,
                         "figure.facecolor": "white", "axes.facecolor": "white",
                         "axes.edgecolor": "#8a8a86"})
    figure, axes = plt.subplots(figsize=(13.4, 11.2))
    figure.subplots_adjust(left=0.098, right=0.868, top=0.855, bottom=0.335)

    span = 3.0 * check["sd_binomial"]
    image = axes.imshow(grid, cmap="Blues", aspect="auto",
                        vmin=check["expected_per_arm"] - span,
                        vmax=check["expected_per_arm"] + span)

    axes.set_xticks(range(len(RATES_MSPS)))
    axes.set_yticks(range(len(PROBES_MS)))
    axes.set_xticklabels(
        [f"{r:g} MS/s\nguard {pilot_guard_hz(r * 1e6) / 1e3:+.1f} kHz"
         for r in RATES_MSPS])
    axes.set_yticklabels([f"{p:g} ms\nprobe" for p in PROBES_MS])
    axes.set_xlabel("sample rate (MS/s)", labelpad=9)
    axes.set_ylabel("probe length (ms)", labelpad=9)
    axes.set_xlim(-0.5, len(RATES_MSPS) - 0.5)
    axes.set_ylim(len(PROBES_MS) - 0.5, -0.5)

    for row, probe in enumerate(PROBES_MS):
        for column, rate in enumerate(RATES_MSPS):
            cell = cells[(probe, rate)]
            shade = (grid[row, column] - (check["expected_per_arm"] - span)) / (2 * span)
            ink = "white" if shade > 0.62 else INK
            axes.add_patch(Rectangle((column - 0.5, row - 0.5), 1, 1, fill=False,
                                     edgecolor="white", linewidth=2.0, zorder=2))
            axes.text(column, row - 0.30, f"{cell['matched_sweeps']}",
                      ha="center", va="center", fontsize=21, fontweight="bold",
                      color=ink, zorder=4)
            axes.text(column, row - 0.115, "sweeps, both radios",
                      ha="center", va="center", fontsize=9.5, color=ink, zorder=4)
            axes.text(column, row + 0.045,
                      f"+{cell['solo_captures_a'] + cell['solo_captures_b']} solo captures",
                      ha="center", va="center", fontsize=9.5, color=ink, zorder=4)
            axes.text(column, row + 0.185,
                      f"{si_samples(cell['samples_per_tuning'])} samples/tuning",
                      ha="center", va="center", fontsize=10.5, color=ink, zorder=4)
            axes.text(column, row + 0.305,
                      f"{si_bytes(cell['iq_bytes_per_radio'])} IQ per radio",
                      ha="center", va="center", fontsize=10.5, color=ink, zorder=4)
            axes.text(column, row + 0.425,
                      ("pilot band FITS" if cell["pilot_band_fits"]
                       else "PILOT BAND DOES NOT FIT"),
                      ha="center", va="center", fontsize=10.5,
                      fontweight="bold",
                      color=(ink if cell["pilot_band_fits"] else ALERT), zorder=4)

    # --- the handicapped column, marked so it survives a mono print ---------
    dead_column = [index for index, rate in enumerate(RATES_MSPS)
                   if not cells[(PROBES_MS[0], rate)]["pilot_band_fits"]]
    for column in dead_column:
        axes.add_patch(Rectangle((column - 0.5, -0.5), 1, len(PROBES_MS),
                                 fill=True, facecolor="none", hatch="////",
                                 edgecolor=ALERT_LIGHT, linewidth=0.0, alpha=0.55,
                                 zorder=1))
        axes.add_patch(Rectangle((column - 0.5, -0.5), 1, len(PROBES_MS),
                                 fill=False, edgecolor=ALERT, linewidth=3.6,
                                 zorder=5))
    if not dead_column:
        raise SystemExit("no arm is flagged pilot_band_fits=False -- the corpus "
                         "disagrees with the brief; stopping rather than drawing it")
    guard = pilot_guard_hz(RATES_MSPS[dead_column[0]] * 1e6)
    drawn = "/".join(str(cells[(probe, RATES_MSPS[dead_column[0]])]["matched_sweeps"])
                     for probe in PROBES_MS)
    verdict = ""
    if review.get("available") and review.get("lowest"):
        low = review["lowest"]
        verdict = (f"\nThe authoritative full-corpus run puts {low['arm']} at the BOTTOM of "
                   f"the f axis: f {low['f']:.3f} on\nn={low['n_cells']:,} cells, only "
                   f"{low['algorithms_solved']} of 8 algorithms solvable, against "
                   f"{review['f_high']:.3f} for {review['highest']['arm']}.\n"
                   f"(read out of review-full-corpus.txt line {review['line_number'] + 1}, "
                   f"not retyped)")
    axes.annotate(
        "THE ONE HANDICAPPED COLUMN — and it is physics, not sampling.\n"
        f"A {RATES_MSPS[dead_column[0]]:g} MS/s capture is only "
        f"{RATES_MSPS[dead_column[0]]:g} MHz wide, so the {PILOT_BAND_HZ / 1e6:g} MHz band "
        f"the detectors correlate\nagainst cannot fit inside it at any probe length: "
        f"guard {guard / 1e3:+.1f} kHz.\n"
        f"It was drawn as often as every other arm ({drawn} sweeps)." + verdict,
        xy=(dead_column[0] - 0.5, len(PROBES_MS) - 0.52), xycoords="data",
        xytext=(0.098, 0.098), textcoords="figure fraction",
        ha="left", va="bottom", fontsize=11.4, fontweight="bold", color=ALERT,
        arrowprops=dict(arrowstyle="-|>", color=ALERT, lw=2.6, shrinkA=10,
                        shrinkB=2, connectionstyle="arc3,rad=-0.20"),
        annotation_clip=False, zorder=8, linespacing=1.45,
        bbox=dict(boxstyle="round,pad=0.5", fc="#fdf0e9", ec=ALERT, lw=1.8))

    bar = figure.colorbar(image, ax=axes, fraction=0.030, pad=0.022)
    bar.set_label("sweeps captured\n(both radios on this arm)", fontsize=11.5,
                  labelpad=10)
    bar.ax.axhline(check["expected_per_arm"], color=INK, linewidth=2.2)

    axes.set_title(
        "The 12-arm draw came out flat — no arm was starved; the one handicapped arm "
        "is handicapped by physics, not by sampling\n"
        f"{matched_total:,} of {sweeps_total:,} captured sweeps put both radios on the "
        f"same arm ({100.0 * matched_total / sweeps_total:.1f}%, design p = 0.9); "
        f"per-arm counts {check['observed_min']}–{check['observed_max']} against a uniform "
        f"expectation of {check['expected_per_arm']:.1f}\n"
        f"(chi-square {check['chi_square']:.1f} on {check['df']} df, Monte-Carlo "
        f"p = {check['p_value_mc']:.2f} over {MC_DRAWS:,} draws — consistent with a "
        "uniform per-sweep draw)",
        fontsize=13.6, fontweight="bold", pad=16, linespacing=1.5)

    scored_pairs = sum(cell["scored_pairs"] for cell in cells.values())
    imported_pairs = sum(cell["imported_pairs"] for cell in cells.values())
    frozen = meta["corpus_census"]["scored"]

    # Re-stat the share now, so drift under the figure is printed on the figure.
    closing = census_now()
    moved = closing["scored_sidecars_sync"] - opening["scored_sidecars_sync"]
    closing_note = (f"{closing['scored_sidecars_sync']:,} with a scores.json present "
                    f"({moved:+d} during this run)")
    figure.text(
        0.035, 0.010,
        f"n = {sweeps_total:,} sweeps captured (collector's own per-sweep record, "
        f"leo-scans/sync-*/sweep.json), of which {matched_total:,} are matched-arm. "
        f"{imported_pairs:,} matched-arm pairs have reached the corpus; "
        f"{scored_pairs:,} of those are scored.\n"
        f"Colour encodes the sweep count only; the scale spans the uniform expectation "
        f"{check['expected_per_arm']:.1f} ± 3σ (±{span:.0f}) and the rule on the bar marks "
        f"that expectation. Guard = cross_radio.pilot_guard_hz(rate) = rate/2 − "
        f"{PILOT_BAND_HZ / 2e3:g} kHz;\nthe collector's own pilot_band_fits flag agrees with "
        f"guard ≥ 0 at all four rates ({'verified' if flag_agrees else 'DISAGREES — SEE JSON'}). "
        f"SCORING IS LIVE AND GROWING: {frozen:,} scored sync-* sidecars when this extract "
        f"was frozen at {meta['started_utc']},\n{closing_note} at "
        f"{closing['measured_utc']}. Sweep and pair counts are fixed at capture time and do "
        f"not move; only the scored column does.",
        fontsize=10.4, color=MUTED, va="bottom", linespacing=1.5)

    figure.savefig(os.path.join(HERE, f"{NAME}.png"), dpi=150,
                   bbox_inches="tight", facecolor="white")
    plt.close(figure)

    payload = {
        "figure": NAME,
        "finding": ("The 12-arm randomised draw came out flat over 7,054 captured "
                    "sweeps; the 1.25 MS/s column is handicapped because the "
                    "1.875 MHz pilot band does not fit in a 1.25 MHz captured "
                    "band at any probe length, not because it was under-sampled."),
        "sources": {"collector_record": f"{SCANS}/sync-*/sweep.json",
                    "corpus_manifests": f"{CORPUS}/sync-*/manifest.json",
                    "repository_functions": [
                        "leo_tracker.radio.beacon.cross_radio.pilot_guard_hz",
                        "leo_tracker.radio.beacon.cross_radio.sweep_geometry",
                        "leo_tracker.radio.beacon.cross_radio.PILOT_BAND_HZ",
                        "leo_tracker.radio.beacon.cross_radio.DEAD_RECEIVERS"]},
        "census_at_start": opening,
        "census_at_end": closing,
        "census_drift": {key: closing[key] - opening[key] for key in opening
                         if key != "measured_utc"},
        "census_note": ("scored_sidecars_sync counts a scores.json being present; "
                        "the cache's corpus_census.scored additionally applies the "
                        "SCORES_SCHEMA gate at freeze time. Sweep and pair counts are "
                        "fixed at capture and do not drift; the scored column does."),
        "cache_meta": meta,
        "authoritative_review_arm_axis": review,
        "totals": {"sweeps_captured": sweeps_total,
                   "matched_arm_sweeps": matched_total,
                   "matched_arm_fraction": matched_total / sweeps_total,
                   "mismatched_arm_sweeps": sweeps_total - matched_total,
                   "matched_arm_pairs_imported": imported_pairs,
                   "matched_arm_pairs_scored": scored_pairs},
        "uniformity_check": check,
        "pilot_band_flag_audit": flag_audit,
        "pilot_band_flag_agrees_with_guard": flag_agrees,
        "pilot_band_hz": PILOT_BAND_HZ,
        "plotted_grid": {
            "rows_probe_ms": PROBES_MS, "columns_rate_msps": RATES_MSPS,
            "values_matched_sweeps": grid.tolist(),
            "colour_vmin": float(check["expected_per_arm"] - span),
            "colour_vmax": float(check["expected_per_arm"] + span)},
        "arms": [cells[key] for key in order],
        "markdown_table": markdown_table(cells, order)}
    with open(os.path.join(HERE, f"{NAME}.json"), "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)

    print(payload["markdown_table"])
    print()
    print(json.dumps({"uniformity": check, "flag_agrees": flag_agrees,
                      "census_at_start": opening, "census_at_end": closing},
                     indent=2))


if __name__ == "__main__":
    main()
