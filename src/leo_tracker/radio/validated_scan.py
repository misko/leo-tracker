"""Receive-only center-shift validation of absolute-frequency features."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import time

import numpy as np

# Empirically established on the field Pluto+/LNB chain: a repeatable
# 1.784678 GHz retune transient survived 1 s but disappeared at 3 s for both
# validation-offset directions.  Keep shorter scans available for diagnosis,
# but never allow them to promote a candidate.
PROMOTION_MIN_SETTLE_SECONDS = 3.0
PROMOTION_MIN_CONFIRMATIONS = 2


@dataclass(frozen=True)
class SpectralFeature:
    absolute_frequency_hz: float
    baseband_offset_hz: float
    prominence_db: float
    power_db: float


@dataclass(frozen=True)
class ValidatedFeature:
    absolute_frequency_hz: float
    frequency_difference_hz: float
    primary_prominence_db: float
    shifted_prominence_db: float
    validation_score: float


@dataclass(frozen=True)
class TimedSamples:
    samples: np.ndarray
    acquisition_utc_ns: int


@dataclass(frozen=True)
class ValidatedScanPoint:
    nominal_center_hz: float
    primary_center_hz: float
    shifted_center_hz: float
    primary_acquisition_utc_ns: int
    shifted_acquisition_utc_ns: int
    acquisition_midpoint_utc_ns: int
    acquisition_delta_ns: int
    overlap_low_hz: float
    overlap_high_hz: float
    primary_features: tuple[SpectralFeature, ...]
    shifted_features: tuple[SpectralFeature, ...]
    validated_features: tuple[ValidatedFeature, ...]
    validation_flag: bool


def _features(samples, center_hz: float, sample_rate_hz: float, fft_size: int,
              overlap: tuple[float, float], min_prominence_db: float,
              max_features: int) -> tuple[SpectralFeature, ...]:
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1 or values.size < fft_size: raise ValueError("acquisition returned too few samples")
    # Average non-overlapping spectra without retaining a spectrogram.
    count = values.size // fft_size; window = np.hanning(fft_size).astype(np.float32)
    power = np.zeros(fft_size, np.float64)
    for index in range(count):
        frame = values[index*fft_size:(index+1)*fft_size]
        power += np.abs(np.fft.fftshift(np.fft.fft(frame*window)))**2
    power /= count
    offsets = np.fft.fftshift(np.fft.fftfreq(fft_size, 1/sample_rate_hz)); absolute = center_hz+offsets
    allowed = np.flatnonzero((absolute >= overlap[0]) & (absolute <= overlap[1]))
    allowed = allowed[(allowed > 0) & (allowed < fft_size-1)]
    if allowed.size < 3: raise ValueError("primary/shifted overlap contains too few FFT bins")
    background = float(np.median(power[allowed])) + np.finfo(float).tiny
    local = allowed[(power[allowed] >= power[allowed-1]) & (power[allowed] > power[allowed+1])]
    prominence = 10*np.log10(power[local]/background)
    local = local[prominence >= min_prominence_db]; prominence = prominence[prominence >= min_prominence_db]
    if local.size > max_features:
        keep = np.argpartition(prominence, -max_features)[-max_features:]
        local, prominence = local[keep], prominence[keep]
    order = np.argsort(absolute[local])
    return tuple(SpectralFeature(float(absolute[index]), float(offsets[index]), float(prominence[item]),
        float(10*np.log10(power[index]+np.finfo(float).tiny))) for item, index in zip(order, local[order]))


def validated_scan(acquire: Callable[[float], np.ndarray], *, nominal_centers_hz: Sequence[float],
                   validation_offset_hz: float, sample_rate_hz: float, fft_size: int = 16384,
                   min_prominence_db: float = 12, frequency_tolerance_hz: float = 2_000,
                   max_features: int = 16, clock_ns: Callable[[], int] = time.time_ns) -> list[ValidatedScanPoint]:
    """Acquire primary/shifted views and retain features fixed in absolute RF."""
    if not nominal_centers_hz or validation_offset_hz == 0 or fft_size < 32:
        raise ValueError("centers, nonzero validation offset, and valid FFT are required")
    if abs(validation_offset_hz) >= sample_rate_hz or frequency_tolerance_hz <= 0:
        raise ValueError("validation offset/tolerance is incompatible with sample rate")
    points = []
    def timed_acquire(center: float) -> TimedSamples:
        before = clock_ns(); result = acquire(center); after = clock_ns()
        if isinstance(result, TimedSamples): return result
        return TimedSamples(np.asarray(result), (before+after)//2)
    for nominal in nominal_centers_hz:
        primary_center, shifted_center = float(nominal), float(nominal+validation_offset_hz)
        overlap = (max(primary_center-sample_rate_hz/2, shifted_center-sample_rate_hz/2),
                   min(primary_center+sample_rate_hz/2, shifted_center+sample_rate_hz/2))
        primary_acquisition = timed_acquire(primary_center)
        shifted_acquisition = timed_acquire(shifted_center)
        primary = _features(primary_acquisition.samples, primary_center, sample_rate_hz, fft_size,
                            overlap, min_prominence_db, max_features)
        shifted = _features(shifted_acquisition.samples, shifted_center, sample_rate_hz, fft_size,
                            overlap, min_prominence_db, max_features)
        matches = []
        used: set[int] = set()
        for first in primary:
            candidates = [(abs(first.absolute_frequency_hz-other.absolute_frequency_hz), i, other)
                          for i, other in enumerate(shifted) if i not in used]
            if not candidates: continue
            difference, index, second = min(candidates)
            if difference <= frequency_tolerance_hz:
                used.add(index)
                score = min(first.prominence_db, second.prominence_db) * (1-difference/frequency_tolerance_hz)
                matches.append(ValidatedFeature((first.absolute_frequency_hz+second.absolute_frequency_hz)/2,
                    difference, first.prominence_db, second.prominence_db, score))
        matches.sort(key=lambda value: value.validation_score, reverse=True)
        primary_utc, shifted_utc = primary_acquisition.acquisition_utc_ns, shifted_acquisition.acquisition_utc_ns
        points.append(ValidatedScanPoint(float(nominal), primary_center, shifted_center,
            primary_utc, shifted_utc, (primary_utc+shifted_utc)//2, shifted_utc-primary_utc, *overlap,
            primary, shifted, tuple(matches), bool(matches)))
    return points


def validated_scan_pyadi(*, uri: str, nominal_centers_hz: Sequence[float], validation_offset_hz: float,
                         sample_rate_hz: float, bandwidth_hz: float, gain_db: float,
                         samples_per_tuning: int, settle_seconds: float = .05, channel: int = 0,
                         **analysis) -> list[ValidatedScanPoint]:
    """Receive-only IIO acquisition adapter; each tuning discards its first refill."""
    adi = importlib.import_module("adi"); sdr = adi.ad9361(uri=uri.removeprefix("pluto://"))
    try:
        sdr.rx_destroy_buffer(); sdr.sample_rate = round(sample_rate_hz)
        sdr.rx_rf_bandwidth = round(bandwidth_hz); sdr.rx_buffer_size = samples_per_tuning
        sdr.rx_enabled_channels = [channel]; setattr(sdr, f"gain_control_mode_chan{channel}", "manual")
        setattr(sdr, f"rx_hardwaregain_chan{channel}", gain_db)
        def acquire(center):
            sdr.rx_lo = round(center); time.sleep(settle_seconds); sdr.rx()
            before = time.time_ns(); values = np.asarray(sdr.rx(), np.complex64); after = time.time_ns()
            return TimedSamples(values, (before+after)//2)
        return validated_scan(acquire, nominal_centers_hz=nominal_centers_hz,
            validation_offset_hz=validation_offset_hz, sample_rate_hz=sample_rate_hz, **analysis)
    finally: sdr.rx_destroy_buffer()


def write_validated_scan(points: Sequence[ValidatedScanPoint], output_dir: Path, metadata: dict) -> None:
    import matplotlib
    matplotlib.use("Agg"); import matplotlib.pyplot as plt
    output_dir.mkdir(parents=True, exist_ok=True)
    acquisition_times = [value for point in points for value in
                         (point.primary_acquisition_utc_ns, point.shifted_acquisition_utc_ns)]
    settle_seconds = float(metadata.get("settle_seconds", 0.0))
    confirmations = int(metadata.get("confirmations", 1))
    promotion_reasons = []
    if settle_seconds < PROMOTION_MIN_SETTLE_SECONDS:
        promotion_reasons.append(
            f"settle_seconds={settle_seconds:g} is below promotion minimum {PROMOTION_MIN_SETTLE_SECONDS:g}; retune artifacts may persist")
    if confirmations < PROMOTION_MIN_CONFIRMATIONS:
        promotion_reasons.append(
            f"confirmations={confirmations} is below promotion minimum {PROMOTION_MIN_CONFIRMATIONS}; sequence-dependent retune artifacts may persist")
    promotion_grade = not promotion_reasons
    confirmed = confirmed_features(points, confirmations=confirmations,
                                   frequency_tolerance_hz=float(metadata.get("frequency_tolerance_hz", 2_000)))
    payload = {"schema": "leo-tracker.validated-if-scan/v3", "created_utc": datetime.now(timezone.utc).isoformat(),
               "scan_start_utc_ns": min(acquisition_times) if acquisition_times else None,
               "scan_end_utc_ns": max(acquisition_times) if acquisition_times else None,
               "promotion_grade": promotion_grade, "promotion_reasons": promotion_reasons,
               "promotion_min_settle_seconds": PROMOTION_MIN_SETTLE_SECONDS,
               "promotion_min_confirmations": PROMOTION_MIN_CONFIRMATIONS,
               "metadata": metadata, "points": [asdict(point) for point in points],
               "validated_feature_count": sum(len(point.validated_features) for point in points),
               "confirmed_features": confirmed, "confirmed_feature_count": len(confirmed)}
    (output_dir/"validated_scan.json").write_text(json.dumps(payload, indent=2, sort_keys=True)+"\n")
    fig, axis = plt.subplots(figsize=(10, 5), constrained_layout=True)
    for i, point in enumerate(points):
        axis.scatter([x.absolute_frequency_hz/1e6 for x in point.primary_features],
                     [x.prominence_db for x in point.primary_features], marker="o", color="tab:blue",
                     label="primary" if i == 0 else None)
        axis.scatter([x.absolute_frequency_hz/1e6 for x in point.shifted_features],
                     [x.prominence_db for x in point.shifted_features], marker="x", color="tab:orange",
                     label="shifted" if i == 0 else None)
        axis.scatter([x.absolute_frequency_hz/1e6 for x in point.validated_features],
                     [min(x.primary_prominence_db,x.shifted_prominence_db) for x in point.validated_features],
                     marker="*", s=100, color="tab:green", label="validated" if i == 0 else None)
    axis.set(xlabel="Absolute frequency (MHz)", ylabel="Prominence (dB)",
             title="Center-shift validated receive-only IF scan")
    if not promotion_grade:
        fig.text(.5, .01, "DIAGNOSTIC ONLY — settle time below promotion-grade minimum",
                 ha="center", color="tab:red", weight="bold")
    axis.grid(alpha=.25); axis.legend(); fig.savefig(output_dir/"validated_scan.png", dpi=160); plt.close(fig)


def confirmed_features(points: Sequence[ValidatedScanPoint], *, confirmations: int,
                       frequency_tolerance_hz: float) -> list[dict]:
    """Require a feature at one nominal center to survive consecutive validation pairs."""
    if confirmations < 1:
        raise ValueError("confirmations must be at least 1")
    confirmed: list[dict] = []
    for start in range(0, len(points), confirmations):
        group = points[start:start+confirmations]
        if len(group) != confirmations or len({p.nominal_center_hz for p in group}) != 1:
            continue
        for seed in group[0].validated_features:
            matches = [seed]
            for point in group[1:]:
                candidates = sorted(point.validated_features,
                    key=lambda feature: abs(feature.absolute_frequency_hz-seed.absolute_frequency_hz))
                if not candidates or abs(candidates[0].absolute_frequency_hz-seed.absolute_frequency_hz) > frequency_tolerance_hz:
                    break
                matches.append(candidates[0])
            if len(matches) == confirmations:
                confirmed.append({
                    "nominal_center_hz": group[0].nominal_center_hz,
                    "absolute_frequency_hz": float(np.median([x.absolute_frequency_hz for x in matches])),
                    "validation_score": min(x.validation_score for x in matches),
                    "confirmations": confirmations,
                    "acquisition_midpoint_utc_ns": [p.acquisition_midpoint_utc_ns for p in group],
                })
    return sorted(confirmed, key=lambda item: item["validation_score"], reverse=True)
