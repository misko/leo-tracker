"""Shared arithmetic for T2 and T3, using the repository's own rules.

``threshold_from`` draws the per-point quantile and ``observation_fires``
applies the "any of ~7 candidate points" rule that defines a cell -- both
imported from the modules the sky survey uses, so a difference between this
radio and the sky corpus cannot be an artefact of two different definitions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.cross_radio import observation_fires  # noqa: E402
from leo_tracker.radio.beacon.survey_comparison import threshold_from  # noqa: E402

METHODS = ["anchor-8", "differential-16", "differential-32",
           "full-frame-acquire", "full-frame-full", "full-frame-verify",
           "glrt-32", "glrt-64"]


def load(path: Path) -> tuple[dict, list[dict]]:
    header, rows = {}, []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") == "header":
            header = record
        elif record.get("kind") == "observation":
            rows.append(record)
    return header, rows


def as_observation(row: dict, arm: str) -> dict:
    """Re-shape one row into what ``observation_fires`` expects.

    ``arm='target'`` reads the exact-code score; ``arm='cross-edge'`` reads the
    opposite edge's score on the very same IQ at the very same points.
    """
    key = "score" if arm == "target" else "cross_edge_score"
    return {"points": [{"methods": {name: {"score": value.get(key)}
                                    for name, value in point["methods"].items()}}
                       for point in row["points"]]}


def own_thresholds(rows: list[dict], *, rate: float = 0.01) -> dict:
    """Per-point thresholds drawn from THIS radio's cross-edge null arm."""
    population: dict = {method: [] for method in METHODS}
    for row in rows:
        for point in row["points"]:
            for method, value in point["methods"].items():
                if value.get("cross_edge_score") is not None:
                    population.setdefault(method, []).append(
                        float(value["cross_edge_score"]))
    return {method: threshold_from(values, false_alarm_rate=rate)
            for method, values in population.items() if values}


def rates(rows: list[dict], thresholds: dict, *, arm: str = "target") -> dict:
    """Per-point and per-cell firing rate for every method."""
    out = {}
    for method in METHODS:
        threshold = (thresholds.get(method) or {}).get("threshold")
        if threshold is None:
            continue
        points = fired_points = 0
        cells = fired_cells = 0
        key = "score" if arm == "target" else "cross_edge_score"
        for row in rows:
            for point in row["points"]:
                value = point["methods"].get(method, {}).get(key)
                if value is None:
                    continue
                points += 1
                fired_points += int(float(value) > threshold)
            fires = observation_fires(as_observation(row, arm), method, threshold)
            if fires is None:
                continue
            cells += 1
            fired_cells += int(fires)
        out[method] = {
            "threshold": threshold,
            "points": points, "points_fired": fired_points,
            "per_point_rate": fired_points / points if points else None,
            "cells": cells, "cells_fired": fired_cells,
            "per_cell_rate": fired_cells / cells if cells else None,
            "points_per_cell": points / cells if cells else None,
        }
    return out


def arm_asymmetry(rows: list[dict]) -> dict:
    """How much hotter the target arm runs than its own cross-edge null.

    Both arms are read at the SAME candidate points, so this isolates one
    thing: those points were chosen by searchers maximising a *target*-edge
    statistic, so the target template is evaluated where it was selected to do
    well while the opposite edge's template is evaluated at a place chosen
    without reference to it.  On a dead cable neither arm holds a signal, so any
    gap between them is selection, not sky.
    """
    out = {}
    for method in METHODS:
        target, cross = [], []
        for row in rows:
            for point in row["points"]:
                value = point["methods"].get(method, {})
                if value.get("score") is not None:
                    target.append(float(value["score"]))
                if value.get("cross_edge_score") is not None:
                    cross.append(float(value["cross_edge_score"]))
        if not target or not cross:
            continue
        target_sorted, cross_sorted = sorted(target), sorted(cross)

        def quantile(values, q):
            return values[min(len(values) - 1, int(q * len(values)))]
        out[method] = {
            "n_target": len(target), "n_cross": len(cross),
            "target_median": quantile(target_sorted, 0.50),
            "cross_median": quantile(cross_sorted, 0.50),
            "target_p99": quantile(target_sorted, 0.99),
            "cross_p99": quantile(cross_sorted, 0.99),
            "median_ratio": (quantile(target_sorted, 0.50)
                             / quantile(cross_sorted, 0.50)
                             if quantile(cross_sorted, 0.50) else None),
            "p99_ratio": (quantile(target_sorted, 0.99)
                          / quantile(cross_sorted, 0.99)
                          if quantile(cross_sorted, 0.99) else None),
        }
    return out


def sky_thresholds(path: Path) -> dict:
    payload = json.loads(Path(path).read_text())
    key = json.dumps([5000000.0, 80.0])
    return payload["thresholds"][key], payload
