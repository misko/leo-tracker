"""Joint RX0/RX1 association with an explicit independent-LNB LO offset."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

from .events import SpectralEvent


@dataclass(frozen=True)
class JointEvent:
    rx0_index: int
    rx1_index: int
    polarity: str
    broadband: bool
    time_iou: float
    lnb_offset_hz: float
    centered_path_correlation: float
    rx0_drift_hz_s: float
    rx1_drift_hz_s: float
    drift_difference_hz_s: float
    bandwidth_ratio: float
    association_score: float

    def to_dict(self) -> dict:
        return asdict(self)


def _overlap(first: SpectralEvent, second: SpectralEvent):
    start = max(first.start_time_s, second.start_time_s)
    stop = min(first.stop_time_s, second.stop_time_s)
    if stop < start:
        return None
    times0, values0 = np.asarray(first.time_s), np.asarray(first.centroid_hz)
    times1, values1 = np.asarray(second.time_s), np.asarray(second.centroid_hz)
    common = times0[(times0 >= start) & (times0 <= stop)]
    if common.size < 3:
        return None
    return common, values0[(times0 >= start) & (times0 <= stop)], np.interp(common, times1, values1)


def compare_events(first: SpectralEvent, second: SpectralEvent, *,
                   max_drift_difference_hz_s: float = 3000) -> JointEvent | None:
    if first.polarity != second.polarity or first.broadband != second.broadband:
        return None
    overlap = _overlap(first, second)
    if overlap is None:
        return None
    times, path0, path1 = overlap
    intersection = min(first.stop_time_s, second.stop_time_s) - max(first.start_time_s, second.start_time_s)
    union = max(first.stop_time_s, second.stop_time_s) - min(first.start_time_s, second.start_time_s)
    time_iou = float(intersection / union) if union > 0 else 1.0
    offset = float(np.median(path1 - path0))
    centered0, centered1 = path0 - np.mean(path0), path1 - np.mean(path1)
    scale = np.linalg.norm(centered0) * np.linalg.norm(centered1)
    correlation = float(np.dot(centered0, centered1) / scale) if scale > 1e-9 else 1.0
    centered_time = times - np.mean(times)
    drift0 = float(np.polyfit(centered_time, path0, 1)[0])
    drift1 = float(np.polyfit(centered_time, path1, 1)[0])
    drift_difference = abs(drift0 - drift1)
    bandwidth_ratio = float(min(first.median_bandwidth_hz, second.median_bandwidth_hz) /
                            max(first.median_bandwidth_hz, second.median_bandwidth_hz))
    drift_score = max(0.0, 1 - drift_difference / max_drift_difference_hz_s)
    correlation_score = (correlation + 1) / 2
    score = .35*time_iou + .30*correlation_score + .20*drift_score + .15*bandwidth_ratio
    return JointEvent(first.receiver if first.receiver == 0 else second.receiver,
                      second.receiver if second.receiver == 1 else first.receiver,
                      first.polarity, first.broadband, time_iou, offset, correlation,
                      drift0, drift1, drift_difference, bandwidth_ratio, float(score))


def associate_receiver_events(rx0: Sequence[SpectralEvent], rx1: Sequence[SpectralEvent], *,
                              min_score: float = .55) -> list[JointEvent]:
    if not rx0 or not rx1:
        return []
    comparisons: dict[tuple[int, int], JointEvent] = {}
    costs = np.full((len(rx0), len(rx1)), 10.0)
    for first_index, first in enumerate(rx0):
        for second_index, second in enumerate(rx1):
            comparison = compare_events(first, second)
            if comparison is not None:
                comparison = JointEvent(first_index, second_index, comparison.polarity,
                    comparison.broadband, comparison.time_iou, comparison.lnb_offset_hz,
                    comparison.centered_path_correlation, comparison.rx0_drift_hz_s,
                    comparison.rx1_drift_hz_s, comparison.drift_difference_hz_s,
                    comparison.bandwidth_ratio, comparison.association_score)
                comparisons[first_index, second_index] = comparison
                costs[first_index, second_index] = 1 - comparison.association_score
    rows, columns = linear_sum_assignment(costs)
    return sorted((comparisons[row, column] for row, column in zip(rows, columns)
                   if (row, column) in comparisons and comparisons[row, column].association_score >= min_score),
                  key=lambda item: item.association_score, reverse=True)


def estimate_lnb_offset_hz(events: Sequence[JointEvent], *, include_broadband: bool = False) -> float:
    selected = [event for event in events if include_broadband or not event.broadband]
    if not selected:
        raise ValueError("no joint events available for LNB offset estimation")
    values = np.asarray([event.lnb_offset_hz for event in selected])
    weights = np.asarray([event.association_score for event in selected])
    order = np.argsort(values); values, weights = values[order], weights[order]
    return float(values[np.searchsorted(np.cumsum(weights), weights.sum()/2)])
