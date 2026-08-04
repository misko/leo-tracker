"""Compact, pass-scale spectrum monitoring and dual-RX motion promotion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import time
from typing import Callable

import numpy as np

from .scout import SpectrumSummary, summarize_samples


@dataclass(frozen=True)
class SpectrumFrame:
    cycle: int
    channel: int
    center_frequency_hz: float
    acquisition_utc_ns: int
    power_dbfs: float
    peak_excess_db: float
    clipped_fraction: float
    bin_width_hz: float
    psd_db: tuple[float, ...]


@dataclass(frozen=True)
class MotionCandidate:
    previous_cycle: int
    current_cycle: int
    center_frequency_hz: float
    shift_hz: float
    rx0_shift_hz: float
    rx1_shift_hz: float
    rx0_correlation: float
    rx1_correlation: float
    channel_disagreement_hz: float
    score: float


@dataclass(frozen=True)
class MonitorResult:
    channels: tuple[int, ...]
    centers_hz: tuple[float, ...]
    cycles: tuple[tuple[SpectrumFrame, ...], ...]
    candidates: tuple[MotionCandidate, ...]


def compact_psd(samples: np.ndarray, *, sample_rate_hz: float, bandwidth_hz: float,
                output_bins: int = 2048, fft_size: int = 8192) -> tuple[np.ndarray, float]:
    """Return an averaged, band-limited PSD without retaining raw IQ."""
    values = np.asarray(samples, dtype=np.complex64)
    if values.ndim != 1 or values.size < 32:
        raise ValueError("compact PSD needs at least 32 complex samples")
    if not 0 < bandwidth_hz <= sample_rate_hz:
        raise ValueError("bandwidth must be positive and no greater than sample rate")
    if output_bins < 16:
        raise ValueError("output_bins must be at least 16")
    size = min(int(fft_size), values.size)
    size = 1 << (size.bit_length() - 1)
    frame_count = values.size // size
    window = np.hanning(size).astype(np.float32)
    accumulator = np.zeros(size, np.float64)
    for index in range(frame_count):
        frame = values[index * size:(index + 1) * size]
        accumulator += np.abs(np.fft.fftshift(np.fft.fft(frame * window))) ** 2
    accumulator /= frame_count
    keep = max(16, int(np.floor(size * bandwidth_hz / sample_rate_hz)))
    keep -= keep % 2
    start = (size - keep) // 2
    band = accumulator[start:start + keep]
    bins = min(output_bins, band.size)
    edges = np.linspace(0, band.size, bins + 1, dtype=int)
    reduced = np.array([np.mean(band[edges[i]:edges[i + 1]]) for i in range(bins)])
    psd_db = 10 * np.log10(reduced + np.finfo(float).tiny)
    return psd_db.astype(np.float32), float(bandwidth_hz / bins)


def spectrum_frame(samples: np.ndarray, *, cycle: int, channel: int,
                   center_frequency_hz: float, acquisition_utc_ns: int,
                   sample_rate_hz: float, bandwidth_hz: float,
                   output_bins: int = 2048, fft_size: int = 8192) -> SpectrumFrame:
    summary: SpectrumSummary = summarize_samples(samples, center_frequency_hz, sample_rate_hz)
    psd, bin_width = compact_psd(samples, sample_rate_hz=sample_rate_hz,
                                 bandwidth_hz=bandwidth_hz, output_bins=output_bins,
                                 fft_size=fft_size)
    return SpectrumFrame(cycle, channel, center_frequency_hz, acquisition_utc_ns,
                         summary.power_dbfs, summary.peak_excess_db,
                         summary.clipped_fraction, bin_width,
                         tuple(float(item) for item in psd))


def estimate_spectral_shift(reference_db: np.ndarray, current_db: np.ndarray, *,
                            bin_width_hz: float, max_shift_hz: float) -> tuple[float, float]:
    """Estimate current-minus-reference frequency displacement by normalized correlation."""
    reference = np.asarray(reference_db, dtype=float)
    current = np.asarray(current_db, dtype=float)
    if reference.ndim != 1 or reference.shape != current.shape or reference.size < 16:
        raise ValueError("spectra must be equal one-dimensional arrays with at least 16 bins")
    if bin_width_hz <= 0 or max_shift_hz <= 0:
        raise ValueError("bin width and maximum shift must be positive")
    max_lag = min(reference.size // 4, int(max_shift_hz // bin_width_hz))
    best: tuple[float, int] | None = None
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            left, right = reference[-lag:], current[:lag]
        elif lag > 0:
            left, right = reference[:-lag], current[lag:]
        else:
            left, right = reference, current
        left = left - np.mean(left); right = right - np.mean(right)
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        correlation = -1.0 if not denominator else float(np.dot(left, right) / denominator)
        if best is None or correlation > best[0]:
            best = (correlation, lag)
    assert best is not None
    return float(best[1] * bin_width_hz), best[0]


def promote_dual_motion(previous: dict[int, SpectrumFrame], current: dict[int, SpectrumFrame], *,
                        min_shift_hz: float = 30_000, max_shift_hz: float = 600_000,
                        min_correlation: float = .55,
                        channel_tolerance_hz: float = 45_000) -> MotionCandidate | None:
    """Promote motion only when both receivers independently measure the same displacement."""
    if set(previous) != {0, 1} or set(current) != {0, 1}:
        return None
    if any(previous[ch].center_frequency_hz != current[ch].center_frequency_hz for ch in (0, 1)):
        raise ValueError("motion comparison requires one common tuning center")
    estimates = {}
    for channel in (0, 1):
        if previous[channel].bin_width_hz != current[channel].bin_width_hz:
            raise ValueError("motion comparison requires equal PSD bin widths")
        estimates[channel] = estimate_spectral_shift(
            np.asarray(previous[channel].psd_db), np.asarray(current[channel].psd_db),
            bin_width_hz=current[channel].bin_width_hz, max_shift_hz=max_shift_hz)
    shift0, corr0 = estimates[0]; shift1, corr1 = estimates[1]
    disagreement = abs(shift0 - shift1)
    if (abs(shift0) < min_shift_hz or abs(shift1) < min_shift_hz
            or corr0 < min_correlation or corr1 < min_correlation
            or disagreement > channel_tolerance_hz):
        return None
    shift = (shift0 + shift1) / 2
    score = min(corr0, corr1) - disagreement / max(channel_tolerance_hz, 1)
    return MotionCandidate(previous[0].cycle, current[0].cycle,
                           current[0].center_frequency_hz, shift, shift0, shift1,
                           corr0, corr1, disagreement, score)


def find_motion_candidates(cycles: tuple[tuple[SpectrumFrame, ...], ...], *,
                           min_shift_hz: float = 30_000, max_shift_hz: float = 600_000,
                           min_correlation: float = .55,
                           channel_tolerance_hz: float = 45_000,
                           max_cycle_lag: int = 1) -> tuple[MotionCandidate, ...]:
    if max_cycle_lag < 1:
        raise ValueError("maximum cycle lag must be at least one")
    candidates: list[MotionCandidate] = []
    lookups = [{(frame.center_frequency_hz, frame.channel): frame for frame in cycle}
               for cycle in cycles]
    for prior_index in range(len(lookups) - 1):
        for current_index in range(prior_index + 1,
                                   min(len(lookups), prior_index + max_cycle_lag + 1)):
            prior, current = lookups[prior_index], lookups[current_index]
            centers = sorted({key[0] for key in prior} & {key[0] for key in current})
            for center in centers:
                candidate = promote_dual_motion(
                    {ch: prior[(center, ch)] for ch in (0, 1)},
                    {ch: current[(center, ch)] for ch in (0, 1)},
                    min_shift_hz=min_shift_hz, max_shift_hz=max_shift_hz,
                    min_correlation=min_correlation, channel_tolerance_hz=channel_tolerance_hz)
                if candidate is not None:
                    candidates.append(candidate)
    return tuple(sorted(candidates, key=lambda item: item.score, reverse=True))


def monitor_channels_pyadi(*, uri: str, centers_hz: list[float], cycles: int,
                           sample_rate_hz: float, bandwidth_hz: float, gain_db: float,
                           samples_per_tuning: int, settle_seconds: float,
                           channels: tuple[int, ...] = (0, 1), output_bins: int = 2048,
                           fft_size: int = 8192, discard_buffers: int = 1,
                           clock_ns: Callable[[], int] = time.time_ns) -> tuple[tuple[SpectrumFrame, ...], ...]:
    """Acquire finite compact monitoring cycles from one persistent AD9361 context."""
    if cycles < 1 or not centers_hz:
        raise ValueError("monitor needs at least one cycle and one center")
    if channels != (0, 1):
        raise ValueError("motion monitor currently requires simultaneous channels 0,1")
    if discard_buffers < 1:
        raise ValueError("at least one post-retune buffer must be discarded")
    adi = importlib.import_module("adi")
    sdr = adi.ad9361(uri=uri.removeprefix("pluto://"))
    acquired: list[tuple[SpectrumFrame, ...]] = []
    try:
        sdr.rx_destroy_buffer(); sdr.sample_rate = round(sample_rate_hz)
        sdr.rx_rf_bandwidth = round(bandwidth_hz); sdr.rx_buffer_size = samples_per_tuning
        sdr.rx_enabled_channels = [0, 1]
        for channel in channels:
            setattr(sdr, f"gain_control_mode_chan{channel}", "manual")
            setattr(sdr, f"rx_hardwaregain_chan{channel}", gain_db)
        for cycle in range(cycles):
            frames: list[SpectrumFrame] = []
            for center in centers_hz:
                sdr.rx_lo = round(center); time.sleep(settle_seconds)
                for _ in range(discard_buffers): sdr.rx()
                raw = np.asarray(sdr.rx())
                if raw.ndim != 2 or raw.shape[0] != 2:
                    raise RuntimeError(f"dual monitor read must return 2xN, got {raw.shape}")
                acquired_ns = clock_ns()
                for channel in channels:
                    frames.append(spectrum_frame(raw[channel], cycle=cycle, channel=channel,
                        center_frequency_hz=center, acquisition_utc_ns=acquired_ns,
                        sample_rate_hz=sample_rate_hz, bandwidth_hz=bandwidth_hz,
                        output_bins=output_bins, fft_size=fft_size))
            acquired.append(tuple(frames))
        return tuple(acquired)
    finally:
        sdr.rx_destroy_buffer()


def write_monitor_report(result: MonitorResult, output_dir: Path, metadata: dict) -> None:
    """Persist compact numeric spectra plus a human-readable candidate summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cycle_count = len(result.cycles); channel_count = len(result.channels)
    center_count = len(result.centers_hz)
    frame_lookup = [{(f.channel, f.center_frequency_hz): f for f in cycle} for cycle in result.cycles]
    first = result.cycles[0][0]; bins = len(first.psd_db)
    psd = np.empty((cycle_count, channel_count, center_count, bins), np.float32)
    power = np.empty((cycle_count, channel_count, center_count), np.float32)
    timestamps = np.empty((cycle_count, channel_count, center_count), np.int64)
    for ci in range(cycle_count):
        for hi, channel in enumerate(result.channels):
            for fi, center in enumerate(result.centers_hz):
                frame = frame_lookup[ci][(channel, center)]
                psd[ci, hi, fi] = frame.psd_db; power[ci, hi, fi] = frame.power_dbfs
                timestamps[ci, hi, fi] = frame.acquisition_utc_ns
    np.savez_compressed(output_dir / "spectra.npz", psd_db=psd, power_dbfs=power,
                        acquisition_utc_ns=timestamps,
                        centers_hz=np.asarray(result.centers_hz),
                        channels=np.asarray(result.channels), bin_width_hz=first.bin_width_hz)
    report = {"schema": "leo-tracker.radio-monitor/v1",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "metadata": metadata, "cycles": cycle_count,
              "channels": list(result.channels), "centers_hz": list(result.centers_hz),
              "bin_width_hz": first.bin_width_hz,
              "candidate_count": len(result.candidates),
              "candidates": [asdict(item) for item in result.candidates],
              "spectra": "spectra.npz"}
    (output_dir / "monitor.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def read_monitor_cycles(report_path: Path) -> tuple[tuple[SpectrumFrame, ...], ...]:
    """Reconstruct compact frames for offline detector reprocessing."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema") != "leo-tracker.radio-monitor/v1":
        raise ValueError("unsupported monitor report schema")
    arrays = np.load(report_path.parent / report["spectra"])
    psd = np.asarray(arrays["psd_db"]); power = np.asarray(arrays["power_dbfs"])
    timestamps = np.asarray(arrays["acquisition_utc_ns"])
    centers = np.asarray(arrays["centers_hz"]); channels = np.asarray(arrays["channels"])
    bin_width = float(arrays["bin_width_hz"])
    if psd.shape[:3] != power.shape or power.shape != timestamps.shape:
        raise ValueError("monitor arrays have inconsistent shapes")
    result = []
    for cycle in range(psd.shape[0]):
        frames = []
        for hi, channel in enumerate(channels):
            for fi, center in enumerate(centers):
                frames.append(SpectrumFrame(cycle, int(channel), float(center),
                    int(timestamps[cycle, hi, fi]), float(power[cycle, hi, fi]),
                    float("nan"), float("nan"), bin_width,
                    tuple(float(item) for item in psd[cycle, hi, fi])))
        result.append(tuple(frames))
    return tuple(result)
