"""Measurement-preserving compact waterfalls and receiver-health telemetry."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np


MEASUREMENT_SCHEMA = "leo-tracker.measurement-waterfall/v2"


@dataclass(frozen=True)
class BlockMeasurement:
    psd_db_raw_per_hz: np.ndarray
    rms_raw: float
    peak_raw: float
    crest_factor_db: float
    clip_fraction: float | None


def measure_block(samples: np.ndarray, sample_rate_hz: float, *, fft_size: int = 16_384,
                  output_bins: int = 4096, adc_full_scale: float | None = None) -> BlockMeasurement:
    """Measure an IQ block without removing its absolute raw-code power scale.

    PSD units are raw complex-sample-code squared per hertz. They are stable for
    gain comparisons but intentionally are not called calibrated dBFS or dBm.
    """
    values = np.asarray(samples, np.complex64)
    if values.ndim != 1 or values.size < 1024:
        raise ValueError("measurement needs at least 1024 one-dimensional complex samples")
    size = min(int(fft_size), values.size)
    size = 1 << (size.bit_length() - 1)
    if output_bins < 64 or output_bins > size or size % output_bins:
        raise ValueError("output bins must divide the FFT size and be at least 64")
    count = values.size // size
    window = np.hanning(size).astype(np.float32)
    scale = sample_rate_hz * float(np.sum(window * window))
    power = np.zeros(size, np.float64)
    for index in range(count):
        frame = values[index * size:(index + 1) * size]
        power += np.abs(np.fft.fftshift(np.fft.fft(frame * window))) ** 2
    power /= count * scale
    width = size // output_bins
    compact = power.reshape(output_bins, width).mean(axis=1)
    psd = 10 * np.log10(compact + np.finfo(float).tiny)
    magnitude = np.abs(values).astype(np.float64)
    rms = float(np.sqrt(np.mean(magnitude * magnitude)))
    peak = float(np.max(magnitude))
    crest = float(20 * np.log10(peak / rms)) if rms > 0 else 0.0
    clipping = None
    if adc_full_scale is not None:
        if adc_full_scale <= 0:
            raise ValueError("ADC full scale must be positive")
        clipping = float(np.mean(
            (np.abs(values.real) >= adc_full_scale) |
            (np.abs(values.imag) >= adc_full_scale)))
    return BlockMeasurement(psd.astype(np.float32), rms, peak, crest, clipping)


def capture_measurement_waterfall(
    blocks: Iterable[tuple[int, Sequence[np.ndarray]] | tuple[int, Sequence[np.ndarray], int | None]], destination: Path, *,
    sample_rate_hz: float, center_frequency_hz: float, bandwidth_hz: float,
    snapshots: int, fft_size: int = 16_384, output_bins: int = 4096,
    samples_per_snapshot: int | None = None, lnb_lo_hz: float | None = None,
    gain_mode: str = "manual", configured_gain_db: float | None = None,
    gain_reader: Callable[[], Sequence[float] | None] | None = None,
    adc_full_scale: float | None = None, identity: dict | None = None,
    psd_quantization_db: float | None = None,
    snapshot_observer: Callable[[int, int, Sequence[np.ndarray], Sequence[np.ndarray],
                                 Sequence[float]], float | None] | None = None,
) -> dict:
    """Capture v2 spectra while preserving absolute power and timing evidence."""
    if snapshots < 2:
        raise ValueError("at least two snapshots are required")
    if gain_mode not in ("manual", "slow_attack", "fast_attack"):
        raise ValueError("unsupported gain mode")
    spectra, rms, peak, crest, clips, utc, gains, read_durations = [], [], [], [], [], [], [], []
    snapshot_centers: list[float] = []
    observer_scores: list[float] = []
    receiver_count = None
    observed_samples = None
    for index, incoming_block in enumerate(blocks):
        if len(incoming_block) == 2:
            utc_ns, incoming = incoming_block
            read_duration_ns = None
        elif len(incoming_block) == 3:
            utc_ns, incoming, read_duration_ns = incoming_block
        elif len(incoming_block) == 4:
            utc_ns, incoming, read_duration_ns, snapshot_center_hz = incoming_block
            snapshot_centers.append(float(snapshot_center_hz))
        else:
            raise ValueError("measurement block must contain UTC, receivers, and optional read duration")
        values = [np.asarray(item, np.complex64) for item in incoming]
        if receiver_count is None:
            receiver_count, observed_samples = len(values), values[0].size
        if not values or len(values) != receiver_count or any(v.size != observed_samples for v in values):
            raise ValueError("receiver count or block size changed during capture")
        measured = [measure_block(v, sample_rate_hz, fft_size=fft_size,
                                  output_bins=output_bins, adc_full_scale=adc_full_scale)
                    for v in values]
        spectra.append([m.psd_db_raw_per_hz for m in measured])
        rms.append([m.rms_raw for m in measured]); peak.append([m.peak_raw for m in measured])
        crest.append([m.crest_factor_db for m in measured])
        clips.append([np.nan if m.clip_fraction is None else m.clip_fraction for m in measured])
        gain = None if gain_reader is None else gain_reader()
        gain_values = ([np.nan] * receiver_count) if gain is None else list(gain)
        gains.append(gain_values)
        if snapshot_observer is not None:
            observer_score = snapshot_observer(index, int(utc_ns), values,
                                               [m.psd_db_raw_per_hz for m in measured],
                                               gain_values)
            observer_scores.append(np.nan if observer_score is None else float(observer_score))
        utc.append(int(utc_ns))
        read_durations.append(np.nan if read_duration_ns is None else int(read_duration_ns))
        if index + 1 >= snapshots:
            break
    if len(spectra) != snapshots:
        raise RuntimeError(f"source ended after {len(spectra)} of {snapshots} snapshots")
    if samples_per_snapshot is not None and samples_per_snapshot != observed_samples:
        raise ValueError("declared samples per snapshot does not match incoming blocks")
    samples_per_snapshot = int(observed_samples)
    spectra_array = np.asarray(spectra, np.float32).transpose(1, 0, 2)
    def receiver_major(values): return np.asarray(values, np.float32).T
    offsets = np.linspace(-sample_rate_hz / 2, sample_rate_hz / 2,
                          output_bins, endpoint=False, dtype=np.float64)
    timestamps = np.asarray(utc, np.int64)
    stored_spectra: np.ndarray = spectra_array
    if psd_quantization_db is not None:
        if not 0 < psd_quantization_db <= .1:
            raise ValueError("PSD quantization must be in (0, 0.1] dB")
        codes = np.rint(spectra_array/psd_quantization_db)
        if not np.all(np.isfinite(codes)) or np.max(np.abs(codes)) > np.iinfo(np.int16).max:
            raise ValueError("PSD values exceed int16 quantization range")
        stored_spectra = codes.astype(np.int16)
    fields = {
        "schema": np.array(MEASUREMENT_SCHEMA), "psd_db_raw_per_hz": stored_spectra,
        "utc_ns": timestamps, "frequency_offsets_hz": offsets,
        "sample_rate_hz": float(sample_rate_hz), "bandwidth_hz": float(bandwidth_hz),
        "center_frequency_hz": float(center_frequency_hz), "fft_size": int(fft_size),
        "samples_per_snapshot": samples_per_snapshot,
        "rms_raw": receiver_major(rms), "peak_raw": receiver_major(peak),
        "crest_factor_db": receiver_major(crest), "clip_fraction": receiver_major(clips),
        "hardware_gain_db": receiver_major(gains), "gain_mode": np.array(gain_mode),
        "configured_gain_db": np.nan if configured_gain_db is None else float(configured_gain_db),
        "identity_json": np.array(json.dumps(identity or {})),
        "read_duration_ns": np.asarray(read_durations, np.float64),
    }
    if snapshot_centers:
        if len(snapshot_centers) != snapshots:
            raise ValueError("center frequency metadata must be supplied for every snapshot")
        fields["center_frequency_hz_by_snapshot"] = np.asarray(snapshot_centers, np.float64)
    if psd_quantization_db is not None:
        fields["psd_db_quantization_db"] = float(psd_quantization_db)
    if lnb_lo_hz is not None:
        fields["lnb_lo_hz"] = float(lnb_lo_hz)
    if snapshot_observer is not None:
        fields["snapshot_observer_score_db"] = np.asarray(observer_scores, np.float32)
    destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(destination, **fields)
    intervals = np.diff(timestamps) / 1e9
    retained_s = snapshots * samples_per_snapshot / sample_rate_hz
    span_s = (timestamps[-1] - timestamps[0]) / 1e9 + samples_per_snapshot / sample_rate_hz
    report = {
        "schema": MEASUREMENT_SCHEMA, "path": str(destination), "snapshots": snapshots,
        "receiver_count": receiver_count, "samples_per_snapshot": samples_per_snapshot,
        "retained_sample_time_s": retained_s, "observation_span_s": span_s,
        "duty_fraction": retained_s / span_s if span_s else 1.0,
        "median_snapshot_interval_s": None if not len(intervals) else float(np.median(intervals)),
        "gain_mode": gain_mode, "configured_gain_db": configured_gain_db,
        "frequency_bin_width_hz": sample_rate_hz/output_bins,
        "psd_quantization_db": psd_quantization_db,
        "first_utc_ns": utc[0], "last_utc_ns": utc[-1], "identity": identity or {},
    }
    if snapshot_centers:
        report["center_frequencies_hz"] = sorted(set(snapshot_centers))
        report["center_exposure_fraction"] = {
            str(center): snapshot_centers.count(center)/len(snapshot_centers)
            for center in sorted(set(snapshot_centers))}
    return report


def load_measurement_waterfall(path: Path) -> dict[str, np.ndarray | float | int | str]:
    with np.load(path, allow_pickle=False) as value:
        schema = str(value["schema"])
        if schema != MEASUREMENT_SCHEMA:
            raise ValueError(f"unsupported measurement schema {schema!r}")
        result = {name: value[name] for name in value.files}
    if "psd_db_quantization_db" in result:
        result["psd_db_raw_per_hz"] = (
            np.asarray(result["psd_db_raw_per_hz"], np.float32)*
            float(result["psd_db_quantization_db"]))
    return result
