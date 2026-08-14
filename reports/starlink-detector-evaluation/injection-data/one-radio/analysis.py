"""Thresholds, detection probabilities and intervals.

The threshold rule and the per-cell firing rule are the repository's own
(``survey_comparison.threshold_from``, ``cross_radio.observation_fires``); only
the null population changes, and that change is the whole experiment.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from leo_tracker.radio.beacon.survey_comparison import (DEFAULT_FALSE_ALARM_RATE,
                                                        threshold_from)
from leo_tracker.radio.beacon.cross_radio import observation_fires

METHODS = ("anchor-8", "differential-16", "differential-32", "glrt-32",
           "glrt-64", "full-frame-full", "full-frame-acquire",
           "full-frame-verify")

#: The report's two published orders, best-first, for the comparison E2 owes.
MODEL_D_RANKING = ("glrt-32", "glrt-64", "anchor-8", "differential-16",
                   "differential-32", "full-frame-verify", "full-frame-full",
                   "full-frame-acquire")
FIRE_COUNT_RANKING = ("full-frame-full", "full-frame-acquire",
                      "full-frame-verify", "differential-32",
                      "differential-16", "glrt-64", "anchor-8", "glrt-32")
SKY_FIRE_RATE = {"full-frame-full": 0.3330, "full-frame-acquire": 0.3329,
                 "full-frame-verify": 0.3319, "differential-32": 0.3309,
                 "differential-16": 0.3270, "glrt-64": 0.3175,
                 "anchor-8": 0.3118, "glrt-32": 0.3089}
SKY_NULL_RATE = {"full-frame-full": 0.0674, "full-frame-acquire": 0.0673,
                 "full-frame-verify": 0.0669, "differential-32": 0.0630,
                 "differential-16": 0.0635, "glrt-64": 0.0610,
                 "anchor-8": 0.0630, "glrt-32": 0.0547}
MODEL_D = {"glrt-32": 0.7945, "glrt-64": 0.7901, "anchor-8": 0.7872,
           "differential-16": 0.7733, "differential-32": 0.7706,
           "full-frame-verify": 0.7651, "full-frame-full": 0.7611,
           "full-frame-acquire": 0.7604}   # mean of dA and dB, report table


def read(path: Path) -> tuple[dict, list[dict]]:
    header, rows = {}, []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        (header.update(row) if row.get("record") == "header" else rows.append(row))
    return header, rows


def point_scores(rows: list[dict], method: str, field: str = "score") -> list[float]:
    return [float(v) for row in rows for point in row["points"]
            if (v := (point["methods"].get(method) or {}).get(field)) is not None]


def calibrate(null_rows: list[dict], *, field: str = "score",
              false_alarm_rate: float = DEFAULT_FALSE_ALARM_RATE) -> dict:
    """One threshold per method at ``false_alarm_rate`` **per point**."""
    return {method: threshold_from(point_scores(null_rows, method, field),
                                   false_alarm_rate=false_alarm_rate)
            for method in METHODS}


def cell_rate(rows: list[dict], method: str, threshold: float | None) -> dict:
    """Share of observations where any claimed point exceeds the threshold."""
    fired = [observation_fires(row, method, threshold) for row in rows]
    usable = [f for f in fired if f is not None]
    n = len(usable)
    k = sum(usable)
    return {"fired": k, "cells": n, "rate": (k / n) if n else None,
            **wilson(k, n)}


def point_rate(rows: list[dict], method: str, threshold: float | None,
               field: str = "score") -> dict:
    if threshold is None:
        return {"fired": 0, "points": 0, "rate": None, "lo": None, "hi": None}
    values = point_scores(rows, method, field)
    k = sum(1 for v in values if v > threshold)
    return {"fired": k, "points": len(values),
            "rate": (k / len(values)) if values else None,
            **wilson(k, len(values))}


def wilson(k: int, n: int, z: float = 1.96) -> dict:
    """Wilson score interval -- behaves at 0 and 1, where Wald does not."""
    if n == 0:
        return {"lo": None, "hi": None}
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"lo": float(max(0.0, centre - half)),
            "hi": float(min(1.0, centre + half))}


def snr_db(rows: list[dict], noise_power: float) -> float:
    """Mean projected signal power over the measured empty-channel power."""
    values = [r["signal_power"] for r in rows if r.get("signal_power") is not None]
    if not values:
        return float("nan")
    mean = float(np.mean(values))
    return 10 * np.log10(mean / noise_power) if mean > 0 else float("-inf")


def spearman(order_a, order_b) -> float:
    rank_a = {name: i for i, name in enumerate(order_a)}
    rank_b = {name: i for i, name in enumerate(order_b)}
    shared = sorted(set(rank_a) & set(rank_b))
    a = np.array([rank_a[s] for s in shared], float)
    b = np.array([rank_b[s] for s in shared], float)
    a -= a.mean(); b -= b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denom) if denom else float("nan")


LOOPBACK_NOTE = ("CABLED LOOPBACK (TX2 -> tee -> 2x30 dB -> RX1,RX2): this "
                 "tests the detectors and the digital pipeline, NOT the LNBs, "
                 "the antenna, or real sky.")
