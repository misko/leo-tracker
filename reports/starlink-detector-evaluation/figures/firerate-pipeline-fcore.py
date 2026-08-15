#!/usr/bin/env python3
"""The coincidence estimator applied to the compact cell cache.

Thresholds, the cell false-alarm rate, the join and ``solve_coincidence`` are
the repository's own (``leo_tracker.radio.beacon.cross_radio``); only the data
plumbing is local, because the cache stores per-observation maxima instead of
whole sidecars.  ``observation_fires`` maximises over a cell's candidate points,
so a stored maximum reproduces it exactly.
"""
from __future__ import annotations

import gzip
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/home/satpi01/leo-tracker/src")

from leo_tracker.radio.beacon.cross_radio import (  # noqa: E402
    DESIGN_MAX_SKEW_MS, solve_coincidence,
)
from leo_tracker.radio.beacon.survey_comparison import (  # noqa: E402
    DEFAULT_FALSE_ALARM_RATE, threshold_from,
)

_HERE = Path(__file__).resolve().parent
_WORK = Path(os.environ.get("REFRESH_WORK", _HERE.parent / "work"))
CACHE = _WORK / "cells.json.gz"


def load(cache: Path = CACHE) -> dict:
    with gzip.open(cache, "rt") as handle:
        return json.load(handle)


def fires(row: dict, method: str, threshold: float | None) -> bool | None:
    """Did this chain claim a signal here?  ``None`` = the method has no score."""
    if threshold is None:
        return None
    value = row["max"].get(method)
    if value is None:
        return None
    return value > threshold


def build(payload: dict) -> dict:
    """Thresholds, empty-sky rate p, and the joined matched-arm cell list."""
    entries = [entry for pair in payload["pairs"] for entry in pair["radios"]]

    populations: dict = defaultdict(list)
    for entry in entries:
        key = (entry["rate"], entry["probe_ms"])
        for row in entry["obs"]:
            if row["t"]:
                continue
            for method, values in (row.get("pts") or {}).items():
                populations[(method, key)].extend(values)
    thresholds = {key: threshold_from(values,
                                      false_alarm_rate=DEFAULT_FALSE_ALARM_RATE)
                  for key, values in populations.items()}

    methods = sorted({method for method, _ in thresholds})

    tally: dict = defaultdict(lambda: [0, 0])
    for entry in entries:
        key = (entry["rate"], entry["probe_ms"])
        for row in entry["obs"]:
            if row["t"]:
                continue
            for method in methods:
                edge = (thresholds.get((method, key)) or {}).get("threshold")
                verdict = fires(row, method, edge)
                if verdict is None:
                    continue
                tally[method][1] += 1
                tally[method][0] += int(verdict)
    false_alarm = {method: {"count": hit, "cells": n,
                            "rate": (hit / n) if n else None}
                   for method, (hit, n) in sorted(tally.items())}

    cells = []
    for pair in payload["pairs"]:
        if not pair["matched_arm"]:
            continue
        left, right = pair["radios"]
        skew = (pair.get("skew_ms") or {}).get("per_tuning") or []
        median = (pair.get("skew_ms") or {}).get("median")
        ahead, behind = defaultdict(list), defaultdict(list)
        for entry, bucket in ((left, ahead), (right, behind)):
            for row in entry["obs"]:
                if row["t"]:
                    bucket[row["i"]].append((entry, row))
        for instant in sorted(set(ahead) & set(behind),
                              key=lambda v: (v is None, v)):
            at = (skew[instant]
                  if isinstance(instant, int) and 0 <= instant < len(skew)
                  else median)
            for entry_a, row_a in ahead[instant]:
                for entry_b, row_b in behind[instant]:
                    cells.append({
                        "sweep": pair["paired_sweep"],
                        "geometry": pair["geometry"],
                        "instant": instant, "skew_ms": at,
                        "channel": row_a["ch"],
                        "arm": entry_a["arm_name"],
                        "rate": entry_a["rate"], "probe_ms": entry_a["probe_ms"],
                        "receiver_pair": f'{row_a["lb"]}|{row_b["lb"]}',
                        "a": (row_a, (entry_a["rate"], entry_a["probe_ms"])),
                        "b": (row_b, (entry_b["rate"], entry_b["probe_ms"])),
                    })
    # Freeze each cell's two verdicts per method once.  Every stratum below is a
    # subset of this same list, so the thresholds never change under it and the
    # bootstrap can resample decisions instead of re-scoring.
    for cell in cells:
        row_a, key_a = cell["a"]
        row_b, key_b = cell["b"]
        verdicts = {}
        for method in methods:
            left = fires(row_a, method,
                         (thresholds.get((method, key_a)) or {}).get("threshold"))
            right = fires(row_b, method,
                          (thresholds.get((method, key_b)) or {}).get("threshold"))
            verdicts[method] = (None if left is None or right is None
                                else (bool(left), bool(right)))
        cell["dec"] = verdicts

    return {"thresholds": thresholds, "false_alarm": false_alarm,
            "methods": methods, "cells": cells, "entries": len(entries)}


def rates(cells: list[dict], method: str, thresholds: dict = None) -> dict:
    fired_a = fired_b = both = usable = 0
    for cell in cells:
        verdict = cell["dec"].get(method)
        if verdict is None:
            continue
        left, right = verdict
        usable += 1
        fired_a += int(left)
        fired_b += int(right)
        both += int(left and right)
    return {"cells": usable, "fired_a": fired_a, "fired_b": fired_b, "both": both}


def estimate(cells: list[dict], method: str, model: dict) -> dict:
    counts = rates(cells, method)
    p = (model["false_alarm"].get(method) or {}).get("rate")
    if not counts["cells"] or p is None:
        return {"solvable": False, "f": None, **counts}
    return {**solve_coincidence(counts["fired_a"] / counts["cells"],
                                counts["fired_b"] / counts["cells"],
                                counts["both"] / counts["cells"], p), **counts}


def stratum(cells: list[dict], model: dict) -> dict:
    """f per algorithm over one slice, plus the min/max band across algorithms."""
    values, detail = {}, {}
    for method in model["methods"]:
        solved = estimate(cells, method, model)
        detail[method] = {"f": solved.get("f"), "solvable": solved.get("solvable"),
                          "reason": solved.get("reason"), "phi": solved.get("phi"),
                          "cells": solved.get("cells"),
                          "p_a": solved.get("p_a"), "p_b": solved.get("p_b"),
                          "p_ab": solved.get("p_ab")}
        if solved.get("solvable"):
            values[method] = solved["f"]
    if not values:
        return {"n_cells": len(cells), "methods_solved": 0, "min": None,
                "max": None, "spread": None, "values": {}, "detail": detail}
    return {"n_cells": len(cells), "methods_solved": len(values),
            "min": min(values.values()), "max": max(values.values()),
            "spread": max(values.values()) - min(values.values()),
            "values": values, "detail": detail}


def cluster_bootstrap_gap(cells: list[dict], model: dict, left: str, right: str,
                          *, draws: int = 300, seed: int = 20260814) -> dict:
    """Resample whole sweeps, recompute the mean f gap between two slices.

    Sweeps, not cells: the sixteen cells of one sweep share an arm, an edge
    order and a skew draw, so resampling cells would treat sixteen correlated
    observations as sixteen independent ones and shrink the interval.
    """
    by_sweep: dict = defaultdict(list)
    for cell in cells:
        by_sweep[cell["sweep"]].append(cell)
    sweeps = sorted(by_sweep)
    dice = random.Random(seed)
    gaps = []
    for _ in range(draws):
        drawn = dice.choices(sweeps, k=len(sweeps))
        pool = [cell for sweep in drawn for cell in by_sweep[sweep]]
        a = [cell for cell in pool if cell["receiver_pair"] == left]
        b = [cell for cell in pool if cell["receiver_pair"] == right]
        per = []
        for method in model["methods"]:
            fa = estimate(a, method, model)
            fb = estimate(b, method, model)
            if fa.get("solvable") and fb.get("solvable"):
                per.append(fb["f"] - fa["f"])
        if per:
            gaps.append(sum(per) / len(per))
    ordered = sorted(gaps)
    if not ordered:
        return {"draws": 0, "sweeps": len(sweeps)}
    def at(frac):
        return ordered[min(len(ordered) - 1, int(frac * len(ordered)))]
    return {"draws": len(ordered), "sweeps": len(sweeps),
            "p05": at(0.05), "p50": at(0.50), "p95": at(0.95),
            "le_zero": sum(1 for value in ordered if value <= 0)}


__all__ = ["load", "build", "stratum", "estimate", "cluster_bootstrap_gap",
           "DESIGN_MAX_SKEW_MS"]
