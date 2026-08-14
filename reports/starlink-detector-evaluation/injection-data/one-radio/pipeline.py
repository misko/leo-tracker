"""The repository's own scoring path, run on one injected probe.

Nothing here re-implements a detector.  ``search_observation`` proposes,
``distinct_points`` collapses the claims, ``confirm_points`` asks all eight
confirmers about every claimed place under the exact, rolled and opposite-edge
templates.  Those eight are the eight the report ranks:

    anchor-8  differential-16  differential-32  glrt-32  glrt-64
    full-frame-full  full-frame-acquire  full-frame-verify
"""
from __future__ import annotations

import numpy as np

from leo_tracker.radio.beacon import survey_scoring as ss
from leo_tracker.radio.beacon.cross_radio import observation_fires

METHODS = ("anchor-8", "differential-16", "differential-32", "glrt-32",
           "glrt-64", "full-frame-full", "full-frame-acquire",
           "full-frame-verify")

FS_HZ = 5_000_000.0
EDGE = "lower"
NULL_EDGE = "upper"

_BANKS: dict = {}


def banks(edge: str = EDGE, sample_rate_hz: float = FS_HZ) -> dict:
    key = (edge, sample_rate_hz)
    if key not in _BANKS:
        _BANKS[key] = ss._banks(edge, sample_rate_hz)
    return _BANKS[key]


def warm() -> None:
    ss.warm((FS_HZ,))
    banks()


def score_probe(samples: np.ndarray, *, sample_rate_hz: float = FS_HZ,
                edge: str = EDGE, null_edge: str | None = NULL_EDGE) -> dict:
    """One observation, in the shape ``observation_fires`` reads.

    ``points[*]["methods"][m]["score"]``  target-edge score at a claimed place
    ``points[*]["methods"][m]["cross_edge_score"]``  the repository's null
    ``points[*]["methods"][m]["control_score"]``     the wrong-code null
    """
    observed = ss.search_observation(samples, sample_rate_hz, edge=edge,
                                     banks=banks(edge, sample_rate_hz))
    points = ss.distinct_points(observed["certificates"], sample_rate_hz)
    confirmed = ss.confirm_points(samples, sample_rate_hz, points, edge=edge,
                                  null_edge=null_edge)
    slim = []
    for point in confirmed:
        methods = {name: {"score": point["methods"][name].get("score"),
                          "control_score": point["methods"][name].get("control_score"),
                          "cross_edge_score": point["methods"][name].get("cross_edge_score")}
                   for name in METHODS if name in point["methods"]}
        slim.append({"point_id": point["point_id"],
                     "epoch_sample": point["epoch_sample"],
                     "cfo_hz": point["cfo_hz"],
                     "claimed_by": point["claimed_by"],
                     "methods": methods})
    return {"points": slim,
            "coarse": {name: {"peak_to_median": row["peak_to_median"],
                              "epoch_sample": row["epoch_sample"],
                              "frequency_offset_hz": row["frequency_offset_hz"]}
                       for name, row in observed["coarse"].items()},
            "searched": {c["method"]: c["score"] for c in observed["certificates"]}}


def point_values(observations: list[dict], method: str, field: str = "score"
                 ) -> list[float]:
    out = []
    for obs in observations:
        for point in obs["points"]:
            value = (point["methods"].get(method) or {}).get(field)
            if value is not None:
                out.append(float(value))
    return out


def fires(observation: dict, method: str, threshold: float | None) -> bool | None:
    """The repository's own per-cell rule, unmodified."""
    return observation_fires(observation, method, threshold)
