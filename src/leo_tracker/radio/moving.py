"""Blind, memory-bounded detection of moving narrowband spectral energy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import tempfile
from typing import Iterator

import numpy as np


@dataclass(frozen=True)
class MovingRidgePoint:
    time_s: float
    frequency_hz: float
    excess_db: float
    z_score: float


@dataclass(frozen=True)
class MovingRidgeResult:
    points: tuple[MovingRidgePoint, ...]
    frame_count: int
    fft_size: int
    hop_size: int
    median_excess_db: float
    median_z_score: float
    frequency_span_hz: float
    fitted_drift_hz_s: float
    moving_score: float
    stationary_spurs_hz: tuple[float, ...]


@dataclass(frozen=True)
class CombPoint:
    time_s: float
    center_frequency_hz: float
    comb_z_score: float
    positive_tone_fraction: float


@dataclass(frozen=True)
class MovingCombResult:
    points: tuple[CombPoint, ...]
    tone_count: int
    tone_spacing_hz: float
    integration_s: float
    spectra_per_integration: int
    median_comb_z_score: float
    median_positive_tone_fraction: float
    frequency_span_hz: float
    fitted_drift_hz_s: float
    score_percentile: float


def _frames(iq: np.memmap, fft_size: int, hop_size: int) -> Iterator[tuple[int, np.ndarray]]:
    window = np.hanning(fft_size).astype(np.float32)
    for start in range(0, iq.size - fft_size + 1, hop_size):
        values = np.asarray(iq[start:start + fft_size], dtype=np.complex64)
        power = np.abs(np.fft.fftshift(np.fft.fft(values * window))) ** 2
        yield start, (10 * np.log10(power + 1e-20)).astype(np.float32)


def detect_moving_ridge(iq_path: str | Path, sample_rate_hz: float, *, fft_size: int = 8192,
                        hop_size: int = 262144, search_hz: tuple[float, float] | None = None,
                        candidates_per_frame: int = 24, max_step_hz: float = 12_000,
                        spur_count: int = 16) -> MovingRidgeResult:
    """Remove the time-stationary spectrum, then Viterbi-track residual peaks.

    IQ is memory mapped and visited twice. Memory is O(FFT bins + frames*candidates),
    independent of capture sample count. No ephemeris or expected curve is consumed.
    """
    path = Path(iq_path)
    if path.stat().st_size % np.dtype("<c8").itemsize:
        raise ValueError("IQ file size is not a whole number of complex64 samples")
    iq = np.memmap(path, dtype="<c8", mode="r")
    if fft_size < 32 or hop_size <= 0 or iq.size < fft_size or candidates_per_frame < 2:
        raise ValueError("invalid detector dimensions")
    frequencies = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    selected = np.ones(fft_size, bool)
    if search_hz is not None:
        selected &= (frequencies >= search_hz[0]) & (frequencies <= search_hz[1])
    selected[:1] = False; selected[-1:] = False
    bins = np.flatnonzero(selected)
    if bins.size < candidates_per_frame: raise ValueError("search range is too narrow")

    # Welford statistics form the stationary receiver/LNB fingerprint.
    mean = np.zeros(fft_size, np.float64); m2 = np.zeros(fft_size, np.float64); count = 0
    for _, spectrum in _frames(iq, fft_size, hop_size):
        count += 1
        delta = spectrum - mean; mean += delta / count; m2 += delta * (spectrum - mean)
    scale = np.sqrt(m2 / max(count - 1, 1))
    scale = np.maximum(scale, 1.0)  # avoid magnifying numerical jitter on strong fixed spurs
    spur_bins = bins[np.argpartition(mean[bins], -min(spur_count, bins.size))[-min(spur_count, bins.size):]]
    spur_bins = spur_bins[np.argsort(mean[spur_bins])[::-1]]

    # Full-band Viterbi linkage avoids candidate-starvation jumps. Backpointers live
    # in a temporary memmap, keeping resident memory independent of capture length.
    max_bins = max(1, int(np.ceil(max_step_hz / (sample_rate_hz / fft_size))))
    parent_dtype = np.int16 if bins.size < np.iinfo(np.int16).max else np.int32
    with tempfile.TemporaryDirectory(prefix="leo-radio-viterbi-") as temporary:
        parents = np.memmap(Path(temporary) / "parents.bin", dtype=parent_dtype, mode="w+",
                            shape=(count, bins.size))
        scores = None
        for frame, (_, spectrum) in enumerate(_frames(iq, fft_size, hop_size)):
            z = ((spectrum - mean) / scale)[bins].astype(np.float64)
            if scores is None:
                scores = z; parents[frame] = np.arange(bins.size, dtype=parent_dtype); continue
            best = np.full(bins.size, -np.inf); best_parent = np.zeros(bins.size, parent_dtype)
            for shift in range(-max_bins, max_bins + 1):
                if shift < 0: current, previous = slice(0, shift), slice(-shift, None)
                elif shift > 0: current, previous = slice(shift, None), slice(0, -shift)
                else: current = previous = slice(None)
                proposed = scores[previous] - .15 * abs(shift) / max_bins
                improve = proposed > best[current]
                current_indexes = np.arange(bins.size)[current]
                previous_indexes = np.arange(bins.size)[previous]
                best[current_indexes[improve]] = proposed[improve]
                best_parent[current_indexes[improve]] = previous_indexes[improve]
            scores = best + z
            parents[frame] = best_parent
        indexes = np.empty(count, np.int32); indexes[-1] = int(np.argmax(scores))
        for frame in range(count - 1, 0, -1): indexes[frame - 1] = parents[frame, indexes[frame]]
        tracked_bins = bins[indexes]
        point_values: list[MovingRidgePoint] = []
        for frame, (start, spectrum) in enumerate(_frames(iq, fft_size, hop_size)):
            index = tracked_bins[frame]
            excess = float(spectrum[index] - mean[index]); z = float(excess / scale[index])
            point_values.append(MovingRidgePoint((start + fft_size / 2) / sample_rate_hz,
                                                  float(frequencies[index]), excess, z))
        points = tuple(point_values)
    times = np.array([p.time_s for p in points]); tracked_f = np.array([p.frequency_hz for p in points])
    excesses = np.array([p.excess_db for p in points]); zs = np.array([p.z_score for p in points])
    drift = float(np.polyfit(times, tracked_f, 1)[0]) if count > 1 else 0.0
    span = float(np.percentile(tracked_f, 95) - np.percentile(tracked_f, 5))
    # Dimensionless evidence combines transient contrast and actual movement. A
    # fixed spur has near-zero excess and span after fingerprint subtraction.
    movement_bins = span / (sample_rate_hz / fft_size)
    moving_score = float(np.median(zs) * np.log1p(max(0.0, movement_bins)))
    return MovingRidgeResult(points, count, fft_size, hop_size, float(np.median(excesses)),
                             float(np.median(zs)), span, drift, moving_score,
                             tuple(float(frequencies[i]) for i in spur_bins))


def detect_moving_comb(iq_path: str | Path, sample_rate_hz: float, *, fft_size: int = 8192,
                       integration_s: float = 1.0, spectra_per_integration: int = 24,
                       tone_count: int = 9, tone_spacing_hz: float = 43_949.5,
                       search_hz: tuple[float, float] = (-500_000, 500_000),
                       max_drift_hz_s: float = 12_000) -> MovingCombResult:
    """Continuity-track a common Doppler shift shared by an odd CW tone comb.

    PSDs are averaged within independent integrations, then each frequency bin's
    stationary median is removed. Only the documented radio comb structure is
    used; no orbit state or expected Doppler curve enters the score.
    """
    if tone_count < 3 or tone_count % 2 != 1 or integration_s <= 0 or spectra_per_integration < 1:
        raise ValueError("tone_count must be odd >=3 and integration settings positive")
    iq = np.memmap(Path(iq_path), dtype="<c8", mode="r")
    integration_samples = round(integration_s * sample_rate_hz)
    frame_count = iq.size // integration_samples
    if frame_count < 3 or fft_size > integration_samples:
        raise ValueError("capture has fewer than three usable integrations")
    frequencies = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    window = np.hanning(fft_size).astype(np.float32)
    with tempfile.TemporaryDirectory(prefix="leo-radio-comb-") as temporary:
        spectra = np.memmap(Path(temporary) / "spectra.bin", dtype=np.float32, mode="w+",
                            shape=(frame_count, fft_size))
        offsets = np.linspace(0, integration_samples - fft_size, spectra_per_integration, dtype=np.int64)
        for frame in range(frame_count):
            total = np.zeros(fft_size, np.float64); base = frame * integration_samples
            for offset in offsets:
                values = np.asarray(iq[base + offset:base + offset + fft_size], np.complex64)
                total += np.abs(np.fft.fftshift(np.fft.fft(values * window))) ** 2
            spectra[frame] = 10 * np.log10(total / spectra_per_integration + 1e-20)
        baseline = np.median(spectra, axis=0)
        mad = np.median(np.abs(spectra - baseline), axis=0)
        scale = np.maximum(1.4826 * mad, .5)
        half = tone_count // 2
        tone_offsets = np.rint(np.arange(-half, half + 1) * tone_spacing_hz / (sample_rate_hz / fft_size)).astype(int)
        low_guard, high_guard = -tone_offsets.min(), fft_size - tone_offsets.max()
        centers = np.arange(low_guard, high_guard)
        centers = centers[(frequencies[centers] >= search_hz[0]) & (frequencies[centers] <= search_hz[1])]
        if centers.size < 3: raise ValueError("comb search range is too narrow")
        scores_by_time = np.empty((frame_count, centers.size), np.float32)
        positive_by_time = np.empty_like(scores_by_time)
        for frame in range(frame_count):
            residual = (spectra[frame] - baseline) / scale
            tones = residual[centers[:, None] + tone_offsets[None, :]]
            scores_by_time[frame] = np.sum(np.clip(tones, -3, 8), axis=1) / np.sqrt(tone_count)
            positive_by_time[frame] = np.mean(tones > 1.0, axis=1)
        max_bins = max(1, int(np.ceil(max_drift_hz_s * integration_s / (sample_rate_hz / fft_size))))
        score = scores_by_time[0].astype(float); parents = np.empty((frame_count, centers.size), np.int32)
        parents[0] = np.arange(centers.size)
        for frame in range(1, frame_count):
            best = np.full(centers.size, -np.inf); parent = np.zeros(centers.size, np.int32)
            for shift in range(-max_bins, max_bins + 1):
                if shift < 0: current, previous = slice(0, shift), slice(-shift, None)
                elif shift > 0: current, previous = slice(shift, None), slice(0, -shift)
                else: current = previous = slice(None)
                proposed = score[previous] - .1 * abs(shift) / max_bins
                improve = proposed > best[current]
                ci, pi = np.arange(centers.size)[current], np.arange(centers.size)[previous]
                best[ci[improve]], parent[ci[improve]] = proposed[improve], pi[improve]
            score = best + scores_by_time[frame]; parents[frame] = parent
        track = np.empty(frame_count, np.int32); track[-1] = np.argmax(score)
        for frame in range(frame_count - 1, 0, -1): track[frame - 1] = parents[frame, track[frame]]
        times = (np.arange(frame_count) + .5) * integration_s
        tracked_f = frequencies[centers[track]]
        tracked_scores = scores_by_time[np.arange(frame_count), track]
        tracked_positive = positive_by_time[np.arange(frame_count), track]
        # Empirical percentile against all time/frequency hypotheses. This is a
        # ranking diagnostic, not a calibrated false-alarm probability.
        percentile = float(np.mean(scores_by_time <= np.median(tracked_scores)) * 100)
        points = tuple(CombPoint(float(times[i]), float(tracked_f[i]), float(tracked_scores[i]),
                                 float(tracked_positive[i])) for i in range(frame_count))
        drift = float(np.polyfit(times, tracked_f, 1)[0])
        span = float(np.percentile(tracked_f, 95) - np.percentile(tracked_f, 5))
        return MovingCombResult(points, tone_count, tone_spacing_hz, integration_s,
                                spectra_per_integration, float(np.median(tracked_scores)),
                                float(np.median(tracked_positive)), span, drift, percentile)
