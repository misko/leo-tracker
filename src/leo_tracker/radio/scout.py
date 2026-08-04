"""Receive-only spectrum scouting and front-end health metrics."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import time

import numpy as np


@dataclass(frozen=True)
class SpectrumSummary:
    center_frequency_hz: float
    power_dbfs: float
    peak_excess_db: float
    clipped_fraction: float
    peak_offset_hz: float | None = None
    peak_frequency_hz: float | None = None


def summarize_samples(samples: np.ndarray, center_frequency_hz: float,
                      sample_rate_hz: float | None = None) -> SpectrumSummary:
    values = np.asarray(samples, dtype=np.complex64)
    if values.ndim != 1 or values.size < 32:
        raise ValueError("spectrum summary needs at least 32 complex samples")
    adc_power = np.mean(np.abs(values) ** 2)
    full_scale_power = 2.0 * 2048.0**2
    power_dbfs = float(10 * np.log10(max(adc_power, np.finfo(float).tiny) / full_scale_power))
    window = np.hanning(values.size)
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(values * window))) ** 2
    peak_excess = float(10 * np.log10(
        max(float(np.max(spectrum)), np.finfo(float).tiny)
        / max(float(np.median(spectrum)), np.finfo(float).tiny)
    ))
    clipped = float(np.mean(
        (np.abs(values.real) >= 2047) | (np.abs(values.imag) >= 2047)
    ))
    peak_index = int(np.argmax(spectrum))
    peak_offset = None if sample_rate_hz is None else float(
        np.fft.fftshift(np.fft.fftfreq(values.size, 1 / sample_rate_hz))[peak_index]
    )
    peak_frequency = None if peak_offset is None else center_frequency_hz + peak_offset
    return SpectrumSummary(center_frequency_hz, power_dbfs, peak_excess, clipped,
                           peak_offset, peak_frequency)


def scan_channels_pyadi(*, uri: str, frequencies_hz: list[float], sample_rate_hz: float,
                        bandwidth_hz: float, gain_db: float, samples_per_frequency: int,
                        settle_seconds: float = 0.05,
                        channels: tuple[int, ...] = (0,)) -> dict[int, list[SpectrumSummary]]:
    """Sweep one or both AD9361 receivers from a single synchronized read path."""
    if not frequencies_hz or samples_per_frequency < 32:
        raise ValueError("frequencies and samples_per_frequency are required")
    if not channels or len(set(channels)) != len(channels) or any(c not in (0, 1) for c in channels):
        raise ValueError("channels must contain channel 0, channel 1, or both exactly once")
    adi = importlib.import_module("adi")
    sdr = adi.ad9361(uri=uri.removeprefix("pluto://"))
    try:
        sdr.rx_destroy_buffer()
        sdr.sample_rate = round(sample_rate_hz)
        sdr.rx_rf_bandwidth = round(bandwidth_hz)
        sdr.rx_buffer_size = samples_per_frequency
        sdr.rx_enabled_channels = list(channels)
        for channel in channels:
            setattr(sdr, f"gain_control_mode_chan{channel}", "manual")
            setattr(sdr, f"rx_hardwaregain_chan{channel}", gain_db)
        result: dict[int, list[SpectrumSummary]] = {channel: [] for channel in channels}
        for frequency in frequencies_hz:
            sdr.rx_lo = round(frequency)
            time.sleep(settle_seconds)
            # First refill can contain samples from before the LO settled.
            sdr.rx()
            raw = sdr.rx()
            if len(channels) == 1:
                values = {channels[0]: np.asarray(raw)}
            else:
                arrays = np.asarray(raw)
                if arrays.ndim != 2 or arrays.shape[0] != len(channels):
                    raise RuntimeError(
                        f"multi-channel Pluto read must return {len(channels)}xN, got {arrays.shape}"
                    )
                values = {channel: arrays[index] for index, channel in enumerate(channels)}
            lengths = {np.asarray(item).size for item in values.values()}
            if len(lengths) != 1:
                raise RuntimeError("multi-channel Pluto read returned unequal channel lengths")
            for channel in channels:
                result[channel].append(
                    summarize_samples(values[channel], frequency, sample_rate_hz)
                )
        return result
    finally:
        sdr.rx_destroy_buffer()


def scan_pyadi(*, uri: str, frequencies_hz: list[float], sample_rate_hz: float,
               bandwidth_hz: float, gain_db: float, samples_per_frequency: int,
               settle_seconds: float = 0.05, channel: int = 0) -> list[SpectrumSummary]:
    """Backward-compatible single-channel sweep."""
    return scan_channels_pyadi(
        uri=uri, frequencies_hz=frequencies_hz, sample_rate_hz=sample_rate_hz,
        bandwidth_hz=bandwidth_hz, gain_db=gain_db,
        samples_per_frequency=samples_per_frequency, settle_seconds=settle_seconds,
        channels=(channel,),
    )[channel]


def write_scan_report(results: list[SpectrumSummary], output_dir: Path, metadata: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema": "leo-tracker.radio-spectrum-scan/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "points": [asdict(item) for item in results],
    }
    (output_dir / "scan.json").write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    frequency_ghz = np.asarray([item.center_frequency_hz for item in results]) / 1e9
    power = np.asarray([item.power_dbfs for item in results])
    excess = np.asarray([item.peak_excess_db for item in results])
    figure, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True, constrained_layout=True)
    axes[0].plot(frequency_ghz, power, marker="o", markersize=3)
    axes[0].set(ylabel="Mean IQ power (dBFS)", title="Pluto+ receive-only IF sweep")
    axes[0].grid(alpha=.25)
    axes[1].plot(frequency_ghz, excess, marker="o", markersize=3, color="tab:orange")
    axes[1].set(xlabel="Center frequency (GHz)", ylabel="Peak / median FFT (dB)")
    axes[1].grid(alpha=.25)
    figure.savefig(output_dir / "scan.png", dpi=160)
    plt.close(figure)


def write_multi_scan_report(results: dict[int, list[SpectrumSummary]], output_dir: Path,
                            metadata: dict) -> None:
    """Write one atomic logical report plus per-channel scan artifacts."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir.mkdir(parents=True, exist_ok=True)
    channels = sorted(results)
    counts = {len(results[channel]) for channel in channels}
    if len(counts) != 1:
        raise ValueError("all channels must contain the same number of scan points")
    centers = [[point.center_frequency_hz for point in results[channel]] for channel in channels]
    if any(item != centers[0] for item in centers[1:]):
        raise ValueError("all channels must use identical tuning centers")
    for channel in channels:
        write_scan_report(results[channel], output_dir / f"rx{channel}", {
            **metadata, "channel": channel, "simultaneous": len(channels) > 1,
        })
    artifact = {
        "schema": "leo-tracker.radio-spectrum-scan-session/v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "metadata": {**metadata, "channels": channels,
                     "simultaneous": len(channels) > 1},
        "point_count_per_channel": next(iter(counts), 0),
        "channel_artifacts": {str(channel): f"rx{channel}/scan.json" for channel in channels},
    }
    (output_dir / "scan.json").write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    figure, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True, constrained_layout=True)
    frequency_ghz = np.asarray(centers[0]) / 1e9
    for channel in channels:
        axes[0].plot(frequency_ghz, [p.power_dbfs for p in results[channel]], label=f"RX{channel}")
        axes[1].plot(frequency_ghz, [p.peak_excess_db for p in results[channel]], label=f"RX{channel}")
    axes[0].set(ylabel="Mean IQ power (dBFS)", title="Simultaneous Pluto+ receive-only IF sweep")
    axes[1].set(xlabel="Center frequency (GHz)", ylabel="Peak / median FFT (dB)")
    for axis in axes:
        axis.grid(alpha=.25); axis.legend()
    figure.savefig(output_dir / "scan.png", dpi=160)
    plt.close(figure)
