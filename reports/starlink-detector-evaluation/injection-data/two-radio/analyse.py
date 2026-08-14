"""Drive cross_radio's own estimator over the dual-rig injection run.

Nothing here reimplements the model.  The cells, the thresholds, the empty-sky
rate p, the solver, the f spread and both negative controls are the repository's
functions; this module only builds the entries they read and adds the two things
the corpus can never supply -- the known occupancy and the directly measured
per-radio detection probability.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

from leo_tracker.radio.beacon import cross_radio as CR

HERE = Path(__file__).parent
RUNS = HERE / "runs"
RADIOS = ("r183", "r165")
PROBE_MS = 20.0
FS = 5_000_000.0


# -- building the shapes cross_radio reads ---------------------------------

def load_sweeps() -> list[dict]:
    out = []
    for path in sorted(RUNS.glob("[0-9][0-9][0-9]-*.json")):
        out.append(json.loads(path.read_text()))
    return out


def _observation(row: dict, radio: str, *, arm: str) -> dict:
    block = row[radio]
    return {"arm": arm, "iq_index": int(row["k"]), "receiver": 0,
            "receiver_label": f"{radio}-rx1", "channel": "loopback",
            "region": "pilot", "edge": "lower",
            "points": block["points"], "rms": block["rms"]}


def entries_for(sweep: dict, radio: str, *, arm_of) -> dict:
    """One radio of one sweep, in the shape cross_radio._entry produces."""
    observations = [_observation(row, radio, arm=arm_of(sweep, row))
                    for row in sweep["instants"]]
    order = [["loopback", "lower"] for _ in sweep["instants"]]
    return {"capture": f"{sweep['sweep']}-{radio}", "path": sweep["sweep"],
            "radio_id": radio, "receiver_labels": [f"{radio}-rx1"],
            "sample_rate_hz": FS, "probe_ms": PROBE_MS,
            "samples_per_tuning": 100_000, "pilot_guard_hz": CR.pilot_guard_hz(FS),
            "receiver_centers_hz": [0.0], "sample_order": order,
            "edge_order": "same", "arm_name": "5 MS/s 20 ms",
            "pilot_band_fits": True, "matched_arm": True,
            "paired_sweep": sweep["sweep"], "peer_radio": None,
            "skew_ms": {"median": 0.0, "per_tuning": [0.0] * len(order)},
            "scores": {"observations": observations, "sample_rate_hz": FS,
                       "probe_ms": PROBE_MS}}


def pair_for(sweep: dict, *, arm_of) -> dict:
    left = entries_for(sweep, RADIOS[0], arm_of=arm_of)
    right = entries_for(sweep, RADIOS[1], arm_of=arm_of)
    return {"paired_sweep": sweep["sweep"], "radios": [left, right],
            "geometry": CR.sweep_geometry(left["sample_order"],
                                          right["sample_order"]),
            "matched_arm": True,
            "skew_ms": {"median": 0.0,
                        "per_tuning": [0.0] * len(left["sample_order"])},
            "f_true": sweep["f_true"], "kind": sweep["kind"]}


def target_arm(sweep, row):
    """Null sweeps are the null arm; every instant of a target sweep -- occupied
    or not -- is a target cell, because f is the fraction of them occupied."""
    return "target" if sweep["kind"] == "target" else "null"


# -- the ground truth the corpus never had ---------------------------------

def direct_rates(pairs: list[dict], sweeps: list[dict], thresholds: dict,
                 methods: list[str]) -> dict:
    """Per-radio Pd and per-radio empty-cell rate, measured against the KNOWN
    occupancy rather than inferred from coincidence."""
    truth = {s["sweep"]: {row["k"]: row["occupied"] for row in s["instants"]}
             for s in sweeps}
    tally = {radio: {m: {"hit": 0, "occ": 0, "fa": 0, "empty": 0}
                     for m in methods} for radio in RADIOS}
    for pair in pairs:
        if pair["kind"] != "target":
            continue
        book = truth[pair["paired_sweep"]]
        for entry, radio in zip(pair["radios"], RADIOS):
            for observation in entry["scores"]["observations"]:
                occupied = book[observation["iq_index"]]
                for method in methods:
                    fired = CR.observation_fires(
                        observation, method,
                        CR._threshold_for(thresholds, method, (FS, PROBE_MS)))
                    if fired is None:
                        continue
                    slot = tally[radio][method]
                    if occupied:
                        slot["occ"] += 1
                        slot["hit"] += int(fired)
                    else:
                        slot["empty"] += 1
                        slot["fa"] += int(fired)
    out = {}
    for radio, per in tally.items():
        out[radio] = {m: {"pd": (v["hit"] / v["occ"]) if v["occ"] else None,
                          "occupied": v["occ"], "hits": v["hit"],
                          "p_empty": (v["fa"] / v["empty"]) if v["empty"] else None,
                          "empty": v["empty"], "false_alarms": v["fa"]}
                      for m, v in per.items()}
    return out


def bootstrap_f(cells: list[dict], thresholds: dict, false_alarm: dict,
                methods: list[str], *, draws: int = 600, seed: int = 7) -> dict:
    """Percentile CI on f per method, resampling cells jointly.

    Instants are independent by construction here -- an independent Bernoulli
    draw and independent noise on each -- so the cell is the resampling unit.
    """
    decisions = []
    for cell in cells:
        row = []
        for method in methods:
            left = CR.observation_fires(
                cell["a"]["observation"], method,
                CR._threshold_for(thresholds, method, cell["a"]["key"]))
            right = CR.observation_fires(
                cell["b"]["observation"], method,
                CR._threshold_for(thresholds, method, cell["b"]["key"]))
            row.append(None if left is None or right is None else (left, right))
        decisions.append(row)
    rates = [(false_alarm.get(m) or {}).get("rate") for m in methods]
    dice = random.Random(seed)
    collected = {m: [] for m in methods}
    spread_draws = []
    for _ in range(draws):
        sample = dice.choices(decisions, k=len(decisions))
        counts = [[0, 0, 0, 0] for _ in methods]
        for row in sample:
            for index, verdict in enumerate(row):
                if verdict is None:
                    continue
                slot = counts[index]
                slot[0] += 1
                slot[1] += verdict[0]
                slot[2] += verdict[1]
                slot[3] += verdict[0] and verdict[1]
        drawn = []
        for index, method in enumerate(methods):
            usable, left, right, both = counts[index]
            if not usable or rates[index] is None:
                continue
            solved = CR.solve_coincidence(left / usable, right / usable,
                                          both / usable, rates[index])
            if solved["solvable"]:
                collected[method].append(solved["f"])
                drawn.append(solved["f"])
        if len(drawn) > 1:
            spread_draws.append(max(drawn) - min(drawn))
    out = {}
    for method, values in collected.items():
        if not values:
            out[method] = {"draws": 0, "p05": None, "p50": None, "p95": None}
            continue
        ordered = sorted(values)
        out[method] = {"draws": len(ordered),
                       "p05": CR._at(ordered, 0.05),
                       "p50": CR._at(ordered, 0.50),
                       "p95": CR._at(ordered, 0.95)}
    return {"per_method": out, "spread_draws": spread_draws}


# -- D5: the joint null ----------------------------------------------------

def joint_null(sweeps: list[dict], methods: list[str],
               rates=(0.30, 0.20, 0.10, 0.05, 0.02, 0.01)) -> dict:
    """Is P(AB) = P(A)P(B) when both radios are silent?

    Measured on the null sweeps, where the two radios were released into the
    same instant with both TX chains parked.  The model assumes p^2 for the
    joint empty-cell rate; this is the first time that has been checked against
    a measured joint null.

    Swept over false-alarm rate because at the 1% operating point the expected
    joint count is far below one and no test can resolve it there.

    **The verdict is Fisher's exact test on the 2x2 table, not a bootstrap
    interval on the gap.**  A percentile interval is the wrong instrument for a
    rare event: where P(AB) is 0 in every resample the interval collapses onto
    the deterministic -P(A)P(B) and excludes zero, marking independent data
    dependent.  Fisher conditions on the margins and stays exact at any count.
    """
    from scipy import stats
    per_instant = []
    scores = {radio: defaultdict(list) for radio in RADIOS}
    for sweep in sweeps:
        if sweep["kind"] != "null":
            continue
        for row in sweep["instants"]:
            cell = {}
            for radio in RADIOS:
                best = {}
                for point in row[radio]["points"]:
                    for name, block in point["methods"].items():
                        value = float(block["score"])
                        if value > best.get(name, -1e9):
                            best[name] = value
                cell[radio] = best
                for name, value in best.items():
                    scores[radio][name].append(value)
            per_instant.append(cell)
    out = {"instants": len(per_instant), "methods": {},
           "basis": "Fisher exact on the 2x2 joint table, per method per threshold"}
    for method in methods:
        rows = []
        pooled = sorted(v for radio in RADIOS for v in scores[radio][method])
        if not pooled:
            continue
        for rate in rates:
            cut = CR._at(pooled, 1.0 - rate)
            both = a_only = b_only = neither = 0
            for cell in per_instant:
                left = cell[RADIOS[0]].get(method, -1e9) > cut
                right = cell[RADIOS[1]].get(method, -1e9) > cut
                if left and right:
                    both += 1
                elif left:
                    a_only += 1
                elif right:
                    b_only += 1
                else:
                    neither += 1
            n = len(per_instant)
            if not n:
                continue
            a, b = both + a_only, both + b_only
            pa, pb, pab = a / n, b / n, both / n
            expected = pa * pb
            table = [[both, a_only], [b_only, neither]]
            try:
                odds, pvalue = stats.fisher_exact(table)
            except Exception:
                odds, pvalue = None, None
            scale = pa * (1 - pa) * pb * (1 - pb)
            rows.append({
                "target_rate": rate, "threshold": cut, "n": n,
                "table": table,
                "p_a": pa, "p_b": pb, "p_ab": pab, "expected": expected,
                "expected_count": expected * n, "both_count": both,
                "gap": pab - expected,
                "odds_ratio": (None if odds is None or not np.isfinite(odds)
                               else float(odds)),
                "fisher_p": (None if pvalue is None else float(pvalue)),
                "phi": ((pab - expected) / scale ** 0.5) if scale > 0 else None,
                "consistent": bool(pvalue is None or pvalue > 0.05)})
        out["methods"][method] = rows
    flat = [r for rows in out["methods"].values() for r in rows]
    tested = [r for r in flat if r["fisher_p"] is not None]
    out["summary"] = {
        "cases": len(flat), "tested": len(tested),
        "consistent": int(sum(1 for r in flat if r["consistent"])),
        "rejected_at_05": int(sum(1 for r in tested if r["fisher_p"] <= 0.05)),
        "min_fisher_p": min((r["fisher_p"] for r in tested), default=None),
        "note": "the 8 algorithms are near-duplicates, so these tests are not "
                "independent of one another"}
    return out


def leak_check(fine: dict, methods: list[str]) -> dict:
    """Is a TX parked at -89.75 dB distinguishable from one with no buffer at all?

    Comparing tail fire rates alone is weak at n=60 -- one exceedance is 1.7%.
    A coherent pilot leaking through a parked port would lift the WHOLE score
    distribution, so the sharper question is whether the two distributions
    differ at all.  Mann-Whitney U on every method, both radios.
    """
    from scipy import stats
    out = {}
    for radio in RADIOS:
        rows = {}
        dead = fine["leak"]["dead"][radio]
        parked = fine["leak"]["parked"][radio]
        for method in methods:
            a = [r["scores"][method] for r in dead if method in r["scores"]]
            b = [r["scores"][method] for r in parked if method in r["scores"]]
            if len(a) < 5 or len(b) < 5:
                continue
            u = stats.mannwhitneyu(a, b, alternative="two-sided")
            rows[method] = {
                "n_dead": len(a), "n_parked": len(b),
                "median_dead": float(np.median(a)),
                "median_parked": float(np.median(b)),
                "u_p_value": float(u.pvalue),
                "parked_higher": bool(float(np.median(b)) > float(np.median(a)))}
        out[radio] = rows
    return out


def null_by_radio(pairs: list[dict], thresholds: dict,
                  methods: list[str]) -> dict:
    """Per-radio fire rate on the NULL arm.

    The solver takes a single p for both chains.  If the two radios sit at
    different empty-cell rates that assumption is wrong, and the error lands in
    f and in both d's.  Measured here rather than assumed.
    """
    tally = {radio: {m: [0, 0] for m in methods} for radio in RADIOS}
    for pair in pairs:
        if pair["kind"] != "null":
            continue
        for entry, radio in zip(pair["radios"], RADIOS):
            for observation in entry["scores"]["observations"]:
                for method in methods:
                    fired = CR.observation_fires(
                        observation, method,
                        CR._threshold_for(thresholds, method, (FS, PROBE_MS)))
                    if fired is None:
                        continue
                    tally[radio][method][1] += 1
                    tally[radio][method][0] += int(fired)
    return {radio: {m: {"fired": v[0], "cells": v[1],
                        "rate": (v[0] / v[1]) if v[1] else None}
                    for m, v in per.items()}
            for radio, per in tally.items()}
