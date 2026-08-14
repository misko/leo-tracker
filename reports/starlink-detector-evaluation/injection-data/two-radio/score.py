"""Score one captured probe through the repository's own detector path.

search_observation -> distinct_points -> confirm_points is exactly the chain
score_entry runs on corpus probes; nothing here reimplements a statistic.
The eight methods that come out are the eight cross_radio reads.
"""
from __future__ import annotations

import numpy as np

from leo_tracker.radio.beacon import survey_scoring as ss

EDGE = "lower"
NULL_EDGE = "upper"


def banks(sample_rate_hz: float, edge: str = EDGE) -> dict:
    return ss._banks(edge, sample_rate_hz)


def score_probe(samples: np.ndarray, sample_rate_hz: float, bank: dict, *,
                edge: str = EDGE, with_null_edge: bool = False) -> list[dict]:
    """The points-with-methods block the cross_radio estimator consumes."""
    found = ss.search_observation(samples, sample_rate_hz, edge=edge, banks=bank)
    points = ss.distinct_points(found["certificates"], sample_rate_hz)
    return ss.confirm_points(samples, sample_rate_hz, points, edge=edge,
                             null_edge=(NULL_EDGE if with_null_edge else None))


def coarse_peak_median(samples: np.ndarray, bank: dict) -> float:
    """The repo's matched fold statistic, config E."""
    return float(ss.fast_scan.probe(samples, bank[ss.CANDIDATE_COARSE])["peak_to_median"])


def observation(points: list[dict], *, arm: str, instant: int,
                receiver_label: str, receiver: int = 0,
                channel: str = "loopback", region: str = "pilot",
                edge: str = EDGE) -> dict:
    """One radio, one instant, in the shape cross_radio._side reads."""
    return {"arm": arm, "iq_index": int(instant), "receiver": receiver,
            "receiver_label": receiver_label, "channel": channel,
            "region": region, "edge": edge,
            "points": [{"point_id": p["point_id"],
                        "epoch_sample": p["epoch_sample"],
                        "cfo_hz": p["cfo_hz"],
                        "claimed_by": p["claimed_by"],
                        "methods": {name: {"score": block["score"]}
                                    for name, block in p["methods"].items()}}
                       for p in points]}


def method_names(points: list[dict]) -> list[str]:
    return sorted({name for p in points for name in p["methods"]})
