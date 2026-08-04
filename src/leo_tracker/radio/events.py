"""Transient positive/negative spectral-event segmentation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Sequence

import numpy as np
from scipy import ndimage


@dataclass(frozen=True)
class SpectralEvent:
    receiver: int
    polarity: str
    start_index: int
    stop_index: int
    start_time_s: float
    stop_time_s: float
    duration_s: float
    frequency_low_hz: float
    frequency_high_hz: float
    median_bandwidth_hz: float
    peak_residual_db: float
    occupancy_fraction: float
    broadband: bool
    time_s: tuple[float, ...]
    centroid_hz: tuple[float, ...]
    lower_edge_hz: tuple[float, ...]
    upper_edge_hz: tuple[float, ...]
    support_pixels: int

    def to_dict(self) -> dict:
        return asdict(self)


def residual_waterfall(psd_db: np.ndarray) -> np.ndarray:
    """Subtract only the per-frequency temporal baseline, preserving polarity."""
    values = np.asarray(psd_db, float)
    if values.ndim != 2 or min(values.shape) < 2:
        raise ValueError("waterfall must be two-dimensional with time and frequency")
    return values - np.median(values, axis=0, keepdims=True)


def _component_event(component: np.ndarray, residual: np.ndarray, times_s: np.ndarray,
                     frequencies_hz: np.ndarray, *, receiver: int, polarity: str,
                     broadband_fraction: float, row_offset: int = 0,
                     total_frequency_bins: int | None = None) -> SpectralEvent:
    rows, columns = np.nonzero(component)
    start, stop = int(rows.min()), int(rows.max())
    event_times, centroids, lowers, uppers, widths = [], [], [], [], []
    signed = residual if polarity == "positive" else -residual
    for row in range(start, stop + 1):
        selected = np.flatnonzero(component[row])
        if selected.size == 0:
            continue
        weights = np.maximum(signed[row, selected], np.finfo(float).eps)
        event_times.append(float(times_s[row]))
        centroids.append(float(np.average(frequencies_hz[selected], weights=weights)))
        lowers.append(float(frequencies_hz[selected[0]]))
        uppers.append(float(frequencies_hz[selected[-1]]))
        widths.append(float(frequencies_hz[selected[-1]] - frequencies_hz[selected[0]] +
                            abs(frequencies_hz[1] - frequencies_hz[0])))
    denominator = total_frequency_bins or component.shape[1]
    occupancy = float(np.median(np.sum(component[start:stop + 1], axis=1)) / denominator)
    return SpectralEvent(
        receiver=receiver, polarity=polarity, start_index=start+row_offset, stop_index=stop+row_offset,
        start_time_s=float(times_s[start]), stop_time_s=float(times_s[stop]),
        duration_s=float(times_s[stop] - times_s[start]),
        frequency_low_hz=float(frequencies_hz[columns.min()]),
        frequency_high_hz=float(frequencies_hz[columns.max()]),
        median_bandwidth_hz=float(np.median(widths)),
        peak_residual_db=float(np.max(signed[component])),
        occupancy_fraction=occupancy, broadband=occupancy >= broadband_fraction,
        time_s=tuple(event_times), centroid_hz=tuple(centroids),
        lower_edge_hz=tuple(lowers), upper_edge_hz=tuple(uppers),
        support_pixels=int(component.sum()))


def detect_spectral_events(
    psd_db: np.ndarray, times_s: Sequence[float], frequencies_hz: Sequence[float], *,
    receiver: int = 0, threshold_db: float = .35, min_time_bins: int = 3,
    min_frequency_bins: int = 3, min_support_pixels: int = 12,
    broadband_fraction: float = .65, smoothing_sigma: tuple[float, float] = (1.0, 1.0),
) -> list[SpectralEvent]:
    """Segment finite-duration spectral events of either residual polarity."""
    values = np.asarray(psd_db, float)
    times = np.asarray(times_s, float); frequencies = np.asarray(frequencies_hz, float)
    if values.shape != (times.size, frequencies.size):
        raise ValueError("waterfall shape must match time and frequency axes")
    if threshold_db <= 0 or min_time_bins < 1 or min_frequency_bins < 1:
        raise ValueError("threshold and minimum extents must be positive")
    if not 0 < broadband_fraction <= 1:
        raise ValueError("broadband fraction must be in (0, 1]")
    residual = residual_waterfall(values)
    smoothed = ndimage.gaussian_filter(residual, smoothing_sigma, mode="nearest")
    events: list[SpectralEvent] = []
    structure = np.ones((3, 3), bool)
    for polarity, mask in (("positive", smoothed >= threshold_db),
                           ("negative", smoothed <= -threshold_db)):
        mask = ndimage.binary_closing(mask, structure=np.ones((2, 2), bool))
        labels, count = ndimage.label(mask, structure=structure)
        counts = np.bincount(labels.ravel(), minlength=count+1)
        objects = ndimage.find_objects(labels)
        for label_index, bounds in enumerate(objects, start=1):
            if bounds is None or counts[label_index] < min_support_pixels:
                continue
            row_slice, frequency_slice = bounds
            if ((row_slice.stop-row_slice.start) < min_time_bins or
                    (frequency_slice.stop-frequency_slice.start) < min_frequency_bins):
                continue
            component = labels[bounds] == label_index
            events.append(_component_event(component, residual[bounds], times[row_slice],
                                           frequencies[frequency_slice],
                                           receiver=receiver, polarity=polarity,
                                           broadband_fraction=broadband_fraction,
                                           row_offset=row_slice.start,
                                           total_frequency_bins=frequencies.size))
    return sorted(events, key=lambda event: (event.start_time_s, event.frequency_low_hz,
                                             event.polarity))


def detect_receiver_events(psd_db: np.ndarray, times_s: Sequence[float],
                           frequencies_hz: Sequence[float], **kwargs) -> list[list[SpectralEvent]]:
    values = np.asarray(psd_db, float)
    if values.ndim != 3:
        raise ValueError("receiver waterfall must be receiver x time x frequency")
    return [detect_spectral_events(values[index], times_s, frequencies_hz,
                                   receiver=index, **kwargs)
            for index in range(values.shape[0])]
