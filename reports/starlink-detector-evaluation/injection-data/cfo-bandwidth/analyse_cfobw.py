"""Turn the raw factorial records into one object per cell.

Detection is decided the way the report decides it, through the repository's own
rules rather than a fresh one invented here: :func:`survey_comparison.threshold_from`
draws a per-candidate-point threshold at a stated false-alarm rate, and
:func:`cross_radio.observation_fires` applies the "any of this observation's
candidate points" rule that turns per-point scores into one verdict per probe.

Two null populations are carried side by side, because they answer different
objections and this sweep can afford both:

``txoff``      the target-edge score at points proposed on a genuinely empty
               channel -- TX at -89.75 dB, same rate, same bandwidth, same gain,
               same probe length.  Pooled across bandwidth within a rate so the
               1% quantile has the draws to stand on.
``crossedge``  the opposite edge's template scored at the very same candidate
               points as the signal arm.  Drawn per (rate, bandwidth), which is
               what ``radio-165/analyse.py`` does; it is conditioned on where the
               target-edge searchers were pointing, so it is recorded as a check
               on ``txoff`` rather than as the headline.

The coarse front ends are scored on their own statistic, ``peak_to_median``,
against the same two null populations -- they propose the candidates, so
``observation_fires`` has nothing to maximise over for them.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, "/home/satpi01/leo-tracker/src")
from leo_tracker.radio.beacon.cross_radio import observation_fires  # noqa: E402
from leo_tracker.radio.beacon.survey_comparison import threshold_from  # noqa: E402

#: Confirmers, in the report's own naming.  ``full-frame-full`` is the headline:
#: the 300-symbol adjudicated statistic over the whole pilot set.
POINT_METHODS = ("full-frame-full", "full-frame-acquire", "full-frame-verify",
                 "anchor-8", "differential-16", "differential-32",
                 "glrt-32", "glrt-64")

#: Searchers scored on ``peak_to_median`` rather than through a candidate point.
COARSE_METHODS = ("coarse-A", "coarse-E")

HEADLINE = "full-frame-full"

OCCUPIED_HALF_WIDTH_HZ = 937_500.0
PILOT_CENTRE_HALF_WIDTH_HZ = 820_312.5

#: The coarse banks' outermost search offset, from
#: ``survey_scoring.COARSE_CONFIGS``.  Neither moves with rate or bandwidth.
COARSE_SEARCH_EDGE_HZ = {"coarse-A": 300_000.0, "coarse-E": 700_000.0}

FALSE_ALARM_RATE = 0.01

#: Two fixed coarse bars carried beside the drawn one.  1.33 is what is deployed
#: in ``fast_scan``; 1.189 is ``survey_scoring.CLEAN_NULL_P99_BY_PROBE_MS[40]``,
#: the 99th percentile a clean 40 ms null reaches -- this sweep's probe length.
#: A 20-probe null arm cannot support a 1% quantile on its own (three
#: exceedances need 300 draws), so the drawn bar is reported with
#: ``supported: false`` and these two stand beside it.  It makes no difference
#: here: the coarse statistic reads ~1.2 on noise and ~6-7 on the signal.
COARSE_FIXED_BARS = {"deployed_1.33": 1.33, "clean_null_p99_40ms_1.189": 1.189}


def read(path: Path) -> list[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    rows = []
    with opener(path, "rt") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _cell_scores(record: dict) -> dict:
    """One probe's best score per method, over its own candidate points."""
    out = {}
    for method in COARSE_METHODS:
        name = method.split("-")[1]
        out[method] = record["coarse"][name]["peak_to_median"]
    return out


def _best(record: dict, method: str, key: str = "score") -> float | None:
    """One probe's best score for one method, over its own candidate points.

    The same maximisation :func:`cross_radio.observation_fires` performs before
    it compares to a threshold, kept as a number instead of a verdict. Pd
    saturates at 1.00 the moment the margin exceeds the bar, so a matrix of Pd
    can be flat across a region where the statistic is falling steadily; this is
    what lets an edge be located inside that region.
    """
    values = [point["methods"][method][key]
              for point in (record.get("points") or [])
              if (point["methods"].get(method) or {}).get(key) is not None]
    return max(values) if values else None


def _point_values(record: dict, key: str) -> dict:
    values = defaultdict(list)
    for point in record.get("points") or []:
        for method, block in point["methods"].items():
            if block.get(key) is not None:
                values[method].append(float(block[key]))
    return values


def thresholds(observations: list[dict], nulls: list[dict]) -> dict:
    """Per-point thresholds from both null arms, keyed as the cells are."""
    txoff_point = defaultdict(lambda: defaultdict(list))
    txoff_coarse = defaultdict(lambda: defaultdict(list))
    # Pooled across bandwidth within a rate, and ALSO kept per (rate, bandwidth).
    # Pooling is what gives the coarse statistic enough draws to support a 1%
    # quantile, but a narrow arm admits less noise than a wide one, so pooling
    # could in principle lend one bandwidth another's null.  The per-cell arm is
    # carried beside it as the check: 40 captures x 2 receivers is 80 probes a
    # cell, which is ~560 point draws, enough to stand on its own.
    cell_point = defaultdict(lambda: defaultdict(list))
    cell_coarse = defaultdict(lambda: defaultdict(list))
    for record in nulls:
        rate = record["sample_rate_hz"]
        cell = (rate, record["rx_bandwidth_hz"])
        for method, values in _point_values(record, "score").items():
            txoff_point[rate][method].extend(values)
            cell_point[cell][method].extend(values)
        for method, value in _cell_scores(record).items():
            txoff_coarse[rate][method].append(value)
            cell_coarse[cell][method].append(value)

    cross_point = defaultdict(lambda: defaultdict(list))
    for record in observations:
        key = (record["sample_rate_hz"], record["rx_bandwidth_hz"])
        for method, values in _point_values(record, "cross_edge_score").items():
            cross_point[key][method].extend(values)

    def draw(store):
        return {key: {method: threshold_from(values,
                                             false_alarm_rate=FALSE_ALARM_RATE)
                      for method, values in methods.items()}
                for key, methods in store.items()}

    return {"txoff_point": draw(txoff_point), "txoff_coarse": draw(txoff_coarse),
            "crossedge_point": draw(cross_point),
            "txoff_point_by_cell": draw(cell_point),
            "txoff_coarse_by_cell": draw(cell_coarse)}


#: A cell has "lost it" when its median statistic falls under this fraction of
#: the same (rate, bandwidth) row's zero-offset value.  A ratio rather than a
#: threshold, because the question is where a row FALLS, and the rows start at
#: different heights: a narrow analog filter costs a few dB at every offset
#: including zero, and a bar shared across rows would read that constant offset
#: as an early cliff.  0.5 is far outside the probe-to-probe spread (the
#: statistic sits within a few percent of its median across 40 probes) and far
#: above the collapsed value (~0.05 against ~0.8), so no swept offset lands near
#: it and the answer does not depend on where between the two the line is drawn.
CLIFF_FRACTION = 0.5


def cliffs(cells: list[dict], method: str = HEADLINE) -> list[dict]:
    """Where each (rate, bandwidth) row loses the signal, against where it should.

    One row per (Fs, B_RX): the largest offset still detected, the smallest
    offset lost, and the two predicted edges the answer is being tested against.
    This is the experiment's actual output -- a limit that is real has to move
    when its own knob moves, and this is the column you read down to see whether
    it did.
    """
    rows: dict = defaultdict(list)
    for cell in cells:
        rows[(cell["sample_rate_requested_hz"],
              cell["rx_bandwidth_requested_hz"])].append(cell)
    out = []
    for (rate, bandwidth), group in sorted(rows.items()):
        group = sorted(group, key=lambda cell: cell["cfo_requested_hz"])
        reference = next((cell for cell in group
                          if cell["cfo_requested_hz"] == 0), None)
        base = None if reference is None else (reference["scores"].get(method)
                                               or {}).get("median")
        last_good, first_bad = None, None
        for cell in group:
            value = (cell["scores"].get(method) or {}).get("median")
            if value is None or base is None:
                continue
            if value >= CLIFF_FRACTION * base:
                if first_bad is None:
                    last_good = cell["cfo_requested_hz"]
            elif first_bad is None:
                first_bad = cell["cfo_requested_hz"]
        out.append({
            "sample_rate_hz": rate, "rx_bandwidth_hz": bandwidth,
            "method": method,
            "zero_offset_score": base,
            "last_detected_offset_hz": last_good,
            "first_lost_offset_hz": first_bad,
            "cliff_midpoint_hz": (None if first_bad is None or last_good is None
                                  else 0.5 * (last_good + first_bad)),
            "offsets_swept_hz": [cell["cfo_requested_hz"] for cell in group],
            # What the two bandwidth limits predict for this row, from the
            # NOMINAL formulae.  The measured corners are in filter_shape; these
            # are here so the summary is self-contained.
            "predicted_digital_edge_hz": (rate - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
            "predicted_analog_edge_hz": (bandwidth - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
            "coarse_search_edge_hz": COARSE_SEARCH_EDGE_HZ["coarse-E"],
        })
    return out


def summarise(rows: list[dict]) -> dict:
    header = next((r for r in rows if r["kind"] == "header"), {})
    observations = [r for r in rows if r["kind"] == "observation"]
    nulls = [r for r in rows if r["kind"] == "null"]
    cell_headers = {(r["sample_rate_hz"], r["rx_bandwidth_hz"],
                     r["offset_nominal_hz"]): r
                    for r in rows if r["kind"] == "cell_header"}
    references = {(r["sample_rate_hz"], r["offset_nominal_hz"]): r
                  for r in rows if r["kind"] == "reference"}
    errors = [r for r in rows if r["kind"] in ("cell_error", "error", "abort")]

    limits = thresholds(observations, nulls)

    grouped = defaultdict(list)
    for record in observations:
        grouped[(record["sample_rate_hz"], record["rx_bandwidth_hz"],
                 record["offset_nominal_hz"])].append(record)

    null_power = defaultdict(list)
    for record in nulls:
        null_power[(record["sample_rate_hz"], record["rx_bandwidth_hz"])].append(
            record["mean_power_counts2"])

    cells = []
    for key in sorted(grouped):
        rate, bandwidth, offset = key
        probes = grouped[key]
        head = cell_headers.get(key, {})
        applied = head.get("applied", {})
        requested = head.get("requested", {})
        reference = references.get((rate, offset), {})

        point_thresholds = limits["txoff_point"].get(rate, {})
        cross_thresholds = limits["crossedge_point"].get((rate, bandwidth), {})
        coarse_thresholds = limits["txoff_coarse"].get(rate, {})
        cell_point_thresholds = limits["txoff_point_by_cell"].get((rate, bandwidth), {})
        cell_coarse_thresholds = limits["txoff_coarse_by_cell"].get((rate, bandwidth), {})

        detections, pd, detections_cross, pd_cross, pd_fixed = {}, {}, {}, {}, {}
        pd_cell = {}
        for method in POINT_METHODS:
            bar = (point_thresholds.get(method) or {}).get("threshold")
            fired = [observation_fires(record, method, bar) for record in probes]
            fired = [value for value in fired if value is not None]
            detections[method] = int(sum(fired))
            pd[method] = (sum(fired) / len(fired)) if fired else None
            bar = (cross_thresholds.get(method) or {}).get("threshold")
            other = [observation_fires(record, method, bar) for record in probes]
            other = [value for value in other if value is not None]
            detections_cross[method] = int(sum(other))
            pd_cross[method] = (sum(other) / len(other)) if other else None
            bar = (cell_point_thresholds.get(method) or {}).get("threshold")
            own = [observation_fires(record, method, bar) for record in probes]
            own = [value for value in own if value is not None]
            pd_cell[method] = (sum(own) / len(own)) if own else None
        for method in COARSE_METHODS:
            bar = (coarse_thresholds.get(method) or {}).get("threshold")
            values = [_cell_scores(record)[method] for record in probes]
            hits = [value > bar for value in values] if bar is not None else []
            detections[method] = int(sum(hits))
            pd[method] = (sum(hits) / len(hits)) if hits else None
            detections_cross[method] = detections[method]
            pd_cross[method] = pd[method]
            bar = (cell_coarse_thresholds.get(method) or {}).get("threshold")
            own = [value > bar for value in values] if bar is not None else []
            pd_cell[method] = (sum(own) / len(own)) if own else None
            for label, fixed in COARSE_FIXED_BARS.items():
                pd_fixed[f"{method}@{label}"] = (
                    sum(value > fixed for value in values) / len(values))

        scores = {}
        for method in POINT_METHODS:
            values = [value for value in
                      (_best(record, method) for record in probes)
                      if value is not None]
            bar = (point_thresholds.get(method) or {}).get("threshold")
            scores[method] = {
                "median": (float(np.median(values)) if values else None),
                "minimum": (float(np.min(values)) if values else None),
                "threshold": bar,
                "margin_db": (None if not values or not bar else
                              float(20 * np.log10(np.median(values) / bar))),
                "probes_scored": len(values)}
        for method in COARSE_METHODS:
            name = method.split("-")[1]
            values = [record["coarse"][name]["peak_to_median"] for record in probes]
            bar = (coarse_thresholds.get(method) or {}).get("threshold")
            scores[method] = {
                "median": float(np.median(values)),
                "minimum": float(np.min(values)),
                "threshold": bar,
                "margin_db": (None if not bar else
                              float(20 * np.log10(np.median(values) / bar))),
                "probes_scored": len(values)}

        power = np.array([record["mean_power_counts2"] for record in probes])
        peak = np.array([record["peak_counts"] for record in probes])
        floor = float(np.mean(null_power.get((rate, bandwidth), [np.nan])))
        # The reference is captured at the TX-start check's own drive, which is
        # only the cell's drive on the arms that run loud.  Comparing a -64 dB
        # cell against a -20 dB reference would price the analog filter at -44
        # dB, so the comparison is dropped rather than reported wrong.
        same_drive = (reference.get("verify_gain_db") is None
                      or reference.get("verify_gain_db") == head.get("tx_gain_db"))
        reference_power = (float(np.mean([row["mean_power_counts2"]
                                          for row in reference.get("reference", [])]))
                           if reference.get("reference") and same_drive else None)

        coarse = {}
        for method in COARSE_METHODS:
            name = method.split("-")[1]
            recovered = np.array([record["coarse"][name]["frequency_offset_hz"]
                                  for record in probes])
            coarse[method] = {
                "peak_to_median_mean": float(np.mean(
                    [record["coarse"][name]["peak_to_median"] for record in probes])),
                "recovered_offset_median_hz": float(np.median(recovered)),
                "recovered_offset_error_median_hz": float(np.median(
                    np.abs(recovered - head.get("offset_hz", offset)))),
                "search_edge_hz": COARSE_SEARCH_EDGE_HZ[method],
            }

        cells.append({
            "sample_rate_requested_hz": rate,
            "sample_rate_readback_hz": applied.get("sample_rate_applied_hz"),
            "rx_bandwidth_requested_hz": bandwidth,
            "rx_bandwidth_readback_hz": applied.get("rx_bandwidth_applied_hz"),
            "tx_bandwidth_requested_hz": requested.get("tx_bandwidth_hz"),
            "tx_bandwidth_readback_hz": applied.get("tx_bandwidth_applied_hz"),
            "tx_sample_rate_readback_hz": applied.get("tx_sample_rate_applied_hz"),
            "rx_gain_requested_db": requested.get("rx_gain_db"),
            "rx_gain_readback_db": applied.get("rx_gain_applied_db"),
            "rx_gain_mode_readback": applied.get("rx_gain_mode"),
            "rx_fir_en_readback": applied.get("rx_fir_en"),
            "cfo_requested_hz": offset,
            "cfo_applied_hz": head.get("offset_hz", offset),
            "tx_gain_db": head.get("tx_gain_db"),
            "probe_ms": head.get("probe_ms"),
            "probe_samples": head.get("probe_samples"),
            "probes": len(probes),
            "captures": len(probes) // 2,
            "detections": detections,
            "pd": pd,
            "detections_crossedge_threshold": detections_cross,
            "pd_crossedge_threshold": pd_cross,
            "pd_own_cell_threshold": pd_cell,
            "pd_coarse_fixed_bar": pd_fixed,
            "pd_headline": pd.get(HEADLINE),
            "scores": scores,
            "score_headline_median": (scores.get(HEADLINE) or {}).get("median"),
            "margin_headline_db": (scores.get(HEADLINE) or {}).get("margin_db"),
            "mean_power_counts2": float(np.mean(power)),
            "power_dbfs": float(10 * np.log10(np.mean(power) / 2048.0 ** 2)),
            "power_dbfs_min": float(10 * np.log10(power.min() / 2048.0 ** 2)),
            "power_dbfs_max": float(10 * np.log10(power.max() / 2048.0 ** 2)),
            "peak_counts_max": float(peak.max()),
            "null_floor_power_counts2": floor,
            "null_floor_dbfs": float(10 * np.log10(floor / 2048.0 ** 2)),
            "snr_db": float(10 * np.log10(max(np.mean(power) - floor, 1e-12) / floor)),
            "reference_power_counts2": reference_power,
            "reference_bandwidth_hz": reference.get("reference_bandwidth_hz"),
            "bandwidth_loss_db": (None if reference_power is None else
                                  float(10 * np.log10(np.mean(power) / reference_power))),
            "coarse": coarse,
            "predicted_digital_edge_hz": (rate - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
            "predicted_analog_edge_hz": (bandwidth - 2 * OCCUPIED_HALF_WIDTH_HZ) / 2,
        })

    audit = [cell for cell in cells
             if cell["sample_rate_readback_hz"] != cell["sample_rate_requested_hz"]
             or cell["rx_bandwidth_readback_hz"] != cell["rx_bandwidth_requested_hz"]]

    return {
        "schema": "leo-tracker.cfo-bandwidth-factorial/v1",
        "source": header,
        "cells": cells,
        # Three cliffs, not one.  The headline statistic is conditioned at
        # coarse-E's epoch and offset (``survey_scoring.CANDIDATE_COARSE``), so
        # its edge is inherited rather than its own; carrying each coarse bank's
        # own edge beside it is what shows the inheritance -- and coarse-A's
        # edge is the one the deployed sky path actually runs into.
        "cliffs": cliffs(cells),
        "cliffs_coarse_A": cliffs(cells, "coarse-A"),
        "cliffs_coarse_E": cliffs(cells, "coarse-E"),
        "cells_run": len(cells),
        "cells_failed": len([r for r in errors if r["kind"] == "cell_error"]),
        "errors": errors,
        "readback_mismatches": [
            {"sample_rate_requested_hz": cell["sample_rate_requested_hz"],
             "sample_rate_readback_hz": cell["sample_rate_readback_hz"],
             "rx_bandwidth_requested_hz": cell["rx_bandwidth_requested_hz"],
             "rx_bandwidth_readback_hz": cell["rx_bandwidth_readback_hz"],
             "cfo_requested_hz": cell["cfo_requested_hz"]} for cell in audit],
        "thresholds": {
            "false_alarm_rate": FALSE_ALARM_RATE,
            "txoff_point_by_rate": {str(key): value
                                    for key, value in limits["txoff_point"].items()},
            "txoff_coarse_by_rate": {str(key): value
                                     for key, value in limits["txoff_coarse"].items()},
            "crossedge_point_by_cell": {str(list(key)): value
                                        for key, value in limits["crossedge_point"].items()},
            "txoff_point_by_cell": {str(list(key)): value
                                    for key, value in limits["txoff_point_by_cell"].items()},
            "txoff_coarse_by_cell": {str(list(key)): value
                                     for key, value in limits["txoff_coarse_by_cell"].items()},
        },
        "detectors": {"point": list(POINT_METHODS), "coarse": list(COARSE_METHODS),
                      "headline": HEADLINE},
        "geometry": {
            "occupied_half_width_hz": OCCUPIED_HALF_WIDTH_HZ,
            "pilot_centre_half_width_hz": PILOT_CENTRE_HALF_WIDTH_HZ,
            "coarse_search_edge_hz": COARSE_SEARCH_EDGE_HZ,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw", nargs="+")
    parser.add_argument("--out", required=True)
    arguments = parser.parse_args()
    rows = []
    for item in arguments.raw:
        rows.extend(read(Path(item)))
    payload = summarise(rows)
    Path(arguments.out).write_text(json.dumps(payload, indent=1))
    print(f"{payload['cells_run']} cells, {payload['cells_failed']} failed, "
          f"{len(payload['readback_mismatches'])} readback mismatches -> "
          f"{arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
