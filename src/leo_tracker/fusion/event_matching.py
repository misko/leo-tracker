"""Rank transient measured Doppler events against a dense TLE pass catalog."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Sequence

import numpy as np


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


@dataclass(frozen=True)
class PassHypothesis:
    name: str
    norad_id: int
    rise_utc: str
    culmination_utc: str
    set_utc: str
    max_elevation_deg: float
    overlap_fraction: float
    fitted_frequency_bias_hz: float
    residual_rms_hz: float
    measured_drift_hz_s: float
    predicted_drift_hz_s: float
    drift_difference_hz_s: float
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def rank_event_against_passes(
    utc_ns: Sequence[int], observed_frequency_hz: Sequence[float], catalog: dict, *,
    residual_scale_hz: float = 50_000, drift_scale_hz_s: float = 1500,
    minimum_overlap_fraction: float = .25, maximum_results: int = 10,
    ambiguity_ratio: float = 1.5,
) -> dict:
    times = np.asarray(utc_ns, np.int64) / 1e9
    observed = np.asarray(observed_frequency_hz, float)
    if times.ndim != 1 or times.size < 3 or observed.shape != times.shape:
        raise ValueError("TLE matching needs at least three timestamps and frequencies")
    if np.any(np.diff(times) <= 0):
        raise ValueError("event timestamps must increase")
    event_duration = max(times[-1] - times[0], 1e-9)
    hypotheses = []
    for satellite in catalog.get("satellites", []):
        for item in satellite.get("passes", []):
            points = [item["rise"], item["culmination"], item["set"]]
            pass_times = np.asarray([_timestamp(point["time"]) for point in points])
            selected = (times >= pass_times[0]) & (times <= pass_times[-1])
            overlap = float(selected.sum() / times.size)
            if selected.sum() < 3 or overlap < minimum_overlap_fraction:
                continue
            expected = np.interp(times[selected], pass_times,
                                 [point["expected_doppler_hz"] for point in points])
            measured = observed[selected]
            bias = float(np.median(measured - expected))
            residual = measured - expected - bias
            residual_rms = float(np.sqrt(np.mean(residual**2)))
            centered = times[selected] - np.mean(times[selected])
            measured_drift = float(np.polyfit(centered, measured, 1)[0])
            predicted_drift = float(np.polyfit(centered, expected, 1)[0])
            drift_difference = abs(measured_drift - predicted_drift)
            elevation = float(item["culmination"]["elevation_deg"])
            score = (overlap * (.5 + .5*min(1, elevation/45)) *
                     math.exp(-residual_rms/residual_scale_hz) *
                     math.exp(-drift_difference/drift_scale_hz_s))
            hypotheses.append(PassHypothesis(
                satellite["name"].strip(), int(satellite["norad_id"]),
                points[0]["time"], points[1]["time"], points[2]["time"], elevation,
                overlap, bias, residual_rms, measured_drift, predicted_drift,
                drift_difference, float(score)))
    hypotheses.sort(key=lambda item: item.score, reverse=True)
    selected = hypotheses[:maximum_results]
    if not selected or selected[0].score < .05:
        classification = "unmatched"
    elif len(selected) > 1 and selected[0].score / max(selected[1].score, 1e-12) < ambiguity_ratio:
        classification = "ambiguous"
    else:
        classification = "ranked-hypothesis"
    return {"classification": classification, "candidate_count": len(hypotheses),
            "ambiguity_ratio": ambiguity_ratio,
            "hypotheses": [item.to_dict() for item in selected],
            "warning": "A ranked hypothesis is not a confirmed satellite identity."}
