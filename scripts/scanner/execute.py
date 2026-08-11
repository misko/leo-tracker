"""Run a :class:`~scanner.plan.ScanPlan` against any radio implementing ``ScanRadio``.

The radio is injected, so the whole executor is exercised offline by ``FakeScanRadio``
and the hardware adapter is only imported when a real scan runs.

Power convention. Everything is reported in dBFS, where 0 dBFS is a full-scale complex
sinusoid, plus ``power_input_referred_db`` = ``dBFS - gain_db``. Neither is absolute
dBm: that needs a calibrated reference this tool does not have. The AD9361's RSSI is
already input-referred, so the RSSI path reports ``dBFS = rssi - gain_db`` inverted back
into the same convention.

Validity. RSSI is only a correct estimate of input power while the ADC is out of
overload -- above overload it stops tracking the input and instead tracks the gain -- so
every result carries ``clipped`` from the radio's overload detector and ``below_floor``
when the band sits at the noise floor. A power without those flags is not usable.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

import numpy as np
import numpy.typing as npt

from .plan import ScanPlan, ScanPoint

DEFAULT_FFT_SIZE = 4096
DEFAULT_FLOOR_DBFS = -95.0


@runtime_checkable
class ScanRadio(Protocol):
    """The minimum a radio must offer to be scanned."""

    gain_db: float

    def configure(self, *, sample_rate_hz: float, analog_bandwidth_hz: float) -> None:
        """Set rate and analog bandwidth. Called once per scan, never in the loop."""

    def tune(self, center_hz: float) -> None: ...

    def read_power_dbfs(self) -> float:
        """Total power in the analog bandwidth, without transferring IQ."""

    def capture(self, sample_count: int) -> npt.NDArray[np.complexfloating]:
        """Contiguous complex samples scaled so that full scale is magnitude 1."""

    def overload(self) -> bool | None:
        """True/False from the overload detector, or None when unavailable."""


@dataclass(frozen=True)
class PointResult:
    center_hz: float
    bandwidth_hz: float
    power_dbfs: float
    power_input_referred_db: float
    tune_hz: float
    mode: str
    clipped: bool | None
    below_floor: bool
    partially_out_of_span: bool
    bins: int | None = None


@dataclass(frozen=True)
class ScanReport:
    results: tuple[PointResult, ...]
    plan: ScanPlan
    elapsed_s: float
    per_tune_s: tuple[float, float] = (0.0, 0.0)   # (p50, p95)
    metadata: dict = field(default_factory=dict)

    @property
    def points_per_second(self) -> float:
        return len(self.results) / self.elapsed_s if self.elapsed_s > 0 else math.inf


def periodogram(samples: npt.NDArray[np.complexfloating], *, sample_rate_hz: float,
                fft_size: int = DEFAULT_FFT_SIZE) -> tuple[npt.NDArray[np.float64],
                                                           npt.NDArray[np.float64]]:
    """Averaged periodogram, computed once per tuning.

    Returned as ``(bin_offsets_hz, power_per_bin)`` normalised so that summing every bin
    recovers ``mean(|x|**2)``, which makes a full-scale complex sinusoid read 0 dBFS
    regardless of ``fft_size``.

    Separated from band selection deliberately: a group can hold dozens of requested
    points, and recomputing the transform for each one made a 61-point group take 377 ms
    instead of a few. The FFT cost, not the USB transfer, dominates the wide-band path.
    """
    samples = np.asarray(samples)
    if samples.ndim != 1:
        raise ValueError("samples must be one dimensional")
    size = min(int(fft_size), samples.size)
    if size < 8:
        raise ValueError("need at least 8 samples to estimate a band power")
    segments = max(1, samples.size // size)
    window = np.hanning(size)
    window_power = float(np.mean(window ** 2))
    accumulated = np.zeros(size, dtype=np.float64)
    for index in range(segments):
        chunk = samples[index * size:(index + 1) * size]
        spectrum = np.fft.fft(chunk * window)
        accumulated += (np.abs(spectrum) ** 2) / (size * size * window_power)
    accumulated /= segments
    return np.fft.fftfreq(size, d=1.0 / sample_rate_hz), accumulated


def band_power_from_periodogram(offsets_hz: npt.NDArray[np.float64],
                                power_per_bin: npt.NDArray[np.float64], *,
                                sample_rate_hz: float, tune_hz: float, center_hz: float,
                                bandwidth_hz: float) -> tuple[float, int, bool]:
    """Sum the bins covering one requested band. Returns (dBFS, bins, out_of_span)."""
    absolute = tune_hz + offsets_hz
    half = bandwidth_hz / 2
    selected = np.abs(absolute - center_hz) <= half
    span_low, span_high = tune_hz - sample_rate_hz / 2, tune_hz + sample_rate_hz / 2
    outside = (center_hz - half) < span_low or (center_hz + half) > span_high
    if not selected.any():
        selected = np.zeros_like(selected)
        selected[int(np.argmin(np.abs(absolute - center_hz)))] = True
    power = float(power_per_bin[selected].sum())
    dbfs = 10 * math.log10(power) if power > 0 else -math.inf
    return dbfs, int(selected.sum()), bool(outside)


def band_power_dbfs(samples: npt.NDArray[np.complexfloating], *, sample_rate_hz: float,
                    tune_hz: float, center_hz: float, bandwidth_hz: float,
                    fft_size: int = DEFAULT_FFT_SIZE) -> tuple[float, int, bool]:
    """Power in an arbitrary band, synthesised by summing periodogram bins.

    Returns ``(dBFS, bins_used, partially_out_of_span)``. Normalised so that summing
    every bin recovers ``mean(|x|**2)``, which makes a full-scale complex sinusoid
    read 0 dBFS regardless of ``fft_size``.
    """
    offsets, power = periodogram(samples, sample_rate_hz=sample_rate_hz,
                                 fft_size=fft_size)
    return band_power_from_periodogram(offsets, power, sample_rate_hz=sample_rate_hz,
                                       tune_hz=tune_hz, center_hz=center_hz,
                                       bandwidth_hz=bandwidth_hz)


def _dwell_rssi(radio: ScanRadio, dwell_s: float) -> float:
    """Average RSSI over the dwell in the power domain, not the log domain."""
    started = time.perf_counter()
    linear: list[float] = [10 ** (radio.read_power_dbfs() / 10)]
    while time.perf_counter() - started < dwell_s:
        linear.append(10 ** (radio.read_power_dbfs() / 10))
    mean = sum(linear) / len(linear)
    return 10 * math.log10(mean) if mean > 0 else -math.inf


def execute_scan(radio: ScanRadio, plan: ScanPlan, *, dwell_s: float = 0.0,
                 fft_size: int = DEFAULT_FFT_SIZE,
                 floor_dbfs: float = DEFAULT_FLOOR_DBFS) -> ScanReport:
    """Execute a plan, setting the analog bandwidth exactly once."""
    if dwell_s < 0:
        raise ValueError("dwell cannot be negative")
    radio.configure(sample_rate_hz=plan.sample_rate_hz,
                    analog_bandwidth_hz=plan.analog_bandwidth_hz)
    gain = float(getattr(radio, "gain_db", 0.0))
    results: list[tuple[int, PointResult]] = []
    tune_times: list[float] = []
    started = time.perf_counter()

    for group in plan.groups:
        tune_started = time.perf_counter()
        radio.tune(group.tune_hz)
        if group.mode == "rssi":
            index = group.point_indices[0]
            point = plan.points[index]
            dbfs = _dwell_rssi(radio, dwell_s)
            results.append((index, _result(point, dbfs, group, gain, radio.overload(),
                                           floor_dbfs, bins=None, outside=False)))
        else:
            wanted = max(fft_size, int(round(dwell_s * plan.sample_rate_hz)))
            samples = radio.capture(wanted)
            offsets, power = periodogram(samples, sample_rate_hz=plan.sample_rate_hz,
                                         fft_size=fft_size)
            for index in group.point_indices:
                point = plan.points[index]
                dbfs, bins, outside = band_power_from_periodogram(
                    offsets, power, sample_rate_hz=plan.sample_rate_hz,
                    tune_hz=group.tune_hz, center_hz=point.center_hz,
                    bandwidth_hz=point.bandwidth_hz)
                results.append((index, _result(point, dbfs, group, gain, radio.overload(),
                                               floor_dbfs, bins=bins, outside=outside)))
        tune_times.append(time.perf_counter() - tune_started)

    elapsed = time.perf_counter() - started
    results.sort(key=lambda item: item[0])
    ordered = tuple(result for _, result in results)
    tune_times.sort()
    p50 = tune_times[len(tune_times) // 2] if tune_times else 0.0
    p95 = tune_times[min(len(tune_times) - 1, int(0.95 * len(tune_times)))] if tune_times else 0.0
    return ScanReport(
        results=ordered, plan=plan, elapsed_s=elapsed, per_tune_s=(p50, p95),
        metadata={
            "dwell_s": dwell_s,
            "gain_db": gain,
            "tunings": plan.tunings,
            "predicted_s": plan.estimated_seconds(dwell_s),
            "clipped_points": sum(1 for r in ordered if r.clipped),
            "floor_points": sum(1 for r in ordered if r.below_floor),
        },
    )


def _result(point: ScanPoint, dbfs: float, group, gain: float, clipped: bool | None,
            floor_dbfs: float, *, bins: int | None, outside: bool) -> PointResult:
    return PointResult(
        center_hz=point.center_hz, bandwidth_hz=point.bandwidth_hz,
        power_dbfs=round(dbfs, 3) if math.isfinite(dbfs) else dbfs,
        power_input_referred_db=round(dbfs - gain, 3) if math.isfinite(dbfs) else dbfs,
        tune_hz=group.tune_hz, mode=group.mode, clipped=clipped,
        below_floor=bool(dbfs <= floor_dbfs), partially_out_of_span=outside, bins=bins)


class FakeScanRadio:
    """Deterministic radio for offline tests: synthesises tones at known frequencies."""

    def __init__(self, tones_hz: Sequence[float] = (), *, gain_db: float = 40.0,
                 amplitude: float = 0.1, noise: float = 1e-4, seed: int = 0,
                 overloaded: bool | None = False):
        self.tones_hz = tuple(float(t) for t in tones_hz)
        self.gain_db = float(gain_db)
        self.amplitude = float(amplitude)
        self.noise = float(noise)
        self._rng = np.random.default_rng(seed)
        self._overloaded = overloaded
        self.sample_rate_hz = 0.0
        self.analog_bandwidth_hz = 0.0
        self.center_hz = 0.0
        self.configure_calls = 0
        self.bandwidth_writes: list[float] = []
        self.tunes: list[float] = []

    def configure(self, *, sample_rate_hz: float, analog_bandwidth_hz: float) -> None:
        self.configure_calls += 1
        self.bandwidth_writes.append(float(analog_bandwidth_hz))
        self.sample_rate_hz = float(sample_rate_hz)
        self.analog_bandwidth_hz = float(analog_bandwidth_hz)

    def tune(self, center_hz: float) -> None:
        self.center_hz = float(center_hz)
        self.tunes.append(self.center_hz)

    def _in_band(self) -> list[float]:
        half = self.analog_bandwidth_hz / 2
        return [t for t in self.tones_hz if abs(t - self.center_hz) <= half]

    def read_power_dbfs(self) -> float:
        power = self.noise ** 2 + sum(self.amplitude ** 2 for _ in self._in_band())
        return 10 * math.log10(power)

    def capture(self, sample_count: int) -> npt.NDArray[np.complexfloating]:
        n = int(sample_count)
        t = np.arange(n) / self.sample_rate_hz
        signal = (self._rng.normal(0, self.noise, n)
                  + 1j * self._rng.normal(0, self.noise, n)) / math.sqrt(2)
        for tone in self._in_band():
            signal += self.amplitude * np.exp(2j * np.pi * (tone - self.center_hz) * t)
        return signal.astype(np.complex128)

    def overload(self) -> bool | None:
        return self._overloaded
